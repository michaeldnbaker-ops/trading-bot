# SPEC-C — Premarket / News quality filter (garbage opens & ghosts)

**Ops gap C (after A and B).** Paper Alpaca only. **Do not implement in this PR.**  
This is a **filter / hardening spec**, not a new signal agent and not crypto/options research.

Ops: ghost and bad opens cluster on **TechnicalAgent + NewsAgent + PremarketAgent**. News is **PROTECTED** (rotation cannot bench it). Premarket is not. Technical is a named bleeder (SPEC-A benches it; this spec still hardens it if it stays on).

---

## Hypothesis

Garbage opens here are two different bugs that look like “the agent stinks”:

1. **Bad signal:** keyword news and tiny gaps fire without price/volume confirmation, then MetaAgent **boosts** `NewsAgent` + `PremarketAgent` / Surge / Momentum as “catalyst alignment” (`meta_agent.py`). Post-mortem already blamed NewsAgent for **−$4,793 WRONG_DIRECTION** and **−$1,873 GAP_LOSS** (2026-07-31). `REQUIRE_CORROBORATION` only blocks *solo* News; Premarket+News together **satisfy** corroboration.
2. **Ghosts:** `invariants.py` `no_ghost_positions` — ledger row `open`, broker does not hold the name. These three agents emit on **yfinance** last price, often with `instrument_type: options` and 2% stops, so the ledger can book a PAPER TRADE / entry that never fills (or fills then immediately disappears) while reports show an open.

**Claim:** tightening News and Premarket *quality gates* (and not logging an open until the paper broker acknowledges a fill) cuts ghost count and WRONG_DIRECTION without removing PROTECTED News as a **catalyst co-signer**. That is higher leverage than adding a 17th agent while these three still spray.

---

## What exists today (repo)

**NewsAgent**

- Yahoo per-ticker RSS + **MarketWatch realtime (not ticker-filtered)** + market feeds
- Keyword bags (`buy`, `beat`, `miss`, …) with no entity check beyond “symbol in title **or** watchlist length == 1”
- Headlines &lt; 2 hours; **no** last-price confirmation that the name moved with the headline
- Emits `instrument_type: options` / single-leg calls|puts
- Meta: never solo; **catalyst boost +0.08** with Premarket/Surge/Momentum
- **PROTECTED** — cannot bench; must filter

**PremarketAgent**

- Only before **9:45 ET**
- Gap ±**1.5%** is enough; volume ≥1.5× is optional (raises conf, does not block)
- Gap-and-go **or** fade (fade → short on a gap-up that fills — can short into a bull open)
- `instrument_type: options`, expiry 1 day
- yfinance 5m bars for “today”

**TechnicalAgent** (ghost/bad-open co-accused)

- 5m RSI stack, **no regime tags**, 3x ETFs on the watchlist
- SPEC-A says bench it; if A is delayed, apply the Technical bullets below anyway

**Ghost definition (code):** ledger open symbols − broker symbols. Fixing agents without fixing “ledger open on intended fill” will not clear the invariant.

---

## Entry / filter rules (quality gate, not new edge)

### C1 — NewsAgent (keep PROTECTED, change emit rules)

Emit **only if all** are true:

1. Ticker **token** appears in the headline (drop the `len(watchlist)==1` bypass).
2. Do **not** attach MarketWatch `realtimeheadlines` to every symbol. Per-ticker Yahoo (and optional Seeking Alpha per ticker) only.
3. **Price confirmation:** same-session move in the signal direction ≥ **0.5%** on Alpaca stream if up, else yfinance 5m, else **skip**. Headline without a move is not a trade. This is the missing half of “catalyst alignment” (Meta already wanted news **plus** live price).
4. Minimum **two** independent headlines agreeing, or one headline **plus** a second agent already in `all_raw_signals` this tick — keep `REQUIRE_CORROBORATION`, but **do not** count Premarket gap-fade as corroboration unless Premarket also passed C2.
5. `instrument_type: equity` unless Ops lifts D. Stop advertising options on keyword hits (that path plus 0.70 conf is how random calls appear).
6. `regime_aversion`: News **shorts** inherit ensemble bear-gate (no bull-tape shorts from a “plunge” headline).
7. Still never solo at Meta.

### C2 — PremarketAgent

1. Gap threshold **±2.5%** (was ±1.5%). 1.5% is ordinary overnight noise vs a 4% stop cap.
2. **Require** early volume ≥ 1.5× (today it only adds confidence).
3. Gap-and-go only if price holds the open through **two** 5m bars, not one tick of `curr_price` vs open.
4. **Disable fade-shorts** unless `BEAR_TREND` or `HIGH_VOL` (fade-short on a gap-up in a bull open is a bull-tape short by another name).
5. Silent after 9:45 **and** no emit if Alpaca has no quote (don’t size off stale Yahoo).
6. `instrument_type: equity` while options are paused.
7. Max 2 Premarket signals/day (these compete for the first-hour half of the daily cap).

### C3 — TechnicalAgent (if still active)

1. Set `regime_affinity` / `aversion` (BULL for longs, BEAR/HIGH_VOL for shorts) — today **none**.
2. Remove 3x ETFs from the watchlist (TQQQ/SQQQ/UPRO/SPXU/TNA/TZA/LABU/LABD).
3. Do not emit below 2 aligned indicators at 0.65+ (single-indicator 0.50 is already blocked; 2-at-0.55 is the spray).
4. Prefer SPEC-A bench over more Technical knobs.

### C4 — Ghost / ledger (required for the invariant)

Agents must not be able to create a ledger **open** without a broker fill:

- `order_executor.execute_signal` / paper options path: write `status=open` only after Alpaca accepts and reports a fill (or a clearly defined pending state that invariants ignore).
- Do not parse a log line `PAPER TRADE:` into `paper_trades.csv` if the order was rejected (this is the historical parse_log path in `trade_ledger.py`).
- Invariants `no_ghost_positions` count **per day** is a success metric for this spec.

This is plumbing, still **paper-only**, still not a live switch.

---

## Exit

No new exit geometry. Existing trails / halt / options manager (for **legacy** XLE/SBUX/F calls — Ops D: manage those; don’t add new ones).

If a Premarket fade was the entry, same 4% ATR cap as everyone else (today Premarket uses a **2%** local stop before normalize; ensemble should still normalize — confirm in implementation, don’t add a third stop family).

---

## Risk gates

- Paper only. No crypto. No new options.
- Do not remove News or Sentiment from `PROTECTED_AGENTS`.
- Do not lift short gate; C2 fade-shorts must respect it.
- Catalyst boost (+0.08) **only** if News passed C1 **and** the co-signer is Surge/Momentum with a real print, not Premarket-at-1.5%-no-volume.
- Daily cap 3 unchanged.

---

## Kill / success criteria

Clock: 10 trading days after filters land (need enough opens to see ghosts go to zero).

**Pass (keep filters):**

1. Ghost count (`invariants` WARN `no_ghost_positions` for equities attributed to News/Premarket/Technical) **= 0** on 8/10 days (reconciling known broker/ledger races is OK if those symbols are not these agents).
2. News+Premarket attributed **new** equity opens down ≥50% vs the prior 10 days, **and** WRONG_DIRECTION $ for News (post-mortem classifier) better than the −$4.8k-class outcome on a per-trade basis (not “zero trades forever”).
3. 20d ensemble `edge` vs SPY does not **worsen** by more than 2 percentage points solely because the bot stopped trading (if A/B are not live, a quieter bad book is still a win).
4. Premarket shorts in BULL_TREND with no HIGH_VOL = **0 fills**.

**Kill / revert filters if:**

1. Ghosts **increase** (filter made more rejected orders that still hit the ledger).
2. News emits **zero** signals for 5 full sessions **and** real catalysts (earnings, clearly tickered headlines + 1%+ prints) were in the watchlist — filter too tight; loosen C1.3 to 0.3% or restore a second feed, don’t go back to MarketWatch-on-every-symbol.
3. Protected News is effectively muted so MetaAgent catalyst path never fires, while Movers/Surge still garbage-open — then C failed its job (quality, not silence).

**Vs SPY:** this spec is not an alpha sleeve. Success is **fewer losing opens** and **zero ghosts**, which should **reduce** the −18% gap, not print a standalone CAGR. Report: News/Premarket 20d P&amp;L and ghost count, plus `report_data` 20d edge as context.

**Vs existing agents:** compare News/Premarket trade counts and $ before vs after, not vs Momentum. Technical should already be benched under A; if not, include it in the before/after table.

---

## Paper-only constraints

- Filters only; no live brokerage flag changes.
- Do not add Unusual Whales / paid news. Keyword RSS + Alpaca paper quotes are enough.
- Do not implement a new NewsAgent_v2 **module** unless filters in-place are unreadable; Ops asked for a quality filter, not another voice. If a v2 file is cleaner, it must keep `name = "NewsAgent"` so PROTECTED + ledger attribution stay stable — **or** explicitly migrate PROTECTED to the new name in the same PR.

---

## Out of scope

- New options ideas (D)
- Crypto (E)
- Replacing SPEC-A/B
- Removing News protection
- Using this spec to raise daily cap “because we filter more”
