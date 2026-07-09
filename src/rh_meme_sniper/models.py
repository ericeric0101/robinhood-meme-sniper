from pydantic import BaseModel, Field
from typing import Any


class RawEvent(BaseModel):
    source: str
    source_id: str
    observed_at: str
    author_handle: str | None = None
    text: str | None = None
    urls: list[str] = Field(default_factory=list)
    contract_addresses: list[str] = Field(default_factory=list)
    pair_addresses: list[str] = Field(default_factory=list)
    symbols: list[str] = Field(default_factory=list)
    names: list[str] = Field(default_factory=list)
    metrics: dict[str, Any] = Field(default_factory=dict)


class CandidateToken(BaseModel):
    cluster_id: str
    contract_address: str
    chain: str = 'robinhood'
    symbol: str | None = None
    name: str | None = None
    pair_address: str | None = None
    pair_created_at: str | None = None
    first_seen_x_at: str | None = None
    first_seen_ca_at: str | None = None
    first_seen_market_at: str | None = None
    first_kol_mentions: list[str] = Field(default_factory=list)
    liquidity_usd: float = 0.0
    volume_1h: float = 0.0
    volume_24h: float = 0.0
    buy_count_1h: int = 0
    sell_count_1h: int = 0
    authenticity_score: float = 0.0
    timing_score: float = 0.0
    market_score: float = 0.0
    hype_score: float = 0.0
    alert_score: float = 0.0
    verdict: str = 'watch'
