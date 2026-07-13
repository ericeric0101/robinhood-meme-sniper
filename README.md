# Robinhood Meme Sniper

Python-first prototype for discovering likely-canonical Robinhood-chain meme coins, reconstructing early timelines, and sending **alert-only** notifications.

## Current status

This repo is currently a **Phase 1 + Phase 2 prototype**, not a production auto-buy sniper.

### What works today
- X/Twitter ingestion via pluggable provider abstraction
  - **Apify** actor `xtdata~twitter-x-scraper` (current default)
  - **TwitterAPI.io** provider support for advanced tweet search
- Tweet normalization: text / author / URL / engagement / contract extraction
- DexScreener enrichment for pair / liquidity / volume / txn stats
- Narrative clustering across tweets + market records
- Candidate scoring:
  - `authenticity_score`
  - `market_score`
  - `timing_score`
  - `hype_score`
  - `alert_score`
- Alert rendering to stdout
- Optional Telegram delivery
- JSON / log artifacts written to `outputs/`
- Batch execution of a JSON **query pack**
- Query-level Dex pair **allowlist / denylist** filtering to reduce market pollution
- Seed-account + keyword-bucket **discovery runner**
- Phase D **KOL-first scanner** for ANSEM-style narrative discovery: scan watchlist handles first, extract cashtags / CAs / Pump.fun-style contracts / creator-signal phrases, and persist entity events to tracking DB
- SQLite-backed **seen-state / alert cooldown dedupe**

### What does **not** exist yet
- Auto-buy / wallet execution
- Position management
- Honeypot / sellability checks
- Full-firehose X scanning
- Long-running scheduler / daemon loop in this repo
- Production-grade canonical-token disambiguation

---

## Repo layout

- `docs/architecture.md` — architecture and future phases
- `.env.example` — local config template
- `configs/query_packs/` — batch query definitions
- `examples/test_alert_payload.json` — sample payload for offline validation
- `src/rh_meme_sniper/` — application package
- `tests/` — automated tests
- `outputs/` — generated artifacts

---

## Install / setup

### Requirements
- Python **3.11**
- `uv` recommended

### Install dependencies
```bash
cd ~/robinhood-meme-sniper
uv sync
```

### Configure environment
```bash
cp .env.example .env
```

Fill the values you actually need.

### Minimum env for offline/sample runs
No secrets required.

### Minimum env for live Apify runs
```env
X_PROVIDER=apify
APIFY_API_TOKEN=...
```

### Minimum env for live TwitterAPI.io runs
```env
X_PROVIDER=twitterapiio
TWITTERAPIIO_API_KEY=...
# or the official-skill alias:
TWITTERAPI_IO_KEY=...
```

Notes:
- TwitterAPI.io currently uses `GET /twitter/tweet/advanced_search`
- `Top` and `Latest` are supported directly
- `Latest + Top` currently degrades to `Latest` for this provider
- `run-alert-loop` now reports `provider` and `usage_summary` (balance before/after + estimated credits used when available)

### Optional env for tracking DB + LLM judge
```env
TRACKING_DB_PATH=./data/tracking.db
TRACKING_RETENTION_DAYS=180
# optional; leave blank at first
TRACKING_DROP_STALE_TOKENS_DAYS=

TRACKING_JUDGE_ENABLED=true
TRACKING_JUDGE_ENDPOINT=https://api.openai.com/v1/chat/completions
TRACKING_JUDGE_API_KEY=...
TRACKING_JUDGE_MODEL=gpt-4.1-mini
TRACKING_JUDGE_TIMEOUT_SECONDS=30
```

Notes:
- The LLM judge is **optional**. If disabled or unavailable, the repo falls back to the built-in rule-based judge.
- Start with a **cheap model** first. This task is classification / denoise / ranking, not long-form generation.
- Recommended first choice: `gpt-4.1-mini` if your endpoint supports it. If not, another cheap OpenAI-compatible mini model is fine.
- For security, it is better that **you put the key into your local `.env` yourself** rather than pasting the raw secret into chat.

### Additional env for Telegram delivery
```env
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
```

---

## CLI commands

Show help:

```bash
cd ~/robinhood-meme-sniper
uv run --python 3.11 python -m rh_meme_sniper.cli --help
```

Current commands:
- `analyze-sample`
- `apify-search`
- `run-alert-loop`
- `run-query-pack`
- `run-discovery`
- `run-kol-scan`
- `prune-tracking-db`
- `rescan-tracked-tokens`

---

## Recommended usage flow

## 1) Offline sanity check with sample payload

Fastest way to verify the pipeline still works:

```bash
cd ~/robinhood-meme-sniper
uv run --python 3.11 python -m rh_meme_sniper.cli analyze-sample examples/test_alert_payload.json
```

This prints alert text only.

If you want both alert text and generated artifacts:

```bash
cd ~/robinhood-meme-sniper
uv run --python 3.11 python -m rh_meme_sniper.cli run-alert-loop sample --sample-path examples/test_alert_payload.json
```

Expected outputs go to:
- `outputs/tweets_sample.json`
- `outputs/pairs_sample.json`
- `outputs/candidates_sample.json`
- `outputs/timelines_sample.json`
- `outputs/alerts_sample.log`

---

## 2) Run one live prototype query

### Basic run
```bash
cd ~/robinhood-meme-sniper
uv run --python 3.11 python -m rh_meme_sniper.cli run-alert-loop 'cashcat robinhood' --max-items 5 --sort Latest --actor-id xtdata~twitter-x-scraper
```

### Same run with TwitterAPI.io provider
```bash
cd ~/robinhood-meme-sniper
X_PROVIDER=twitterapiio \
TWITTERAPIIO_API_KEY=... \
uv run --python 3.11 python -m rh_meme_sniper.cli run-alert-loop 'cashcat robinhood' --max-items 5 --sort Latest
```

Typical summary output now includes:

```json
{
  "provider": "twitterapiio",
  "usage_summary": {
    "balance_before": {"recharge_credits": 1000000, "total_bonus_credits": 20000},
    "balance_after": {"recharge_credits": 999700, "total_bonus_credits": 20000},
    "credits_used_estimate": 300
  }
}
```

### Same run, but inspect only counts + artifact paths
```bash
cd ~/robinhood-meme-sniper
uv run --python 3.11 python -m rh_meme_sniper.cli apify-search 'cashcat robinhood' --max-items 5 --sort Latest --actor-id xtdata~twitter-x-scraper
```

### Send Telegram alerts too
```bash
cd ~/robinhood-meme-sniper
uv run --python 3.11 python -m rh_meme_sniper.cli run-alert-loop 'cashcat robinhood' --max-items 5 --sort Latest --actor-id xtdata~twitter-x-scraper --send-telegram
```

---

## 3) Reduce Dex pollution with pair allow/deny filters

A common failure mode is: the X query is good, but DexScreener returns many unrelated Robinhood pairs.

Use repeated options:

```bash
cd ~/robinhood-meme-sniper
uv run --python 3.11 python -m rh_meme_sniper.cli run-alert-loop 'cashcat robinhood' \
  --max-items 5 \
  --sort Latest \
  --actor-id xtdata~twitter-x-scraper \
  --pair-allow-term 'cash cat' \
  --pair-deny-term 'baby' \
  --pair-deny-term 'pepe'
```

### Filtering behavior
- If any `--pair-allow-term` is provided, a pair must match **at least one** allow term.
- If any `--pair-deny-term` matches, the pair is dropped.
- Matching is case-insensitive.
- Matching currently checks Dex pair fields such as token name, symbol, URL, and pair address.

This is intended to cut down obvious Dex contamination, not to replace real authenticity scoring.

---

## 4) Batch-run a query pack

Query packs are JSON files containing multiple live queries.

Run the included prototype pack:

```bash
cd ~/robinhood-meme-sniper
uv run --python 3.11 python -m rh_meme_sniper.cli run-query-pack configs/query_packs/xtdata_low_frequency_prototype.json
```

With Telegram delivery:

```bash
cd ~/robinhood-meme-sniper
uv run --python 3.11 python -m rh_meme_sniper.cli run-query-pack configs/query_packs/xtdata_low_frequency_prototype.json --send-telegram
```

The command prints a JSON summary containing:
- actor id
- run count
- per-query counts
- per-query artifact paths

---

## 5) Controlled broad scanning with `run-discovery`

This command expands a **watchlist of seed accounts** plus a **set of keyword buckets** into many live queries.

Included starter configs:
- `configs/watchlists/robinhood_seed_accounts.json`
- `configs/query_buckets/robinhood_discovery.json`

Example:

```bash
cd ~/robinhood-meme-sniper
uv run --python 3.11 python -m rh_meme_sniper.cli run-discovery \
  configs/watchlists/robinhood_seed_accounts.json \
  configs/query_buckets/robinhood_discovery.json
```

With Telegram + dedupe state:

```bash
cd ~/robinhood-meme-sniper
uv run --python 3.11 python -m rh_meme_sniper.cli run-discovery \
  configs/watchlists/robinhood_seed_accounts.json \
  configs/query_buckets/robinhood_discovery.json \
  --send-telegram \
  --state-db-path ./data/state.db \
  --alert-cooldown-seconds 21600
```

What it does:
- Expands account templates like `from:{handle} robinhood`
- Adds keyword buckets like `cashcat robinhood`
- Dedupes identical query/sort/max_items combinations
- Runs each query through the normal live alert pipeline

---

## 6) Phase D KOL-first scanner

`run-kol-scan` is the first ANSEM-style scanner path. Instead of requiring you to know every future token name, it scans watchlist handles first and records entity activity signals into `tracking.db`.

Included starter config:
- `configs/watchlists/phase_d_kol_seed.json`

Example low-cost manual run:

```bash
cd ~/robinhood-meme-sniper
X_PROVIDER=twitterapiio \
uv run --python 3.11 python -m rh_meme_sniper.cli run-kol-scan \
  configs/watchlists/phase_d_kol_seed.json \
  --tracking-db-path ./data/tracking.db \
  --max-items 3 \
  --max-tweet-age-days 14
```

What it records:
- `tracked_entities`: durable handle/entity rows such as `x:blknoiz06`
- `entity_events`: per-post signals including event type, cashtags, Pump.fun-style contracts, URLs, mentioned handles, raw text, and query

Current D-2 event judging is deterministic and intentionally conservative. It classifies lifecycle event types before falling back to generic extraction classes:

| Event type | Meaning |
|---|---|
| `tease` | vibe-check / soon / cooking style pre-token hints |
| `denial` | KOL denies launch, affiliation, or ownership |
| `identity_linked` | text links a person/entity/mascot/name to the token narrative |
| `community_coordination` | community takeover, trench coordination, rally-around-one-CA language |
| `fee_or_airdrop_catalyst` | creator-fee, stimmy, airdrop, holder rewards language |
| `momentum_confirmation` | breaking out, volume, trending, ATH, sending language |
| `exit_risk` | rug/scam/dev-dumped/do-not-buy/honeypot risk language |
| `contract` / `cashtag` / `link` / `mention` | fallback classes when lifecycle rules do not match |

The JSON summary includes `event_type_counts` per handle, which is the quick view for whether a watchlist run found denial, catalyst, exit-risk, or only generic mentions.

This command does **not** auto-buy. It is the first layer for KOL/entity-first narrative memory; query packs remain useful for follow-up probes, backfill, and canonical CA verification.

---

## 7) Stateful dedupe / cooldown

`run-alert-loop`, `run-query-pack`, and `run-discovery` all support:

```bash
--state-db-path ./data/state.db
--alert-cooldown-seconds 3600
```

Behavior:
- Alerts are keyed primarily by **contract address**
- If the same candidate reappears within the cooldown window, the alert is suppressed
- This affects emitted alerts / Telegram spam control; JSON artifacts still get written for the current run

Use this when polling repeatedly so you do not get spammed by the same candidate every cycle.

---

## Query pack format

Example:

```json
{
  "name": "xtdata-low-frequency-prototype",
  "actor_id": "xtdata~twitter-x-scraper",
  "pair_allow_terms": ["cash cat", "office cat"],
  "pair_deny_terms": ["pepe", "wallet"],
  "queries": [
    {
      "id": "cashcat-robinhood",
      "query": "cashcat robinhood",
      "sort": "Latest",
      "max_items": 5
    },
    {
      "id": "robinhood-office-cat-exact",
      "query": "\"Robinhood Office Cat\"",
      "sort": "Top",
      "max_items": 5,
      "pair_allow_terms": ["office cat"],
      "pair_deny_terms": ["compliance", "wallet"]
    }
  ]
}
```

### Query pack rules
- top-level `actor_id` is the default actor
- top-level `pair_allow_terms` / `pair_deny_terms` apply to all queries by default
- per-query `pair_allow_terms` / `pair_deny_terms` override top-level defaults
- per-query `actor_id` can override the pack default

---

## Output artifacts

Every alert-loop run writes:
- `tweets_<slug>.json`
- `pairs_<slug>.json`
- `candidates_<slug>.json`
- `timelines_<slug>.json`
- `alerts_<slug>.log`

Use them like this:

### `tweets_*.json`
Raw/normalized tweet context for the query.

### `pairs_*.json`
Dex pairs that survived filtering.

### `candidates_*.json`
Best file to inspect scoring and verdicts.

### `timelines_*.json`
First-seen timestamps and timeline-related fields.

### `alerts_*.log`
Final alert text that would be delivered.

---

## Current interpretation of this project

Today, this repo should be treated as:

> **a Robinhood-chain meme narrative discovery + authenticity scoring + alerting prototype**

It should **not** yet be treated as:

> a fully autonomous sniper that scans everything on X, decides the canonical token with high confidence, and auto-buys it

---

## Testing

Run the full test suite:

```bash
cd ~/robinhood-meme-sniper
uv run --python 3.11 pytest -q
```

If you accidentally use system Python 3.9, typing features like `str | None` will fail. Use Python 3.11.

---

## Current best-practice operating mode

For now, the safest pattern is:

1. run a sample sanity check
2. run one or more narrow live queries
3. inspect `outputs/candidates_*.json`
4. refine allowlist / denylist terms
5. only then consider using the alerts operationally

---

## Notes

- The current default X prototype ingestion uses **Apify** actor `xtdata~twitter-x-scraper`.
- TwitterAPI.io is now wired as an alternate provider through `X_PROVIDER=twitterapiio`.
- `xtdata` is best treated as a **low-frequency prototype / historical source**, not a production high-frequency poller.
- Safe to push to GitHub **only if secrets stay out of the repo**.
- Commit `.env.example`; never commit real `.env`.
