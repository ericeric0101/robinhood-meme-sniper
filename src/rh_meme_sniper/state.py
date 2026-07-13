from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from rh_meme_sniper.models import CandidateToken, NarrativeCluster, RawEvent


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def _iso_to_epoch(value: str) -> float:
    normalized = value.replace('Z', '+00:00')
    return datetime.fromisoformat(normalized).timestamp()


@dataclass(slots=True)
class SeenAlertDecision:
    key: str
    should_emit: bool


class SeenState:
    def __init__(self, sqlite_path: str | Path) -> None:
        self.sqlite_path = Path(sqlite_path)
        self.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.sqlite_path)

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                '''
                CREATE TABLE IF NOT EXISTS seen_alerts (
                    alert_key TEXT PRIMARY KEY,
                    contract_address TEXT,
                    cluster_id TEXT,
                    candidate_name TEXT,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    seen_count INTEGER NOT NULL DEFAULT 1
                )
                '''
            )

    @staticmethod
    def build_alert_key(candidate: CandidateToken, cluster: NarrativeCluster) -> str:
        if candidate.contract_address:
            return f'contract:{candidate.contract_address.lower()}'
        if cluster.cluster_id:
            return f'cluster:{cluster.cluster_id.lower()}'
        if cluster.canonical_name:
            return f'name:{cluster.canonical_name.lower()}'
        return 'unknown'

    def should_emit(self, candidate: CandidateToken, cluster: NarrativeCluster, *, cooldown_seconds: int) -> SeenAlertDecision:
        key = self.build_alert_key(candidate, cluster)
        with self._connect() as conn:
            row = conn.execute(
                'SELECT last_seen_at FROM seen_alerts WHERE alert_key = ?',
                (key,),
            ).fetchone()
        if row is None:
            return SeenAlertDecision(key=key, should_emit=True)
        last_seen_at = str(row[0])
        age_seconds = _iso_to_epoch(_utc_now_iso()) - _iso_to_epoch(last_seen_at)
        return SeenAlertDecision(key=key, should_emit=age_seconds >= cooldown_seconds)

    def record_emit(self, candidate: CandidateToken, cluster: NarrativeCluster, *, key: str | None = None) -> None:
        alert_key = key or self.build_alert_key(candidate, cluster)
        now = _utc_now_iso()
        with self._connect() as conn:
            existing = conn.execute(
                'SELECT seen_count, first_seen_at FROM seen_alerts WHERE alert_key = ?',
                (alert_key,),
            ).fetchone()
            if existing is None:
                conn.execute(
                    '''
                    INSERT INTO seen_alerts (
                        alert_key, contract_address, cluster_id, candidate_name, first_seen_at, last_seen_at, seen_count
                    ) VALUES (?, ?, ?, ?, ?, ?, 1)
                    ''',
                    (
                        alert_key,
                        candidate.contract_address,
                        cluster.cluster_id,
                        candidate.name or cluster.canonical_name,
                        now,
                        now,
                    ),
                )
            else:
                conn.execute(
                    '''
                    UPDATE seen_alerts
                    SET last_seen_at = ?,
                        seen_count = seen_count + 1,
                        contract_address = ?,
                        cluster_id = ?,
                        candidate_name = ?
                    WHERE alert_key = ?
                    ''',
                    (now, candidate.contract_address, cluster.cluster_id, candidate.name or cluster.canonical_name, alert_key),
                )


class TrackingState:
    def __init__(self, sqlite_path: str | Path) -> None:
        self.sqlite_path = Path(sqlite_path)
        self.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.sqlite_path)

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                '''
                CREATE TABLE IF NOT EXISTS tracked_tokens (
                    contract_address TEXT PRIMARY KEY,
                    symbol TEXT,
                    name TEXT,
                    pair_address TEXT,
                    chain TEXT,
                    query TEXT,
                    cluster_id TEXT,
                    canonical_name TEXT,
                    tracking_status TEXT NOT NULL DEFAULT 'watch',
                    tracking_reason TEXT,
                    first_seen_x_at TEXT,
                    first_seen_ca_at TEXT,
                    first_seen_market_at TEXT,
                    first_discovered_at TEXT NOT NULL,
                    last_updated_at TEXT NOT NULL
                )
                '''
            )
            columns = {row[1] for row in conn.execute("PRAGMA table_info(tracked_tokens)").fetchall()}
            if 'tracking_status' not in columns:
                conn.execute("ALTER TABLE tracked_tokens ADD COLUMN tracking_status TEXT NOT NULL DEFAULT 'watch'")
            if 'tracking_reason' not in columns:
                conn.execute("ALTER TABLE tracked_tokens ADD COLUMN tracking_reason TEXT")
            conn.execute(
                '''
                CREATE TABLE IF NOT EXISTS mentions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    contract_address TEXT,
                    source TEXT,
                    source_id TEXT,
                    observed_at TEXT,
                    author_handle TEXT,
                    query TEXT,
                    text TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(source_id, contract_address, query)
                )
                '''
            )
            conn.execute(
                '''
                CREATE TABLE IF NOT EXISTS market_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    contract_address TEXT NOT NULL,
                    pair_address TEXT,
                    symbol TEXT,
                    name TEXT,
                    chain TEXT,
                    query TEXT,
                    liquidity_usd REAL,
                    volume_1h REAL,
                    volume_24h REAL,
                    buy_count_1h INTEGER,
                    sell_count_1h INTEGER,
                    captured_at TEXT NOT NULL
                )
                '''
            )
            conn.execute(
                '''
                CREATE TABLE IF NOT EXISTS tracked_entities (
                    entity_key TEXT PRIMARY KEY,
                    entity_type TEXT NOT NULL,
                    display_name TEXT,
                    source TEXT,
                    metadata_json TEXT,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL
                )
                '''
            )
            conn.execute(
                '''
                CREATE TABLE IF NOT EXISTS entity_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_key TEXT NOT NULL,
                    source TEXT,
                    source_id TEXT,
                    observed_at TEXT,
                    author_handle TEXT,
                    query TEXT,
                    event_type TEXT NOT NULL,
                    symbols_json TEXT NOT NULL DEFAULT '[]',
                    contracts_json TEXT NOT NULL DEFAULT '[]',
                    urls_json TEXT NOT NULL DEFAULT '[]',
                    entities_json TEXT NOT NULL DEFAULT '[]',
                    text TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(entity_key, source_id, event_type)
                )
                '''
            )

    def record_entity(self, *, entity_key: str, entity_type: str, display_name: str, source: str = 'x', metadata_json: str = '{}') -> None:
        now = _utc_now_iso()
        with self._connect() as conn:
            conn.execute(
                '''
                INSERT INTO tracked_entities (
                    entity_key, entity_type, display_name, source, metadata_json, first_seen_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(entity_key) DO UPDATE SET
                    entity_type=excluded.entity_type,
                    display_name=excluded.display_name,
                    source=excluded.source,
                    metadata_json=excluded.metadata_json,
                    last_seen_at=excluded.last_seen_at
                ''',
                (entity_key, entity_type, display_name, source, metadata_json, now, now),
            )

    def record_entity_event(
        self,
        *,
        entity_key: str,
        query: str,
        event: RawEvent,
        event_type: str,
        symbols_json: str,
        contracts_json: str,
        urls_json: str,
        entities_json: str,
    ) -> None:
        now = _utc_now_iso()
        with self._connect() as conn:
            conn.execute(
                '''
                INSERT OR IGNORE INTO entity_events (
                    entity_key, source, source_id, observed_at, author_handle, query,
                    event_type, symbols_json, contracts_json, urls_json, entities_json, text, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''',
                (
                    entity_key,
                    event.source,
                    event.source_id,
                    event.observed_at,
                    event.author_handle,
                    query,
                    event_type,
                    symbols_json,
                    contracts_json,
                    urls_json,
                    entities_json,
                    event.text,
                    now,
                ),
            )

    def record_candidate(self, *, query: str, candidate: CandidateToken, cluster: NarrativeCluster) -> None:
        now = _utc_now_iso()
        with self._connect() as conn:
            conn.execute(
                '''
                INSERT INTO tracked_tokens (
                    contract_address, symbol, name, pair_address, chain, query, cluster_id, canonical_name,
                    tracking_status, tracking_reason, first_seen_x_at, first_seen_ca_at, first_seen_market_at, first_discovered_at, last_updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(contract_address) DO UPDATE SET
                    symbol=excluded.symbol,
                    name=excluded.name,
                    pair_address=excluded.pair_address,
                    chain=excluded.chain,
                    query=excluded.query,
                    cluster_id=excluded.cluster_id,
                    canonical_name=excluded.canonical_name,
                    tracking_status=excluded.tracking_status,
                    tracking_reason=excluded.tracking_reason,
                    first_seen_x_at=COALESCE(tracked_tokens.first_seen_x_at, excluded.first_seen_x_at),
                    first_seen_ca_at=COALESCE(tracked_tokens.first_seen_ca_at, excluded.first_seen_ca_at),
                    first_seen_market_at=COALESCE(tracked_tokens.first_seen_market_at, excluded.first_seen_market_at),
                    last_updated_at=excluded.last_updated_at
                ''',
                (
                    candidate.contract_address,
                    candidate.symbol,
                    candidate.name,
                    candidate.pair_address,
                    candidate.chain,
                    query,
                    cluster.cluster_id,
                    cluster.canonical_name,
                    candidate.tracking_status,
                    candidate.tracking_reason,
                    candidate.first_seen_x_at,
                    candidate.first_seen_ca_at,
                    candidate.first_seen_market_at,
                    now,
                    now,
                ),
            )
        self.record_market_snapshot(query=query, candidate=candidate)

    def record_mention(self, *, query: str, contract_address: str | None, event: RawEvent) -> None:
        now = _utc_now_iso()
        with self._connect() as conn:
            conn.execute(
                '''
                INSERT OR IGNORE INTO mentions (
                    contract_address, source, source_id, observed_at, author_handle, query, text, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ''',
                (
                    contract_address,
                    event.source,
                    event.source_id,
                    event.observed_at,
                    event.author_handle,
                    query,
                    event.text,
                    now,
                ),
            )

    def record_market_snapshot(self, *, query: str, candidate: CandidateToken) -> None:
        with self._connect() as conn:
            conn.execute(
                '''
                INSERT INTO market_snapshots (
                    contract_address, pair_address, symbol, name, chain, query,
                    liquidity_usd, volume_1h, volume_24h, buy_count_1h, sell_count_1h, captured_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''',
                (
                    candidate.contract_address,
                    candidate.pair_address,
                    candidate.symbol,
                    candidate.name,
                    candidate.chain,
                    query,
                    candidate.liquidity_usd,
                    candidate.volume_1h,
                    candidate.volume_24h,
                    candidate.buy_count_1h,
                    candidate.sell_count_1h,
                    _utc_now_iso(),
                ),
            )

    def list_tracked_tokens(self) -> list[dict[str, str | None]]:
        with self._connect() as conn:
            rows = conn.execute(
                '''
                SELECT contract_address, symbol, name, pair_address, chain, query
                FROM tracked_tokens
                ORDER BY last_updated_at DESC
                '''
            ).fetchall()
        return [
            {
                'contract_address': row[0],
                'symbol': row[1],
                'name': row[2],
                'pair_address': row[3],
                'chain': row[4],
                'query': row[5],
            }
            for row in rows
        ]

    def prune(
        self,
        *,
        retention_days: int,
        drop_stale_tracked_tokens_days: int | None = None,
    ) -> dict[str, Any]:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).replace(microsecond=0).isoformat().replace('+00:00', 'Z')
        tracked_cutoff = None
        if drop_stale_tracked_tokens_days is not None:
            tracked_cutoff = (datetime.now(timezone.utc) - timedelta(days=drop_stale_tracked_tokens_days)).replace(microsecond=0).isoformat().replace('+00:00', 'Z')
        with self._connect() as conn:
            mentions_deleted = conn.execute('DELETE FROM mentions WHERE created_at < ?', (cutoff,)).rowcount
            snapshots_deleted = conn.execute('DELETE FROM market_snapshots WHERE captured_at < ?', (cutoff,)).rowcount
            tracked_deleted = 0
            if tracked_cutoff is not None:
                tracked_deleted = conn.execute(
                    '''
                    DELETE FROM tracked_tokens
                    WHERE last_updated_at < ?
                      AND contract_address NOT IN (
                        SELECT DISTINCT contract_address FROM market_snapshots WHERE contract_address IS NOT NULL
                      )
                    ''',
                    (tracked_cutoff,),
                ).rowcount
        return {
            'retention_days': retention_days,
            'deleted_mentions': int(mentions_deleted or 0),
            'deleted_market_snapshots': int(snapshots_deleted or 0),
            'deleted_tracked_tokens': int(tracked_deleted or 0),
        }
