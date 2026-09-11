# Rotation contract (Learning Loop)

**Applies to specs A, B, and C.** Paper Alpaca only. No crypto. Later options variants (Ops D, paused) must use this same contract — they are **not** specified here.

Assume **learn / rotate / weight work** after Ops PR `bc-652b78ab`. Do not design A/B/C around the historical blinds. Do not add another always-on equal-weight voice.

---

## Historical blinds (why this contract exists)

| Blind | What happened | Spec assumption **after** Ops PR |
|---|---|---|
| Ensemble ignored benches | Rotator wrote `logs/agent_summary.json` `active: false`; ticks still called every agent | Ensemble **skips** `active: false` every tick (`_load_benched_agent_names`) |
| MetaAgent weights stuck at 1.0 | Weights read an empty PerformanceLogger → `DEFAULT_WEIGHTS` | Weights from **ledger 20d closed P&L**; new sleeves **not** born at 1.0 |
| Improver human-only | Markdown recs, no state change | Improver/rotator can **DISABLE** (not only 3-day bench) when kill criteria fire |
| Friday `learned_params.json` not loaded | `get_agent_adjustment()` had no callers; avoid-list was rebuilt from ledger | Ensemble **consumes** per-agent conf deltas and this spec’s KEEP/DISABLE flags. Avoid-list still ledger-capped (max 8). Learner must **not** move the short hard-gate or net-long cap |

---

## Sleeve lifecycle (not always-on)

```
cold (active: false)  →  rotator PROMOTES when a failing
                         sibling is BENCHED
                      →  live (generate_signals runs)
                      →  MetaAgent weights from ledger
                         + regime boost/penalty
                      →  KEEP | BENCH (3d rest) | DISABLE (until Ops)
```

1. **Ship cold.** New `name` is instantiated in `Ensemble.agents` so promotion does not require a code deploy, but `agent_summary.json` starts `active: false`. It must **not** emit on day one beside Technical/OptionsFlow at weight 1.0.
2. **Rotate in** only when the rotator benches a listed sibling (`AGENT_VARIANTS`). That is the “failing sleeve” trigger.
3. **Regime mute** even while live: `regime_affinity` / `regime_aversion` on every signal. MetaAgent boosts intersection with `RegimeDetector`, multiplies weight by `(1 - REGIME_PENALTY)` on aversion. Wrong-regime → no fills, not “equal say.”
4. **Performance mute:** after `MIN_TRADES_TO_EVALUATE` (10) closed attributed trades, evaluator flags → rotator BENCH or Improver DISABLE per that spec’s table. Unproven sleeves stay at `MIN_AGENT_WEIGHT` (not 1.0) until 10 closed trades.
5. **DISABLE ≠ 3-day bench.** Today `BENCH_DAYS = 3` then auto-REACTIVATE. Kill/DISABLE must set a non-expiring bench (Improver already documents `benched_at: "2099-01-01T00:00:00+00:00"`) so the sleeve cannot sneak back as equal-weight.

---

## KEEP / BENCH / DISABLE (shared language)

Use **broker** for money, **ledger** for attribution (`report_data.py`). Windows match evaluator: 5d / 20d.

| Verdict | Who writes state | Meaning |
|---|---|---|
| **KEEP** | none (stay `active: true`) | Sleeve beat SPY **and** beat the replaced sibling on the spec’s clock |
| **BENCH** | `agent_rotator` (3 days) | Underperform flag, rest, then eligible to rotate in again |
| **DISABLE** | Improver / explicit summary flag | Kill criteria hit. Not a candidate in `_find_replacement`. Ops must re-enable |

Win rate is **not** a KEEP/DISABLE input (tight 4% stops → low WR can still have expectancy).

---

## MetaAgent / rotator wiring (all new names)

| Hook | Required |
|---|---|
| `Ensemble.agents` | Present but **cold** |
| `agent_summary.json` | `{ "active": false }` at ship |
| `AGENT_VARIANTS` | Listed as **replacement** of a bleeder, not as a peer that is always preferred |
| `DEFAULT_WEIGHTS` | Key exists so ledger P&L is counted; **initial live weight = MIN_AGENT_WEIGHT** until 10 closed trades |
| `PROTECTED_AGENTS` | **Do not** add A/B/C names |
| `regime_affinity` / `regime_aversion` | Required on every signal |
| Friday learner | May nudge `confidence_threshold_delta` only; **must not** enable the sleeve, lift `block_shorts`, or raise daily cap |
| Options variants (later) | Same cold → promote → KEEP/DISABLE. Not in A/B/C |

`_find_replacement` already prefers variants with `active: false`. That is the promotion path. Do **not** list bleeders as variants *of* the new sleeve in a way that a later bench of A re-promotes Technical (rotator v1.4 newly_benched guard helps; still do not put Technical first on A’s variant list).
