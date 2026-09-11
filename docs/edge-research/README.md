# Edge research (paper trading only)

This folder holds **research and specs**, not live trading code.

- **Paper Alpaca only.** No live brokerage changes. No new live/trading code paths in this PR.
- Specs here are for Learning Loop / Ops review before any agent is implemented.
- Implementation PRs (if approved) must keep `PAPER_TRADING=true` and use existing paper Alpaca clients.

**Ops lock (2026-09-11):** inventory and gap order below are authoritative. Do not add crypto edges. Do not add new options ideas until equity exits + scorecard are green.

**Learning Loop lock:** A/B/C are **cold rotatable sleeves** (and in-place filters), not always-on equal-weight agents. See [ROTATION-CONTRACT.md](ROTATION-CONTRACT.md). Assume learn/rotate/weight work after Ops PR `bc-652b78ab`.

## Contents

| File | Ops gap | What it is |
|---|---|---|
| [SURVEY.md](SURVEY.md) | — | Confirmed roster vs `ensemble.py`, measurement loop, Ops book notes |
| [ROTATION-CONTRACT.md](ROTATION-CONTRACT.md) | — | Cold → promote on bench → KEEP/BENCH/DISABLE; MetaAgent + rotator hooks |
| [SPEC-A-regime-equity.md](SPEC-A-regime-equity.md) | **A** | Regime-aware equity L/S **rotated in** when bleeders are benched |
| [SPEC-B-bear-shorts.md](SPEC-B-bear-shorts.md) | **B** | Fade-rally short, **BEAR/HIGH_VOL only**, rotated in — not a third always-on short |
| [SPEC-C-news-premarket-quality.md](SPEC-C-news-premarket-quality.md) | **C** | Premarket strict **variant** + News in-place filter (News is PROTECTED) |

## Explicitly out of scope

- **D — Options:** pause new options ideas. Existing paper calls (XLE/SBUX/F via OptionsFlow + `options_executor`) stay an Ops/exits problem, not a research sleeve.
- **E — Crypto:** `CryptoAgent` is a **separate** `crypto_scheduler` sleeve. Ops wants it **OFF** for the main equity bot. No new crypto edges.
- Do **not** implement these specs in this PR.
- Do **not** lift the shorts-only-in-BEAR/HIGH_VOL hard gate.
