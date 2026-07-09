from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from rh_meme_sniper.models import RawEvent



def _iso_from_millis(value: int | float | None) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(float(value) / 1000.0, tz=timezone.utc).isoformat().replace('+00:00', 'Z')



def normalize_pair_item(item: dict[str, Any]) -> RawEvent:
    base = item.get("baseToken") or {}
    txns_1h = ((item.get("txns") or {}).get("h1") or {})
    metrics = {
        "chain_id": item.get("chainId"),
        "liquidity_usd": float(((item.get("liquidity") or {}).get("usd") or 0.0)),
        "volume_24h": float(((item.get("volume") or {}).get("h24") or 0.0)),
        "volume_1h": float(((item.get("volume") or {}).get("h1") or 0.0)),
        "buy_count_1h": int(txns_1h.get("buys") or 0),
        "sell_count_1h": int(txns_1h.get("sells") or 0),
        "pair_created_at": _iso_from_millis(item.get("pairCreatedAt")),
    }
    return RawEvent(
        source="dexscreener",
        source_id=str(item.get("pairAddress") or ""),
        observed_at=metrics["pair_created_at"] or "",
        urls=[value for value in [item.get("url")] if value],
        contract_addresses=[value for value in [base.get("address")] if value],
        pair_addresses=[value for value in [item.get("pairAddress")] if value],
        symbols=[value for value in [base.get("symbol")] if value],
        names=[value for value in [base.get("name")] if value],
        metrics=metrics,
    )
