from __future__ import annotations

import re
from collections import defaultdict

from rh_meme_sniper.models import NarrativeCluster, RawEvent

STOPWORDS = {
    "robinhood",
    "official",
    "the",
    "cto",
    "real",
    "coin",
    "token",
    "ca",
    "is",
    "live",
}

ADDRESS_RE = re.compile(r"0x[a-f0-9]{40}", re.IGNORECASE)


def _normalize_name(value: str) -> str:
    value = ADDRESS_RE.sub(" ", value.lower())
    cleaned = re.sub(r"[^a-z0-9]+", " ", value).strip()
    tokens = [token for token in cleaned.split() if token and token not in STOPWORDS]
    return " ".join(tokens[:2]) or cleaned



def cluster_events(events: list[RawEvent]) -> list[NarrativeCluster]:
    buckets: dict[str, list[RawEvent]] = defaultdict(list)
    display_names: dict[str, str] = {}
    for event in events:
        source_names = event.names or event.symbols or [event.text or event.source_id]
        raw_name = source_names[0]
        key = _normalize_name(raw_name)
        buckets[key].append(event)
        display_names.setdefault(key, raw_name)

    clusters: list[NarrativeCluster] = []
    for key, bucket in buckets.items():
        aliases = sorted({name for event in bucket for name in event.names if name})
        related_contracts = sorted({ca for event in bucket for ca in event.contract_addresses if ca})
        related_pairs = sorted({pair for event in bucket for pair in event.pair_addresses if pair})
        related_handles = sorted({handle for event in bucket if event.author_handle for handle in [event.author_handle]})
        clusters.append(
            NarrativeCluster(
                cluster_id=key.replace(" ", "-") or "unknown",
                canonical_name=display_names[key],
                aliases=aliases,
                related_contracts=related_contracts,
                related_pairs=related_pairs,
                related_handles=related_handles,
                events=sorted(bucket, key=lambda item: item.observed_at),
            )
        )
    return clusters
