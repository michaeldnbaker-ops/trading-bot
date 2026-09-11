# SPEC-02 — IntermediateMomentumAgent (12-1, paper only)

**Status:** spec for Learning Loop / Ops review. **Do not implement in this PR.**  
**Priority:** #2 of 3.  
**Paper trading only.** No live brokerage.

---

## Hypothesis

The live **long** book is stacked on *fast* momentum:

| Agent | Horizon |
|---|---|
| AlpacaSurge / MoversAgent | minutes to same-day |
| PremarketAgent | first 15 minutes |
| MomentumAgent | 5 / 10 / 20 **day** ROC vs SPY, ~15-name watchlist |
| BreakoutAgent | 20-day / 52-week high |
| TechnicalAgent | 5-minute RSI/MACD |

`MeanReversionAgent` is the only slowish *anti*-momentum long (dip toward SMA20 while above SMA200).

`signal_research.py` already defines a distinct rule that none of those agents use:

```python
"mom12_1_long": lambda p, pp: p.ret12_1 > 20 and p.Close > p.sma50
# ret12_1 = (close[-21] / close[-252] - 1) * 100   # 12-month return skipping last month
```

That is classic 12-1 cross-sectional momentum: winners over the last year **excluding the most recent month** (the skip avoids the exact dip MeanReversionAgent is trying to buy).

**Claim:** a long-only 12-1 sleeve on a broad liquid universe will fire on different days and often different names than MomentumAgent (5d ROC) and MeanReversionAgent (RSI 30 / lower band), and will still pass the same ATR / daily-cap / net-long gates. That is diversification of **timescale**, which the ensemble currently does not have on the long side.

This is **not** “add another momentum clone.” If paper overlap with MomentumAgent+BreakoutAgent is high, the spec has failed (see success metrics).

---

## Entry

**Name:** `IntermediateMomentumAgent`  
**Direction:** `long` only.  
**Instrument:** equity (`instrument_type: equity`, `strategy: mom12_1`). Leave the options router for ≥0.70 Meta-merged signals if Ops wants leverage later; v1 should not self-label `single_leg_calls`.

**Setup (daily bars, all required):**

1. `ret12_1 > 20` — 12-month return excluding the last 21 sessions &gt; +20%.
2. `Close > SMA50` — still in an intermediate uptrend (the research rule). **Also require `Close > SMA200`** so this cannot fight MeanReversion’s trend filter or the falling-knife gate.
3. Rank cross-sectionally: among names that pass (1)–(2), take the top N by `ret12_1` (N = 3, same cap as MeanReversion).
4. Liquidity: price ≥ $5, 20d avg volume ≥ 500k.
5. Optional confirmation (does not replace the rank): 12-1 return also ahead of SPY’s own 12-1 (relative strength at the *same* horizon, not 5-day). Skip SPY itself as a trade (MomentumAgent already skips SPY; this agent should too — trading the benchmark is not an edge vs SPY).

**Confidence:**

- Base 0.64 at the 20% 12-1 threshold.
- +0.02 per extra 5 points of 12-1, cap 0.82.
- Enough to participate in 2-agent merges; not so high that it monopolizes the daily cap of 3.

**Do not enter:**

- HIGH_VOL if `BreakoutAgent` would be averse for the same reason (optional v1: set `regime_aversion = ["HIGH_VOL"]` **or** leave unaverse and let MetaAgent weights decide — **prefer aversion**, because 12-1 momentum is a trend-follower and `deep_research.py` showed trend/breakout bleed in high vol).
- Names with `ret12_1` driven by a single gap month that then reversed (implementation: require at least 8 of last 12 months positive, or skip if max monthly return &gt; 50% of the 12-1 — keep it simple in v1: skip if last 21d return &lt; −15% *and* 12-1 just above 20, i.e. the skip-month is a crash).

---

## Exit

Same production exits as other equity longs:

- ATR(14)×1.5 stop, **4% cap**
- Trailing stop + `widen_trails_on_survivors`
- Falling-knife skip on entry (2-day ≤ −8%)
- Daily loss halt de-risks losers

**Hold horizon expectation:** days to weeks, not minutes. That is the point. Do not add a same-day Premarket-style expiry. `MAX_HOLD_DAYS` in the ledger is currently 5 for *simulated* expiry — implementation PR must not rely on that 5-day fake close for this sleeve (known ledger vs broker-trail mismatch). Success measurement should use **broker** round-trips where possible (`report_data` / Alpaca paper fills), with ledger attribution.

---

## Risk gates

**Agent-level**

- Long only
- `regime_affinity = ["BULL_TREND", "NEUTRAL"]`
- `regime_aversion = ["HIGH_VOL"]` (see above)
- Max 3 signals/tick
- `MIN_CONFIDENCE = 0.55`

**Ensemble-level (do not change)**

- Net-long 100% / gross 2.0× / daily cap 3 / $20k BP reserve
- Avoid-list, falling knife, dedup
- Meta consensus 2-agent or solo ≥ 0.65
- Paper only

**Rotator:** variants `["MomentumAgent", "BreakoutAgent"]`. Not PROTECTED. Not a News-style corroboration-required agent.

**Weighting:** add to `DEFAULT_WEIGHTS`. Because 12-1 trades infrequently, **do not** judge it at 10 trades if those 10 arrived in one week of a melt-up; evaluator already requires 10 trades — Ops should wait for 20 before benching.

---

## Universe

Broader than MomentumAgent’s 15 names; similar to MeanReversionAgent + `signal_research.py` UNIVERSE (mega-cap, cyclicals, sector ETFs, **laggards included** so the cross-section is not “last decade’s winners only”).

~80 liquid US listings is enough. Do not scan the entire CRSP tape on a 60s tick — compute 12-1 **once per day** (cache 6 hours). Intraday ticks should only re-emit if the name is still above SMA50.

Dynamic universe injection is **not** useful here (a same-day gainer does not have a 12-1 rank). Ignore injected names without 252 sessions.

---

## Paper-only constraints

- Cache and yfinance daily bars only; no new market-data vendor required.
- No live Alpaca changes.
- Do not increase `DAILY_TRADE_CAP` to “give this agent room.” It competes for the existing 3 slots. If it never wins a slot because surge/movers always outrank it, that is a **MetaAgent ranking** problem to review — not a reason to widen the cap (ensemble comments: extra slots are filled by worse candidates).
- If it systematically loses the cap to 0.78 surge signals, implementation may add a **once-per-day** reserved slot only after Ops approval (separate spec). Default: no reserved slot.

---

## How to measure success

**Minimum paper window:** 20 closed trades spanning **both** a quiet BULL_TREND stretch and at least some HIGH_VOL (to confirm aversion). Calendar: enough to see whether 12-1 names are held through noise the 4% stop would have shaken out of 5d ROC trades.

### Vs SPY

- 20-day and since-start `report_data` edge (`bot_pct - spy_pct`) is the **ensemble** score, not this sleeve’s. For the sleeve:
  - Dollar P&amp;L / (dollar-days risked) vs buying SPY with the **same** 0.5% risk budget on those entry days.
  - Target: sleeve excess return vs SPY **on its own holding periods** ≥ 0 after costs (paper spread ~0). If the agent is just levered-SPY, `ret12_1` of the names will hug SPY — fail if average beta-adjusted excess ≈ 0 and names are QQQ components only.
- Correlation of daily sleeve P&amp;L vs SPY daily return: useful as diagnosis, not a hard gate. A 12-1 long book **will** be long-beta. Success is **better names than SPY**, not zero beta.

### Vs existing agents

| Sibling | Pass | Fail |
|---|---|---|
| MomentumAgent | Median holding period longer; Jaccard overlap of (symbol, date) &lt; 0.40 | Same symbols same week as 5d ROC longs ≥ 60% |
| BreakoutAgent | Many 12-1 names are **not** 20d high on entry day | Entry days cluster on 20d/52w breaks |
| MeanReversionAgent | Opposite location: this agent enters extended (`Close > SMA50`, 12-1 &gt; 20), MR enters oversold above 200-day | Both buying the same dip |
| MoversAgent | Almost no overlap (horizon) | Competing for the same daily cap on the same tick with ≥5% names |

Numeric:

- Epoch PF ≥ 1.15 **or** 20d P&amp;L ≥ ensemble median of **long** agents with ≥10 trades.
- Must not be the rotator’s worst 20d loser while MeanReversion is green in the same bull tape (that would mean 12-1 is the crowded-momentum bleed the dip-buyer was added to offset).

### Replay before paper

`signal_research.py` already simulates `mom12_1_long` through production stops. Implementation PR should **re-run** that named rule on the current UNIVERSE, print the survivor table, and only then enable the agent on paper. If it drops out of the survivor list (`CI excludes 0`, ≥70% years, PF &gt; 1.15), **do not ship** — the spec dies with the measurement.

---

## Registration checklist (implementation PR only)

1. `intermediate_momentum_agent.py` / `mom12_1_agent.py` with a stable `name`
2. `Ensemble.agents`, `DEFAULT_WEIGHTS`, `AGENT_VARIANTS`
3. Daily cache so e2-micro does not download 80 tickers × 252 bars every 60s
4. Unit test: a name with `ret12_1=10` does not signal; `ret12_1=25` and `Close>sma50>sma200` does

---

## Out of scope

- Short 12-1 (momentum crash) — that is closer to SPEC-01 / ShortMomentum, and shorts remain bear-gated
- Crypto 12-1
- Raising net-long or daily cap
