# Robinhood Meme Sniper

Python-first architecture for a Robinhood-chain meme coin discovery / authenticity / alerting system.

## Current scope
This repo currently contains:
- Phase 1 design: authenticity + timeline engine
- Phase 2 design: alert-only system
- `.env.example` listing the APIs / secrets to configure
- Python package skeleton for later implementation

## Proposed phases
1. **Phase 1 — Authenticity + timeline**
   - cluster same-narrative tokens
   - extract CA / pair / links
   - build first-seen timeline across X + market data
   - decide likely canonical token
2. **Phase 2 — Alert-only**
   - emit Telegram alerts for high-confidence candidates
   - no auto-buy yet
3. **Phase 3 — Tiny auto-probe buys**
   - optional and guarded
   - only after Phase 1/2 quality is validated

## Layout
- `docs/architecture.md` — system design
- `.env.example` — required env vars
- `src/rh_meme_sniper/` — Python package skeleton

## Notes
- Phase 1 + 2 architecture is documented in `docs/architecture.md`.
- X ingestion can be done either via **Apify** or official **X API**.
- Telegram alert delivery is configured via local `.env`.
- Safe to push to GitHub **after** you keep secrets out of the repo.
- Commit `.env.example`; never commit real `.env`.
