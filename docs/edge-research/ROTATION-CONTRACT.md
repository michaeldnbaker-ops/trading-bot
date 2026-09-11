# Rotation contract (Learning Loop)

**Applies to specs A, B, and C.** Docs-only / research HOLD. Paper Alpaca only. No crypto. No strategy implementation.

Rotator vocabulary is the **only** state language these specs use: **FLAG** → **BENCHED** → **PROMOTED** / **REACTIVATED**. Do not use KEEP / DISABLE.

---

## What the code actually does (`agent_rotator.py`)

Twice daily (`market_scheduler` 10:00 and 15:30 ET):

1. `AgentEvaluator.evaluate()` may **FLAG** an agent (20d P&amp;L negative and &gt;20% worse than ensemble avg, ≥10 trades).
2. For each FLAG, if the name is not in `PROTECTED_AGENTS` and active count &gt; `MIN_ACTIVE_AGENTS` (2): rotator writes `active: false` and logs **BENCHED**.
3. `_find_replacement` walks `AGENT_VARIANTS[benched_name]` and **PROMOTED** the first variant that is missing from summary or `active: false`.
4. After `BENCH_DAYS` (3), a benched name is **REACTIVATED** (`active: true`, `benched_at: null`).

`improver_agent.py` is **not** on the scheduler. It writes markdown recs only. It **cannot** auto-apply these specs, retire a sleeve, or skip REACTIVATED.

`StrategyLearner.get_agent_adjustment()` has **no callers**. Friday `learned_params.json` confidence deltas **do not** retune a new sleeve until Ops wires that hook. Specs must not assume Friday learn will raise/lower a new agent’s bar.

---

## Historical blinds (inventory, not a second loop)

| Blind | Code today |
|---|---|
| Ensemble ignored benches | `ensemble._load_benched_agent_names` skips `active: false` — assume this holds |
| MetaAgent weights stuck at 1.0 | Weights from ledger 20d closed P&amp;L when enough trades exist; else `DEFAULT_WEIGHTS` |
| Improver human-only | Still true. Promotion path = **rotator only** |
| Friday params unused | Still true. Do not spec learner-driven retune |

---

## Sleeve lifecycle (not always-on)

```
ship cold (active: false)
    → FLAG on a bleeder (evaluator)
    → BENCHED bleeder (rotator)
    → PROMOTED this sleeve (AGENT_VARIANTS first hit)
    → live: generate_signals + MetaAgent weight/regime
    → if this sleeve is later FLAG'd: BENCHED (3d) then REACTIVATED
```

1. **Ship cold.** Name is in `Ensemble.agents` so a promote does not need a second deploy, but `agent_summary.json` starts `active: false` so ticks skip it. Not equal-weight on day one.
2. **PROMOTED only** when rotator **BENCHED** a listed sibling. That is the only on-ramp.
3. **Regime while live:** `regime_affinity` / `regime_aversion` on every signal. MetaAgent boosts intersection with `RegimeDetector`, applies `REGIME_PENALTY` on aversion. Wrong regime → mute via weight/empty signals, not a fake rotator event.
4. **After 3 days** the **BENCHED bleeder is REACTIVATED**. Both may then run unless the bleeder is FLAG'd again. Specs must not invent a permanent-off flag; that is not in the rotator.
5. **PROTECTED** names are never BENCHED. FLAG on them logs “reducing weight instead of benching.” A sleeve that only lists a PROTECTED parent in `AGENT_VARIANTS` will **never** be PROMOTED.

---

## FLAG / BENCHED / PROMOTED / REACTIVATED

| Event | Who | Meaning for A/B/C |
|---|---|---|
| **FLAG** | `agent_evaluator` | Underperform vs ensemble 20d. Numeric FLAG math is existing code; **new-sleeve kill numbers vs SPY are TODO** until Ops daily scorecard |
| **BENCHED** | `agent_rotator` | `active: false` for 3 days. If this was the bleeder, replacement may be PROMOTED in the same cycle |
| **PROMOTED** | `agent_rotator` | Cold sleeve `active: true`. First inactive `AGENT_VARIANTS` entry |
| **REACTIVATED** | `agent_rotator` | Bench expired. Bleeder or sleeve returns to the tick |

Qualitative stay-active vs FLAG (no KEEP/DISABLE): see each spec vs **SPY** and **regime**. Numeric thresholds = **TODO (Ops daily scorecard)**.

Win rate is not a FLAG input for these specs (4% stop cap → low WR can still have expectancy). Evaluator already uses P&amp;L.

---

## Wiring every new name (when an implementation PR exists — not this HOLD PR)

| Hook | Required |
|---|---|
| `Ensemble.agents` | Present, **cold** |
| `agent_summary.json` | `{ "active": false }` at ship |
| `AGENT_VARIANTS` | **First** substitute of a **non-PROTECTED** bleeder |
| `DEFAULT_WEIGHTS` | Key so ledger P&amp;L can attribute; do not count on 1.0 forever |
| `PROTECTED_AGENTS` | Do **not** add A/B/C names |
| `regime_affinity` / `aversion` | On every signal |
| Friday learner | **Unused** for retune until `get_agent_adjustment` is wired |
| Improver | Advisory markdown only — not a promotion path |

`_find_replacement` prefers variants with `active: false`. Do not list the bleeder as first variant **of** the new sleeve (v1.4 `newly_benched` helps; still don’t resurrect Technical as A’s substitute).

Later options variants (Ops D, paused) would use this same FLAG/BENCHED/PROMOTED path. Not specified here.
