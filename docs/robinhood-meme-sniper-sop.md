# Robinhood Meme Sniper SOP

This file is the repo-portable handoff for continuing work on `robinhood-meme-sniper` after cloning onto another machine (for example, your Raspberry Pi Hermes host).

## Current role of the repo

This repo is currently an **alert-first Robinhood-chain meme discovery prototype**.
It is **not** an auto-buy sniper.

### What it can do now
- Run sample/offline validation from `examples/test_alert_payload.json`
- Query X/Twitter through Apify actor `xtdata~twitter-x-scraper`
- Enrich candidates with DexScreener market data
- Apply pair allow/deny filters to reduce Dex contamination
- Batch-run multiple searches via query packs
- Run controlled broad scanning via watchlists + query buckets
- Suppress repeated alerts with SQLite seen-state / cooldown logic
- Optionally send Telegram alerts

### What it still cannot do
- Auto-buy / execute trades
- Manage positions
- Perform honeypot/sellability checks
- Reliably scan the full X firehose
- Act as a production daemon without an external scheduler

## Required runtime habits

- Use **Python 3.11**
- Prefer commands in the form:

```bash
uv run --python 3.11 ...
```

- Keep secrets redacted in docs/commits
- Treat Apify billing/quota failures as external blockers, not as proof the repo logic is broken

## Key files

- `README.md`
- `docs/architecture.md`
- `configs/query_packs/xtdata_low_frequency_prototype.json`
- `configs/watchlists/robinhood_seed_accounts.json`
- `configs/query_buckets/robinhood_discovery.json`
- `src/rh_meme_sniper/pipeline.py`
- `src/rh_meme_sniper/cli.py`
- `src/rh_meme_sniper/state.py`
- `tests/test_pipeline.py`

## Standard continuation workflow

1. Read `README.md` and this SOP first.
2. Check git state:

```bash
git status --short
git branch --show-current
git remote -v
```

3. If changing behavior, add or extend tests first.
4. Implement the smallest working change.
5. Run verification:

```bash
cd ~/robinhood-meme-sniper
uv run --python 3.11 pytest -q
```

6. For CLI changes, also run help or a real command.
7. Update `README.md` and starter configs in the same commit when behavior changes.
8. Commit only source/config/docs changes; do not commit build artifacts.

## Known-good verification commands

### Full tests
```bash
cd ~/robinhood-meme-sniper
uv run --python 3.11 pytest -q
```

### CLI help
```bash
cd ~/robinhood-meme-sniper
uv run --python 3.11 python -m rh_meme_sniper.cli --help
```

### Sample alert loop
```bash
cd ~/robinhood-meme-sniper
uv run --python 3.11 python -m rh_meme_sniper.cli run-alert-loop sample \
  --sample-path examples/test_alert_payload.json
```

### Sample dedupe check
```bash
cd ~/robinhood-meme-sniper
uv run --python 3.11 python -m rh_meme_sniper.cli run-alert-loop sample \
  --sample-path examples/test_alert_payload.json \
  --state-db-path ./data/state.db \
  --alert-cooldown-seconds 3600
```
Run it twice; the second run should suppress the duplicate alert.

### Query pack
```bash
cd ~/robinhood-meme-sniper
uv run --python 3.11 python -m rh_meme_sniper.cli run-query-pack \
  configs/query_packs/xtdata_low_frequency_prototype.json \
  --state-db-path ./data/state.db \
  --alert-cooldown-seconds 21600
```

### Discovery runner
```bash
cd ~/robinhood-meme-sniper
uv run --python 3.11 python -m rh_meme_sniper.cli run-discovery \
  configs/watchlists/robinhood_seed_accounts.json \
  configs/query_buckets/robinhood_discovery.json \
  --state-db-path ./data/state.db \
  --alert-cooldown-seconds 21600
```

## Seed discovery inputs

### Watchlist accounts
Primary:
- `jiggacapital`
- `DoxxedChannel`
- `dexevents_miu`

Secondary:
- `Tribalcyrpto`
- `MogOfWallstreet`
- `Mavericks100xs`

### Starter keyword buckets
- `cashcat robinhood`
- `"office cat" robinhood`
- `"Robinhood Office Cat"`

## Important operational caveats

1. **Apify can block live verification**
   - Example failure: `402 Payment Required`
   - When this happens, verify sample path and tests, then report the external blocker clearly.

2. **Dex contamination is normal**
   - Tune `pair_allow_terms` / `pair_deny_terms` in configs instead of assuming the pipeline is broken.

3. **Dedupe is alert suppression, not full stateful intelligence**
   - Current seen-state mainly suppresses repeated alerts keyed by contract address.
   - It is useful, but not the same thing as a mature ranking / memory engine.

4. **This repo still needs a scheduler for full automation**
   - The discovery runner exists, but recurring execution still needs cron/Hermes scheduling/systemd/etc.

## Pre-push checklist

- [ ] `uv run --python 3.11 pytest -q` passes
- [ ] relevant CLI command/help was run after the change
- [ ] README/SOP/configs match current behavior
- [ ] no secrets committed
- [ ] no build artifacts staged (`*.egg-info`, `build/`, `dist/`)
- [ ] `git status` clean after commit
