# SPEC-A — Regime-aware equity long/short (replace bleeders)

**Ops gap A (do first).** Paper Alpaca only. **Do not implement in this PR.**  
**Not options. Not crypto.**

---

## Hypothesis

The paper book is ~**−18% vs SPY up**. Ops names the chronic bleeders as **OptionsFlowAgent** and **TechnicalAgent**, with **BreakoutAgent** and **SectorRotationAgent** in the rotation-guide bleeder set.

Repo facts that match that diagnosis:

- `TechnicalAgent` has **no** `regime_affinity` / `regime_aversion`. It scores 5-minute RSI/MACD/BB/SMA and emits L/S at 0.55. Rotator already stripped its PROTECTED status (9% win-rate note in `agent_rotator.py`).
- `OptionsFlowAgent` is a yfinance P/C–IV **proxy**, not flow. It labels `instrument_type: options` and is in the path that put **paper calls** on XLE/SBUX/F. Ops has paused new options ideas; this spec **replaces the equity decision**, it does not add another options product.
- `BreakoutAgent` already averse to `HIGH_VOL` (`deep_research.py`: −$28/trade, PF 0.85) but remains a long-breakout voice in a book that `signal_research.py` ranked **below** buy-the-dip.
- `SectorRotationAgent` longs leaders / shorts laggards with **no HIGH_VOL** affinity; its shorts are then crushed by the ensemble bear-gate or leak as lagging-sector longs in the wrong tape.
- `MeanReversionAgent` is the one measured long add (PF ~1.67–1.68, 19/22 years) and is **half-wired** (not in `DEFAULT_WEIGHTS`).

**Claim:** one **equity** regime switcher — long only the rules that already won in-repo *in bull/neutral*, short only the rules that won in-repo *in BEAR/HIGH_VOL* — can replace Technical + OptionsFlow as a decision source, compete for the daily cap of 3, and be **killed** if it does not beat SPY on a stated clock.

This is a **replacement**, not a 17th overlapping voice. Implementation must bench or hard-mute Technical and OptionsFlow while the replacement is on (rotator already knows how; OptionsFlow is not PROTECTED).

---

## Entry

**Name:** `RegimeEquityAgent` (stable `name` for ledger / MetaAgent / evaluator).

**Instrument:** `instrument_type: equity`. Do **not** self-label `single_leg_calls` / `puts`. Do not send v1 through `options_executor`. Ops D: options paused.

**Regime from `RegimeDetector` (already computed each tick). Do not invent a second detector.**

| Active regimes | Side | Rule (already in research files) | Must not clone |
|---|---|---|---|
| `BULL_TREND` or `NEUTRAL`, and **not** `HIGH_VOL` | **Long** | MeanReversion-style: `Close > SMA200` and (lower BB **or** RSI 20–30 **or** Close &lt; SMA20 with RSI 35–50). Optional second slot: `mom12_1_long` only if SPEC overlap with MomentumAgent stays low (see success) | Technical 5m RSI; OptionsFlow P/C; Breakout 20d high in HIGH_VOL |
| `BEAR_TREND` or `HIGH_VOL` | **Short** (equity) | `Close < SMA200` and RSI &gt; 60 (`short_rally_downtrend` in `short_research.py`), **or** 20d breakdown already in a downtrend | Bull-tape shorts (ensemble will reject; agent must stay **silent**); OptionsFlow puts |
| `BULL_TREND` **and** `HIGH_VOL` | Prefer **no new longs** from the breakout/ROC family; MR dip-longs allowed only if `Close > SMA200` and not falling-knife | Aligns Breakout’s aversion without relying on Breakout itself |
| `LOW_VOL` alone | No new entries from this agent in v1 | Macro already lists LOW_VOL as “everything”; don’t pretend we have a squeeze sleeve |

**Universe:** MeanReversion watchlist (liquid names, $5 / 500k vol). No Movers IPO screen. No 3x ETFs (Technical’s TQQQ/SQQQ/etc. are a known noise source). No crypto.

**Confidence:** 0.64–0.80, same band as MeanReversion so MetaAgent can merge with PROTECTED shorts in bear (for shorts) or with Momentum in bull (for longs) without always solo-scraping.

**Caps:** max 2 signals/tick from this agent (daily cap is already 3 ensemble-wide).

**While this agent is live on paper:**

- Bench **TechnicalAgent** and **OptionsFlowAgent** (`agent_summary.json` `active: false`). They are the bleeders this replaces. Do not leave them firing “for diversity.”
- Do **not** bench PROTECTED agents.
- Breakout / SectorRotation: leave rotator to flag them; do not also clone their signals here.

---

## Exit

Reuse production equity exits only:

- `_normalize_geometry`: ATR(14)×1.5, **4% cap**
- Broker trail + `widen_trails_on_survivors`
- Falling-knife skip on **long** entry (2-day ≤ −8%)
- RiskAgent halt cuts **losers**

No new options exit rules. No share fallback from a failed option (this agent never routes options).

---

## Risk gates

- Paper only. `PAPER_TRADING=true`.
- Shorts: agent silent unless `BEAR_TREND` or `HIGH_VOL` (same as `ensemble.py` `block_shorts`). **Do not** change that gate.
- `regime_affinity` / `regime_aversion` **must be set on every signal** (Technical’s hole).
- Longs blocked by existing net-long 100% / gross 2.0× / $20k BP / daily cap.
- Dedup one position/symbol.
- Bridge: equity sizing (`RISK_PER_TRADE_PCT` 0.5%), not the options contract sizer.
- Not PROTECTED in v1. Variants: `["MeanReversionAgent", "MomentumAgent"]` for longs conceptually; shorts conceptually `["BearishPatternAgent", "ShortMomentumAgent"]` — rotator dict is one list; use `["MeanReversionAgent", "ShortMomentumAgent"]` and document.
- Add `RegimeEquityAgent` to `DEFAULT_WEIGHTS`. If MeanReversion stays, **also** add MeanReversion to `DEFAULT_WEIGHTS` (plumbing, not a new edge).

---

## Kill criteria (required)

Clock starts on first **paper fill** attributed to this agent. Use broker equity for money, ledger for attribution (`report_data` rule).

**Kill the agent (bench + stop new entries) if any:**

1. **20 trading days** with ≥10 closed attributed trades and 20d sleeve `edge` vs SPY (`bot` window on those days, or sleeve $ vs SPY $ on the same notional) **&lt; 0** *and* SPY 20d **&gt; 0** — i.e. it is the same “SPY up, we down” failure as the current book.
2. **20d P&amp;L** worse than the benched Technical+OptionsFlow combined 20d (you made the bleeders quieter and the replacement is worse).
3. **Overlap fail:** ≥60% of this agent’s (symbol, session) longs also appear as Technical or OptionsFlow would have (replay their `generate_signals` offline if benched). Then it is a rename, not a replace.
4. **Bull-tape shorts:** any fill with `direction=short` while regimes were not `BEAR_TREND`/`HIGH_VOL`. Gate bug — kill and fix; do not “tune.”
5. **Gross violation** of 4% stop cap or 0.5% risk on a fill.

**Survive (keep running) if:**

- 20d `report_data` **ensemble** edge vs SPY improves vs the −18% baseline **and** this agent’s attributed PF ≥ 1.15 on ≥10 closed trades, **or**
- In a BEAR/HIGH_VOL window, attributed shorts are net positive while SPY 20d is negative (hedge doing the job), even if full-period CAGR still trails a roaring bull SPY.

Do **not** use win rate as the kill metric (ensemble already knows tight stops → low WR can still have expectancy).

---

## How to measure vs SPY and vs existing agents

**Vs SPY:** `report_data` 20d and since-start `edge`. Sleeve-level: sum attributed realized+unrealized / dollar-days risked vs buying SPY with the same 0.5% risk on entry days. Target: 20d edge ≥ 0 while SPY ≥ 0, or positive $ in SPY-down weeks.

**Vs bleeders:** 20d P&amp;L vs Technical + OptionsFlow (the replaced set). Must be better, not merely different.

**Vs keepers:** Jaccard overlap of trade days vs MeanReversion (longs should be similar *on dip days* — that is OK if Technical is gone) and vs ShortMomentum (shorts should be **fade-rally**, not breakdown — overlap &lt; 50%).

**Replay before paper:** run `bb_reversion_long` / `rsi_oversold_long` / `short_rally_downtrend` through production ATR-cap 4% **with the live short gate**. If shorts are not gated, do not ship (that is how the book already died).

---

## Paper-only constraints

- No live client. No crypto. No new options.
- Do not raise `DAILY_TRADE_CAP` to give this agent room.
- Do not un-bench Technical/OptionsFlow without a new Ops decision if kill criteria fire.

---

## Out of scope

- CryptoAgent
- New options structures / putting XLE-SBUX-F “right”
- Lifting short gate
- PROTECTING this agent on day one
