from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from rh_meme_sniper.alerts.telegram import TelegramAlerter, render_candidate_alert
from rh_meme_sniper.cluster.narrative_cluster import cluster_events
from rh_meme_sniper.config import settings
from rh_meme_sniper.models import CandidateToken, NarrativeCluster
from rh_meme_sniper.score.authenticity import score_clusters
from rh_meme_sniper.state import SeenState, TrackingState
from rh_meme_sniper.sources.apify_client import ApifyAccessError
from rh_meme_sniper.sources.apify_x import is_no_results_item, normalize_tweet_item, tweet_record
from rh_meme_sniper.sources.dex_client import DexScreenerClient
from rh_meme_sniper.sources.x_source import get_x_provider
from rh_meme_sniper.sources.dexscreener import normalize_pair_item
from rh_meme_sniper.storage.json_store import JsonStore
from rh_meme_sniper.tracking_judge import get_tracking_judge_from_settings


@dataclass(slots=True)
class AlertLoopArtifacts:
    tweet_count: int
    pair_count: int
    cluster_count: int
    alert_count: int
    output_paths: dict[str, str]
    alerts: list[str]
    clusters: list[NarrativeCluster]
    provider_name: str | None = None
    usage_summary: dict[str, Any] | None = None


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


def _candidate_from_pair_item(item: dict[str, Any], *, fallback_query: str = '') -> CandidateToken:
    base = item.get('baseToken') or {}
    txns_1h = ((item.get('txns') or {}).get('h1') or {})
    pair_created_at = _parse_iso8601(str(normalize_pair_item(item).metrics.get('pair_created_at') or ''))
    return CandidateToken(
        cluster_id=fallback_query or str(base.get('symbol') or base.get('name') or base.get('address') or 'tracked'),
        contract_address=str(base.get('address') or ''),
        symbol=str(base.get('symbol') or '') or None,
        name=str(base.get('name') or '') or None,
        pair_address=str(item.get('pairAddress') or '') or None,
        pair_created_at=pair_created_at.isoformat().replace('+00:00', 'Z') if pair_created_at else None,
        first_seen_market_at=pair_created_at.isoformat().replace('+00:00', 'Z') if pair_created_at else None,
        liquidity_usd=float(((item.get('liquidity') or {}).get('usd') or 0.0)),
        volume_1h=float(((item.get('volume') or {}).get('h1') or 0.0)),
        volume_24h=float(((item.get('volume') or {}).get('h24') or 0.0)),
        buy_count_1h=int(txns_1h.get('buys') or 0),
        sell_count_1h=int(txns_1h.get('sells') or 0),
    )


def judge_candidate_tracking_status(
    *,
    query: str,
    candidate: CandidateToken,
    cluster: NarrativeCluster,
) -> tuple[str, str]:
    generic_terms = {
        'robinhood', 'vlad', 'bitstamp', 'token', 'coin', 'meme', 'memecoin',
    }
    name = (candidate.name or cluster.canonical_name or '').strip()
    name_lower = name.lower()
    symbol = (candidate.symbol or '').strip().lower()
    query_lower = query.lower()

    if name_lower in generic_terms or symbol in generic_terms:
        return 'ignore', 'generic_or_brand_term'

    brand_derivative_suffixes = {
        'wallet', 'payments', 'payment', 'protocol', 'summer', 'bull', 'ai',
    }
    if name_lower.startswith('robinhood '):
        tail_words = [part for part in name_lower.split()[1:] if part]
        if tail_words and any(word in brand_derivative_suffixes for word in tail_words):
            return 'ignore', 'generic_robinhood_brand_derivative'

    blob_markers = (' ca ', ' https://', ' http://', ' delivered ', ' return ', ' next.', '\n')
    if len(name) >= 80 and any(marker in name_lower for marker in blob_markers):
        return 'ignore', 'garbage_extracted_name'

    if 'favorite meme' in query_lower and name_lower in {'robinhood', 'vlad'}:
        return 'ignore', 'generic_or_brand_term'

    query_terms = {part for part in ''.join(ch if ch.isalnum() else ' ' for ch in query_lower).split() if part}
    candidate_name_norm = ' '.join(part for part in ''.join(ch if ch.isalnum() else ' ' for ch in name_lower).split() if part)
    candidate_symbol_norm = symbol.replace('$', '')
    exact_symbol_match = bool(candidate_symbol_norm) and candidate_symbol_norm in query_terms
    exact_name_match = bool(candidate_name_norm) and candidate_name_norm in query_lower
    exact_cluster_match = bool(cluster.canonical_name) and cluster.canonical_name.strip().lower() == name_lower
    if (
        candidate.contract_address
        and (candidate.first_seen_ca_at or candidate.first_seen_market_at)
        and (exact_symbol_match or exact_name_match or exact_cluster_match)
        and candidate.liquidity_usd >= 5000
        and (candidate.volume_24h >= 10000 or candidate.market_score >= 85 or candidate.authenticity_score >= 85)
    ):
        return 'strong_candidate', 'canonical_exact_match_boost'

    if candidate.verdict == 'alert' and candidate.alert_score >= 80 and candidate.liquidity_usd >= 5000:
        return 'strong_candidate', 'high_signal_candidate'
    if candidate.contract_address and (candidate.first_seen_ca_at or candidate.first_seen_market_at):
        return 'watch', 'has_contract_signal'
    return 'ignore', 'low_signal_candidate'


def apply_tracking_judge(
    *,
    query: str,
    candidate: CandidateToken,
    cluster: NarrativeCluster,
    llm_judge: Any | None = None,
) -> tuple[str, str]:
    baseline_status, baseline_reason = judge_candidate_tracking_status(query=query, candidate=candidate, cluster=cluster)
    hard_ignore_reasons = {
        'generic_or_brand_term',
        'generic_robinhood_brand_derivative',
        'garbage_extracted_name',
    }
    hard_strong_reasons = {
        'canonical_exact_match_boost',
    }
    if baseline_status == 'ignore' and baseline_reason in hard_ignore_reasons:
        return baseline_status, baseline_reason
    if baseline_status == 'strong_candidate' and baseline_reason in hard_strong_reasons:
        return baseline_status, baseline_reason

    judge = llm_judge or get_tracking_judge_from_settings()
    if judge is not None:
        try:
            decision = judge.judge(query=query, candidate=candidate, cluster=cluster)
            if isinstance(decision, dict):
                status = str(decision.get('status') or '').strip()
                reason = str(decision.get('reason') or '').strip() or 'llm_judge'
                if status in {'ignore', 'watch', 'strong_candidate'}:
                    return status, reason
        except Exception:
            pass
    return baseline_status, baseline_reason


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


def _parse_iso8601(value: str | None) -> datetime | None:
    if not value:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace('Z', '+00:00'))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _filter_recent_tweets(
    raw_tweets: list[dict[str, Any]],
    *,
    max_tweet_age_days: int | None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    if max_tweet_age_days is None:
        return raw_tweets
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=max_tweet_age_days)
    filtered: list[dict[str, Any]] = []
    for item in raw_tweets:
        observed_at = _parse_iso8601(str(item.get('createdAt') or item.get('created_at') or ''))
        if observed_at and observed_at >= cutoff:
            filtered.append(item)
    return filtered


def _load_json_config(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text())
    return payload if isinstance(payload, dict) else {}


def _coerce_query_id(raw_id: str | None, query: str) -> str:
    normalized = (raw_id or '').strip()
    return normalized or _slugify(query)


def _total_available_credits(balance: dict[str, Any] | None) -> int | None:
    if not isinstance(balance, dict):
        return None
    recharge = int(balance.get('recharge_credits') or 0)
    bonus = int(balance.get('total_bonus_credits') or 0)
    return recharge + bonus


def _build_usage_summary(
    provider_name: str | None,
    balance_before: dict[str, Any] | None,
    balance_after: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not balance_before and not balance_after:
        return None
    total_before = _total_available_credits(balance_before)
    total_after = _total_available_credits(balance_after)
    credits_used_estimate = None
    if total_before is not None and total_after is not None:
        credits_used_estimate = max(0, total_before - total_after)
    return {
        'provider': provider_name,
        'balance_before': balance_before,
        'balance_after': balance_after,
        'credits_used_estimate': credits_used_estimate,
    }


def _safe_get_account_balance(provider: Any) -> dict[str, Any] | None:
    getter = getattr(provider, 'get_account_balance', None)
    if not callable(getter):
        return None
    try:
        return getter()
    except Exception:
        return None


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
                        'require_tweet_match_for_alerts': bool(template.get('require_tweet_match_for_alerts', False)),
                        'max_tweet_age_days': int(template['max_tweet_age_days']) if template.get('max_tweet_age_days') is not None else None,
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
                'require_tweet_match_for_alerts': bool(item.get('require_tweet_match_for_alerts', False)),
                'max_tweet_age_days': int(item['max_tweet_age_days']) if item.get('max_tweet_age_days') is not None else None,
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


def _record_tracking_state(*, tracking_db_path: Path | None, query: str, clusters: list[NarrativeCluster]) -> None:
    if not tracking_db_path:
        return
    tracking_state = TrackingState(tracking_db_path)
    for cluster in clusters:
        candidate = cluster.canonical_candidate
        if not candidate or not candidate.contract_address:
            continue
        status, reason = apply_tracking_judge(query=query, candidate=candidate, cluster=cluster)
        candidate.tracking_status = status
        candidate.tracking_reason = reason
        if status == 'ignore':
            continue
        tracking_state.record_candidate(query=query, candidate=candidate, cluster=cluster)
        for event in cluster.events:
            tracking_state.record_mention(query=query, contract_address=candidate.contract_address, event=event)
    tracking_state.prune(
        retention_days=settings.tracking_retention_days,
        drop_stale_tracked_tokens_days=settings.tracking_drop_stale_tokens_days,
    )


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
    require_tweet_match_for_alerts: bool = False,
    max_tweet_age_days: int | None = None,
) -> AlertLoopArtifacts:
    output_dir = output_dir or settings.output_dir
    provider = get_x_provider(actor_id=actor_id)
    balance_before = _safe_get_account_balance(provider)
    raw_tweets = _cap_items(
        [
            item
            for item in provider.search_tweets(
                query=query,
                max_items=max_items,
                sort=sort,
                tweet_language=tweet_language,
            )
            if isinstance(item, dict) and not is_no_results_item(item)
        ],
        max_items,
    )
    raw_tweets = _filter_recent_tweets(raw_tweets, max_tweet_age_days=max_tweet_age_days)
    balance_after = _safe_get_account_balance(provider)
    tweet_events = [normalize_tweet_item(item) for item in raw_tweets]

    dex_queries = _build_dex_queries(query, tweet_events)
    raw_pairs = [] if require_tweet_match_for_alerts and not raw_tweets else _fetch_pairs(dex_queries, chain_id=settings.chain_id)
    raw_pairs = filter_pair_items(raw_pairs, allow_terms=pair_allow_terms, deny_terms=pair_deny_terms)

    payload = {"tweets": raw_tweets, "pairs": raw_pairs}
    artifacts = run_alert_loop_from_payload(
        query=query,
        payload=payload,
        output_dir=output_dir,
        send_telegram=send_telegram,
        pair_allow_terms=pair_allow_terms,
        pair_deny_terms=pair_deny_terms,
        state_db_path=state_db_path,
        alert_cooldown_seconds=alert_cooldown_seconds,
    )
    artifacts.provider_name = getattr(provider, 'provider_name', None)
    artifacts.usage_summary = _build_usage_summary(artifacts.provider_name, balance_before, balance_after)
    return artifacts


def run_query_pack(
    *,
    query_pack_path: Path,
    send_telegram: bool = False,
    output_dir: Path | None = None,
    state_db_path: Path | None = None,
    alert_cooldown_seconds: int = 3600,
    tracking_db_path: Path | None = None,
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
            require_tweet_match_for_alerts=bool(item.get('require_tweet_match_for_alerts', False)),
            max_tweet_age_days=int(item['max_tweet_age_days']) if item.get('max_tweet_age_days') is not None else None,
        )
        _record_tracking_state(tracking_db_path=tracking_db_path, query=query, clusters=getattr(artifacts, 'clusters', []))
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
    tracking_db_path: Path | None = None,
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
            require_tweet_match_for_alerts=bool(item.get('require_tweet_match_for_alerts', False)),
            max_tweet_age_days=int(item['max_tweet_age_days']) if item.get('max_tweet_age_days') is not None else None,
        )
        _record_tracking_state(tracking_db_path=tracking_db_path, query=str(item['query']), clusters=getattr(artifacts, 'clusters', []))
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


def rescan_tracked_tokens(*, tracking_db_path: Path, chain_id: str | None = None) -> dict[str, Any]:
    tracking_state = TrackingState(tracking_db_path)
    tracked_tokens = tracking_state.list_tracked_tokens()
    runs: list[dict[str, Any]] = []
    for item in tracked_tokens:
        query_terms = [value for value in [item.get('contract_address'), item.get('symbol'), item.get('name')] if value]
        pair_items = _fetch_pairs(query_terms, chain_id=chain_id or settings.chain_id, limit_per_query=5)
        matching = None
        target_contract = str(item.get('contract_address') or '').lower()
        for pair in pair_items:
            base = pair.get('baseToken') or {}
            if str(base.get('address') or '').lower() == target_contract:
                matching = pair
                break
        if matching is None and pair_items:
            matching = pair_items[0]
        if matching is None:
            runs.append({'contract_address': item.get('contract_address'), 'status': 'no_pair_found'})
            continue
        candidate = _candidate_from_pair_item(matching, fallback_query=str(item.get('query') or 'tracked-token'))
        tracking_state.record_market_snapshot(query=str(item.get('query') or item.get('symbol') or item.get('name') or item.get('contract_address') or ''), candidate=candidate)
        runs.append(
            {
                'contract_address': candidate.contract_address,
                'symbol': candidate.symbol,
                'name': candidate.name,
                'pair_address': candidate.pair_address,
                'liquidity_usd': candidate.liquidity_usd,
                'volume_1h': candidate.volume_1h,
                'volume_24h': candidate.volume_24h,
            }
        )
    tracking_state.prune(
        retention_days=settings.tracking_retention_days,
        drop_stale_tracked_tokens_days=settings.tracking_drop_stale_tokens_days,
    )
    return {
        'tracked_token_count': len(tracked_tokens),
        'rescanned_count': len(runs),
        'runs': runs,
    }
