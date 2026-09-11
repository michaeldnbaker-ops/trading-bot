# Edge research (paper trading only)

This folder holds **research and specs**, not live trading code.

- **Paper Alpaca only.** No live brokerage changes. No new live/trading code paths in this PR.
- Specs here are for Learning Loop / Ops review before any agent is implemented.
- Implementation PRs (if approved) must keep `PAPER_TRADING=true` and use existing paper Alpaca clients.

**Ops lock (2026-09-11):** inventory and gap order below are authoritative. Do not add crypto edges. Do not add new options ideas until equity exits + scorecard are green.

**Learning Loop lock:** A/B/C are **cold** sleeves **PROMOTED** only when a bleeder is **BENCHED**. Rotator words: FLAG / BENCHED / PROMOTED / REACTIVATED — not KEEP/DISABLE. One first-slot owner per parent: Technical is A then B; OptionsFlow is A only; B on-ramps = Volatility + Movers (empty slots reserved; Ops PR #3 will not steal them). Cold names **must be seeded** `{ "active": false }` in `agent_summary.json` — missing-from-summary is not a promote (Ops PR #3 `_find_replacement`). 3-day **REACTIVATED** is expected. Numeric kill boxes = **TODO** (Day-1 scorecard is a **NOT LOCKED** footnote — do not invent cutoffs). Improver cannot auto-apply. Friday learn does not retune until `get_agent_adjustment` is wired. See [ROTATION-CONTRACT.md](ROTATION-CONTRACT.md).

## Contents

| File | Ops gap | What it is |
|---|---|---|
| [SURVEY.md](SURVEY.md) | — | Confirmed roster vs `ensemble.py`, measurement loop, Ops book notes |
| [ROTATION-CONTRACT.md](ROTATION-CONTRACT.md) | — | FLAG → BENCHED → PROMOTED / REACTIVATED; exclusive A/B `AGENT_VARIANTS` parents |
| [SPEC-A-regime-equity.md](SPEC-A-regime-equity.md) | **A** | Regime-aware equity L/S **PROMOTED** when Technical / OptionsFlow / Breakout / SectorRotation is BENCHED (A first on Technical; sole new name on OptionsFlow) |
| [SPEC-B-bear-shorts.md](SPEC-B-bear-shorts.md) | **B** | Fade-rally short, **BEAR/HIGH_VOL only**; **PROMOTED** from Volatility / Movers (empty reserved); second on Technical after A; not on OptionsFlow |
| [SPEC-C-news-premarket-quality.md](SPEC-C-news-premarket-quality.md) | **C** | Premarket strict **PROMOTED** when Premarket is BENCHED; News in-place (PROTECTED) |

## Explicitly out of scope

- **D — Options:** pause new options ideas. Existing paper calls (XLE/SBUX/F via OptionsFlow + `options_executor`) stay an Ops/exits problem, not a research sleeve.
- **E — Crypto:** `CryptoAgent` is a **separate** `crypto_scheduler` sleeve. Ops wants it **OFF** for the main equity bot. No new crypto edges.
- Do **not** implement these specs in this PR.
- Do **not** lift the shorts-only-in-BEAR/HIGH_VOL hard gate.
