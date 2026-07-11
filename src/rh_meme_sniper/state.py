from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from rh_meme_sniper.models import CandidateToken, NarrativeCluster


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
