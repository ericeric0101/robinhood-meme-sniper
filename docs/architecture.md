# Robinhood Meme Sniper Architecture

> Goal: build a **Python** system that identifies likely-canonical Robinhood-chain meme coins, reconstructs first-seen timelines, and sends **alert-only** Telegram notifications before any auto-buy phase.

## Why Python?
- excellent for API aggregation and ETL
- strong ecosystem for cron / CLI / async HTTP
- easy to run on Mac / Raspberry Pi / server
- easy to test, version, and push to GitHub

## Phase breakdown

### Phase 1 — Authenticity + timeline engine
Output: one normalized `Candidate` record per narrative cluster.

Responsibilities:
1. ingest raw signals from Apify X scraper / official X API / DexScreener / GeckoTerminal / optional on-chain explorer
2. extract token identifiers:
   - token contract address (CA)
   - pair address
   - symbol / name
   - linked website / TG / X handle
3. cluster records that appear to describe the same narrative
   - e.g. many variants of "Robinhood Office Cat"
4. build event timeline:
   - first X mention time
   - first CA mention time
   - first market listing / pair creation time
   - first meaningful liquidity
   - first KOL mention time(s)
5. score authenticity
   - which CA is most likely the canonical / original token?

### Phase 2 — Alert-only system
Output: Telegram alerts, JSON snapshots, ranked watchlist.

Responsibilities:
1. poll sources every N minutes
2. update cluster and scoring state
3. trigger alert when threshold met
4. write artifacts for review:
   - `outputs/candidates.json`
   - `outputs/timelines.json`
   - `outputs/alerts.log`
5. avoid duplicate alerts with cooldown / state db

### Phase 3 — Tiny auto-probe buys (not yet implementing)
Will require:
- DEX execution path
- max daily loss
- per-trade cap
- sellability / honeypot checks
- duplicate / spoof protection

## Core architecture

```text
           +-------------------+
           |   source pollers  |
           | Apify/X / Dex /   |
           | Gecko / Explorer  |
           +---------+---------+
                     |
                     v
           +-------------------+
           | raw event store    |
           | json/sqlite        |
           +---------+---------+
                     |
                     v
           +-------------------+
           | extraction layer   |
           | CA / links / name  |
           +---------+---------+
                     |
                     v
           +-------------------+
           | clustering layer   |
           | same narrative?    |
           +---------+---------+
                     |
                     v
           +-------------------+
           | timeline builder   |
           | first seen events  |
           +---------+---------+
                     |
                     v
           +-------------------+
           | scoring engine     |
           | authenticity/hype  |
           +---------+---------+
                     |
          +----------+----------+
          |                     |
          v                     v
+-------------------+   +-------------------+
| Telegram alerts   |   | JSON/CSV outputs  |
+-------------------+   +-------------------+
```

## Data model

### RawEvent
One source observation.

Fields:
- `source`: `x`, `dexscreener`, `geckoterminal`, `explorer`, `manual`
- `source_id`: post id / pair id / tx hash
- `observed_at`
- `author_handle` (for X)
- `text`
- `urls`
- `contract_addresses[]`
- `pair_addresses[]`
- `symbols[]`
- `names[]`
- `metrics` (volume, liquidity, txns, etc.)

### NarrativeCluster
Represents one meme narrative, not one contract.

Fields:
- `cluster_id`
- `canonical_name`
- `aliases[]`
- `related_contracts[]`
- `related_pairs[]`
- `related_handles[]`
- `status`: `unknown`, `contested`, `likely_canonical`, `spoof_heavy`, `dead`

### CandidateToken
Represents one specific CA inside a cluster.

Fields:
- `contract_address`
- `chain = robinhood`
- `symbol`
- `name`
- `pair_address`
- `pair_created_at`
- `first_seen_x_at`
- `first_seen_ca_at`
- `first_seen_market_at`
- `first_kol_mentions[]`
- `liquidity_usd`
- `volume_5m`, `volume_1h`, `volume_24h`
- `buy_count_1h`, `sell_count_1h`
- `authenticity_score`
- `timing_score`
- `market_score`
- `hype_score`
- `alert_score`
- `verdict`: `watch`, `alert`, `avoid`

## Authenticity scoring (Phase 1)

Proposed weighted factors:
- **CA first-seen precedence (25%)**
  - first contract mentioned in credible sources gets strong boost
- **source consistency (20%)**
  - later posts, links, and market pages repeatedly point to same CA
- **market precedence (15%)**
  - earliest meaningful pair / liquidity / trading
- **KOL precedence (15%)**
  - earlier credible KOL mentions for same CA
- **official-link coherence (15%)**
  - website / Telegram / X handle all point to same token
- **spoof penalty (10%)**
  - duplicate names, suspicious copycats, fragmented liquidity

Interpretation:
- `>= 80`: likely canonical
- `60–79`: contested / needs review
- `< 60`: likely spoof or weak evidence

## Alert logic (Phase 2)
Alert only if all are true:
1. `authenticity_score >= threshold`
2. `market_score >= threshold`
3. candidate age still inside early window
4. not already alerted recently
5. liquidity above minimum
6. there is at least one identifiable CA

### Example alert payload
```json
{
  "cluster": "Robinhood Office Cat",
  "canonical_ca": "0x...",
  "authenticity_score": 86,
  "first_seen_x_at": "2026-07-09T01:18:00Z",
  "first_kol": ["@kol_a", "@kol_b"],
  "pair_created_at": "2026-07-09T01:23:00Z",
  "liq_usd": 102340,
  "alert_score": 88,
  "verdict": "alert"
}
```

## Python package layout

```text
src/rh_meme_sniper/
  config.py              # env loading
  models.py              # pydantic models
  sources/
    x_source.py          # X poller
    dexscreener.py       # DexScreener poller
    geckoterminal.py     # GeckoTerminal poller
    explorer.py          # optional explorer integration
  extract/
    ca_parser.py         # address extraction from text/urls
    entity_parser.py     # symbol/name/link extraction
  cluster/
    narrative_cluster.py # group same-story tokens
  score/
    authenticity.py
    timing.py
    market.py
    hype.py
  alerts/
    telegram.py
    dedupe.py
  storage/
    sqlite_store.py
    json_store.py
  cli.py                 # manual run / daemon entrypoints
```

## Scheduling
- local dev: manual CLI run
- server / Pi: cron every 2–5 minutes or a long-running loop
- recommend SQLite for dedupe / state

## Validation strategy
1. replay known historical launches
2. verify whether system picked the real canonical token
3. compare canonical pick vs obvious spoofs
4. measure false alerts and missed alerts

## GitHub guidance
Yes — this project is suitable for GitHub.

Push these:
- code
- tests
- docs
- `.env.example`
- sample output schemas (sanitized)

Do **not** push:
- `.env`
- API secrets
- wallet keys
- live Telegram tokens
- raw proprietary exports with secrets

## Immediate implementation target
For now, implement only:
- source pollers
- extraction
- clustering
- scoring
- Telegram alert-only delivery

Do **not** implement auto-buy in the first iteration.
