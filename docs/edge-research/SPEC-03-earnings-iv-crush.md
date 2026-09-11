# SPEC-03 — EarningsIvCrushAgent (paper options only)

**Status:** spec for Learning Loop / Ops review. **Do not implement in this PR.**  
**Priority:** #3 of 3 (only true *options* edge among the three).  
**Paper Alpaca options only.** `options_executor._clients()` already hardcodes `paper=True`. Do not add a live options path.

---

## Hypothesis

The account already learned that **share leverage + overnight gaps** is how paper equity nearly died (`options_executor.py`: AMKR −14.5% gap, 4× intended risk; 2.43× gross turned a −2.5% SPY day into −15%). The response was a **router**: signals with confidence ≥ 0.70 may buy single-leg calls/puts, max loss = premium.

That is still a **directional** bet with an options wrapper. It does not harvest implied volatility.

Meanwhile:

- `OptionsFlowAgent` reads yfinance P/C, IV rank, skew — then emits the same long/short directional dict as every other agent.
- `EarningsAgent` *buys* the pre-earnings run-up (long gamma **and** long IV into the print) and/or trades the post-gap **direction**.
- Post-event, IV on liquid names typically **collapses**. Directional agents either chase the gap (EarningsAgent) or ignore the event.

**Claim:** a small, defined-risk **paper** sleeve that is short expensive post-print IV (credit spread or short straddle *only if* defined-risk via long wings) on liquid names, **after** the event, is orthogonal to every directional agent and uses the options stack for its actual economic reason (theta/IV), not as a leveraged share substitute.

This is the highest-risk spec of the three (options microstructure, assignment, theta). v1 is deliberately **narrow**: calendar post-earnings, defined-risk credit verticals, tiny size, paper only.

---

## Entry

**Name:** `EarningsIvCrushAgent`  
**Side:** options **credit**; economically short IV, not a naked short share. Underlying direction is **hedged by construction** (vertical spread).  
**When:** first regular-session window **after** the earnings release (next RTH open if AMC; same morning if BMO), not 3–5 days before (that is EarningsAgent’s run-up).

**Setup (all required):**

1. Name is in a **liquid** options universe: SPY, QQQ, plus mega-cap names already on `OptionsFlowAgent` / `EarningsAgent` watchlists (AAPL, MSFT, NVDA, AMZN, META, GOOGL, TSLA). No Movers-universe IPOs.
2. Earnings **just occurred** (0–1 session ago), confirmed by yfinance calendar or a prior EarningsAgent watch — do not guess.
3. Front-week or front-month IV (or ATM straddle as % of spot) is **elevated vs the name’s own 20-session median** (v1 proxy: yfinance `ticker.options` IV if present; if IV missing, **skip** — do not fake IV from HV).
4. Spread quality: same `MAX_SPREAD_PCT = 15` as `options_executor`. If no contract pair clears it, skip.
5. Structure v1: **defined-risk credit vertical** aligned to *not* being a naked directional:
   - Default: **iron condor** (call credit + put credit) 25–50 DTE if both wings exist (reuse `MIN_DTE, MAX_DTE` from options_executor), **or** a single credit vertical if only one side is liquid.
   - Width: next listed strike beyond 1× expected-move (straddle) so a “normal” post-print day does not blow the short strike immediately.
6. Confidence: 0.62–0.70 (participate, but **below** `OPTIONS_MIN_CONFIDENCE` 0.70 so the **directional** options router does not also buy calls on the same name from EarningsAgent without Meta merge). MetaAgent merge of this agent with EarningsAgent **directional** on the same symbol must **cancel or drop this agent** (conflict: long delta vs short IV). Spec: if any other agent has a same-symbol long/short equity or single-leg option signal this tick, **do not emit**.

**Do not enter:**

- Before the print (IV long / run-up is EarningsAgent).
- HIGH_VOL crash days where SPY is limit-down and spreads are fiction.
- When `OPTIONS_ENABLED` is false.
- When paper `options_buying_power` cannot cover the **max loss** of the defined-risk structure (width × 100 × contracts).

---

## Exit

Do **not** use share ATR geometry. Options have their own manager:

- Existing `manage_options_exits()` in the ensemble tick (profit 2× / stop 0.5× premium for **long** options). **Credit spreads need the inverse rules.** Implementation must extend that manager with a `structure=credit` branch:
  - Take profit at **50% of max credit** (standard),
  - Stop at **2× credit received** (still inside max loss of the vertical),
  - Time: close at `CLOSE_DTE = 10` (already in options_executor) **or** 5 sessions after entry, whichever first (IV crush is front-loaded).
- Hard max loss = width × 100 × contracts, sized to `OPTIONS_RISK_PCT` (default 1% of **paper** equity) **or smaller** (v1: 0.5% to respect `RISK_PER_TRADE_PCT` spirit).
- No share fallback. If the contract cannot be placed, **skip** — do not `execute_signal` shares. Buying shares would destroy the hypothesis.

---

## Risk gates

**Agent-level**

- Never naked short calls/puts. Never short shares as a proxy.
- Max **one** IV-crush position at a time in v1 (this sleeve is research, not a second daily cap consumer).
- `regime_affinity = ["LOW_VOL", "NEUTRAL", "BULL_TREND"]` — crush after a print in a functioning two-way market.
- `regime_aversion = ["HIGH_VOL", "BEAR_TREND"]` until paper proves otherwise (credit spreads in a crash are how this sleeve dies).
- Emit a signal dict the bridge understands, **or** a dedicated paper options path that records the same ledger fields. If the bridge’s options sizer assumes **long** premium (`contracts = dollar_risk / (premium * 100)`), **do not reuse it blindly** for credits. Implementation PR must size by **max loss of the vertical**, not as if buying calls.

**Ensemble-level**

- Does **not** consume the equity daily cap of 3 if Ops prefers a parallel options research cap of 1. If that requires a code change, keep it paper-gated (`PAPER_TRADING` and `OPTIONS_ENABLED`).
- Dedup: if the name already has an equity position, skip (existing one-position-per-symbol).
- MetaAgent: treat as `instrument_type: options` with `strategy: credit_vertical_iv_crush` so it does not merge with `single_leg_calls` on the same key `(symbol, direction)`. **Use `direction: "short_vol"` or omit equity direction** — if MetaAgent only groups `long|short`, pick a sentinel and teach merge to not combine with share shorts. This is the one plumbing change allowed in an implementation PR; this spec PR does not make it.

**Paper-only**

- Alpaca **paper** options API only.
- No live enablement flag.
- Document in the implementation PR how assignment / early exercise is handled on paper (if paper does not model assignment faithfully, **say so** and measure with mid prices + expiry, not fantasy fills).

---

## Universe

Earnings calendar ∩ {SPY, QQQ, AAPL, MSFT, NVDA, AMZN, META, GOOGL, TSLA, AMD, NFLX}. Expand only after 20 paper events.

No dynamic-universe injection.

---

## Paper-only constraints

- This spec is **invalid** if it requires Unusual Whales / paid flow. yfinance IV + listed chain only (same honesty as OptionsFlowAgent). If IV is missing too often, the spec does not get a paid vendor in a stealth PR — it comes back to Ops.
- Do not set `OPTIONS_MIN_CONFIDENCE` lower globally to feed this agent into the long-option router.
- Do not increase `OPTIONS_RISK_PCT`.
- Do not implement “short straddle” without wings in v1.

---

## How to measure success

Options P&amp;L vs SPY is easy to cheat (a quiet week of collected premium looks like alpha). Use **event-level** stats.

### Vs SPY

- Per event: `(option_pnl / max_loss)` vs SPY return **from entry to exit**. Crush sleeve should have **low |correlation|** with SPY on those windows (target |ρ| &lt; 0.4). If P&amp;L just tracks QQQ, it is a disguised directional bet — fail.
- Do not require beating SPY CAGR. Require: **positive expectancy per event** with max loss respected (no gap beyond the vertical’s max loss).

### Vs existing agents

| Sibling | Pass | Fail |
|---|---|---|
| EarningsAgent | This agent enters **after** print; EarningsAgent’s run-up is **before**. Zero same-tick merge | Both long calls into the event |
| OptionsFlowAgent | This agent’s P&amp;L comes from credit/IV, not from a P/C directional call | Same `single_leg_calls` fills |
| options_executor directional | Different `strategy` string; no share fallback | Router buys calls on the same name |

Numeric bars after ≥ 20 **paper** events:

- Hit rate on “closed at ≥50% of credit” ≥ 50%, **or** PF ≥ 1.2 on event P&amp;L.
- **Zero** trades with realized loss &gt; stated max loss (otherwise paper execution is wrong).
- Count of skips for “missing IV” / “wide spread” logged; if skips &gt; 80% of earnings in universe, the data proxy is insufficient — stop, don’t loosen filters.

### Vs ledger / evaluator

Evaluator ranks **dollar** P&amp;L. A 0.5%-of-equity options sleeve will look small next to share trades. **Do not bench this agent** for low 20d $ vs BreakoutAgent. Implementation should tag trades so Improver/Ops review uses **R-multiple** (pnl / max_loss) for this name. Until evaluator supports that, Ops reviews the spec slice manually; rotator: add to a do-not-bench list **or** document that 10 share-equivalent trades will never accumulate.

---

## Registration checklist (implementation PR only)

1. New module; `name = "EarningsIvCrushAgent"`
2. Register in ensemble **after** Ops signs the MetaAgent `(symbol, direction)` merge issue
3. `DEFAULT_WEIGHTS` entry
4. Extend `manage_options_exits` for credit structures — **required**, not optional
5. Ledger: record option symbols (OCC), credit received, max loss, not a fake share `entry_price`
6. `AGENT_VARIANTS`: `["OptionsFlowAgent", "EarningsAgent"]` is **wrong economically**; better `[]` until a second vol agent exists. Do not let the rotator “promote EarningsAgent” as a substitute for short-IV.

---

## Out of scope

- Live options
- Naked shorts, ratio spreads, weekly lotteries
- Using this agent to lift the equity short gate
- Pre-earnings long IV (already EarningsAgent, and usually the expensive side)

---

## Why this is #3 not #1

It needs options-manager plumbing, MetaAgent merge rules, and honest IV data. Gaps #1 and #2 reuse the equity path that already survives replay. This gap is still the correct **options** diversifier; it is not the cheapest to try.
