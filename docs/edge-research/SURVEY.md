# BluSterling ensemble survey — paper trading

**Scope:** read-only inventory of agents/strategies in this repo, how performance is measured, and the highest-leverage *missing* edges. Specs for the top three gaps live beside this file. **Paper Alpaca only. No live brokerage. No strategy implementation in this PR.**

Docs and comments still say “12-agent ensemble.” Production `ensemble.py` actually constructs **16 signal agents** plus MetaAgent, RiskAgent, and an inline Alpaca surge detector. Crypto is a **separate** 24/7 scheduler.

---

## 1. Agent / strategy inventory

Shared pipeline every equity tick (`market_scheduler.py` → `ensemble.run_cycle`):

1. `RegimeDetector.detect()` — SPY SMA20/50, VIX, RSI, 52w high → `{BULL_TREND, BEAR_TREND, HIGH_VOL, LOW_VOL, BREAKOUT, OVERSOLD, OVERBOUGHT, NEUTRAL}`
2. `RiskAgent.assess()` — daily/weekly loss halt, VIX halt, confidence multiplier
3. Each active agent `generate_signals()`
4. Alpaca surge overlay
5. `MetaAgent.synthesize()` — P&L weights, regime boost/penalty, 2-agent consensus (or high solo bar)
6. Ensemble gates (daily cap, exposure, short-regime, avoid-list, falling-knife, dedup, ATR geometry)
7. `AgentRiskBridge.evaluate_signal()` — fields, `MIN_CONFIDENCE=0.50`, stop/target sanity, sizing
8. High-conviction (`≥ 0.70`) may route to **paper** `options_executor`; else paper shares

**Shared risk gates (all equity agents, after synthesis):**

| Gate | Where | Paper note |
|---|---|---|
| Daily new equity entries | `DAILY_TRADE_CAP` default **3** (auto_tune can change) | After 10:00 ET remaining budget halved |
| Buying-power reserve | `< $20k` → no new entries | Paper Alpaca account |
| Gross / net exposure | Gross `> 2.0x` or net long `> 100%` blocks **longs** only | Shorts exempt (they cut net) |
| Short regime hard gate | Shorts allowed **only** if `BEAR_TREND` or `HIGH_VOL` | Measured 2026-08-14: shorts-always-on destroyed the book |
| Learner avoid-list | Worst epoch losers, cap 8 symbols | From `trade_ledger.epoch_trades()`, not `learned_params.json` |
| Falling knife | Longs with ≤ −8% over 2 daily bars skipped | AMKR overnight-gap lesson |
| One position per symbol | Ledger open LONG or SHORT | Both sides blocked |
| ATR geometry | Stop 1.5× ATR(14), **capped at 4%** of entry | Target is a distant marker; real exit is trail |
| Bridge | Confidence, price geometry, risk 0.5%/trade, notional cap | PDT rejection is **live-only**; paper always allowed |
| Meta consensus | 2+ agents **or** raw solo ≥ 0.65 long / 0.72 short | NewsAgent **never** solo; BEAR eases short solo / raises long solo |
| Options overlay | `OPTIONS_MIN_CONFIDENCE` default 0.70 | Paper client hardcoded `paper=True` in `options_executor._clients()` |

Registration for a signal agent today:

- Instantiate in `Ensemble.agents` (`ensemble.py`)
- Appear in `MetaAgent.DEFAULT_WEIGHTS` or the agent **never gets P&L weight** (falls through to 1.0, then is invisible to the power-curve dict)
- Optional: `AGENT_VARIANTS` in `agent_rotator.py` for substitute-on-bench
- Optional: `PROTECTED_AGENTS` (never benched)
- Bench state: `logs/agent_summary.json` `active: false`

### 1.1 Signal agents in the equity ensemble

| Agent | Side | Signal idea | Agent-level gates | Ensemble registration | Regime |
|---|---|---|---|---|---|
| **TechnicalAgent** | Long **and** short | RSI, MACD, Bollinger, SMA20/50; 2+ indicators to clear 0.55 | `MIN_CONFIDENCE=0.55`; watchlist ETFs + mega-cap + 3x bull/bear | In `Ensemble.agents` + `DEFAULT_WEIGHTS`. Rotator variants: Momentum, Breakout | **None** (`regime_affinity` not set) |
| **NewsAgent** | Long **and** short | RSS keyword sentiment (Yahoo/MarketWatch), headlines &lt; 2h | Score → confidence; `REQUIRE_CORROBORATION` in MetaAgent | In ensemble + weights. Variants: Sentiment, OptionsFlow. **PROTECTED** (never benched) | **None** |
| **SentimentAgent** | Long **and** short **SPY only** | Contrarian F&G / VIX / put-call + SPY 5d mom | `MIN_CONFIDENCE=0.52` | In ensemble + weights. Variants: News, OptionsFlow. **PROTECTED** | **None** |
| **MomentumAgent** | **Long only** | 5/10/20d ROC, volume, RS vs SPY, price &gt; SMA20 | `MIN_ROC_5D=2%`, `MIN_RS=1.05`, skips SPY itself | In ensemble + weights. Variants: Breakout, Technical | Affinity: `BULL_TREND`, `BREAKOUT`. No aversion |
| **BreakoutAgent** | **Long only** | Break 20d / 50d / 52w high, vol ≥ 1.5×, candle &gt; 1 ATR, prior consolidation | `MIN_CONFIDENCE=0.55` | In ensemble + weights. Variants: Momentum, Technical | Affinity: `BREAKOUT`, `BULL_TREND`. **Aversion: `HIGH_VOL`** (deep_research: breakouts −$28/trade, PF 0.85 in high vol) |
| **BearishPatternAgent** | **Short only** | Double top, H&S, LH/LL, death cross, support break, RSI divergence | `MIN_CONFIDENCE=0.55` | In ensemble + weights. Variant: ShortMomentum. **PROTECTED** (short-book floor) | Affinity: `BEAR_TREND`, `HIGH_VOL`, `OVERBOUGHT`. **Aversion: `BULL_TREND`** |
| **ShortMomentumAgent** | **Short only** | Negative ROC, distribution volume, below SMA, RS weakness vs SPY | `MAX_ROC_5D=-1.5%` | In ensemble + weights. Variant: BearishPattern. **PROTECTED** | Affinity: `BEAR_TREND`, `HIGH_VOL`. **Aversion: `BULL_TREND`** |
| **EarningsAgent** | Long **and** short | Pre-earnings run-up (calls 3–5d out); post-gap continuation; gap-and-crap fade | Gap ≥ 3%, vol 2× | In ensemble + weights. Variants: Macro, News | Affinity: `BULL_TREND`, `BEAR_TREND`, `HIGH_VOL`, `NEUTRAL`. No aversion |
| **MacroAgent** | Long **and** short (SPY/QQQ or defensives) | Yield curve, DXY, gold, TLT, VIX, XLU/XLP vs SPY → risk-on / risk-off | `MIN_CONFIDENCE=0.55` | In ensemble + weights. Variants: Earnings, Sentiment | Affinity: **all five** including `LOW_VOL`. No aversion |
| **PremarketAgent** | Long **and** short | Gap-and-go / gap-fill / pre-market volume | **Silent after 9:45 ET**; gap ±1.5% | In ensemble + weights. Variant: SectorRotation | Affinity: BULL/BEAR/HIGH_VOL/BREAKOUT/NEUTRAL |
| **SectorRotationAgent** | Long leaders, short laggards (sector ETFs) | 1m/3m ROC of 11 sector ETFs vs SPY | Outperform +1.5% / underperform −1.5% | In ensemble + weights. Variant: Premarket | Affinity: `BULL_TREND`, `BEAR_TREND`, `NEUTRAL` — **not HIGH_VOL** |
| **OptionsFlowAgent** | Long **and** short | **Proxy**, not paid flow: yfinance P/C, IV rank, OI, IV skew | P/C &lt; 0.5 bull / &gt; 1.2 bear | In ensemble + weights. Variants: News, Sentiment | Affinity: BULL/BEAR/HIGH_VOL/NEUTRAL |
| **VolatilityAgent** | Long **and** short | Statistical overextension (BB %B + RSI extreme) **only if range is decelerating** | `MIN_CONFIDENCE=0.55`; ATR stop/target | In ensemble + weights. **Not in `AGENT_VARIANTS`** | **None** — claimed orthogonal, never boosted/penalized |
| **IntermarketAgent** | **Long only** (by design) | Intraday driver → beneficiary: WTI→XOM/CVX or airlines; gold→NEM/GDX; copper→FCX; 10Y→XLF | Session move vs threshold on 5m bars | In ensemble + weights. **Not in `AGENT_VARIANTS`** | Affinity: BULL/BEAR/HIGH_VOL/NEUTRAL. Explicitly **no shorts** (clean-epoch shorts 1W–3L) |
| **MoversAgent** | Long gainers, short losers | Yahoo `day_gainers` / `day_losers` continuation; whole-market universe | `\|move\|≥5%`, price ≥ $5, vol ≥ 500k | In ensemble + weights. **Not in `AGENT_VARIANTS`**. No static watchlist (dynamic screens) | Affinity: `BULL_TREND`, `BEAR_TREND`, `HIGH_VOL` |
| **MeanReversionAgent** | **Long only** | Buy-the-dip **above SMA200**: lower BB, RSI&lt;30, or pullback to SMA20. Added 2026-08-12 after `signal_research.py` ranked these PF 1.67–1.68 vs breakout PF 1.38 | Price ≥ $5, vol ≥ 500k, RSI floor 20 (news-crash skip), max 3 signals/tick | In `Ensemble.agents` **only**. **Missing from `DEFAULT_WEIGHTS` and `AGENT_VARIANTS`** | Affinity: `BULL_TREND`, `NEUTRAL`, `HIGH_VOL`. No aversion. (Originally used non-canonical regime strings; fixed to detector names) |

### 1.2 Not in `Ensemble.agents` (but real)

| Module | Side | Role | Notes |
|---|---|---|---|
| **AlpacaSurgeDetector** | Long / short | Inline `_scan_surges`: ≥1.5% real-time move, cap 5/tick, conf 0.65–0.78 | Emits `instrument_type=options`. Not a class, not in DEFAULT_WEIGHTS, not rotatable |
| **RiskAgent** | None | Portfolio halt / conf multiplier | Monitor only |
| **MetaAgent** | None | Merge, weight, top-2 per tick | Wrapper name stored on fills as `MetaAgent(AgentA, AgentB)` |
| **CryptoAgent** | Long / short BTC ETH SOL | Same TA as TechnicalAgent, 24/7 | `crypto_scheduler.py` cron every 15 min. **Not** in equity ensemble. `instrument_type=crypto` |
| **ensemble_v11.py** | — | Legacy orchestrator | Comments say `ensemble.py` replaced it; scheduler imports `ensemble.py` |

Almost every equity agent labels `instrument_type: "options"` and `strategy: single_leg_calls|puts`. That is a **label**, not an options edge. Execution still goes through the high-conviction paper-options router or falls back to shares. MeanReversionAgent is the exception (`instrument_type: equity`, `strategy: mean_reversion`). CryptoAgent is `crypto_spot`.

---

## 2. How performance is measured vs SPY / ledger

**Rule in `report_data.py`:** broker (Alpaca **paper**) is truth for money; ledger is truth for attribution. If they disagree by &gt; $250, the snapshot warns instead of silently flattering.

### 2.1 Files and metrics

| Layer | File | What it measures |
|---|---|---|
| Ledger | `trade_ledger.py` → `data/paper_trades.csv` | Per-trade P&L, side, agents, status. Epoch filter `epoch_trades()` (post-2026-07-02) for rotation/weights so duplicate-bug era does not vote |
| Broker vs SPY | `report_data.py` `windows` | Bot % vs SPY % over 1d / 5d / 20d / since `$100k` start. `edge = bot_pct - spy_pct` |
| Daily email | `daily_reporter.py` | Renders those windows (Bot / SPY / Edge) |
| Weekly email | `weekly_reporter.py` | SPY **and QQQ** buy-and-hold for the same week |
| Agent rank | `agent_evaluator.py` | 5d / 20d / all-time P&L from ledger; flag if 20d P&L **negative** and &gt;20% worse than ensemble avg; `MIN_TRADES_TO_EVALUATE=10` |
| Backtest vs SPY | `portfolio_backtest.py`, `verify_combined.py`, `final_sweep.py`, `replay.py` | Historical equity curve vs SPY CAGR / max DD (used to set net-long 100%, cap 3, shorts-in-bear) |
| Signal research | `signal_research.py`, `short_research.py`, `deep_research.py` | Per-rule $/trade, PF, % years profitable, regime split. Survivorship bias stated in-file |

### 2.2 Evaluate → rotate → weight → improve → learn

```
tick (60s, RTH)
  ensemble → paper orders → ledger

10:00 ET and 15:30 ET  (market_scheduler.run_eval_cycle)
  AgentEvaluator.evaluate() → logs/latest_eval.json
  if flagged: AgentRotator.run_rotation() → logs/agent_summary.json + rotation_log.jsonl

every MetaAgent.synthesize
  _load_performance_weights() from ledger 20d closed P&L (power curve, MIN_AGENT_WEIGHT=0.40)

Friday 15:45 ET  (run_learning_cycle)
  StrategyLearner.learn() → logs/learned_params.json
    per-agent conf/stop deltas, best hours, avoid_symbols, worst_symbols

consumed next ticks
  Ensemble._avoid_symbols()  ← epoch ledger damage (NOT learned_params.json)
  auto_tune.load()           ← data/tuning.json (ATR mult, solo bar, daily cap)
  agents.get_agent_adjustment()  ← **defined, never called**

not in the scheduler
  daily_postmortem.py   cron 17:00 ET → analysis/ post-mortem markdown + trade_context classify
  improver_agent.py     suggested cron 21:00 ET → analysis/recommendations_*.md (advisory only)
  auto_tune.py          Sunday job → data/tuning.json (one param per run, revert if equity fell)
```

**Honest holes in the loop (surprises, not new edges):**

1. `StrategyLearner.get_agent_adjustment()` / `get_worst_symbols()` have **zero callers**. Agents do not self-tune from Friday JSON. The ensemble’s avoid-list was rewritten to ignore that JSON after a corrupted-era list blacklisted ~40 names including AAPL/MSFT/NVDA.
2. `performance_logger.ENSEMBLE_AGENTS` is still the original five names (`Technical, News, Sentiment, Risk, Meta`). Evaluator no longer uses it; weekly reporter still unions it.
3. `MeanReversionAgent` is live in the tick loop but **absent** from `DEFAULT_WEIGHTS` and `AGENT_VARIANTS`, so MetaAgent cannot tilt it and the rotator cannot substitute it.
4. `ImproverAgent` is not wired into `market_scheduler.py`.
5. `AGENT_VARIANTS` also omits Volatility, Intermarket, Movers — rotator cannot swap those categories.

---

## 3. Coverage map and ranked gaps

### What is already crowded

- **Short-horizon long momentum:** MomentumAgent (5–20d ROC), BreakoutAgent (20d/52w), MoversAgent (same-day ≥5%), AlpacaSurge, Premarket gap-and-go, Technical golden-cross.
- **Continuation shorts (same regime, same idea):** ShortMomentum + BearishPattern, both affinity BEAR/HIGH_VOL, both aversion BULL, both PROTECTED. Movers/Technical/News/Macro/SectorRotation can also short, but the dedicated short *book* is two correlated pattern/momentum voices.
- **News/flow overlay:** News, Sentiment, OptionsFlow (yfinance proxy). MetaAgent already boosts News+Surge/Momentum/Premarket (“catalyst alignment”).
- **Long mean reversion in an uptrend:** MeanReversionAgent (the 2026-08-12 add). VolatilityAgent is a *different* two-sided statistical fade **without** the SMA200 filter MeanReversion treats as non-negotiable.

### What regimes exist vs who uses them

| Regime | Who specializes | Gap |
|---|---|---|
| BULL_TREND | Most long agents | Saturated |
| BEAR_TREND / HIGH_VOL | Short specialists + hard short gate | Short *style* is continuation only |
| BREAKOUT | Breakout, Momentum, Premarket | Breakout averse to HIGH_VOL (good) |
| LOW_VOL | Macro lists it among “everything”; nobody else | **Unused specialist** (`vol_squeeze_long` exists in `signal_research.py` and has no agent) |
| OVERSOLD | Detector emits it | MeanReversion uses HIGH_VOL/BULL, not OVERSOLD tag |
| OVERBOUGHT | BearishPattern affinity | No dedicated long-fade in bull (correct: shorts blocked in bull) |

### Ranked highest-leverage gaps (tied to *this* repo, not generic factor lists)

**#1 — Short-side mean reversion (sell rallies *below* the 200-day), BEAR/HIGH_VOL only**  
`signal_research.py` / MeanReversionAgent already established that **buy-the-dip above SMA200** is the best long rule in-repo (PF ~1.67–1.68, 19/22 years), while breakout sat near the bottom. `short_research.py` was written specifically to test the inverse (`short_rally_downtrend`: price &lt; SMA200 and RSI &gt; 60) and the comments on ShortMomentum/BearishPattern quote those tables — but **no agent implements that fade**. Existing shorts are breakdown/death-cross/momentum *continuation*. VolatilityAgent can short overbought names with no trend filter, which is the failure mode MeanReversionAgent’s docstring warns about. A third short *style* also helps the 2-agent short consensus rule, which is structurally fragile when only two PROTECTED shorts exist.

**#2 — Intermediate-horizon 12-1 cross-sectional momentum (long)**  
`MomentumAgent` is 5/10/20-day ROC on ~15 names and skips SPY. `signal_research.py` already encodes `mom12_1_long` (`ret12_1 > 20` and price &gt; SMA50) as a **different** rule from ROC, breakout, and pullback. The live long book is therefore three flavors of *fast* momentum plus one dip-buyer. A slower 12-minus-1 sleeve is the cleanest timescale diversifier that is already sitting in the research file and is not correlated with MeanReversionAgent (they fire on opposite RSI/price locations).

**#3 — Post-earnings IV crush as a defined-risk *paper options* edge**  
`options_executor.py` exists because equity gap risk (AMKR −14.5% overnight) nearly wrecked the paper book — but it only **re-expresses high-conviction directional signals** as long calls/puts. `OptionsFlowAgent` is a yfinance chain proxy, not an options P&amp;L engine. `EarningsAgent` is the wrong side of the event for premium: it *buys* the pre-earnings run-up. Nobody harvests the post-print IV collapse. That is the only missing sleeve that is actually an **options** edge (theta/IV), not an equity direction with an options label — and it can stay on the existing paper Alpaca options client (`paper=True`), 25–50 DTE window, premium-as-max-loss.

Honorable mention (not spec’d): `vol_squeeze_long` (LOW_VOL → breakout) is an unused detector regime. Lower priority than #2 because BreakoutAgent already owns 20d highs; squeeze would be a *filter* on an existing voice more than a new edge.

---

## 4. Implementation constraints for any follow-up PR (not this one)

Paper-only contract if Learning Loop / Ops approves a spec:

- Keep `PAPER_TRADING` default `true`. Do not add a live Alpaca trading client. `options_executor._clients()` must remain `paper=True`.
- Register in `ensemble.py`, `DEFAULT_WEIGHTS`, and `AGENT_VARIANTS`. Fix MeanReversionAgent’s missing weight/variant entries in the same plumbing PR if that agent is kept.
- Reuse `trade_ledger` attribution (`primary_agent` / contributors) so evaluator/rotator/MetaAgent see the new name.
- Success vs SPY: `report_data` windows plus a tagged slice in the ledger (agent name).
- Success vs existing agents: 20d ledger P&amp;L and correlation of *trade days* against the nearest sibling (do not add a voice that fires on the same symbols/ticks as Momentum or ShortMomentum).
- Do not lift the shorts-only-in-BEAR/HIGH_VOL hard gate to “make a short agent useful.”

---

## 5. Surprises from this survey

1. **Wrong roster size in standing docs** — CLAUDE.md / MetaAgent header still say 12 agents; `ensemble.py` runs 16 signal agents.
2. **Git remote** in this checkout is `michaeldnbaker-ops/trading-bot`, not the `mddnnbr-tech/trading-bot` URL in `CLAUDE.md`.
3. **MeanReversionAgent is half-wired** — in the tick loop, missing from MetaAgent weights and rotator variants.
4. **Learner output is largely a diary** — Friday JSON is written; agents never read `get_agent_adjustment`. Avoid-list was re-sourced to the ledger after the learner blacklisted the universe.
5. **Options are a router, not a strategy** — almost every agent claims `instrument_type=options`; only the 0.70 confidence path actually buys paper contracts, always single-leg directional.
6. **PerformanceLogger roster is stale** (5 names). Ledger-backed evaluator is the real source.
7. **LOW_VOL / OVERSOLD** are detected every cycle and almost never consumed.
8. **Crypto is isolated on purpose** (`crypto_scheduler.py`) — not an equity-ensemble gap.
9. No `docs/` tree existed before this folder; specs live here to match “research first” (`signal_research.py`, `short_research.py`, `deep_research.py` already live at repo root).
