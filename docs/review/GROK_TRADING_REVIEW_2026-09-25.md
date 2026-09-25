# BluSterling paper book — trading review (2026-09-25)

Paper only. This is not a live-trading recommendation. The live Alpaca
business account stays off. Nothing in this review, and nothing in the
code change that accompanies it, enables live orders.

The book does not have a demonstrated edge. `replay.py` already measured
the live ensemble at about **−$1 per trade**, with a 95% confidence
interval of roughly **−$79 to +$77** — indistinguishable from zero, on
~174 trades. The same file says a naive breakout run through the exit
mechanics can look profitable. That split is the whole story: the
plumbing can be made not to explode, and the signals are mostly noise.
Costs, slippage, gaps, and survivorship bias eat what little gross
edge the prettier backtests claim.

MSTR, AMD, and MXL blew through the ~$320 per-trade risk figure because
that figure was a comment and an environment variable, not a check on
the order that actually went out. A notional clamp landed later
(`MAX_NOTIONAL_USD`, default $1,500). It was never a dollar-risk cap,
and one code path rounded a too-expensive share up to 1.

---

## Verdict

Do not add capital. Do not turn size-tilt on. Do not treat the
20-year Sharpe numbers in `portfolio_backtest.py` / `final_sweep.py` as
evidence this ensemble beats SPY. Those runs sized positions at up to
10% of equity with no $320 cap and no $1,500 cap, on a universe of
names that exist today.

The only strategy family with a consistent historical *ranking* is
buy-the-dip inside an uptrend (RSI / Bollinger / pullback to the 20-day,
price above the 200-day). Even that number is flattered by survivorship.
It is a research hypothesis, not a license to size up.

---

## (a) Strategies — edge, or noise?

Sixteen signal sources feed `Ensemble.agents` (`ensemble.py` around
186–200). Almost every one emits `instrument_type: "options"` and a
fixed percent stop. `Ensemble._normalize_geometry` then replaces the
stop with ATR(14)×1.5, capped at 4% of price. The agent’s own target
is bookkeeping. The real exit is a broker trailing stop of 2–6%
(`order_executor.py` around 292–304). So the “strategy” is: some
indicator fires, then a generic trail manages the trade. After
commissions-that-aren’t-charged on Alpaca paper, and after a realistic
spread on MSTR/AMD/MXL, most of these rules are coin flips with a
negative skew from gaps.

### Kill (stop letting them open risk)

| Agent | What it actually does | Why it is not an edge |
|---|---|---|
| `MomentumAgent` | Labeled 5/10/20-day rate of change. The fetch is **5-minute bars** (`momentum_agent.py` 80–95). `iloc[-6]` is ~30 minutes, not 5 days. SMA20 is ~100 minutes. | The rule that was researched is not the rule that trades. This is intraday noise with a daily name. |
| `BreakoutAgent` | 20/50-day high plus a volume spike (`breakout_agent.py` 84–144). | `signal_research.py` ranked `breakout20_long` near the **bottom** of the long table (docstring in `mean_reversion_agent.py` 12–17: +$47/trade, PF 1.38, 15 of 22 years — and that figure is pre-cost, survivorship-biased, and was sized larger than production). |
| `MoversAgent` | Yahoo day-gainers / day-losers, continuation if the move is already ≥5% (`movers_agent.py` 13–37). | Chasing a completed intraday move. By the time the screen prints, the edge (if any) is the spread. High-beta names (the MSTR/AMD/MXL problem) concentrate here. |
| `NewsAgent` | Keyword hits on Yahoo/MarketWatch RSS, headlines older than 2 hours dropped (`news_agent.py` 44–63). | Bag-of-words is not informed order flow. The post-mortem in `meta_agent.py` (168–174) already calls it the largest loss source (−$4,793 wrong-direction, −$1,873 gap). It is still in `PROTECTED_AGENTS` (`agent_rotator.py` 139–140), so the rotator cannot bench it. |
| `SentimentAgent` | VIX, Fear & Greed, put/call, then a directional lean (`sentiment_agent.py` 138–155). | One macro mood, applied as a trade. Contrarian-at-extremes is a slow effect. At 60-second ticks it is a confidence nudge pretending to be a signal. Also protected from benching. |
| `OptionsFlowAgent` | yfinance chain put/call and IV, explicitly **not** real flow (`options_flow_agent.py` 1–16). | A proxy for a data source you do not have. No reason to expect an edge after the bid/ask on the chain you are reading. |
| `EarningsAgent` | Pre-earnings drift and post-earnings gap chase (`earnings_agent.py` 115–140). | This is the overnight-gap trade. The book’s stops do not cover it (AMKR, cited in `ensemble.py` ~799–804: −14.5% gap, ~4× intended risk). Trading the event that breaks the risk model is backwards. |
| `TechnicalAgent` | RSI 35/65, MACD, Bollinger, SMA 20/50. Thresholds were **widened so signals fire more often** (`technical_agent.py` 52–67). Watchlist includes 3× ETFs (44–45). | Classic indicator stack, thresholds tuned by hand on the same names you trade. That is overfitting. Single-indicator confidence starts at 0.50, which is the bridge floor. |
| `PremarketAgent` | Gap-and-go / gap-fill on a short watchlist. | Same gap risk as earnings, thinner data. yfinance premarket is a bad tape. |
| `CryptoAgent` | RSI/MACD on BTC/ETH/SOL. | Already hard-off (`session_gates.py` `CRYPTO_TRADING_ENABLED = False`). Leave it off. 24/7 gap and weekend risk, no edge shown. |

### Keep (as risk gates, not as alpha)

- **Shorts only in `BEAR_TREND` / `HIGH_VOL`** (`ensemble.py` ~349–361). The sweep that motivated this is the least-wrong result in the repo: shorting a bull tape destroyed more than the short book made. Keep the hard gate. Do not “improve” it by letting solo shorts back into an uptrend.
- **Falling-knife block** on longs down ≥8% in two days (`ensemble.py` 810–817). Correct, and too narrow (one name can gap 8% overnight without a two-day trend).
- **One position per symbol, daily entry cap 3** (`ensemble.py` `DAILY_TRADE_CAP`). The cap is a concentration limit, not an edge. Keep it.
- **Paper lock** (`invariants.paper_only_violation`, `TradingClient(..., paper=True)`). Keep. Do not wire a live flag.

### Test (paper, pre-registered, or do not trade)

These are the only rules worth a clock. None is cleared for more size.
`SIZE_TILT_ENABLED` stays false until a rule passes the bars below on
**closed** trades, after a cost worse than 10 bps.

| Rule | Where | What “pass” means |
|---|---|---|
| Buy-the-dip, price above the 200-day (RSI < 30 or close below the lower band, or pullback to the 20-day) | `MeanReversionAgent`, and the rules in `signal_research.py` / `portfolio_backtest.signals_on` | The ranking versus breakout is the finding (`mean_reversion_agent.py` 7–32). Absolute +$90/trade is not. Survivorship flatters dips in companies that survived. Need a point-in-time universe, or at least a holdout of names that were delisted, before this is anything but a hypothesis. |
| Short weakness **only** below the 200-day, and only in a down regime | `ShortMomentumAgent` (this one does use daily bars, `short_momentum_agent.py` 95–105) and `BearishPatternAgent` | Keep them able to fire in a bear tape so the book is not structurally long-only. Do not judge them on bull-market P&L. Pattern code (double top, head and shoulders) is discretionary-looking and easy to overfit; the trend filter is the part worth testing. |
| Intermarket maps (oil → XLE, gold → GDX, yields → XLF) | `IntermarketAgent` | A small set of stated mappings, measured from today’s session. Worth a paper log. Not worth a full-size slot until the map has a closed-trade sample. |
| Volatility fade when the range is already contracting | `VolatilityAgent` | Reasonable hypothesis (don’t fade an expanding range). Unproven. |
| Sector relative strength, slow | `SectorRotationAgent` | A monthly/quarterly factor, sampled every 60 seconds. If tested, hold days, not ticks. |
| Macro risk-on/off ETFs | `MacroAgent` | A regime switch, not a stock pick. Use it to scale gross exposure. Do not let it open a third tech long. |

`AlpacaSurgeDetector` (`ensemble.py` `_scan_surges`) is a 1.5% tape-reading rule with confidence up to 0.78. Same problem as movers: you are paying the spread on a move that already happened. Do not test it until the momentum timeframe bug is fixed and the signal is the one the research file measured.

---

## (b) Risk management

### What the $320 cap actually was

`RISK_PER_TRADE` defaults to 320 in `order_executor.py` and
`trade_ledger.py`. The sizer that mattered was
`RISK_PER_TRADE_PCT` = 0.5% of equity in `agent_risk_bridge.py`
(~52, ~296). On a $100k account that is **$500**, not $320. On a
$160k reading of `ACCOUNT_BALANCE` it is more. A 4% stop against a
$500 budget is about **$12,500** of stock. A 4% trail on that
notional loses $500. An 8–15% gap — normal for MSTR, and not rare for
AMD or MXL — loses $1,000–$1,900. That is the blow-through. The
notional clamp (`MAX_NOTIONAL_USD` $1,500 and 2% of equity) cuts the
steady-state loss at a 4% trail to about $60. It does not enforce
$320, and it did not exist on the path that printed those trades.

Holes that were still open before this change:

1. **Dollar cap unused.** Nothing took `min(risk_budget, 320)` before
   shares were chosen.
2. **`max(1, int(notional / price))`.** A name priced above $1,500
   (MSTR has traded there) became 1 share, larger than the notional
   cap. `int()` truncates; `max(1, …)` undoes the truncation.
3. **Options max loss was 1% of equity** (`options_executor.py`
   `OPTIONS_RISK_PCT`, default 1.0) — about $1,000. Premium is the
   whole loss. One expensive contract was skipped only when it
   exceeded that 1%, not when it exceeded $320.
4. **The bridge options branch forces 1 contract**
   (`agent_risk_bridge.py` 246: `max(int(...), 1)`). The equity
   fallback then treats that premium `total_cost` as share notional.
   The order path now re-sizes shares. The `max(..., 1)` is still a
   lie in the approval log. Not removed in this change, because
   deleting it changes which signals get approved upstream.
5. **The trail is at least 2% and at most 6%**, even when the signal
   stop is tighter (`order_executor.py` ~292). Sizing to the signal
   stop and then resting a wider trail understates risk. The order
   path now sizes to the trail distance. A gap **through** the trail
   is still uncapped. That is the remaining MSTR problem: $1,500 × a
   25% gap is $375, which is over $320. A 40% gap is $600. No stop
   order fixes that. Smaller notional on high-ATR names would. That
   is recommended, not implemented — it changes every position, and
   the right ATR multiple is a research choice, not a typo.

### Position sizing

Equity shares are `min(risk_budget / stop_distance, notional / price)`,
floored (`agent_risk_bridge.py` ~296–322, now also capped by
`risk_caps.risk_per_trade_usd()`). Crypto fractional sizing is
unreachable for new entries. Options quantity is
`option_contracts(ask, min(1% of equity, $320))`.

Size tilt (`size_tilt.py`) can raise notional to 1.5× ($2,250) for an
agent with positive after-cost expectancy. Default **off**. Leave it
off. $2,250 × a 20% gap is $450.

### Stops and exits

- New equity entries: market order, then a GTC trailing stop sized to
  the filled quantity, with a retry (`order_executor.py`
  `_submit_equity_bracket`, `_submit_trail_with_retry`).
- If the trail submit fails, the position is still ledgered and
  `ensure_protective_exits` is supposed to re-arm it every tick.
  That backstop is real. It is also a cancel-and-replace on undersized
  trails (`widen_trails_on_survivors`), which has left names naked
  before. The naked-position kill switch (no new entries while any
  equity is unprotected) is the right response. Keep it.
- Options have no trailing stop at Alpaca. `submit_option_protective_stop`
  sends a plain stop at 50% of premium. If the broker refuses, the
  position is naked except for the premium you already paid. Defined
  risk only if you sized the premium. That is why the $320 premium cap
  matters.
- Winners can be held with a trail that ratchets tighter
  (`_trail_for_profit`). Losers on a daily-loss halt are market-closed
  (`Ensemble._derisk_on_halt`). Winners are left on. Fine, as long as
  the trail is actually resting.

### Daily loss

`RiskAgent` halts new entries at **3% of equity** on the day and **8%**
on the week (`risk_agent.py` 34–35, 105–124), using **broker equity
versus the prior close**, not the ledger (178–193). That part is
right — the ledger has been the flattering source before. VIX ≥ 35
also halts. Three consecutive losing *ledger* trades also halt
(132). Consecutive-loss uses the ledger; the daily dollar halt uses
the broker. Do not “simplify” those back into one number.

3% of $100k is $3,000. That is about nine full $320 losses, or one
bad gap day if notionals are large. The halt does not flatten winners
and does not cap a gap that happens after the close, because the
process is not submitting stops outside what the broker already holds.

### Correlation and concentration

There is **no pairwise correlation cap and no sector cap**. The
binding limits are:

- net long ≤ 100% of equity (`MAX_NET_LONG_PCT`, `ensemble.py` ~124)
- gross ≤ 2× (`MAX_GROSS_LEVERAGE_ENTRY`)
- 3 new equity entries a day
- one open position per symbol (ledger)
- buying power reserve $20,000

`MAX_OPEN_POSITIONS` (default 10) does **not** stop entries at 10. It
stops them at 30, and the comment says that is a runaway breaker for
a bad ledger (`ensemble.py` ~57–66, 315–327). Ten highly correlated
tech longs — NVDA, AMD, AVGO, MSTR, SMCI — sail through a 100% net
cap. That is how a flat SPY day becomes a −1.5% book day. The
exposure helper (`exposure.py`) correctly signs SQQQ/TQQQ. **SPXU was
missing** and was counted as long S&P; this change adds it at −3.
That is not a sector limit.

### Overnight and gap risk

Equity exits are GTC trails. They do not protect the open. The AMKR
note in `_normalize_geometry` (`ensemble.py` 810–812) is the spec: a
6% trail lost $1,170 on a −14.5% gap, about 4× intended risk. Earnings
entries make this worse on purpose. Nothing flattens the book before the close. Nothing
refuses a name whose 20-day ATR implies a gap larger than
`$320 / notional`. Recommended, not done: if daily ATR% × notional
would lose more than $320, cut shares until it would not. Until that
exists, high-ATR single names are a hole in the hard cap no matter
how correct the stop math is.

---

## (c) Backtest and paper measurement

### Look-ahead

`replay.py` and `signal_research.py` do the right mechanical thing:
indicators on bars strictly before day `t`, fill at the next open,
and if both the stop and a new high print in one daily bar, the stop
is assumed to fill first. That is pessimistic and honest **for daily
bars**. It is not the live path. Live momentum was looking at a
5-minute bar that includes the current print.

`Ensemble._normalize_geometry` was using `ATR.iloc[-1]` on a daily
history that **includes today’s unfinished bar** during regular
hours. Today’s high and low widened or tightened the stop before the
close. `risk_caps.completed_bar_value` now uses the prior completed
bar until 16:00 ET (`ensemble.py` ~794–803). After the close, today’s
bar is kept.

`trade_ledger.refresh_open_positions` can still mark a stop from a
yfinance daily bar. Broker-held names are forced back to open
(the comment around the broker override). Ghosts (ledger open, broker
flat) get closed. Good. Do not let the simulated path be the P&L
you judge agents on when the broker fill exists.

### Survivorship

Stated in `signal_research.py` (the universe is names listed **today**)
and again in `mean_reversion_agent.py` (28–32) and
`portfolio_backtest.py`. Buy-the-dip is the rule this bias flatters
most. Comparisons between rules on the same universe are usable.
Any CAGR, Sharpe, or “beats SPY” sentence from those scripts is not
a live expectancy. `final_sweep.py` still sweeps `pos10` (10% of
equity). Production is 2% and $1,500. A sweep that cannot lose more
than the live caps is the one that matches the bot. This change
applies `dynamic_risk_shares` inside `replay.py`, `signal_research.py`,
and `portfolio_backtest.py`, so a 10% config cannot outgrow $320 of
stop-risk or $1,500 of notional. Re-run them before quoting the old
Sharpe.

### Leakage and dishonest paper numbers

- **Open marks voted.** `agent_evaluator.evaluate` added unrealized
  P&L into the 5-day and 20-day windows that FLAG, promote, and
  (via the same report) size-tilt. `meta_agent.py` (466–470) already
  stopped doing this after NewsAgent held open winners at weight 1.0
  while its closed trades bled. The evaluator now skips open rows
  (`agent_evaluator.py` ~307). An open winner can no longer keep a
  bad agent off the bench, and it cannot qualify a size tilt.
- **Log backfill treated $320 as notional.**
  `parse_paper_trade_line` set `shares = 320 / entry`. On a 4% stop
  that is ~25× too few shares, so reconstructed P&L was far too
  small. Shares are now `risk / |entry − stop|`
  (`trade_ledger.py` `_shares_for_risk`). This only affects rows
  built from scheduler log lines, not rows `record_trade` wrote with
  a real fill. Do not re-parse a ledger you already trust; you would
  rewrite history.
- **Full P&L is credited to every co-signer.** A two-agent trade
  adds the same dollars to both leaves (`trade_ledger.per_agent_attribution`,
  and the evaluator loop). The ensemble average is then the mean of
  those overlapping sums. Corroborated trades count twice. Relative
  rank among agents who always trade together is distorted toward
  whoever sat on the crowded winners. Not changed: the tests and the
  rotator assume full credit. Recommended: split P&L by 1/N leaves,
  or score a trade once.
- **Paper friction is 10 bps and a $2 floor** (`trade_ledger.py`
  `ROUND_TRIP_COST_BPS`). Alpaca paper commission is $0, so some
  haircut is right. 10 bps is not the spread on MSTR, a 3× ETF, or
  an option with a 15% quote width (the options filter allows
  `MAX_SPREAD_PCT = 15`). Kill/promote decisions on those names are
  still optimistic. Recommended: name-level spread, or a flat 25–50
  bps on anything with ATR above ~4%.
- **Costs were absent from the research harnesses** that justified
  the 4% stop and the net-long gate. Adding the live size caps does
  not add slippage. Those Sharpes are still gross.

### Are paper results measured honestly?

Broker equity is the right daily P&L for the halt and the email
(`risk_agent.py` 178–193, `report_data`). The ledger is the right
source for *attribution*, and it has been wrong often enough that
the code comments list the incidents (duplicate entries before
2026-07-02, ghost SUNB, naked exits, a fill ledgered at the signal
price). The epoch filter (`LEDGER_EPOCH_START` 2026-07-02) keeps
pre-fix duplicates out of weights. Use it. Do not quote all-time
win rate.

A paper fill is not a live fill. Paper market orders on MSTR do not
pay the spread you will pay with real money. Any “we’re green on
paper” that has not survived the cost haircut and a gap day is not
an edge.

---

## (d) Ensemble weights and rotation

`MetaAgent` (`meta_agent.py`):

- Weights are from **closed**, epoch-filtered, after-cost P&L over
  20 days (414–480). Good correction. If fewer than
  `MIN_TRADES_FOR_WEIGHTING` closed trades exist, everyone sits on
  default weight 1.0. That is a long time at equal weight, which
  means the noisy agents vote as hard as anyone.
- Negative P&L collapses to a floor, not to zero. Rotation is
  supposed to do the killing. It often cannot (below).
- Merge: agreeing agents average confidence and add
  `AGREEMENT_BONUS` per extra name (343–376). Two weak agents can
  outrank one specific agent. That rewards correlation, which this
  book already has too much of.
- Solo shorts are blocked unless raw confidence is high or the tape
  is bear/high-vol (184–199). NewsAgent may not enter alone
  (174–183). Those gates are earned.
- Conflict on the same symbol keeps the higher-confidence side
  (381–407). It does not flatten. Fine.

`AgentRotator` (`agent_rotator.py`):

- FLAG → bench for 3 days, worst 20-day after-cost P&L first.
- **A benched agent does not trade, so its 20-day sample dies, so
  the reactivation rule (positive expectancy and ≥10 trades) cannot
  pass.** The file says this outright (60–63, 163–166). A bench is
  permanent. “Promote” then substitutes a sibling from
  `AGENT_VARIANTS` — Momentum for Technical for Breakout — which is
  the same family. You rotate noise for noise.
- `PROTECTED_AGENTS` includes NewsAgent and SentimentAgent, the two
  weakest information sources, plus both short agents (139–140).
  Protecting the short agents is justified (a bull-market P&L bench
  removed the only hedge). Protecting News and Sentiment is not.
- `MIN_ACTIVE_AGENTS = 2` can refuse a bench. You can be stuck
  trading the survivors of a bad family.

This is not “promote winners, kill losers.” It is “equal weight
until a small sample, then bench into a sibling, and never call the
loser back because it has no new trades.” Kill list in section (a)
should be a manual pin (`pinned_reason`), not a hope that the
rotator discovers it.

---

## (e) Operational risk

| Topic | State |
|---|---|
| Paper vs live | Hard-refused. `TradingClient` is constructed `paper=True`. `PAPER_TRADING=false` / `TRADING_MODE=live` raise. Do not undo this. |
| Market hours | New equity entries only 09:30–16:00 ET on a session day (`session_gates.equity_entries_allowed`). Crypto entries off. Holidays have a fallback list if `pandas_market_calendars` is missing. |
| Duplicate entries | The 2026-07-01 bug was re-entry every tick because the ledger never saw the reject. Ensemble skips a symbol the **ledger** already has open (`ensemble.py` ~523–535). That misses a broker position the ledger dropped. **This change** blocks a new equity entry when the broker holds the symbol, holds an option on it, or has any open order on it, and it claims the symbol for the rest of the ET day after a submit so a missed ledger write cannot stack a second order (`order_executor.py` `_duplicate_block`). A client with no `get_all_positions` is treated as a test stub, not a broker. If the broker read throws, the entry is refused. |
| Partial fills | The 15-second wait cancels a still-working DAY order and trails whatever the broker actually holds. The 2026-09-24 P fill (arrived after the wait, trail sized to the partial) is why an unfilled attempt **keeps** the day-claim. |
| Retries | Failed submits cool down for 1 hour (`FAILURE_COOLDOWN_SEC`). An exception used to return status `logged` and a “PAPER TRADE (log-only)” line, which reads as a clean skip when the order may have reached Alpaca. It now returns `status: error` and keeps the day-claim. |
| Idempotency key | Still no `client_order_id`. The day-claim and the broker check cover one process. Two processes, or a restart in the seconds before the position shows up, can still double-send. Recommended: a deterministic client order id per symbol per day, and treat Alpaca’s duplicate-id reject as a block, not a retry. |
| Options stacking | `options_executor.py` ~144–160 already refuses a second contract on the same underlying. Good. That path did not apply the $320 cap. It does now. |
| Error handling | Agent exceptions are throttled to one warning per 15 minutes (`ensemble.py` ~431–442). Good. A blanket `except: pass` still wraps geometry (`ensemble.py` `_normalize_geometry` tail) and several gates. A failed ATR fetch silently keeps the agent’s 2% stop. That is survivable only because the order path now sizes off the trail. |
| Logging | `logs/scheduler.log` is the human tape. The ledger CSV is the attribution tape. They diverge. Invariants (`invariants.py`) exist to yell when they do. Read the CRITICAL lines. Do not add another P&L calculator. |
| Secrets | `.env` is gitignored. This review does not change credentials, does not print keys, and does not deploy. The VM auto-deploy pulls **upstream** `master`. A PR on this fork does not deploy until someone merges it upstream. Leave it that way until you want the risk-cap patch in the paper loop. |

---

## Ranked changes

### Done in this change (bugs and hard caps only)

1. **Dollar-risk cap on the order.** `risk_caps.dynamic_risk_shares`
   floors shares so `shares × trail_distance ≤ RISK_PER_TRADE` and
   `shares × price ≤ notional`. Zero shares if one share breaks either
   cap. `order_executor._submit_equity_bracket` uses it.
   `agent_risk_bridge` takes `min(percent budget, $320)` before it
   sizes equity.
2. **Options premium cap.** `option_contracts` refuses a contract
   whose cost exceeds `min(1% of equity, $320)`.
3. **Duplicate-entry guard** on the broker position, option-on-underlying,
   resting order, ledger, and a same-day claim after submit.
   Exceptions no longer look like a logged paper trade.
4. **Evaluation bias, the mechanical kind.** Research harnesses size
   with the same function (no more silent 10% notional). Log backfill
   shares use stop distance. Live ATR ignores today’s unfinished bar.
   Evaluator rotation stats ignore open marks.
5. **SPXU** counted as −3× in `exposure.py` (it is on the technical
   watchlist and was previously +1×).

Tests: `test_risk_caps.py`. Existing paper-only tests still pass
(`test_learning_loop.py`, `test_size_tilt.py`, `test_risk_health.py`,
`test_orphan_fills_rotation.py`).

### Recommended, not done

1. **Gap budget.** Size shares so `ATR% × notional ≤ $320`, not merely
   the 4% trail. This is the actual MSTR/AMD/MXL hole left after the
   notional clamp. It will shrink high-vol names a lot. Decide the
   multiple on a replay that uses the live caps, then ship it.
2. **Pin the kill list** in `agent_summary.json` (`pinned_reason`)
   for News, Sentiment, OptionsFlow, Earnings, Technical, Momentum,
   Breakout, Movers, Premarket. Do not wait for the rotator. Remove
   News and Sentiment from `PROTECTED_AGENTS`.
3. **Drop `max(..., 1)` contracts** in `agent_risk_bridge.py` line 246
   so an options-typed signal cannot advertise a contract the premium
   cap would refuse.
4. **`client_order_id`** per symbol per ET day.
5. **Split co-signer P&L** (or stop averaging overlapping sums)
   before the next rotation.
6. **Cost model** that is wider than 10 bps on high-ATR names and
   options. Re-score FLAG/PROMOTE after that, not before.
7. **Point-in-time universe** before anyone quotes the 2005–2026
   Sharpe again.
8. **Sector / single-theme cap** (e.g. one semiconductor name, gross
   tech under some fraction of equity). The 100% net cap does not
   do this.
9. **No new risk into the close** on names reporting earnings within
   a day, and no `EarningsAgent` gap chase.
10. **Fix or delete `MomentumAgent`’s 5-minute “5-day” ROC** if you
    insist on keeping it. Do not “fix” it by loosening thresholds
    further.

---

## What to run

Paper stays on. Live stays off. Size tilt stays off.

- **Kill now (pin, do not trade):** NewsAgent, SentimentAgent,
  OptionsFlowAgent, EarningsAgent, TechnicalAgent, MomentumAgent,
  BreakoutAgent, MoversAgent, PremarketAgent. Crypto stays off.
- **Keep:** the short-only-in-bear gate, the falling-knife block, the
  daily entry cap, the naked-exit kill switch, the paper lock, and
  both short agents *as a hedge sleeve*, not as a bull-market alpha
  sleeve.
- **Test, small, closed-trade, after a harsher cost:** MeanReversion
  (200-day filter, no exceptions), bear-regime shorts, intermarket
  maps, volatility fade. Promote nothing on fewer than 30 closed
  trades, and not on an open mark.
