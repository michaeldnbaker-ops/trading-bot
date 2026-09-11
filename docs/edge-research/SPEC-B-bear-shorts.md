# SPEC-B — BEAR/HIGH_VOL short (equity downside)

**Ops gap B (after A).** Paper Alpaca only. **Do not implement in this PR.**  
**Not a new options product.** Ops D pauses new options ideas until equity exits + scorecard are green. “Puts” here means **downside expression**; v1 is **equity short**. Existing `options_executor` may still wrap a high-confidence short as a paper put — do not design a new put structure in this spec.

**No crypto.**

---

## Hypothesis

Shorts are already **hard-gated** to `BEAR_TREND` or `HIGH_VOL` (`ensemble.py`, measured 2026-08-14: shorts always-on CAGR 1.2% vs bear-only 9.0%). Do **not** lift that gate.

The dedicated short book is two **PROTECTED continuation** agents:

- `ShortMomentumAgent` — negative ROC, distribution, RS vs SPY
- `BearishPatternAgent` — patterns, death cross, breakdowns

Same affinity, same aversion (`BULL_TREND`), same rotator variants of each other. MetaAgent’s 2-agent short consensus is then a two-name clique. `IntermarketAgent` is longs-only on purpose. `VolatilityAgent` shorts overbought names **without** SMA200. `MoversAgent` shorts same-day losers (continuation again).

`short_research.py` already tested the **inverse of the best long rule** (`short_rally_downtrend`: `Close < SMA200` and RSI &gt; 60) and the short specialists’ docstrings quote that table. **No agent fires it.**

**Claim:** a fade-rally-below-200 short that is **PROMOTED** when **VolatilityAgent** or **MoversAgent** is **BENCHED**, **if** those lists are empty or B-first. Ops PR #3 may fill them with MeanReversion / Momentum / Breakout — then B must be listed **FIRST** on those parents (ahead of those names) or fall back to Technical’s second slot. Second on **TechnicalAgent** after A. **Does not** list OptionsFlowAgent — A owns that parent as the long-regime replacement. Not a third always-on PROTECTED clone. Silent in bull. Improver cannot apply this. Friday learn does not retune it.

If SPEC-A is also in the roster, A is long-only while live; **this name owns fade-rally shorts**.

Shared: [ROTATION-CONTRACT.md](ROTATION-CONTRACT.md).

Paper “put” language: if Ops later lifts D, a put **variant** would use the same FLAG/BENCHED/PROMOTED path. Until then, **equity short only**.

---

## Entry

**Name:** `ShortMeanReversionAgent`

**Direction:** `short` only. `instrument_type: equity`, `strategy: short_mean_reversion`.

**All required (daily bars; skip if `< 210` sessions):**

1. Current ensemble regimes include `BEAR_TREND` or `HIGH_VOL`. Else return `[]`.
2. `Close < SMA200`
3. RSI(14) &gt; 60 (stronger: RSI &gt; 70)
4. Price ≥ $5, 20d volume ≥ 500k
5. Not a fresh 20-day **breakdown** with RSI &lt; 40 (that is ShortMomentum / BearishPattern — do not clone)
6. Not on avoid-list; no open position in the symbol

**Confidence:** 0.66 (RSI&gt;60) / 0.70 (bounce above SMA20 still under SMA200) / 0.74 (RSI&gt;70). Cap 0.86.

**Universe:** MeanReversion list plus ShortMomentum’s high-beta names (TSLA, ARKK, COIN, …). No crypto. No dynamic IPO screen for v1.

**Max 3 signals/tick when live.**

---

## MetaAgent / rotator — FLAG / BENCHED / PROMOTED / REACTIVATED

**Default: cold.** `active: false`. No day-one shorts beside the two PROTECTED continuation agents.

PROTECTED shorts (**BearishPatternAgent**, **ShortMomentumAgent**) are **never BENCHED**. FLAG on them → rotator log “reducing weight instead of benching.” Listing B only as their variant would **never PROMOTE** B.

**PROMOTED in** when rotator **BENCHED** a listed parent. `_find_replacement` takes the **first inactive** variant only — B must not share a first slot with A.

**Prefer Volatility / Movers** if Ops leaves those keys empty **or** lists B first.

| Bleeder **BENCHED** | Ordered `AGENT_VARIANTS` | Role |
|---|---|---|
| VolatilityAgent | **Prefer:** `[ShortMeanReversionAgent]` (empty key or B-only). **If Ops PR #3 keeps MeanReversion:** `[ShortMeanReversionAgent, MeanReversionAgent]` — **B first** | Preferred B on-ramp |
| MoversAgent | **Prefer:** `[ShortMeanReversionAgent]`. **If Ops PR #3 keeps Momentum / Breakout:** `[ShortMeanReversionAgent, MomentumAgent, BreakoutAgent]` — **B first** | Preferred B on-ramp |
| TechnicalAgent | RegimeEquityAgent **(A) first**, **then ShortMeanReversionAgent (B)**, then Momentum / Breakout | Unchanged. B if A is already active or A is not in the roster |
| OptionsFlowAgent | **Not B.** A owns this parent | Do **not** put B on this list |

Do **not** list PROTECTED shorts (BearishPattern / ShortMomentum) as B’s promote parents — they are never **BENCHED**.

**Contingency (Ops PR #3 occupancy):** that branch already has `VolatilityAgent → MeanReversionAgent` and `MoversAgent → MomentumAgent, BreakoutAgent`. MeanReversion is a **long** dip sleeve; Momentum / Breakout are continuation longs. If they remain **first** and are missing or inactive, they are **PROMOTED** instead of B. Learning Loop requires **B-first** so the short on-ramp does not depend on those longs staying active.

Options if Ops keeps those names on the lists:

1. **B-first (required if these stay B’s parents).** Same two keys; B ahead of MeanReversion / Momentum / Breakout. This is the intended contingency.
2. **Drop Vol/Movers as B parents** and use **Technical second slot only** (A then B). B then waits until Technical is **BENCHED** and A is already live.
3. **Not allowed:** OptionsFlow (A only); Breakout / SectorRotation first slots (A); PROTECTED shorts; Intermarket → Macro.

B **second** behind MeanReversion / Momentum / Breakout is **not** sufficient.

After **REACTIVATED** of that bleeder (3d), B may still be active beside the returned parent. That is **expected**, not a reject. “Replace bleeders” lasts 3 days unless **FLAG** fires again. PROTECTED shorts stay on; MetaAgent may downweight them if 20d P&amp;L is bad.

**Regime while live:**

| Detector | MetaAgent | Agent |
|---|---|---|
| BEAR_TREND or HIGH_VOL | Affinity boost | May emit shorts |
| BULL_TREND (no HIGH_VOL) | Aversion penalty | `generate_signals` returns `[]` |
| BENCHED | — | Skip |

If B is FLAG'd → **BENCHED** 3d → **REACTIVATED**. No Improver off-switch. Friday learn does not retune B.

---

## Exit

Same as other equity shorts: ATR×1.5 stop **capped 4%** above entry, broker trail, halt de-risks losers. No target-as-real-exit.

Do **not** add options-manager rules here.

---

## Risk gates

- Paper only.
- Agent-level silence unless BEAR/HIGH_VOL — belt and suspenders on top of ensemble `block_shorts`.
- `regime_affinity = ["BEAR_TREND", "HIGH_VOL"]`
- `regime_aversion = ["BULL_TREND"]`
- `MIN_CONFIDENCE = 0.55`
- Not PROTECTED.
- `DEFAULT_WEIGHTS` key; `AGENT_VARIANTS` as in the PROMOTED table. **Do not** put B on OptionsFlow. **Do not** put B first on Technical if A ships. **Do not** put Technical first on B’s own variant list. If Ops fills Volatility / Movers, **B must be first** on those lists (ahead of MeanReversion / Momentum / Breakout) or B uses Technical-second only.
- **Do not** emit `direction: long`.
- **Do not** implement new put spreads while Ops D is paused.

---

## FLAG vs remain-active (qualitative; numbers TODO)

Clock: first paper short fill after **PROMOTED**. Score **BEAR/HIGH_VOL sessions** vs SPY. Do not require beating SPY CAGR over a bull year. **Numeric thresholds = TODO pending Ops daily scorecard.**

**Remain active when, qualitatively:**

- Vs **SPY:** in BEAR/HIGH_VOL, sleeve $ should not be another long-book dump; shorts should hedge a down tape, not fight an up tape (and they should have **no fills** in pure BULL).
- Vs **agents:** fade vs continuation (not the same names/days as ShortMomentum/BearishPattern breakdowns); names below SMA200, not Volatility overbought-above-200.

**FLAG (then rotator BENCHED → REACTIVATED) when, qualitatively:**

- Vs **regime:** any short fill outside BEAR/HIGH_VOL.
- Vs **SPY:** shorts lose while SPY is already down (failed hedge), or they are the only activity in a roaring bull (gate leak).
- Vs **agents:** clone of PROTECTED shorts, or new options tickets while D is paused.

Zero fills in a bull tape = **not** a FLAG. That is the gate working.

---

## How to measure vs SPY and vs existing agents

**Vs SPY:** only score sessions tagged BEAR/HIGH_VOL. Sleeve contribution should be **positive** when SPY 20d is negative or flat. `report_data` full-period edge is context, not the pass/fail for this sleeve.

**Vs existing agents:**

| Sibling | Pass | Fail |
|---|---|---|
| ShortMomentum / BearishPattern | Fade vs continuation; overlap &lt; 50% | Same names, same day, RSI &lt; 40 breakdowns |
| VolatilityAgent shorts | ≥90% of trades have `px < sma200` | Shorting overbought names still above the 200-day |
| Technical shorts | Technical **REACTIVATED** after 3d (expected); B must not be 5m RSI clones while both run | Same 5m RSI shorts |
| OptionsFlow / paper puts | No new options tickets from this `name` while D is paused | XLE/SBUX/F-style calls/puts attributed here |

**Replay:** `short_research.py` `short_rally_downtrend` with production stop + **bear gate on**. Survivorship bias runs against shorts — a small positive is stronger evidence than the same number on longs.

---

## Paper-only constraints

- Equity shorts on paper Alpaca. No live. No crypto. **No strategy code in this PR.**
- No new options until Ops D is lifted.
- Do not assume Friday learner retunes B.
- Do not list OptionsFlowAgent as a B parent.
- Prefer Volatility / Movers as B parents only if empty or B-first; do not sit second behind MeanReversion / Momentum / Breakout.
- Do not change `SOLO_SHORT_CONFIDENCE` except via existing auto_tune.

---

## Out of scope

- Lifting `block_shorts`
- Crypto
- Always-on third short
- Replacing PROTECTED shorts
- Improver auto-apply
- Inverse ETF overlay as the product
