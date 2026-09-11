# Edge research (paper trading only)

This folder holds **research and specs**, not live trading code.

- **Paper Alpaca only.** No live brokerage changes. No new live/trading code paths in this PR.
- Specs here are for Learning Loop / Ops review before any agent is implemented.
- Implementation PRs (if approved) must keep `PAPER_TRADING=true`, use existing paper Alpaca clients, and register through the same ensemble + ledger hooks as current agents.

## Contents

| File | What it is |
|---|---|
| [SURVEY.md](SURVEY.md) | Inventory of every current agent/strategy, how P&L is measured vs SPY/ledger, and ranked coverage gaps |
| [SPEC-01-short-mean-reversion.md](SPEC-01-short-mean-reversion.md) | Gap #1 — sell strength below the 200-day (BEAR / HIGH_VOL only) |
| [SPEC-02-intermediate-momentum.md](SPEC-02-intermediate-momentum.md) | Gap #2 — 12-1 cross-sectional momentum (long, slower than existing ROC agents) |
| [SPEC-03-earnings-iv-crush.md](SPEC-03-earnings-iv-crush.md) | Gap #3 — post-earnings IV crush as a defined-risk **paper options** edge |

## Non-goals of this PR

- Do **not** implement the three proposed agents.
- Do **not** change `ensemble.py` execution, `order_executor.py`, or Alpaca live vs paper flags.
- Do **not** un-gate shorts in a bull tape (existing measured rule stands).
