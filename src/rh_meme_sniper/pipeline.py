from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rh_meme_sniper.alerts.telegram import TelegramAlerter, render_candidate_alert
from rh_meme_sniper.cluster.narrative_cluster import cluster_events
from rh_meme_sniper.config import settings
from rh_meme_sniper.models import CandidateToken, NarrativeCluster
from rh_meme_sniper.score.authenticity import score_clusters
from rh_meme_sniper.state import SeenState
from rh_meme_sniper.sources.apify_client import ApifyAccessError, ApifyClient
from rh_meme_sniper.sources.apify_x import build_search_input, is_no_results_item, normalize_tweet_item, tweet_record
from rh_meme_sniper.sources.dex_client import DexScreenerClient
from rh_meme_sniper.sources.dexscreener import normalize_pair_item
from rh_meme_sniper.storage.json_store import JsonStore


@dataclass(slots=True)
class AlertLoopArtifacts:
    tweet_count: int
    pair_count: int
    cluster_count: int
    alert_count: int
    output_paths: dict[str, str]
    alerts: list[str]
    clusters: list[NarrativeCluster]


@dataclass(slots=True)
class QueryPackRunArtifacts:
    actor_id: str | None
    runs: list[dict[str, Any]]


@dataclass(slots=True)
class DiscoveryRunArtifacts:
    runs: list[dict[str, Any]]


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "query"


def _dedupe_preserve(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        normalized = value.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


def _normalize_terms(values: list[str] | None) -> list[str]:
    if not values:
        return []
    return [value.strip().lower() for value in values if value and value.strip()]


def filter_pair_items(
    pair_items: list[dict[str, Any]],
    *,
    allow_terms: list[str] | None = None,
    deny_terms: list[str] | None = None,
) -> list[dict[str, Any]]:
    normalized_allow = _normalize_terms(allow_terms)
    normalized_deny = _normalize_terms(deny_terms)
    if not normalized_allow and not normalized_deny:
        return pair_items

    filtered: list[dict[str, Any]] = []
    for item in pair_items:
        base = item.get("baseToken") or {}
        haystack = " ".join(
            str(value)
            for value in [
                base.get("name"),
                base.get("symbol"),
                item.get("url"),
                item.get("pairAddress"),
            ]
            if value
        ).lower()

        if normalized_allow and not any(term in haystack for term in normalized_allow):
            continue
        if normalized_deny and any(term in haystack for term in normalized_deny):
            continue
        filtered.append(item)
    return filtered


def _build_dex_queries(query: str, tweet_events: list[Any]) -> list[str]:
    raw_queries = [query]
    for event in tweet_events:
        raw_queries.extend(event.contract_addresses)
        raw_queries.extend(event.names)
        raw_queries.extend(event.symbols)
    return _dedupe_preserve(raw_queries)[:10]


def _fetch_pairs(query_terms: list[str], chain_id: str | None = None, limit_per_query: int = 5) -> list[dict[str, Any]]:
    client = DexScreenerClient()
    deduped: dict[str, dict[str, Any]] = {}
    for term in query_terms:
        for item in client.search_pairs(term):
            if chain_id and item.get("chainId") != chain_id:
                continue
            pair_address = item.get("pairAddress") or item.get("url") or f"{term}:{len(deduped)}"
            deduped.setdefault(str(pair_address), item)
            if len(deduped) >= limit_per_query * max(1, len(query_terms)):
                return list(deduped.values())
    return list(deduped.values())


def _timeline_records(clusters: list[NarrativeCluster]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for cluster in clusters:
        for candidate in cluster.candidates:
            records.append(
                {
                    "cluster_id": cluster.cluster_id,
                    "canonical_name": cluster.canonical_name,
                    "contract_address": candidate.contract_address,
                    "first_seen_x_at": candidate.first_seen_x_at,
                    "first_seen_ca_at": candidate.first_seen_ca_at,
                    "first_seen_market_at": candidate.first_seen_market_at,
                    "pair_created_at": candidate.pair_created_at,
                    "first_kol_mentions": candidate.first_kol_mentions,
                }
            )
    return records


def _candidate_records(clusters: list[NarrativeCluster]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for cluster in clusters:
        for candidate in cluster.candidates:
            payload = candidate.model_dump()
            payload["canonical_name"] = cluster.canonical_name
            payload["cluster_status"] = cluster.status
            records.append(payload)
    return records


def _write_alert_log(output_dir: Path, slug: str, alerts: list[str]) -> Path:
    path = output_dir / f"alerts_{slug}.log"
    content = "\n\n---\n\n".join(alerts) if alerts else ""
    path.write_text(content)
    return path


def _cap_items(items: list[dict[str, Any]], max_items: int) -> list[dict[str, Any]]:
    if max_items <= 0:
        return items
    return items[:max_items]


def _load_json_config(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text())
    return payload if isinstance(payload, dict) else {}


def _coerce_query_id(raw_id: str | None, query: str) -> str:
    normalized = (raw_id or '').strip()
    return normalized or _slugify(query)


def _build_discovery_queries(
    *,
    watchlist: dict[str, Any],
    query_buckets: dict[str, Any],
) -> list[dict[str, Any]]:
    account_templates = [item for item in query_buckets.get('account_query_templates', []) if isinstance(item, dict)]
    keyword_queries = [item for item in query_buckets.get('keyword_queries', []) if isinstance(item, dict)]
    default_allow_terms = list(query_buckets.get('pair_allow_terms') or [])
    default_deny_terms = list(query_buckets.get('pair_deny_terms') or [])

    runs: list[dict[str, Any]] = []
    seen_signatures: set[tuple[str, str, int]] = set()
    for group_name in ('primary_accounts', 'secondary_accounts'):
        for handle in watchlist.get(group_name, []) or []:
            normalized_handle = str(handle).strip().lstrip('@')
            if not normalized_handle:
                continue
            for template in account_templates:
                query_template = str(template.get('query_template') or '').strip()
                if not query_template:
                    continue
                query = query_template.format(handle=normalized_handle)
                sort = str(template.get('sort') or 'Latest')
                max_items = int(template.get('max_items') or 25)
                signature = (query, sort, max_items)
                if signature in seen_signatures:
                    continue
                seen_signatures.add(signature)
                runs.append(
                    {
                        'id': f"{str(template.get('id_prefix') or 'acct')}-{_slugify(normalized_handle)}",
                        'query': query,
                        'sort': sort,
                        'max_items': max_items,
                        'pair_allow_terms': list(template.get('pair_allow_terms') or default_allow_terms),
                        'pair_deny_terms': list(template.get('pair_deny_terms') or default_deny_terms),
                    }
                )

    for item in keyword_queries:
        query = str(item.get('query') or '').strip()
        if not query:
            continue
        sort = str(item.get('sort') or 'Top')
        max_items = int(item.get('max_items') or 100)
        signature = (query, sort, max_items)
        if signature in seen_signatures:
            continue
        seen_signatures.add(signature)
        runs.append(
            {
                'id': _coerce_query_id(item.get('id'), query),
                'query': query,
                'sort': sort,
                'max_items': max_items,
                'pair_allow_terms': list(item.get('pair_allow_terms') or default_allow_terms),
                'pair_deny_terms': list(item.get('pair_deny_terms') or default_deny_terms),
            }
        )

    return runs


def _apply_seen_state(
    *,
    clusters: list[NarrativeCluster],
    alerts: list[str],
    state_db_path: Path | None,
    alert_cooldown_seconds: int,
) -> list[str]:
    if not state_db_path or not alerts:
        return alerts

    seen_state = SeenState(state_db_path)
    filtered_alerts: list[str] = []
    alert_iter = iter(alerts)
    for cluster in clusters:
        candidate = cluster.canonical_candidate
        if not candidate or candidate.verdict != 'alert':
            continue
        alert_text = next(alert_iter)
        decision = seen_state.should_emit(candidate, cluster, cooldown_seconds=alert_cooldown_seconds)
        if not decision.should_emit:
            continue
        seen_state.record_emit(candidate, cluster, key=decision.key)
        filtered_alerts.append(alert_text)
    return filtered_alerts


def run_alert_loop_from_payload(
    *,
    query: str,
    payload: dict[str, Any],
    output_dir: Path,
    send_telegram: bool = False,
    pair_allow_terms: list[str] | None = None,
    pair_deny_terms: list[str] | None = None,
    state_db_path: Path | None = None,
    alert_cooldown_seconds: int = 3600,
) -> AlertLoopArtifacts:
    output_dir.mkdir(parents=True, exist_ok=True)
    store = JsonStore(output_dir)
    slug = _slugify(query)

    raw_tweets = [item for item in payload.get("tweets", []) if isinstance(item, dict) and not is_no_results_item(item)]
    raw_pairs = [item for item in payload.get("pairs", []) if isinstance(item, dict)]
    raw_pairs = filter_pair_items(raw_pairs, allow_terms=pair_allow_terms, deny_terms=pair_deny_terms)

    tweet_events = [normalize_tweet_item(item) for item in raw_tweets]
    pair_events = [normalize_pair_item(item) for item in raw_pairs]
    clusters = score_clusters(cluster_events(tweet_events + pair_events))
    alerts = [render_candidate_alert(cluster.canonical_candidate) for cluster in clusters if cluster.canonical_candidate and cluster.canonical_candidate.verdict == "alert"]
    alerts = _apply_seen_state(
        clusters=clusters,
        alerts=alerts,
        state_db_path=state_db_path,
        alert_cooldown_seconds=alert_cooldown_seconds,
    )

    if send_telegram and alerts:
        if not settings.telegram_bot_token or not settings.telegram_chat_id:
            raise RuntimeError("Telegram credentials are missing in .env")
        alerter = TelegramAlerter(settings.telegram_bot_token, settings.telegram_chat_id)
        for alert_text in alerts:
            alerter.send_text(alert_text)

    paths = {
        "tweets": str(store.write(f"tweets_{slug}.json", [tweet_record(item) for item in raw_tweets])),
        "pairs": str(store.write(f"pairs_{slug}.json", raw_pairs)),
        "candidates": str(store.write(f"candidates_{slug}.json", _candidate_records(clusters))),
        "timelines": str(store.write(f"timelines_{slug}.json", _timeline_records(clusters))),
        "alerts": str(_write_alert_log(output_dir, slug, alerts)),
    }

    return AlertLoopArtifacts(
        tweet_count=len(raw_tweets),
        pair_count=len(raw_pairs),
        cluster_count=len(clusters),
        alert_count=len(alerts),
        output_paths=paths,
        alerts=alerts,
        clusters=clusters,
    )


def run_live_alert_loop(
    *,
    query: str,
    max_items: int = 100,
    sort: str = "Top",
    tweet_language: str | None = "en",
    output_dir: Path | None = None,
    send_telegram: bool = False,
    actor_id: str | None = None,
    pair_allow_terms: list[str] | None = None,
    pair_deny_terms: list[str] | None = None,
    state_db_path: Path | None = None,
    alert_cooldown_seconds: int = 3600,
) -> AlertLoopArtifacts:
    if not settings.apify_api_token:
        raise RuntimeError("APIFY_API_TOKEN is missing in .env")

    output_dir = output_dir or settings.output_dir
    selected_actor_id = actor_id or settings.apify_x_actor_id
    apify = ApifyClient(settings.apify_api_token, selected_actor_id)
    actor_input = build_search_input(
        query,
        max_items=max_items,
        sort=sort,
        tweet_language=tweet_language,
        actor_id=selected_actor_id,
    )
    run = apify.run_actor(actor_input)
    raw_tweets = _cap_items(
        [item for item in run.items if isinstance(item, dict) and not is_no_results_item(item)],
        max_items,
    )
    tweet_events = [normalize_tweet_item(item) for item in raw_tweets]

    dex_queries = _build_dex_queries(query, tweet_events)
    raw_pairs = _fetch_pairs(dex_queries, chain_id=settings.chain_id)
    raw_pairs = filter_pair_items(raw_pairs, allow_terms=pair_allow_terms, deny_terms=pair_deny_terms)

    payload = {"tweets": raw_tweets, "pairs": raw_pairs}
    return run_alert_loop_from_payload(
        query=query,
        payload=payload,
        output_dir=output_dir,
        send_telegram=send_telegram,
        pair_allow_terms=pair_allow_terms,
        pair_deny_terms=pair_deny_terms,
        state_db_path=state_db_path,
        alert_cooldown_seconds=alert_cooldown_seconds,
    )


def run_query_pack(
    *,
    query_pack_path: Path,
    send_telegram: bool = False,
    output_dir: Path | None = None,
    state_db_path: Path | None = None,
    alert_cooldown_seconds: int = 3600,
) -> QueryPackRunArtifacts:
    config = json.loads(query_pack_path.read_text())
    actor_id = config.get("actor_id")
    default_allow_terms = list(config.get("pair_allow_terms") or [])
    default_deny_terms = list(config.get("pair_deny_terms") or [])

    runs: list[dict[str, Any]] = []
    for item in config.get("queries", []):
        if not isinstance(item, dict):
            continue
        query = str(item.get("query") or "").strip()
        if not query:
            continue
        selected_actor_id = item.get("actor_id") or actor_id
        artifacts = run_live_alert_loop(
            query=query,
            max_items=int(item.get("max_items") or 100),
            sort=str(item.get("sort") or "Top"),
            send_telegram=send_telegram,
            actor_id=str(selected_actor_id) if selected_actor_id else None,
            output_dir=output_dir,
            pair_allow_terms=list(item.get("pair_allow_terms") or default_allow_terms),
            pair_deny_terms=list(item.get("pair_deny_terms") or default_deny_terms),
            state_db_path=state_db_path,
            alert_cooldown_seconds=alert_cooldown_seconds,
        )
        runs.append(
            {
                "id": item.get("id") or _slugify(query),
                "query": query,
                "sort": str(item.get("sort") or "Top"),
                "max_items": int(item.get("max_items") or 100),
                "tweet_count": artifacts.tweet_count,
                "pair_count": artifacts.pair_count,
                "cluster_count": artifacts.cluster_count,
                "alert_count": artifacts.alert_count,
                "output_paths": artifacts.output_paths,
            }
        )

    return QueryPackRunArtifacts(actor_id=str(actor_id) if actor_id else None, runs=runs)


def run_discovery(
    *,
    watchlist_path: Path,
    query_buckets_path: Path,
    send_telegram: bool = False,
    output_dir: Path | None = None,
    actor_id: str | None = None,
    state_db_path: Path | None = None,
    alert_cooldown_seconds: int = 3600,
) -> DiscoveryRunArtifacts:
    watchlist = _load_json_config(watchlist_path)
    query_buckets = _load_json_config(query_buckets_path)
    queries = _build_discovery_queries(watchlist=watchlist, query_buckets=query_buckets)

    runs: list[dict[str, Any]] = []
    default_actor_id = actor_id or query_buckets.get('actor_id') or settings.apify_x_actor_id
    for item in queries:
        artifacts = run_live_alert_loop(
            query=str(item['query']),
            max_items=int(item.get('max_items') or 100),
            sort=str(item.get('sort') or 'Top'),
            send_telegram=send_telegram,
            actor_id=str(item.get('actor_id') or default_actor_id),
            output_dir=output_dir,
            pair_allow_terms=list(item.get('pair_allow_terms') or []),
            pair_deny_terms=list(item.get('pair_deny_terms') or []),
            state_db_path=state_db_path,
            alert_cooldown_seconds=alert_cooldown_seconds,
        )
        runs.append(
            {
                'id': _coerce_query_id(str(item.get('id') or ''), str(item['query'])),
                'query': str(item['query']),
                'sort': str(item.get('sort') or 'Top'),
                'max_items': int(item.get('max_items') or 100),
                'tweet_count': artifacts.tweet_count,
                'pair_count': artifacts.pair_count,
                'cluster_count': artifacts.cluster_count,
                'alert_count': artifacts.alert_count,
                'output_paths': artifacts.output_paths,
            }
        )

    return DiscoveryRunArtifacts(runs=runs)
