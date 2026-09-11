# SPEC-C — Premarket / News quality filter (garbage opens & ghosts)

**Ops gap C (after A and B).** Paper Alpaca only. **Do not implement in this PR.**  
**No crypto. No new options.** Shared lifecycle: [ROTATION-CONTRACT.md](ROTATION-CONTRACT.md). Assume learn/rotate/weight work after Ops PR `bc-652b78ab`.

This spec is **not** a 17th always-on equal-weight agent.

- **Premarket:** ship `PremarketAgent_strict` **cold**; rotator promotes it when Premarket (or SectorRotation) is benched.
- **News:** **PROTECTED** — rotator cannot bench it. Quality rules patch **in place** on `name = "NewsAgent"`. MetaAgent **downweights** (does not DISABLE to zero news). Optional `NewsAgent_strict` is **not** auto-promoted (PROTECTED parent never benches). If Ops later allows a protected-swap, it still must not run both News voices at weight 1.0.
- **Technical:** ghosts/bad opens → SPEC-A `RegimeEquityAgent` rotate-in, not another Technical clone. C3 knobs apply only if Technical is still `active`.

Ops: ghost and bad opens cluster on **TechnicalAgent + NewsAgent + PremarketAgent**.

---

## Hypothesis

Garbage opens here are two different bugs that look like “the agent stinks”:

1. **Bad signal:** keyword news and tiny gaps fire without price/volume confirmation, then MetaAgent **boosts** `NewsAgent` + `PremarketAgent` / Surge / Momentum as “catalyst alignment” (`meta_agent.py`). Post-mortem already blamed NewsAgent for **−$4,793 WRONG_DIRECTION** and **−$1,873 GAP_LOSS** (2026-07-31). `REQUIRE_CORROBORATION` only blocks *solo* News; Premarket+News together **satisfy** corroboration.
2. **Ghosts:** `invariants.py` `no_ghost_positions` — ledger row `open`, broker does not hold the name. These three agents emit on **yfinance** last price, often with `instrument_type: options` and 2% stops, so the ledger can book a PAPER TRADE / entry that never fills (or fills then immediately disappears) while reports show an open.

**Claim:** a **rotatable** Premarket strict variant plus an in-place News quality gate (News stays PROTECTED) cuts ghost count and WRONG_DIRECTION. That is higher leverage than an always-on News_v2 at weight 1.0 beside the old NewsAgent.

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
- SPEC-A rotates `RegimeEquityAgent` in when Technical is benched; C3 only if Technical is still `active`

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

### C2 — PremarketAgent_strict (rules live on the **variant**, not always-on old Premarket)

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

## MetaAgent / rotator — activate and deactivate

**PremarketAgent_strict (rotatable)**

- Ship in `Ensemble.agents`, `DEFAULT_WEIGHTS` key, `active: false`. Live weight starts at `MIN_AGENT_WEIGHT`.
- First substitute when rotator benches:

| Failing sleeve | Promote |
|---|---|
| PremarketAgent | **PremarketAgent_strict** |
| SectorRotationAgent | PremarketAgent_strict (second to SPEC-A if both listed; A wins if that cycle benched SectorRotation for P&amp;L) |

- **Regime while live:** same detector as Premarket; fade-shorts only if BEAR/HIGH_VOL (C2.4). MetaAgent aversion on bull-tape shorts. After 9:45 ET the module returns `[]` (time mute, not a bench).
- **Deactivate:** BENCH on evaluator flag; DISABLE if ghost count rises or bull-tape Premarket shorts fill. Do not auto-reactivate the **old** PremarketAgent as this variant’s substitute (do not list PremarketAgent first on strict’s `AGENT_VARIANTS`).

**NewsAgent (PROTECTED — weight, don’t bench)**

- In-place C1 filter always applies once the implementation PR lands (that is a gate, not a new voice).
- MetaAgent: 20d closed News P&amp;L ≤ 0 → `MIN_AGENT_WEIGHT`; keep `REQUIRE_CORROBORATION`. Catalyst boost **only** if C1 passed.
- Rotator: **PROTECTED** — never `active: false`. DISABLE on News means “Ops reverts C1,” not bench-to-zero (that recreates 2026-07-29 structurally-no-catalyst).
- Friday learner may raise News conf threshold; must not clear C1 price confirmation.

**TechnicalAgent:** if still active, C3 + SPEC-A promotion. If A already benched Technical, C3 is N/A.

**Ledger C4** is always-on plumbing (not a sleeve). No MetaAgent weight.

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

## KEEP / BENCH / DISABLE

Clock: 10 trading days after **PremarketAgent_strict is promoted** (or after C1 lands, for News). Ghost metric is invariants `no_ghost_positions` on names these agents opened.

| Verdict | Vs SPY | Vs existing agents | Action |
|---|---|---|---|
| **KEEP** | 20d ensemble `edge` vs SPY does **not** worsen by &gt; 2pp **because the bot went silent**; preferably gap vs the −18% baseline **narrows** | Ghosts = 0 on ≥8/10 days for News/Premarket/Technical; News+Premarket **new** opens down ≥50% vs prior 10d **and** News WRONG_DIRECTION $ / trade better than the 2026-07-31 class; Premarket shorts in BULL without HIGH_VOL = 0 fills | Strict stays active; C1 stays |
| **BENCH** | Inconclusive 20d (strict has &lt;10 trades) | Ghosts down but WRONG_DIRECTION flat | 3-day rest on **strict** only |
| **DISABLE strict** | Ensemble edge **worse** by &gt; 2pp and the only change was fewer first-hour trades that had been winning vs SPY | Ghosts **increase**; or overlap ≥70% with old Premarket would-have signals (filter did nothing) | `PremarketAgent_strict` `benched_at=2099-01-01`. Do **not** auto-promote old Premarket |
| **Revert C1 (Ops)** | Real tickered catalysts + ≥0.5% prints in watchlist and News emitted **zero** for 5 sessions | Movers/Surge still garbage-open — C silenced News instead of cleaning it | Loosen C1.3 to 0.3% or add a second **per-ticker** feed. Do not restore MarketWatch-on-every-symbol. Do not bench PROTECTED News |

**Vs SPY:** C is not an alpha sleeve. KEEP is “fewer losing opens / zero ghosts” helping the −18% gap. Do not KEEP a mute-the-bot outcome that trails SPY more.

**Vs existing agents:** before/after News and Premarket counts and $, not vs Momentum. Technical belongs in the table only if still `active`.

---

## Paper-only constraints

- Filters + one **cold** Premarket variant; no live brokerage flag changes.
- Do not add Unusual Whales / paid news.
- Do not run `NewsAgent` and `NewsAgent_strict` both `active` at weight 1.0.
- DISABLE on strict does not auto-lift after 3 days.

---

## Out of scope

- New options ideas (D)
- Crypto (E)
- Always-on Premarket_strict beside old Premarket
- Removing News from `PROTECTED_AGENTS`
- Using this spec to raise daily cap “because we filter more”
