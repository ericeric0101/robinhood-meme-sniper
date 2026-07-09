from __future__ import annotations

from collections import defaultdict

from rh_meme_sniper.models import CandidateToken, NarrativeCluster, RawEvent



def _earliest(values: list[str]) -> str | None:
    values = [value for value in values if value]
    return min(values) if values else None



def _build_candidate(cluster: NarrativeCluster, contract_address: str, events: list[RawEvent]) -> CandidateToken:
    names = [name for event in events for name in event.names if name]
    symbols = [symbol for event in events for symbol in event.symbols if symbol]
    market_events = [event for event in events if event.source == "dexscreener"]
    x_events = [event for event in events if event.source == "x"]
    first_seen_x_at = _earliest([event.observed_at for event in x_events])
    first_seen_market_at = _earliest([event.metrics.get("pair_created_at") or event.observed_at for event in market_events])
    liquidity_usd = max((float(event.metrics.get("liquidity_usd") or 0.0) for event in market_events), default=0.0)
    volume_1h = max((float(event.metrics.get("volume_1h") or 0.0) for event in market_events), default=0.0)
    volume_24h = max((float(event.metrics.get("volume_24h") or 0.0) for event in market_events), default=0.0)
    buy_count_1h = max((int(event.metrics.get("buy_count_1h") or 0) for event in market_events), default=0)
    sell_count_1h = max((int(event.metrics.get("sell_count_1h") or 0) for event in market_events), default=0)
    return CandidateToken(
        cluster_id=cluster.cluster_id,
        contract_address=contract_address,
        symbol=symbols[0] if symbols else None,
        name=names[0] if names else cluster.canonical_name,
        pair_address=next((pair for event in events for pair in event.pair_addresses if pair), None),
        pair_created_at=first_seen_market_at,
        first_seen_x_at=first_seen_x_at,
        first_seen_ca_at=_earliest([event.observed_at for event in events]),
        first_seen_market_at=first_seen_market_at,
        first_kol_mentions=sorted({event.author_handle for event in x_events if event.author_handle}),
        liquidity_usd=liquidity_usd,
        volume_1h=volume_1h,
        volume_24h=volume_24h,
        buy_count_1h=buy_count_1h,
        sell_count_1h=sell_count_1h,
    )



def _score_candidates(candidates: list[CandidateToken]) -> list[CandidateToken]:
    first_seen_values = [candidate.first_seen_ca_at for candidate in candidates if candidate.first_seen_ca_at]
    earliest_seen = min(first_seen_values) if first_seen_values else None
    for candidate in candidates:
        authenticity = 40.0
        if earliest_seen and candidate.first_seen_ca_at == earliest_seen:
            authenticity += 25.0
        authenticity += min(candidate.liquidity_usd / 1000.0, 20.0)
        authenticity += min(candidate.volume_1h / 2500.0, 10.0)
        authenticity += min(len(candidate.first_kol_mentions) * 5.0, 10.0)
        if candidate.name and "cto" in candidate.name.lower():
            authenticity -= 20.0

        market = min(candidate.liquidity_usd / 500.0, 50.0) + min(candidate.volume_1h / 1000.0, 30.0)
        market += min(candidate.buy_count_1h / 10.0, 10.0)
        market += min(candidate.sell_count_1h / 10.0, 10.0)
        timing = 80.0 if earliest_seen and candidate.first_seen_ca_at == earliest_seen else 55.0
        hype = min(len(candidate.first_kol_mentions) * 20.0, 60.0)
        hype += min(candidate.volume_1h / 5000.0, 20.0)
        hype += min(candidate.buy_count_1h / 20.0, 20.0)
        alert_score = authenticity * 0.5 + market * 0.3 + timing * 0.1 + hype * 0.1
        verdict = "alert" if authenticity >= 80 and market >= 55 and alert_score >= 75 else "watch"

        candidate.authenticity_score = round(authenticity, 2)
        candidate.market_score = round(market, 2)
        candidate.timing_score = round(timing, 2)
        candidate.hype_score = round(hype, 2)
        candidate.alert_score = round(alert_score, 2)
        candidate.verdict = verdict
    return sorted(candidates, key=lambda item: item.alert_score, reverse=True)



def score_clusters(clusters: list[NarrativeCluster]) -> list[NarrativeCluster]:
    scored_clusters: list[NarrativeCluster] = []
    for cluster in clusters:
        grouped: dict[str, list[RawEvent]] = defaultdict(list)
        for event in cluster.events:
            for contract in event.contract_addresses:
                grouped[contract].append(event)
        candidates = [_build_candidate(cluster, contract, events) for contract, events in grouped.items()]
        cluster.candidates = _score_candidates(candidates)
        cluster.canonical_candidate = cluster.candidates[0] if cluster.candidates else None
        cluster.status = "likely_canonical" if cluster.canonical_candidate and cluster.canonical_candidate.authenticity_score >= 80 else "unknown"
        scored_clusters.append(cluster)
    return scored_clusters
