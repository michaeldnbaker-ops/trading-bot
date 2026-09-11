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

**Claim:** a third short *style* (fade a bounce **below** the 200-day) that **rotates in** when a non-PROTECTED short-capable bleeder is benched — not a third always-on PROTECTED clone. Silent in bull. Improves bear-tape P&amp;L vs SPY without recreating always-on shorts.

If SPEC-A is also in the roster, A is long-only while live; **this name owns fade-rally shorts** so kill/KEEP is isolated.

Shared lifecycle: [ROTATION-CONTRACT.md](ROTATION-CONTRACT.md). Assume learn/rotate/weight work after Ops PR `bc-652b78ab`.

Paper “put” language: if Ops later lifts D, the **same** entry may rotate in as a paper-put **variant** under this contract. Until then, **equity short only**.

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

## MetaAgent / rotator — activate and deactivate

**Default: cold.** In `Ensemble.agents` + `DEFAULT_WEIGHTS` key, `active: false`. No day-one shorts beside the two PROTECTED continuation agents. Unproven live weight = `MIN_AGENT_WEIGHT` until 10 closed trades.

PROTECTED shorts (**BearishPatternAgent**, **ShortMomentumAgent**) are **never** benched to make room for B. B rotates in when a **non-PROTECTED** short-capable bleeder is benched:

| Failing sleeve (benched) | `AGENT_VARIANTS` first substitute |
|---|---|
| OptionsFlowAgent | **ShortMeanReversionAgent** (then RegimeEquityAgent if A is long-only) |
| TechnicalAgent | **ShortMeanReversionAgent** for the short side of that hole; A still first for longs |
| VolatilityAgent | **ShortMeanReversionAgent** |
| MoversAgent | **ShortMeanReversionAgent** (loser-continuation vs fade-rally) |

Do not list B as a variant *of* the PROTECTED pair (rotator cannot bench them; listing B there never promotes). After B is live, PROTECTED shorts stay on; MetaAgent **downweights** them if 20d P&amp;L ≤ 0 (`MIN_AGENT_WEIGHT`) instead of disabling the short book.

**Regime while live:**

| Detector | MetaAgent | Agent |
|---|---|---|
| BEAR_TREND or HIGH_VOL | Affinity boost | May emit shorts |
| BULL_TREND (no HIGH_VOL) | Aversion penalty | `generate_signals` returns `[]` |
| `active: false` | — | Skip |

**Deactivate:** BENCH on evaluator flag; DISABLE on kill table; weight mute at `MIN_AGENT_WEIGHT` if 20d P&amp;L ≤ 0 after 10 trades. Friday learner: conf delta only — never `active: true` in a bull tape, never lift `block_shorts`.

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
- Not PROTECTED in v1.
- `DEFAULT_WEIGHTS` **key** only; live weight starts at `MIN_AGENT_WEIGHT`.
- `AGENT_VARIANTS` as in the activate table. Do **not** put Technical first on B’s own variant list.
- Daily cap / BP / gross / dedup unchanged.
- **Do not** emit `direction: long`.
- **Do not** implement new put spreads while Ops D is paused.

---

## KEEP / BENCH / DISABLE

Clock: first paper short fill after promotion. Score **only BEAR/HIGH_VOL sessions** vs SPY. Do not require beating SPY CAGR over a bull year.

| Verdict | Vs SPY | Vs existing agents | Action |
|---|---|---|---|
| **KEEP** | Sleeve $ > 0 while SPY 20d ≤ 0 (or SPY down ≥ 3% on the window) | Overlap with ShortMomentum+BearishPattern &lt; 50%; 20d $ **not worse** than those two **in the same bear window**; ≥90% of trades have `px < sma200` | Stay active |
| **BENCH** | Evaluator 20d flag, ≥10 trades | Inconclusive vs PROTECTED shorts | 3-day rest |
| **DISABLE** | Any short fill outside BEAR/HIGH_VOL; **or** sleeve $ &lt; 0 while SPY is down ≥ 3% (failed hedge) | PF &lt; 1.0 and **worse** than ShortMomentum+BearishPattern in the same window after ≥10 trades; **or** overlap ≥ 70% (clone); **or** new options tickets while D is paused | `benched_at=2099-01-01` |

Zero fills in a bull tape = **not** DISABLE. That is the gate working.

---

## How to measure vs SPY and vs existing agents

**Vs SPY:** only score sessions tagged BEAR/HIGH_VOL. Sleeve contribution should be **positive** when SPY 20d is negative or flat. `report_data` full-period edge is context, not the pass/fail for this sleeve.

**Vs existing agents:**

| Sibling | Pass | Fail |
|---|---|---|
| ShortMomentum / BearishPattern | Fade vs continuation; overlap &lt; 50% | Same names, same day, RSI &lt; 40 breakdowns |
| VolatilityAgent shorts | ≥90% of trades have `px < sma200` | Shorting overbought names still above the 200-day |
| Technical shorts | Technical should be benched under SPEC-A; if not, this agent must not be 5m RSI clones | Same 5m RSI shorts |
| OptionsFlow / paper puts | No new options tickets from this `name` while D is paused | XLE/SBUX/F-style calls/puts attributed here |

**Replay:** `short_research.py` `short_rally_downtrend` with production stop + **bear gate on**. Survivorship bias runs against shorts — a small positive is stronger evidence than the same number on longs.

---

## Paper-only constraints

- Equity shorts on paper Alpaca. No live. No crypto.
- No new options until Ops D is lifted. A later put **variant** of this sleeve ships **cold** and rotates in under the same contract — not a silent always-on add.
- DISABLE does not auto-clear after `BENCH_DAYS`.
- Do not change `SOLO_SHORT_CONFIDENCE` except via auto_tune as today.

---

## Out of scope

- Lifting `block_shorts`
- Crypto
- Always-on third short at weight 1.0
- Replacing PROTECTED shorts (they stay; MetaAgent may downweight)
- Inverse ETF overlay as the product
