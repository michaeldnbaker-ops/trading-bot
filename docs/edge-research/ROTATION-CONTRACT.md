# Rotation contract (Learning Loop)

**Applies to specs A, B, and C.** Docs-only / research HOLD. Paper Alpaca only. No crypto. No strategy implementation.

Rotator vocabulary is the **only** state language these specs use: **FLAG** → **BENCHED** → **PROMOTED** / **REACTIVATED**. Do not use KEEP / DISABLE.

---

## What the code actually does (`agent_rotator.py`)

Twice daily (`market_scheduler` 10:00 and 15:30 ET):

1. `AgentEvaluator.evaluate()` may **FLAG** an agent (20d P&amp;L negative and &gt;20% worse than ensemble avg, ≥10 trades).
2. For each FLAG, if the name is not in `PROTECTED_AGENTS` and active count &gt; `MIN_ACTIVE_AGENTS` (2): rotator writes `active: false` and logs **BENCHED**.
3. `_find_replacement` walks `AGENT_VARIANTS[benched_name]` **in list order** and **PROMOTED** the **first** variant that is missing from summary or `active: false`. Later names in the same list are not promoted in that cycle. Two sleeves cannot both be “first.”
4. After `BENCH_DAYS` (3), a benched name is **REACTIVATED** (`active: true`, `benched_at: null`). The bleeder returns to the tick. **PROMOTED does not permanently replace the bleeder.** That 3-day window is expected rotator behavior, not a spec reject. Permanent off is not in the rotator. If the bleeder **FLAG**s again after REACTIVATED, it can be **BENCHED** again — and the next inactive ordered variant may then be **PROMOTED**.

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
4. **After 3 days** the **BENCHED bleeder is REACTIVATED**. “Replace bleeders” is **temporary** unless **FLAG** fires again. Both the bleeder and the **PROMOTED** sleeve may then run. This is expected. Specs must not invent a permanent-off flag.
5. **PROTECTED** names are never BENCHED. FLAG on them logs “reducing weight instead of benching.” A sleeve that only lists a PROTECTED parent in `AGENT_VARIANTS` will **never** be PROMOTED.

---

## FLAG / BENCHED / PROMOTED / REACTIVATED

| Event | Who | Meaning for A/B/C |
|---|---|---|
| **FLAG** | `agent_evaluator` | Underperform vs ensemble 20d. Numeric FLAG math is existing code; **new-sleeve kill numbers vs SPY are TODO** until Ops daily scorecard |
| **BENCHED** | `agent_rotator` | `active: false` for 3 days. If this was the bleeder, replacement may be PROMOTED in the same cycle |
| **PROMOTED** | `agent_rotator` | Cold sleeve `active: true`. **Only the first inactive** `AGENT_VARIANTS` entry |
| **REACTIVATED** | `agent_rotator` | Bench expired after 3 days. Bleeder returns. Expected; not a failed replacement |

Qualitative stay-active vs FLAG (no KEEP/DISABLE): see each spec vs **SPY** and **regime**. Numeric thresholds = **TODO (Ops daily scorecard)**.

Win rate is not a FLAG input for these specs (4% stop cap → low WR can still have expectancy). Evaluator already uses P&amp;L.

---

## Wiring every new name (when an implementation PR exists — not this HOLD PR)

| Hook | Required |
|---|---|
| `Ensemble.agents` | Present, **cold** |
| `agent_summary.json` | `{ "active": false }` at ship |
| `AGENT_VARIANTS` | Ordered list. **One first-slot owner per parent.** See map below |
| `DEFAULT_WEIGHTS` | Key so ledger P&amp;L can attribute; do not count on 1.0 forever |
| `PROTECTED_AGENTS` | Do **not** add A/B/C names |
| `regime_affinity` / `aversion` | On every signal |
| Friday learner | **Unused** for retune until `get_agent_adjustment` is wired |
| Improver | Advisory markdown only — not a promotion path |

`_find_replacement` prefers variants with `active: false` **in list order**. Do not list the bleeder as first variant **of** the new sleeve (v1.4 `newly_benched` helps; still don’t resurrect Technical as A’s substitute).

---

## Proposed `AGENT_VARIANTS` ownership (A and B must not collide)

This HOLD branch’s `agent_rotator.py` still has **empty** Volatility / Movers keys (`_find_replacement` → `None`). Ops **PR #3** (`cursor/learn-rotate-daily-email-4aed`) **may** (and on that branch already does) occupy them:

```
"VolatilityAgent": ["MeanReversionAgent"],
"MoversAgent":     ["MomentumAgent", "BreakoutAgent"],
```

Those names are **already-active longs / continuation**, not Spec B. If they sit **first** and are missing or `active: false`, they are **PROMOTED** instead of B. Learning Loop still requires **B-first** on those parents so the short on-ramp does not depend on MeanReversion / Momentum / Breakout remaining active. An implementation PR (not this HOLD) must write **one** ordered list per parent. A and B **must not** both claim first slot on the same parent.

**Locked proposal (unchanged single-owners):**

| Parent **BENCHED** | Ordered variants (first inactive is **PROMOTED**) | Owner |
|---|---|---|
| `TechnicalAgent` | **`RegimeEquityAgent` (A), then `ShortMeanReversionAgent` (B)**, then existing Momentum / Breakout | A first, B second. **Do not reorder.** |
| `OptionsFlowAgent` | **`RegimeEquityAgent` (A) only** as the new name, then existing News / Sentiment | **A.** B does **not** share OptionsFlow |
| `BreakoutAgent` | **`RegimeEquityAgent` (A)**, then existing Momentum / Technical | A |
| `SectorRotationAgent` | **`RegimeEquityAgent` (A)**, then existing Premarket | A |
| `PremarketAgent` | **`PremarketAgent_strict` (C)** | C (SPEC-C) |

**B on-ramps — prefer Volatility / Movers, with a PR #3 contingency:**

| Parent **BENCHED** | Prefer (empty **or** B-first) | If Ops keeps MeanReversion / Momentum / Breakout |
|---|---|---|
| `VolatilityAgent` | `["ShortMeanReversionAgent"]` only, or B **first** | **Required:** `["ShortMeanReversionAgent", "MeanReversionAgent"]` — B **ahead of** MeanReversion |
| `MoversAgent` | `["ShortMeanReversionAgent"]` only, or B **first** | **Required:** `["ShortMeanReversionAgent", "MomentumAgent", "BreakoutAgent"]` — B **ahead of** Momentum / Breakout |

**Prefer** those two parents if Ops leaves the keys empty **or** lists B first. MeanReversion is a **long** dip-above-200 sleeve; promoting it when Volatility is **BENCHED** does not fill the fade-rally **short** hole.

### Contingency if Ops will not put B first on Vol / Movers

Pick **one** (still no OptionsFlow; still Technical = A then B):

1. **Same parents, reorder (preferred contingency).** B first on Volatility / Movers, Ops names second. Rotator then **PROMOTED** B while B is cold (`active: false`), even if MeanReversion / Momentum / Breakout stay on the list.
2. **Technical second slot only.** B promotes only when Technical is **BENCHED** *and* A is already `active: true` (or A is not in the roster). Slow on-ramp; no Vol/Movers path.
3. **Do not** take OptionsFlow (A), Breakout/SectorRotation first slots (A), or PROTECTED shorts (never **BENCHED**). Intermarket → Macro (PR #3) is longs-only — not a B parent.

Listing B **second** behind MeanReversion / Momentum / Breakout is **not** a valid on-ramp: those siblings are typically already active, and if any of them is inactive/missing they win the first slot instead of B.

Why this split:

- Rotator **cannot** promote two cold sleeves from one **BENCHED** parent in the same cycle.
- OptionsFlow is the long-regime / proxy-flow bleeder → **A** replaces that equity decision. B is a fade-rally short; it does **not** list OptionsFlow.
- Technical is L/S with no regime tags → **A first**. If A is already `active: true` when Technical is **BENCHED** again, the first inactive is **B**.
- Volatility and Movers already short without SMA200 / as same-day continuation → **B’s preferred** on-ramps that do not fight A, **if** B is first (or the key is still empty).

If A is not in the roster at all, B may sit first on Technical. If both ship, Technical order above is required.

**REACTIVATED (expected, not a reject):** `BENCH_DAYS = 3` then the bleeder is **REACTIVATED**. A/B/C staying live beside a returned bleeder is correct. “Replace bleeders” lasts those 3 days unless the bleeder **FLAG**s again.

Later options variants (Ops D, paused) would use this same FLAG/BENCHED/PROMOTED path. Not specified here.
