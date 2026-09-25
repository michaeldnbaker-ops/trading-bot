# Edge research (paper trading only)

This folder holds **research and specs**, not live trading code.

- **Paper Alpaca only.** No live brokerage changes. No new live/trading code paths in this PR.
- Specs here are for Learning Loop / Ops review before any agent is implemented.
- Implementation PRs (if approved) must keep `PAPER_TRADING=true` and use existing paper Alpaca clients.

**Ops lock (2026-09-11):** inventory and gap order below are authoritative. Do not add crypto edges. Do not add new options ideas until equity exits + scorecard are green.

**Learning Loop lock (2026-09-16, envelope updated 2026-09-23):** **Single focus = Spec A** RegimeEquity **validation + provisional kill criteria** ([SPEC-A-VALIDATION.md](SPEC-A-VALIDATION.md)). **5/5** healthy `[PAPER]` EODs banked (Sep 16, 17, 18, 21, 22). Spec A trade count = **0**. Provisional kill numerics are filed. Spec B implementation is **PARKED** — do **not** PROMOTE B. Spec C is **quality-filter-only, not alpha**. Day-1 scorecard (2026-09-11) is **NOT LOCKED**. Scorecard email restored **2026-09-16**; Sep 14–15 are **gap days (no backfill)**. **HOLD / DO NOT APPLY:** the `active: false` seed is a **prep artifact only** ([SPEC-A-SEED-PREP.md](SPEC-A-SEED-PREP.md)). Do **not** write it into live `agent_summary.json`. Do **not** seed into the ensemble. **NO PROMOTE. No weight-test.** No cold PROMOTE. Rotator words: FLAG / BENCHED / PROMOTED / REACTIVATED — not KEEP/DISABLE. Parents while B is parked: Technical is **A first**; OptionsFlow is **A only**. Improver cannot auto-apply. Friday learn does not retune until `get_agent_adjustment` is wired. See [ROTATION-CONTRACT.md](ROTATION-CONTRACT.md).

## Contents

| File | Ops gap | What it is |
|---|---|---|
| [SURVEY.md](SURVEY.md) | — | Confirmed roster vs `ensemble.py`, measurement loop, Ops book notes |
| [ROTATION-CONTRACT.md](ROTATION-CONTRACT.md) | — | FLAG → BENCHED → PROMOTED / REACTIVATED; exclusive A/B `AGENT_VARIANTS` parents |
| [SPEC-A-regime-equity.md](SPEC-A-regime-equity.md) | **A** | Regime-aware equity L/S **PROMOTED** when Technical / OptionsFlow / Breakout / SectorRotation is BENCHED (A first on Technical; sole new name on OptionsFlow) |
| [SPEC-A-VALIDATION.md](SPEC-A-VALIDATION.md) | **A** | **Focus:** mini → purged walk-forward → locked OOS → paper envelope (**5/5 EODs banked**) → provisional kill numerics. Seed still **DO NOT APPLY**. **NO PROMOTE** |
| [SPEC-A-SEED-PREP.md](SPEC-A-SEED-PREP.md) | **A** | **FILED — DO NOT APPLY.** Exact `RegimeEquityAgent` `{ "active": false }` row. Do **not** seed into the ensemble |
| [SPEC-B-bear-shorts.md](SPEC-B-bear-shorts.md) | **B PARKED** | Fade-rally short — **implementation frozen**; do **not** PROMOTE B |
| [SPEC-C-news-premarket-quality.md](SPEC-C-news-premarket-quality.md) | **C quality-filter-only** | Premarket / News = **quality filter, not alpha**; implementation frozen |

## Explicitly out of scope

- **D — Options:** pause new options ideas. Existing paper calls (XLE/SBUX/F via OptionsFlow + `options_executor`) stay an Ops/exits problem, not a research sleeve.
- **E — Crypto:** `CryptoAgent` is a **separate** `crypto_scheduler` sleeve. Ops wants it **OFF** for the main equity bot. No new crypto edges.
- Do **not** implement these specs in this PR.
- Do **not** lift the shorts-only-in-BEAR/HIGH_VOL hard gate.
