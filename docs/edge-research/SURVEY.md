# BluSterling ensemble survey — paper trading

**Ops inventory is authoritative (2026-09-11).** This file confirms it against the repo and records how P&L is measured. Specs A/B/C only. **Paper Alpaca. No live brokerage. No strategy implementation in this PR.**

---

## 0. Ops snapshot (do not rediscover)

| Fact | Ops | Repo check |
|---|---|---|
| Roster | 16 signal agents + RiskAgent + MetaAgent | **Match.** `Ensemble.agents` is exactly those 16, then `self.risk` / `self.meta` |
| Crypto | Separate sleeve; **OFF for main equity bot**; no new crypto edges | **Match.** `CryptoAgent` is not in `Ensemble.agents`. `crypto_scheduler.py` is 24/7 cron. Specs here add **zero** crypto |
| PROTECTED from bench | News, Sentiment, BearishPattern, ShortMomentum | **Match.** `agent_rotator.PROTECTED_AGENTS` |
| Chronic bleeders | OptionsFlow, Technical (+ Breakout / SectorRotation in rotation guide) | Matches rotator history (`UPLOAD_GUIDE_rotation_fix.md`) and Technical’s 9% WR note when protection was removed |
| Book vs SPY | ~**−18%** while SPY is positive | Measured in `report_data.py` windows (`bot_pct - spy_pct`). Broker is truth for money |
| Ghost / bad opens | Technical + News + Premarket | `invariants.py` `no_ghost_positions`: ledger open, broker does not hold. Those three fire often on yfinance prices and `instrument_type=options` labels |
| Options in book | OptionsFlow + `options_executor` calls on **XLE / SBUX / F** | Router: `OPTIONS_MIN_CONFIDENCE` 0.70, paper client `paper=True`. **Ops: pause new options ideas** until equity exits + scorecard green |
| Shorts | Gates already restrict to BEAR/HIGH_VOL | **Match.** `ensemble.py` `block_shorts` unless `BEAR_TREND` or `HIGH_VOL` |

Standing comments still say “12-agent ensemble” (`CLAUDE.md`, `meta_agent.py` header). That is stale documentation, not a second roster.

---

## 1. Confirmed agent inventory

Pipeline every RTH tick (`market_scheduler.py` → `ensemble.run_cycle`):

1. `RegimeDetector.detect()` — `{BULL_TREND, BEAR_TREND, HIGH_VOL, LOW_VOL, BREAKOUT, OVERSOLD, OVERBOUGHT, NEUTRAL}`
2. `RiskAgent.assess()` — halt / confidence multiplier
3. 16 agents `generate_signals()` (benched names skipped via `agent_summary.json`)
4. Alpaca surge overlay (inline, **not** a 17th roster agent)
5. `MetaAgent.synthesize()` — 20d ledger weights, regime boost/penalty, consensus
6. Ensemble gates → `AgentRiskBridge` → paper shares, or paper options if conf ≥ 0.70

**Shared gates (paper):** daily cap 3; BP reserve $20k; gross 2.0× / net long 100% (longs); shorts **only** BEAR/HIGH_VOL; avoid-list from epoch ledger (max 8); falling-knife longs; one position/symbol; ATR stop 1.5× capped 4%; bridge `MIN_CONFIDENCE=0.50`; Meta 2-agent or solo ≥ 0.65 long / 0.72 short; **NewsAgent never solo**.

### 1.1 The 16 signal agents (`ensemble.py` order)

| Agent | Side | Signal idea | Key gates | Registration | Regime |
|---|---|---|---|---|---|
| TechnicalAgent | L/S | RSI/MACD/BB/SMA20-50 on 5m bars | `MIN_CONFIDENCE=0.55`; 3x ETFs on watchlist | `DEFAULT_WEIGHTS`; variants Momentum/Breakout; **not PROTECTED** (chronic bleeder) | **None** |
| NewsAgent | L/S | RSS keyword sentiment, &lt;2h | Meta **must** corroborate; still PROTECTED | variants Sentiment/OptionsFlow | **None** |
| SentimentAgent | L/S **SPY only** | VIX, F&G, P/C, SPY 5d mom | `MIN_CONFIDENCE=0.52` | **PROTECTED** | **None** |
| MomentumAgent | **Long** | 5/10/20d ROC + RS vs SPY | skips SPY; `MIN_ROC_5D=2%` | variants Breakout/Technical | Affinity BULL/BREAKOUT |
| BreakoutAgent | **Long** | 20d/50d/52w high + vol spike | Ops: rotation-guide bleeder | variants Momentum/Technical | Affinity BREAKOUT/BULL; **aversion HIGH_VOL** |
| BearishPatternAgent | **Short** | H&S, death cross, breakdowns | — | **PROTECTED**; variant ShortMomentum | Affinity BEAR/HIGH_VOL/OVERBOUGHT; aversion BULL |
| ShortMomentumAgent | **Short** | Negative ROC, RS weakness | — | **PROTECTED**; variant BearishPattern | Affinity BEAR/HIGH_VOL; aversion BULL |
| EarningsAgent | L/S | Pre-run-up, post-gap, fade | gap ≥ 3%, vol 2× | variants Macro/News | Affinity BULL/BEAR/HIGH_VOL/NEUTRAL |
| MacroAgent | L/S | Yields, DXY, gold, TLT, defensives | — | variants Earnings/Sentiment | Affinity includes LOW_VOL; no aversion |
| PremarketAgent | L/S | Gap-and-go / fade | **Silent after 9:45 ET**; gap ±1.5% | variant SectorRotation; Ops: ghost/bad opens | Affinity BULL/BEAR/HIGH_VOL/BREAKOUT/NEUTRAL |
| SectorRotationAgent | L leaders, S laggards | 11 sector ETFs vs SPY 1m/3m | Ops: rotation-guide bleeder | variant Premarket | Affinity BULL/BEAR/NEUTRAL — **not HIGH_VOL** |
| OptionsFlowAgent | L/S | yfinance P/C, IV rank, skew **proxy** | chronic bleeder; paper **calls in book** | variants News/Sentiment | Affinity BULL/BEAR/HIGH_VOL/NEUTRAL |
| VolatilityAgent | L/S | BB/RSI extreme **if decelerating** | — | **not in AGENT_VARIANTS** (empty slot reserved for Spec B) | **None** |
| IntermarketAgent | **Long only** | Intraday WTI/gold/copper/10Y → names | longs only by design | **not in AGENT_VARIANTS** | Affinity BULL/BEAR/HIGH_VOL/NEUTRAL |
| MoversAgent | L gainers, S losers | Yahoo day_gainers/losers, ≥5% | $5 / 500k vol | **not in AGENT_VARIANTS** (empty slot reserved for Spec B) | Affinity BULL/BEAR/HIGH_VOL |
| MeanReversionAgent | **Long** | Dip **above SMA200** (BB / RSI / pullback) | max 3/tick; RSI floor 20 | **In agents list only** — missing `DEFAULT_WEIGHTS` and `AGENT_VARIANTS` | Affinity BULL/NEUTRAL/HIGH_VOL |

### 1.2 Not roster agents

| Module | Role | Ops |
|---|---|---|
| RiskAgent | Halt / conf multiplier | Gate, not a signal source |
| MetaAgent | Weight + merge, top-2/tick | Wrapper on fills |
| AlpacaSurgeDetector | Inline ≥1.5% stream overlay | Not in the 16 |
| CryptoAgent + `crypto_scheduler.py` | BTC/ETH/SOL 24/7 | **OFF for main equity bot. No new crypto.** |
| `ensemble_v11.py` | Legacy | Scheduler imports `ensemble.py` |

Almost every agent labels `instrument_type: options`. That is a **label**. Fills go through the 0.70 paper-options router or shares. MeanReversionAgent is the equity exception. **No new options specs until Ops scorecard is green.**

---

## 2. Performance vs SPY / ledger / learning loop

**Rule (`report_data.py`):** paper **broker** is truth for money; **ledger** is truth for attribution. Gap &gt; $250 → warn, do not flatter.

| Layer | File | Metric |
|---|---|---|
| Money vs SPY | `report_data.snapshot()` windows | 1d / 5d / 20d / since $100k: `edge = bot_pct - spy_pct`. Ops: book ~**−18%** vs SPY up |
| Attribution | `trade_ledger` → `data/paper_trades.csv` | Side, agents, realized/unrealized. Rotation uses `epoch_trades()` (post-2026-07-02) |
| Rank / rotate | `agent_evaluator` → `agent_rotator` | 5d/20d P&L; flag if 20d **negative** and &gt;20% worse than avg; ≥10 trades. Twice daily 10:00 & 15:30 ET |
| Weight | `MetaAgent._load_performance_weights` | 20d **closed** ledger P&L, floor 0.40 |
| Learn | Friday 15:45 `strategy_learner` | Writes `learned_params.json`. **`get_agent_adjustment()` has no callers** |
| Avoid-list | `Ensemble._avoid_symbols` | Epoch damage, not Friday JSON (learner once blacklisted ~40 names) |
| Improve | `daily_postmortem` (17:00), `improver_agent` (not in scheduler), `auto_tune` (Sunday) | Classify GAP_LOSS / WRONG_DIRECTION; one bounded param/week |
| Research | `signal_research.py`, `short_research.py`, `deep_research.py`, `replay.py` | Per-rule PF / years / regime. Survivorship bias stated in-file |

---

## 3. Ops gap order (this PR)

| # | Gap | Why this repo, not generic finance | Spec |
|---|---|---|---|
| **A** | Regime-aware **equity** L/S **PROMOTED** when Technical / OptionsFlow / Breakout / SectorRotation is **BENCHED** | Technical has **no** regime tags and is a named bleeder; OptionsFlow is a proxy bleeder with paper calls (**A owns this parent**; B does not share it). Breakout / SectorRotation sit on bleeder↔bleeder `AGENT_VARIANTS`. MeanReversion is half-wired. Book ~−18% vs SPY | [SPEC-A](SPEC-A-regime-equity.md) |
| **B** | Short/downside **PROMOTED** from **VolatilityAgent / MoversAgent** (empty slots reserved; do not reassign); **second** on Technical after A; **not** on OptionsFlow; live only in BEAR/HIGH_VOL | Gates already block bull-tape shorts. Dedicated shorts are two PROTECTED continuation agents (cannot BENCH them to promote B). `_find_replacement` promotes only the first inactive variant — A and B cannot both claim first slot | [SPEC-B](SPEC-B-bear-shorts.md) |
| **C** | Premarket **strict** PROMOTED when Premarket is BENCHED; News quality in-place (PROTECTED → FLAG cannot BENCH) | Ghosts: Technical + News + Premarket. News keyword RSS; Premarket ±1.5% gaps. Catalyst boost rewards News+Premarket together | [SPEC-C](SPEC-C-news-premarket-quality.md) |

**Paused (Ops D):** any new options product. Existing XLE/SBUX/F calls are exits/scorecard. Future options variants would use FLAG/BENCHED/PROMOTED — not specified here.

**Forbidden (Ops E):** crypto edges; wiring CryptoAgent into the equity ensemble.

**Learning Loop:** A/B/C ship **cold** and are **PROMOTED** only when `agent_rotator` **BENCHED** a sibling in `AGENT_VARIANTS`. First inactive variant only — A owns OptionsFlow; Technical is A then B; B on-ramps = Volatility + Movers (empty reserved; Ops PR #3 will not steal them). Words: FLAG / BENCHED / PROMOTED / REACTIVATED — not KEEP/DISABLE. 3-day **REACTIVATED** is expected (replace is temporary unless FLAG fires again). Improver is **not** on the scheduler and cannot apply specs. Friday `get_agent_adjustment` is **unused** — do not assume learner retune. Numeric kill thresholds = **TODO** until Ops daily scorecard. Shared: [ROTATION-CONTRACT.md](ROTATION-CONTRACT.md).

---

## 4. Plumbing holes (do not spec a second control plane)

1. `MeanReversionAgent` missing from `DEFAULT_WEIGHTS` / `AGENT_VARIANTS`. New sleeves: both, start `active: false`.
2. `get_agent_adjustment()` has no callers. Specs must not depend on Friday conf deltas.
3. `performance_logger.ENSEMBLE_AGENTS` still five names — evaluator is ledger-backed.
4. Today `AGENT_VARIANTS` maps bleeders to **other bleeders**. Volatility / Movers have **no** keys — **reserved for Spec B**. Ops PR #3 leaves them empty. A and B **must not** both claim first slot: Technical → A then B; OptionsFlow → **A only**; B on-ramps = Volatility + Movers. Do not reassign B’s parents. Map: [ROTATION-CONTRACT.md](ROTATION-CONTRACT.md).
5. `BENCH_DAYS = 3` then **REACTIVATED**. “Replace bleeders” is a **3-day window** unless **FLAG** fires again. Expected rotator behavior, not a reject. No permanent-off event.
6. Improver writes `analysis/recommendations_*.md` only. Not a promotion path.

---

## 5. Paper-only contract (research HOLD)

- `PAPER_TRADING=true`. No live Alpaca trading client.
- Do not lift `block_shorts`.
- Do not add options strategies until Ops D is lifted.
- Do not touch `crypto_scheduler.py` / `CryptoAgent` except to keep them **out** of the equity bot.
- Vs SPY: `report_data` 20d `edge`. Qualitative FLAG/promote rules in SPEC-A/B/C; **numeric kill TODOs** pending scorecard.
- Vs existing agents: overlap vs the **BENCHED** sibling.
- New names ship `active: false`. This PR is **docs only**.
