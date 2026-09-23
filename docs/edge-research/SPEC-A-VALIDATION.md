# SPEC-A validation — RegimeEquity design + kill criteria

**Updated: 2026-09-23 — Edge Research / CoS prep clear (seed not applied).**

**HOLD. DO NOT APPLY. Do not seed into the ensemble.** Paper-only. No live. No crypto. No strategy code. Spec B **PARKED** — do **not** PROMOTE B. Spec C = **quality filter only, not alpha**.

CoS / Learning Loop cleared filing an `active: false` **prep artifact only**: [SPEC-A-SEED-PREP.md](SPEC-A-SEED-PREP.md). **Still HOLD:** no PROMOTE, no weight-test, do **not** apply the seed into live `agent_summary.json` or the ensemble. A separate CoS greenlight is required before any write.

Sleeve definition (entry, exits, parents): [SPEC-A-regime-equity.md](SPEC-A-regime-equity.md). Rotator words: [ROTATION-CONTRACT.md](ROTATION-CONTRACT.md). Exact cold-seed JSON (prep, not applied): [SPEC-A-SEED-PREP.md](SPEC-A-SEED-PREP.md).

---

## Status

| Item | Lock |
|---|---|
| Spec A implementation | **HOLD** — validation envelope only; **no strategy code** |
| Seed into ensemble / live `agent_summary.json` | **DO NOT APPLY** — prep artifact filed 2026-09-23; **not written** |
| PROMOTE / weight-test | **NO PROMOTE. No weight-test.** Blocked until separate CoS greenlight **and** parent BENCHED |
| Spec B implementation | **PARKED** — do not code, do not PROMOTE |
| Spec C | **Quality filter only** — not an alpha sleeve |
| Day-1 scorecard (2026-09-11) | **NOT LOCKED** — ensemble context, not FLAG math |
| ≥5 healthy `[PAPER]` EODs | **5/5 banked** (LL authoritative: Sep 16, 17, 18, 21, 22). Clock restarted 2026-09-16. Sep 14–15 gap days, no backfill |
| Spec A trade count | **0** — banked EODs are book context for kill numerics, not Spec A expectancy |
| Numeric kill boxes | **Provisional** (filled 2026-09-23 from the 5/5 window). Re-fill from Spec A paper trades after seed — seed still **not applied** |
| Cold PROMOTE | **Forbidden** — seed `{ "active": false }` first, and that seed is **not applied** by this doc |

---

## What this file is

The **validation + kill** contract for `RegimeEquityAgent` (Spec A). It does **not** add a 17th always-on voice, does **not** ship strategy code, and does **not** seed the ensemble.

**Primary metric:** expectancy **after costs**.  
**Secondary:** vs SPY (`report_data` edge). Win rate is not a pass/fail (4% stop cap).

**Regime (locked definition):** **direction × vol**, **point-in-time prior close**. Use only information known at the previous session close. Do **not** classify on the same-day print. Do **not** invent a second detector — map `RegimeDetector` labels onto this product (BULL / BEAR / NEUTRAL × HIGH_VOL / not-HIGH_VOL). `LOW_VOL` alone is not an A entry regime (SPEC-A v1).

---

## Path to PROMOTE (Learning Loop)

1. Mini backtest (expectancy after costs, DD, trade count)
2. Walk-forward (rolling IS→OOS, purged); majority-pass + catastrophic veto
3. Locked OOS holdout (no retune)
4. Paper envelope: ≥30–50 Spec A trades **or** ≥5 healthy `[PAPER]` EODs after 2026-09-16 restore — **5/5 banked** (LL authoritative: Sep 16, 17, 18, 21, 22). Spec A trade count is still **0**
5. Seed `agent_summary.json`: `{ "active": false }` — **NOT APPLIED** (prep artifact only; needs separate CoS seed-write greenlight). **Do not seed into the ensemble**
6. PROMOTE only when parent **BENCHED**: TechnicalAgent (A first) or OptionsFlowAgent (A only). Do **not** PROMOTE Spec B. **NO PROMOTE yet.** No weight-test

---

## Banked healthy EODs (context for kill numerics; Spec A trade count = 0)

| Date | Subject | Equity | Day P&L | vs SPY 1d edge | Entries | Notes |
|---|---|---|---|---|---|---|
| 2026-09-16 | Market day | $78,467 | +0.26% | (restore day) | — | Full 16:35 ET; AM restore ping excluded |
| 2026-09-17 | Market day | $79,436 | +1.13% | ~0.00% | 3 | All leaf $0 that day |
| 2026-09-18 | Market day | $79,496 | −0.07% | +0.05% | 3 | |
| 2026-09-21 | BluSterling Daily | $79,587 | +0.03% | −1.52% | 3 | OptionsFlow FLAG |
| 2026-09-22 | BluSterling Daily | $79,949 | +0.44% | +0.45% | 6 | OptionsFlow benched; crypto opens that day (later flattened) |

Window equity $78,467 → $79,949 (+$1,482). Book since-start still ~−20% vs SPY edge ~−25% (book context, not Spec A expectancy).

Post-bank: 2026-09-23 Daily $79,422 (−0.89%); no new crypto opens; ETH −$45 residual flatten. session_gates on VM (`fa605bd` / PR#10). Ledger SOL/BTC ghosts reconciled (Ops/LL).

Day-1 (2026-09-11) stays **NOT LOCKED** and does not count. Sep 14–15 stay **gap days** — no backfill.

---

## Pipeline (in order; do not skip)

```
mini backtest
  → walk-forward (rolling, purged)
  → locked OOS
  → paper envelope
       (≥30–50 trades  OR  ≥5 healthy scorecard days)  ← 5/5 EODs banked; Spec A trades = 0
  → seed agent_summary { "active": false }             ← PREP ONLY — DO NOT APPLY
  → PROMOTE only when Technical or OptionsFlow is BENCHED   ← NO PROMOTE
```

No cold PROMOTE. Missing-from-summary is not a promote (Ops PR #3 `_find_replacement`). Improver cannot apply. Friday `get_agent_adjustment` is unused — do not retune from learner JSON. **Do not seed into the ensemble from this doc.**

### 1. Mini backtest

Replay in-repo rules only (`bb_reversion_long` / `rsi_oversold_long` / `short_rally_downtrend`) through **production** ATR×1.5 **4% cap** and the **live short gate** (silent unless BEAR/HIGH_VOL). Paper-cost model: commissions + spread + the 4% stop as a real exit. Universe = MeanReversion liquid names ($5 / 500k vol). No 3x ETFs. No crypto. No options.

**Pass to walk-forward (qualitative until Spec A sample exists):** after-cost expectancy not obviously ≤ 0 on the in-sample slice; regime tags actually change side; no bull-tape short fills.

### 2. Walk-forward (rolling, purged)

Rolling windows. **Purge** the embargo around each fold so labels / regime at *t* cannot leak from *t+1* bars or same-day close. Regime on every bar = **prior close** only (direction × vol). Do **not** refit thresholds inside a test fold. Do **not** use Day-1 (2026-09-11) or gap days (Sep 14–15) as a fold.

**Pass to locked OOS:** after-cost expectancy > 0 **across** folds (not one lucky window). Wrong-regime slices must be flat or sized-down (see kill structure). Majority-pass + catastrophic veto.

### 3. Locked OOS

One **held-out** slice, frozen **before** looking at it. No parameter edits after unblinding. Same cost model, same prior-close regime, same short gate.

**OOS expectancy ≤ $0 after costs → kill** (provisional floor below; inequality was already committed).

### 4. Paper envelope

Paper Alpaca only. `PAPER_TRADING=true`. Envelope is **observation**, not a live sleeve, until PROMOTE. **NO PROMOTE.**

**Minimum sample (either):**

- **≥30–50 attributed Spec A trades** — **not met** (Spec A trade count = 0), **or**
- **≥5 healthy scorecard days** — **5/5 banked**

**Healthy scorecard day** = a session whose daily scorecard email arrived and is usable (ticks / attribution / vs-SPY present). **Sep 14–15 are gap days — do not count, do not backfill.** Day-1 (2026-09-11) is a **NOT LOCKED** footnote and does **not** start this clock. Clock **restarted 2026-09-16** (email restored). Learning Loop has the 5/5. That does **not** authorize seed apply, weight-test, or PROMOTE.

### 5. Seed `{ "active": false }` — PREP ONLY, DO NOT APPLY

Before any rotator cycle can see A, the live summary must contain an explicit cold row. **That write is not authorized.** The filed shape lives in [SPEC-A-SEED-PREP.md](SPEC-A-SEED-PREP.md). Short form (do **not** copy into `agent_summary.json` from this PR):

```json
{ "RegimeEquityAgent": { "active": false } }
```

**`active` must be false.** Seed first, on a later CoS greenlight. **Then** wait for a parent **BENCHED**. No cold PROMOTE. Roster presence alone is not enough. **Do not seed into the ensemble. Do not activate.**

### 6. PROMOTE (A only; B PARKED) — NOT AUTHORIZED

**PROMOTED** only when rotator **BENCHED** a listed parent **and** A is already seeded `active: false`. **Neither condition is cleared for action. NO PROMOTE. No weight-test.**

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

Costs in the primary metric: commission + spread + stop/trail as taken. Do not report pre-cost expectancy as the headline. Provisional friction model for the kill floor: **10 bps round-trip + $2 floor**.

---

## Kill criteria (provisional — fill from Spec A paper trades when seeded)

**Inequalities stay locked. Numbers below are provisional** from the banked book window (Spec A trade count = 0). They are **not** a promote path and **not** a license to seed. Do **not** retune from Day-1 (2026-09-11) or from Sep 14–15 gap days. When Spec A paper trades exist, replace these boxes from that sample.

| # | Kill / FLAG | Provisional number | Source |
|---|---|---|---|
| 1 | **OOS / rolling paper expectancy after costs** | Kill if ≤ **$0** after costs at **N ≥ 20** Spec A trades | LEARNINGS; friction = 10 bps RT + $2 floor |
| 2 | **Sleeve drawdown budget** | Kill / size-down if Spec A sleeve DD > **3% of equity** (~$2,400 at $80k) or > **1.5×** mini-backtest DD | Book window max day −0.89% (Sep 23); keep tight until sample |
| 3 | **Paper vs backtest divergence** | Kill if paper expectancy diverges > **~25%** from locked OOS | Profit mandate |
| 4 | **Wrong-regime trades** | Any Spec A fill outside allowed direction × vol (prior-close) → flat/size-down; repeat → FLAG | Spec A design |
| 5 | **Hygiene (not alpha)** | Elevated weight + ~0 entries / high fetch-404 → **FLAG hygiene**, not a promote path | Sep 23: 68 fetch/404s book-wide |

Detail that stays with the structure:

1. After-cost OOS expectancy **≤ $0** → **kill** (do not PROMOTE; if already live, evaluator **FLAG**). Sample floor **N ≥ 20** Spec A trades.
2. Sleeve peak-to-trough **> 3% of equity** (~$2,400 at $80k) **or > 1.5×** mini-backtest DD → **kill** / size-down.
3. \|paper expectancy − locked-OOS expectancy\| / \|OOS\| **> ~25%** → **kill** / **FLAG** (do not “tune” via unused Friday deltas).
4. Wrong cell of **direction × vol** (prior close) → **no new entries** and **flat or size-down**. Any **short fill in BULL without HIGH_VOL** is a gate bug → **FLAG**. Repeat wrong-regime fills → FLAG.
5. MetaAgent / `DEFAULT_WEIGHTS` **elevated** while attributed **entries ≈ 0**, or a high fetch-404 count, → **FLAG hygiene**. Not “quiet success” and not a promote path.

**Remain-active (qualitative, unchanged):** vs SPY the sleeve is not another always-on long book while SPY is down; vs BENCHED sibling it is better and not a clone; vs regime, longs in BULL/NEUTRAL (not HIGH_VOL), silent on bull-tape shorts.

Do not treat “ensemble got quieter” as success vs SPY. Zero fills in a bull-tape short book = gate working for shorts, **but** elevated weight + entries ≈ 0 on the **live long** sleeve is still row 5.

---

## Handoff checklist to Learning Loop

- [x] ≥5 healthy scorecard days (LL 5/5)
- [x] Validation doc filled with provisional numbers from those days
- [x] Seed checklist prep ready ([SPEC-A-SEED-PREP.md](SPEC-A-SEED-PREP.md)) — **artifact only**
- [ ] `active: false` seed present for RegimeEquityAgent — **blocked until CoS seed greenlight; DO NOT APPLY; do not seed into the ensemble**
- [ ] Weight-test — **blocked until separate CoS greenlight**
- [ ] Parent bleeder BENCHED (Technical or OptionsFlow) before PROMOTE
- [ ] CoS notified; no cold PROMOTE — **NO PROMOTE yet**

---

## Scorecard clock (2026-09-16)

| Date | What it is |
|---|---|
| 2026-09-11 | Day-1 scorecard — **NOT LOCKED**. Footnote only. Does **not** count toward ≥5 healthy days |
| 2026-09-14 | **Gap day** — no scorecard. **No backfill** |
| 2026-09-15 | **Gap day** — no scorecard. **No backfill** |
| **2026-09-16** | Scorecard email **restored**. **≥5-day clock restarts here** |
| 2026-09-16, 17, 18, 21, 22 | **5/5 healthy EODs banked** (LL) |
| 2026-09-23 | Post-bank Daily. Provisional kill numerics filed. Seed prep filed. **Seed not applied** |
| After envelope + **applied** seed + parent BENCHED | PROMOTE A (not B) — **not cleared** |

---

## Paper-only constraints

- No strategy implementation in this PR. No live client. No crypto. No new options.
- **Do not** edit live `agent_summary.json`. **Do not** seed RegimeEquityAgent into the ensemble. **Do not** set `active: true`.
- Do not lift `block_shorts`. Do not raise `DAILY_TRADE_CAP`.
- Do not PROMOTE B (PARKED). Do not treat C as alpha.
- Do not seed-and-PROMOTE in the same change. Seed `{ "active": false }` first — and only after a separate CoS apply greenlight.
- Provisional kill numbers are book-window context. Spec A expectancy is unmeasured (0 Spec A trades).

---

## Out of scope

- Applying the seed or activating RegimeEquityAgent
- Weight-test or PROMOTE
- Spec B / C implementation
- CryptoAgent
- New options structures
- Improver auto-apply
- Friday learner retune
- Always-on ship / PROTECTING A
