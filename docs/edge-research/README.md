# Edge research (paper trading only)

This folder holds **research and specs**, not live trading code.

- **Paper Alpaca only.** No live brokerage changes. No new live/trading code paths in this PR.
- Specs here are for Learning Loop / Ops review before any agent is implemented.
- Implementation PRs (if approved) must keep `PAPER_TRADING=true` and use existing paper Alpaca clients.

**Ops lock (2026-09-11):** inventory and gap order below are authoritative. Do not add crypto edges. Do not add new options ideas until equity exits + scorecard are green.

## Contents

| File | Ops gap | What it is |
|---|---|---|
| [SURVEY.md](SURVEY.md) | — | Confirmed roster vs `ensemble.py`, measurement loop, Ops book notes |
| [SPEC-A-regime-equity.md](SPEC-A-regime-equity.md) | **A** | Regime-aware equity long/short to **replace bleeders**, with kill criteria vs SPY |
| [SPEC-B-bear-shorts.md](SPEC-B-bear-shorts.md) | **B** | Additional short/downside edge, **BEAR/HIGH_VOL only** (gates already restrict shorts) |
| [SPEC-C-news-premarket-quality.md](SPEC-C-news-premarket-quality.md) | **C** | Premarket/News (and Technical) quality filter — fewer garbage opens / ghosts |

## Explicitly out of scope

- **D — Options:** pause new options ideas. Existing paper calls (XLE/SBUX/F via OptionsFlow + `options_executor`) stay an Ops/exits problem, not a research sleeve.
- **E — Crypto:** `CryptoAgent` is a **separate** `crypto_scheduler` sleeve. Ops wants it **OFF** for the main equity bot. No new crypto edges.
- Do **not** implement these specs in this PR.
- Do **not** lift the shorts-only-in-BEAR/HIGH_VOL hard gate.
