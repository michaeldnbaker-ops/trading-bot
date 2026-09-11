# SPEC-01 — ShortMeanReversionAgent (paper only)

**Status:** spec for Learning Loop / Ops review. **Do not implement in this PR.**  
**Priority:** #1 of 3 (highest leverage vs the current roster).  
**Paper trading only.** No live brokerage, no change to Alpaca live/paper flags.

---

## Hypothesis

The ensemble’s best *measured* long rule is buy-the-dip **inside an uptrend** (price above SMA200). That is `MeanReversionAgent`, justified by `signal_research.py` (`bb_reversion_long` / `rsi_oversold_long` / `trend_pullback_long`: PF ~1.67–1.68, profitable 19/22 years).

The inverse — **sell strength inside a downtrend** (price below SMA200, RSI extended up) — is already encoded as `short_rally_downtrend` in `short_research.py` and cited in the ShortMomentum / BearishPattern docstrings. **No agent fires that rule.**

Existing dedicated shorts are continuation:

- `ShortMomentumAgent` — negative ROC, distribution, RS weakness
- `BearishPatternAgent` — breakdowns, death cross, H&S

Those two are correlated, both PROTECTED, both affinity BEAR/HIGH_VOL. `VolatilityAgent` can short RSI/BB extremes **without** the SMA200 filter that MeanReversionAgent calls non-negotiable (dropping it “turns the same rules into catching downtrends” on the long side; on the short side it is shorting strength in a bull — exactly what `ensemble.py` now hard-blocks).

**Claim:** a fade-the-rally-below-200 agent, allowed only when the ensemble already permits shorts (`BEAR_TREND` or `HIGH_VOL`), will:

1. Diversify the short book (mean-reversion vs continuation).
2. Give MetaAgent a third short *style* so 2-agent short consensus is not structurally a two-name clique.
3. Stay inside the 2026-08-14 finding: shorts-always-on CAGR 1.2% vs shorts-in-bear CAGR 9.0%.

Survivorship bias in `short_research.py` runs **against** shorts (failures are missing). A break-even or modestly positive paper result here is stronger evidence than the same number on the long side.

---

## Entry

**Name:** `ShortMeanReversionAgent`  
**Direction:** `short` only.  
**Instrument (v1):** equity (`instrument_type: equity`, `strategy: short_mean_reversion`). Do **not** label as options; do not consume the 0.70 options router. Short options in a crash are a different product and a later spec.

**Setup (must all be true on yesterday’s daily bar; fill conceptually next tick / next open):**

1. `Close < SMA200` — downtrend filter (literal inverse of MeanReversionAgent’s `px > sma200`).
2. One of:
   - `RSI(14) > 60` (`short_rally_downtrend`), or
   - `RSI(14) > 70` (`short_rsi_overbought`, higher conviction), or
   - `Close > SMA20` while still `< SMA200` (bounce into resistance).
3. Price ≥ $5, 20-day average volume ≥ 500k (same floors as MeanReversion / Movers).
4. Not on the ensemble avoid-list.

**Confidence sketch (keep in MeanReversion’s band so MetaAgent can merge):**

| Setup | Base conf |
|---|---|
| RSI &gt; 70 and below 200-day | 0.74 |
| RSI &gt; 60 and Close &gt; SMA20 | 0.70 |
| RSI &gt; 60 only | 0.66 |

Cap ~0.86. Deeper bounce toward SMA200 = slightly higher conf.

**Do not enter:**

- `BULL_TREND` without `HIGH_VOL` (ensemble will reject anyway; agent should stay silent to avoid burning MetaAgent slots).
- Names MeanReversionAgent would buy (above SMA200).
- Same-tick continuation breakdowns that ShortMomentum / BearishPattern already own (`Close` making new 20-day lows with RSI &lt; 40). Those are siblings, not this edge.

---

## Exit

Reuse production geometry — do not invent a new stop family:

- Ensemble `_normalize_geometry`: ATR(14)×`atr_stop_mult` (default 1.5), **capped at 4%** above entry for shorts.
- Broker-side trailing stop via `order_executor` (same as other equity shorts).
- `widen_trails_on_survivors` after 2+ days.
- RiskAgent halt still de-risks **losing** shorts.

No profit target as the real exit. `target_price` is the 4×-stop bookkeeping marker ensemble already writes.

---

## Risk gates

**Agent-level**

- Short only; never emit `direction: long`.
- `regime_affinity = ["BEAR_TREND", "HIGH_VOL"]`
- `regime_aversion = ["BULL_TREND"]` (same contract as the two PROTECTED shorts)
- `MIN_CONFIDENCE = 0.55`
- Max 3 signals per tick (mirror MeanReversionAgent)
- Skip if RSI is news-crash extreme on the *down* side (this agent fades *up* bounces; a fresh waterfall is ShortMomentum’s job)

**Ensemble-level (already exist; do not relax)**

- Shorts blocked unless `BEAR_TREND` or `HIGH_VOL`
- Daily cap 3, buying-power $20k reserve, gross 2.0×, net-long 100% (shorts exempt from net-long block)
- MetaAgent: shorts need 2 agents unless raw conf ≥ solo short bar (0.72, or eased in bear)
- Bridge: stop above entry for shorts, 0.5% risk / 2% notional
- Dedup: no second position in the same symbol
- `PAPER_TRADING=true`

**Consensus design (v1):** this agent should be able to *co-sign* a BearishPattern / ShortMomentum / Movers short on the same name when the bounce fails, and should also be allowed to fire solo in bear at the eased solo bar. Do **not** add it to `REQUIRE_CORROBORATION`.

**Do not** add to `PROTECTED_AGENTS` in v1. Earn protection the way ShortMomentum did — after a bear tape, not before. Rotator variants: `["BearishPatternAgent", "ShortMomentumAgent"]`.

---

## Universe

Start from MeanReversionAgent’s watchlist (liquid mega-cap + sector ETFs + cyclicals) **plus** the short specialists’ high-beta names (TSLA, ARKK, COIN, high-short-interest names already on ShortMomentum’s list).

Do **not** use MoversAgent’s unrestricted gainer/loser screen for v1 (continuation shorts on new listings are a different failure mode; this spec is SMA200-relative).

Dynamic-universe injection from `Ensemble._dynamic_universe` may add names; the SMA200 filter still applies, so IPO debris without 200 days of history is skipped (`len(df) < 210`).

---

## Paper-only constraints

- Implement (when approved) as a new `short_mean_reversion_agent.py` + registration only. No live client.
- Do not change `block_shorts` logic in `ensemble.py`.
- Do not route v1 to `execute_options_trade`.
- Paper ledger must record `primary_agent=ShortMeanReversionAgent` (or contributor if merged) so evaluator/rotator/weights work. Add the name to `MetaAgent.DEFAULT_WEIGHTS`.
- If `PAPER_TRADING` is ever false in env, this agent must still no-op unless Ops explicitly flips a dedicated flag (default off for live).

---

## How to measure success

**Minimum paper window:** 20 closed trades **or** one full BEAR/HIGH_VOL episode, whichever is later. Do not rotate this agent on a 5-day window (`MIN_TRADES_TO_EVALUATE` is already 10; treat 20 as the research bar).

### Vs SPY

- In **BEAR_TREND / HIGH_VOL** sessions: agent sleeve contribution (sum of realized+unrealized on its attributed trades) should be **positive** while SPY 20d is negative or flat. That is the hedge job.
- In **BULL_TREND**: this agent should have **~zero trades** (gate working). Any bull-tape shorts are a spec fail, not a P&amp;L fail.
- Do **not** require the sleeve to beat SPY CAGR in a bull year. Requiring that would recreate shorts-always-on.

Use `report_data` 20d bot-vs-SPY as context, plus a ledger slice `all_agents` contains `ShortMeanReversionAgent`.

### Vs existing agents

Compare the same BEAR/HIGH_VOL window:

| Sibling | Must look different |
|---|---|
| ShortMomentumAgent / BearishPatternAgent | Lower overlap: share of this agent’s symbols that those two also signalled **the same day** &lt; 50%. If overlap ≥ 70%, it is a clone — kill it. |
| VolatilityAgent shorts | This agent’s names are below SMA200; Volatility shorts may be overbought above the 200-day. Track % of trades with `px < sma200`. Target ≥ 90%. |
| MeanReversionAgent | Orthogonal by construction (above vs below 200-day). Zero same-symbol opposite-side the same day after dedup. |

Numeric bars (paper, epoch-filtered):

- Profit factor ≥ 1.15 **in BEAR/HIGH_VOL attributed trades**, or CI-low on $/trade ≥ 0 (same language as `short_research.py` “regime hedge”).
- Win rate is **not** the goal (ensemble already learned 20% WR + tight stops can still work). Use expectancy and PF.
- Must not drag 20d ensemble avg below the no-agent baseline by more than the rotator’s 20% underperform flag **while SPY is down**. If SPY is up and this agent lost money, that is the bull-tape leak — inspect gates first.

### Replay before paper (recommended, still not this PR)

Extend `short_research.py` (or a thin wrapper) so `short_rally_downtrend` is run through **production** ATR-cap 4% / trail / 0.5% risk, gated to SPY-bear/high-vol only. Ship the table in the implementation PR. Survivorship bias remains; say so.

---

## Registration checklist (implementation PR only)

1. `short_mean_reversion_agent.py` with `name = "ShortMeanReversionAgent"`
2. `Ensemble.agents` append
3. `DEFAULT_WEIGHTS["ShortMeanReversionAgent"] = 1.0`
4. `AGENT_VARIANTS` entries both directions with the two existing short specialists
5. Ledger + evaluator pick it up automatically via `t.all_agents`
6. Tests: silent in synthetic BULL_TREND; emits only if `Close < sma200`

---

## Out of scope

- Lifting the short hard gate
- Inverse ETFs as a substitute (exposure.py already treats them as short; this spec is single-name fade)
- Crypto
