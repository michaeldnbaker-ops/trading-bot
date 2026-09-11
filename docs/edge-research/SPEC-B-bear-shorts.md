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

**Claim:** a third short *style* (fade a bounce **below** the 200-day), silent in bull, improves bear-tape P&amp;L vs SPY without recreating always-on shorts. If SPEC-A already includes this rule inside `RegimeEquityAgent`, this spec is the **standalone / kill-isolated** short sleeve so Ops can keep A’s longs if A’s shorts fail (or vice versa).

Paper “put” language: if Ops later lifts D, the **same** entry may route through the existing 0.70 paper put path. Until then, **equity short only** so scorecard/exits stay on shares.

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

**Max 3 signals/tick.**

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
- Not PROTECTED in v1 (PROTECTED shorts already exist; this one earns it).
- `AGENT_VARIANTS`: `["BearishPatternAgent", "ShortMomentumAgent"]` both directions.
- Add to `DEFAULT_WEIGHTS`.
- Daily cap / BP / gross / dedup unchanged.
- **Do not** emit `direction: long`.
- **Do not** implement new put spreads, naked puts, or inverse-ETF-as-alpha (exposure.py already maps inverse ETFs; not this edge).

If SPEC-A is also live: **one** of (A’s short rule, this agent) should own fade-rally shorts so they do not double-fire. Prefer this dedicated name for attribution/kill, and keep A long-only if both ship.

---

## Kill criteria

Start clock on first paper short fill.

**Kill if:**

1. Any short fill while regimes were **not** BEAR/HIGH_VOL.
2. 10+ closed trades in BEAR/HIGH_VOL and PF &lt; 1.0 **and** worse than ShortMomentum+BearishPattern in the **same** window (no diversification, just another loser).
3. Symbol-day overlap with those two PROTECTED shorts ≥ 70% — clone; kill.
4. Sleeve loses money in a window where SPY is **down** ≥ 3% (failed hedge). Losing in a bull tape with **zero fills** is a pass on this criterion.

**Keep if:** BEAR/HIGH_VOL attributed $ &gt; 0 while SPY 20d ≤ 0, overlap &lt; 50%, and no bull-tape fills.

Do not require beating SPY CAGR over a bull year. That requirement is how always-on shorts got into the book.

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
- No new options until Ops D is lifted. If D lifts later, reuse this **entry** and existing put router; new spec, not a silent add.
- Do not change `SOLO_SHORT_CONFIDENCE` / bear solo easing except as auto_tune already allows.

---

## Out of scope

- Lifting `block_shorts`
- Crypto
- Inverse ETF overlay as the product
- Replacing PROTECTED shorts (they stay; this diversifies them)
