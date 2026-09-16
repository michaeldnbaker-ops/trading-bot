# SPEC-A validation — RegimeEquity design + kill criteria

**CoS / Learning Loop lock (2026-09-16).** Single research focus. **Docs only. Do not implement.** Paper Alpaca. **No crypto. No new options.**

**Frozen:** Spec B implementation is **PARKED** — do **not** PROMOTE B. Spec C is a **quality filter only, not alpha**. See [SPEC-B-bear-shorts.md](SPEC-B-bear-shorts.md) and [SPEC-C-news-premarket-quality.md](SPEC-C-news-premarket-quality.md).

Sleeve definition (entry, exits, parents): [SPEC-A-regime-equity.md](SPEC-A-regime-equity.md). Rotator words: [ROTATION-CONTRACT.md](ROTATION-CONTRACT.md).

---

## Status

| Item | Lock |
|---|---|
| Spec A implementation | **HOLD** — validation design only |
| Spec B implementation | **PARKED** — do not code, do not PROMOTE |
| Spec C | **Quality filter only** — not an alpha sleeve |
| Day-1 scorecard (2026-09-11) | **NOT LOCKED** — ensemble context, not FLAG math |
| ≥5-day healthy-scorecard clock | **Restarts 2026-09-16** |
| Sep 14–15 | **Gap days** — no backfill |
| Scorecard email | **Restored 2026-09-16** |
| Numeric kill boxes | **TODO** until ≥5 healthy scorecard days |
| Learning Loop handoff | **Not before** ≥5 healthy scorecard days |
| Cold PROMOTE | **Forbidden** — seed `{ "active": false }` first |

---

## What this file is

The **validation + kill** contract for `RegimeEquityAgent` (Spec A). It does **not** add a 17th always-on voice and does **not** ship strategy code.

**Primary metric:** expectancy **after costs**.  
**Secondary:** vs SPY (`report_data` edge). Win rate is not a pass/fail (4% stop cap).

**Regime (locked definition):** **direction × vol**, **point-in-time prior close**. Use only information known at the previous session close. Do **not** classify on the same-day print. Do **not** invent a second detector — map `RegimeDetector` labels onto this product (BULL / BEAR / NEUTRAL × HIGH_VOL / not-HIGH_VOL). `LOW_VOL` alone is not an A entry regime (SPEC-A v1).

---

## Pipeline (in order; do not skip)

```
mini backtest
  → walk-forward (rolling, purged)
  → locked OOS
  → paper envelope
       (≥30–50 trades  OR  ≥5 healthy scorecard days)
  → seed agent_summary { "active": false }
  → PROMOTE only when Technical or OptionsFlow is BENCHED
```

No cold PROMOTE. Missing-from-summary is not a promote (Ops PR #3 `_find_replacement`). Improver cannot apply. Friday `get_agent_adjustment` is unused — do not retune from learner JSON.

### 1. Mini backtest

Replay in-repo rules only (`bb_reversion_long` / `rsi_oversold_long` / `short_rally_downtrend`) through **production** ATR×1.5 **4% cap** and the **live short gate** (silent unless BEAR/HIGH_VOL). Paper-cost model: commissions + spread + the 4% stop as a real exit. Universe = MeanReversion liquid names ($5 / 500k vol). No 3x ETFs. No crypto. No options.

**Pass to walk-forward (qualitative until boxes fill):** after-cost expectancy not obviously ≤ 0 on the in-sample slice; regime tags actually change side; no bull-tape short fills.

### 2. Walk-forward (rolling, purged)

Rolling windows. **Purge** the embargo around each fold so labels / regime at *t* cannot leak from *t+1* bars or same-day close. Regime on every bar = **prior close** only (direction × vol). Do **not** refit thresholds inside a test fold. Do **not** use Day-1 (2026-09-11) or gap days (Sep 14–15) as a fold.

**Pass to locked OOS:** after-cost expectancy > 0 **across** folds (not one lucky window). Wrong-regime slices must be flat or sized-down (see kill structure).

### 3. Locked OOS

One **held-out** slice, frozen **before** looking at it. No parameter edits after unblinding. Same cost model, same prior-close regime, same short gate.

**OOS expectancy ≤ 0 → kill** (box numeric still TODO — the inequality is committed).

### 4. Paper envelope

Paper Alpaca only. `PAPER_TRADING=true`. Envelope is **observation**, not a live sleeve, until PROMOTE.

**Minimum sample (either):**

- **≥30–50 attributed trades**, **or**
- **≥5 healthy scorecard days**

**Healthy scorecard day** = a session whose daily scorecard email arrived and is usable (ticks / attribution / vs-SPY present). **Sep 14–15 are gap days — do not count, do not backfill.** Day-1 (2026-09-11) is a **NOT LOCKED** footnote and does **not** start this clock. Clock **restarts 2026-09-16** (email restored). Do not hand to Learning Loop before the ≥5 healthy days.

### 5. Seed `{ "active": false }`

Before any rotator cycle can see A:

```json
{ "RegimeEquityAgent": { "active": false } }
```

(exact `agent_summary.json` row shape as other agents). **Seed first. Then** wait for a parent **BENCHED**. No cold PROMOTE. Roster presence alone is not enough.

### 6. PROMOTE (A only; B PARKED)

**PROMOTED** only when rotator **BENCHED** a listed parent **and** A is already seeded `active: false`.

| Parent **BENCHED** | Who is **PROMOTED** | Lock |
|---|---|---|
| `TechnicalAgent` | **A first** (`RegimeEquityAgent`) | B is **PARKED** — do **not** PROMOTE B even if A is already `active: true` |
| `OptionsFlowAgent` | **A only** | B is **not** on this list |
| `BreakoutAgent` / `SectorRotationAgent` | A (per SPEC-A table) | Still A-only while B is PARKED |
| `VolatilityAgent` / `MoversAgent` | **None** while B is PARKED | Empty `[]` stays empty |

Do **not** PROMOTE `ShortMeanReversionAgent` (B) from this lock. Do **not** PROMOTE `PremarketAgent_strict` as alpha (C is quality-filter-only).

After `BENCH_DAYS` the bleeder is **REACTIVATED** (expected). Permanent off is not in the rotator.

---

## Metrics

| Rank | Metric | Use |
|---|---|---|
| **Primary** | **Expectancy after costs** | Pass/fail for mini / WF / OOS / paper↔backtest. Broker = money; ledger = attribution |
| **Secondary** | **vs SPY** | `report_data` 1d / 5d / 20d / since `edge = bot_pct - spy_pct`. Sleeve must not be the reason the book lags SPY in the wrong direction |
| Not a gate | Win rate | 4% stop cap → low WR can still have positive expectancy |
| Context | vs BENCHED sibling | Attributed P&L better than Technical / OptionsFlow over the same window; not a 5m RSI or P/C clone |

Costs in the primary metric: commission + spread + stop/trail as taken. Do not report pre-cost expectancy as the headline.

---

## Pre-committed kill structure

**Inequalities are locked. Numeric boxes stay `TODO` until ≥5 healthy scorecard days (clock from 2026-09-16).** Do **not** invent cutoffs from Day-1 (2026-09-11) or from Sep 14–15 gap days.

| # | Kill / FLAG | Structure (committed) | Numeric box |
|---|---|---|---|
| 1 | **OOS expectancy ≤ 0** | After-cost OOS expectancy **≤ 0** → **kill** (do not PROMOTE; if already live, evaluator **FLAG**) | `TODO` (exact $ / R after ≥5 healthy days) |
| 2 | **DD > budget** | Sleeve peak-to-trough **>** pre-committed drawdown budget → **kill** / **FLAG** | `TODO` (budget $ or % after ≥5 healthy days) |
| 3 | **Paper ↔ backtest expectancy divergence > ~25%** | \|paper expectancy − locked-OOS expectancy\| / \|OOS\| **>~25%** → **kill** / **FLAG** (do not “tune” via unused Friday deltas) | `~25%` committed; exact rounding / min-trade floor `TODO` |
| 4 | **Wrong-regime must flat / size-down** | Wrong cell of **direction × vol** (prior close) → **no new entries** and **flat or size-down**. Any **short fill in BULL without HIGH_VOL** is a gate bug → **FLAG** | `TODO` (size-down multiple after ≥5 healthy days) |
| 5 | **Elevated weight + entries ≈ 0 → FLAG** | MetaAgent / `DEFAULT_WEIGHTS` **elevated** while attributed **entries ≈ 0** over the envelope → **FLAG** (ghost / attribution hole, not “quiet success”) | `TODO` (weight floor + entry count after ≥5 healthy days) |

**Remain-active (qualitative, unchanged):** vs SPY the sleeve is not another always-on long book while SPY is down; vs BENCHED sibling it is better and not a clone; vs regime, longs in BULL/NEUTRAL (not HIGH_VOL), silent on bull-tape shorts.

Do not treat “ensemble got quieter” as success vs SPY. Zero fills in a bull-tape short book = gate working for shorts, **but** elevated weight + entries ≈ 0 on the **live long** sleeve is still row 5.

---

## Scorecard clock (2026-09-16)

| Date | What it is |
|---|---|
| 2026-09-11 | Day-1 scorecard — **NOT LOCKED**. Footnote only. Does **not** count toward ≥5 healthy days |
| 2026-09-14 | **Gap day** — no scorecard. **No backfill** |
| 2026-09-15 | **Gap day** — no scorecard. **No backfill** |
| **2026-09-16** | Scorecard email **restored**. **≥5-day clock restarts here** |
| After ≥5 healthy days | Fill numeric kill boxes; only then consider Learning Loop handoff |
| After envelope + seed + parent BENCHED | PROMOTE A (not B) |

Handoff to Learning Loop requires **≥5 healthy scorecard days**. Do not start that handoff on a thin or gapped sample.

---

## Paper-only constraints

- No strategy implementation in this PR. No live client. No crypto. No new options.
- Do not lift `block_shorts`. Do not raise `DAILY_TRADE_CAP`.
- Do not PROMOTE B (PARKED). Do not treat C as alpha.
- Do not seed-and-PROMOTE in the same change. Seed `{ "active": false }` first.
- Do not lock numeric boxes from Day-1 or from gap days.

---

## Out of scope

- Spec B / C implementation
- CryptoAgent
- New options structures
- Improver auto-apply
- Friday learner retune
- Always-on ship / PROTECTING A
