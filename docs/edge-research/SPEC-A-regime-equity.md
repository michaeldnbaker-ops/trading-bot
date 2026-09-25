# SPEC-A — Regime-aware equity long/short (3-day BENCHED window)

**CoS / Learning Loop lock (2026-09-16): single focus.** Validation envelope + provisional kill numerics live in [SPEC-A-VALIDATION.md](SPEC-A-VALIDATION.md) (**5/5** healthy EODs banked 2026-09-23; Spec A trades = 0). Cold-seed JSON is a **prep artifact only** — [SPEC-A-SEED-PREP.md](SPEC-A-SEED-PREP.md). **DO NOT APPLY. Do not seed into the ensemble. NO PROMOTE. No weight-test.** Spec B is **PARKED** (do not PROMOTE B). Spec C is **quality-filter-only, not alpha**. Day-1 (2026-09-11) is **NOT LOCKED**. ≥5-day healthy-scorecard clock **restarted 2026-09-16** (Sep 14–15 gap days, no backfill) and is **5/5 banked**.

**Ops gap A (do first).** Paper Alpaca only. **Do not implement in this PR.**  
**Not options. Not crypto.** “Replace bleeders” lasts `BENCH_DAYS = 3` then **REACTIVATED** — expected, not a reject.

---

## Hypothesis

The paper book is ~**−18% vs SPY up**. Ops names the chronic bleeders as **OptionsFlowAgent** and **TechnicalAgent**, with **BreakoutAgent** and **SectorRotationAgent** in the rotation-guide bleeder set.

Repo facts that match that diagnosis:

- `TechnicalAgent` has **no** `regime_affinity` / `regime_aversion`. It scores 5-minute RSI/MACD/BB/SMA and emits L/S at 0.55. Rotator already stripped its PROTECTED status (9% win-rate note in `agent_rotator.py`).
- `OptionsFlowAgent` is a yfinance P/C–IV **proxy**, not flow. It labels `instrument_type: options` and is in the path that put **paper calls** on XLE/SBUX/F. Ops has paused new options ideas; this spec **replaces the equity decision**, it does not add another options product.
- `BreakoutAgent` already averse to `HIGH_VOL` (`deep_research.py`: −$28/trade, PF 0.85) but remains a long-breakout voice in a book that `signal_research.py` ranked **below** buy-the-dip.
- `SectorRotationAgent` longs leaders / shorts laggards with **no HIGH_VOL** affinity; its shorts are then crushed by the ensemble bear-gate or leak as lagging-sector longs in the wrong tape.
- `MeanReversionAgent` is the one measured long add (PF ~1.67–1.68, 19/22 years) and is **half-wired** (not in `DEFAULT_WEIGHTS`).

**Claim:** one **equity** regime switcher — long the in-repo dip rules in bull/neutral, short only in BEAR/HIGH_VOL if B is not shipped — is **PROMOTED** when Technical / OptionsFlow / Breakout / SectorRotation is **BENCHED**. A **owns OptionsFlow** (B does not share that parent). On Technical, A is **first** ordered variant, B is **second**. Not a 17th always-on equal-weight voice.

Shared lifecycle and exclusive parent map: [ROTATION-CONTRACT.md](ROTATION-CONTRACT.md). Improver cannot apply this spec. Friday learn does not retune it until `get_agent_adjustment` is wired.

**If both A and B ship:** A is **long-only** while live; B owns fade-rally shorts. Do not double-fire the same short rule.

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

**Caps:** max 2 signals/tick **when live** (daily cap is already 3 ensemble-wide).

---

## MetaAgent / rotator — FLAG / BENCHED / PROMOTED / REACTIVATED

**Default: cold.** `RegimeEquityAgent` is in `Ensemble.agents` and `DEFAULT_WEIGHTS`. **Must seed** `agent_summary.json` `{ "active": false }`. Missing-from-summary is **not** a promote (Ops PR #3 `_find_replacement`). Ensemble skips it until **PROMOTED**.

**PROMOTED in** — only `agent_rotator`, same cycle as **BENCHED** on a non-PROTECTED bleeder. `_find_replacement` **PROMOTED** only the **first seeded `active: false`** name in the parent’s list. A and B must not both sit first on the same parent (see contract ownership table).

| Bleeder **BENCHED** (after evaluator **FLAG**) | Ordered `AGENT_VARIANTS` (first seeded `active: false` is **PROMOTED**) |
|---|---|
| TechnicalAgent | **RegimeEquityAgent (A), then ShortMeanReversionAgent (B)**, then Momentum / Breakout |
| OptionsFlowAgent | **RegimeEquityAgent (A) only** as the new sleeve, then News / Sentiment. **B is not on this list.** |
| BreakoutAgent | **RegimeEquityAgent (A)**, then Momentum / Technical |
| SectorRotationAgent | **RegimeEquityAgent (A)**, then Premarket |

Do **not** list Technical or OptionsFlow as substitutes *of* RegimeEquityAgent (v1.4 resurrection). PROTECTED agents are never BENCHED to make room for A. Do **not** take VolatilityAgent or MoversAgent — those are **B’s** reserved empty on-ramps.

After `BENCH_DAYS` the bleeder is **REACTIVATED**. A may stay active; both can run. That 3-day “replace” is **expected rotator behavior**, not a failed replacement. Permanent off requires another **FLAG** (then **BENCHED** again). If Technical **FLAG**s again while A is already `active: true`, the first seeded `active: false` variant is **B** (B must already be in `agent_summary.json`).

**Regime while live (MetaAgent, every tick):**

| Detector set | MetaAgent | Agent emit |
|---|---|---|
| BULL_TREND or NEUTRAL, not HIGH_VOL | Affinity boost on **longs** | Longs only |
| BEAR_TREND or HIGH_VOL | Affinity on shorts only if B is not the short owner; else `[]` on shorts | No bull-tape shorts |
| Intersection with `regime_aversion` | Weight × (1 − REGIME_PENALTY) | Prefer `[]` |
| BENCHED (`active: false`) | No signals | Skip |

**If A is FLAG'd:** rotator **BENCHED** A for 3 days then **REACTIVATED**. There is no Improver off-switch.

Friday `learned_params.json` does **not** retune this sleeve until Ops wires `get_agent_adjustment`.

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
- **Not PROTECTED.**
- `DEFAULT_WEIGHTS` key for attribution. `AGENT_VARIANTS` as in the PROMOTED table (A first on Technical and sole new name on OptionsFlow). **Seed** `agent_summary.json` `{ "active": false }`.

---

## FLAG vs remain-active (qualitative; numeric kill = TODO)

**Authoritative kill structure:** [SPEC-A-VALIDATION.md](SPEC-A-VALIDATION.md) (primary = expectancy after costs; vs SPY secondary). **Provisional** numerics filed 2026-09-23 from the banked 5/5 book window (Spec A trade count = 0): kill if after-cost expectancy ≤ **$0** at **N ≥ 20**; sleeve DD > **3% of equity** (~$2,400 at $80k) or > **1.5×** mini-backtest DD; paper vs locked OOS divergence > **~25%**. Those boxes do **not** authorize seed, weight-test, or PROMOTE.

Clock for Spec A expectancy starts on first paper fill after **PROMOTED**. Broker = money, ledger = attribution. **NO PROMOTE. Do not seed into the ensemble** — seed prep is [SPEC-A-SEED-PREP.md](SPEC-A-SEED-PREP.md) only. Do **not** invent cutoffs from Day-1 (NOT LOCKED) or from Sep 14–15 gap days.

**Remain active (no rotator event) when, qualitatively:**

- Vs **SPY:** sleeve is not repeating “SPY up, book down.” In bull/neutral, attributed longs should not be the reason `report_data` 20d edge stays deeply negative. In BEAR/HIGH_VOL, if A still shorts, those shorts should not be the reason the book lags a falling SPY in the *wrong* direction (long into the dump).
- Vs **BENCHED sibling:** attributed P&amp;L better than the parent that was **BENCHED** to PROMOTE A over the same window; not a clone of Technical/OptionsFlow would-have signals; regime tags actually change side vs a no-regime Technical.
- Vs **regime:** longs in BULL/NEUTRAL (not HIGH_VOL); silent on bull-tape shorts.

**FLAG (evaluator → rotator may BENCHED A, then REACTIVATED after 3d) when, qualitatively:**

- Vs **SPY:** sleeve is another always-on long book while SPY is down, or another bleeder while SPY is up.
- Vs **BENCHED sibling:** worse than the sibling that was BENCHED to PROMOTE it, or same symbols/sessions as Technical 5m RSI / OptionsFlow P/C.
- Vs **regime:** any **short fill in BULL_TREND without HIGH_VOL** (gate bug — FLAG; do not “tune” via unused Friday deltas).

Do not treat “ensemble got quieter” as success vs SPY.

> **Day-1 footnote (2026-09-11) — NOT LOCKED / pending several market days.** Book: equity $82,288, day +0.78%. Bot vs SPY 1d edge −0.07% | 5d −1.83% vs −1.15% | 20d −6.92% vs −1.75% | since −17.71% vs +3.30%. **Ensemble context only**, not A FLAG math (ticks 71/~390, entries 0, almost all agent P&L $0; MetaAgent compound rows 1.00/$0 = attribution gap). Today FLAG/BENCHED/PROMOTED/REACTIVATED = **none**. `MomentumAgent` already `status=benched` is **not a today event** and not an A kill or new parent. A’s primary parents stay Technical / OptionsFlow / Breakout / SectorRotation. **≥5-day clock restarts 2026-09-16** (scorecard email restored; Sep 14–15 gap, no backfill). See [SPEC-A-VALIDATION.md](SPEC-A-VALIDATION.md) and [ROTATION-CONTRACT.md](ROTATION-CONTRACT.md).

---

## How to measure vs SPY and vs existing agents

**Vs SPY:** `report_data` 20d and since-start `edge`. Sleeve-level: sum attributed realized+unrealized / dollar-days risked vs buying SPY with the same 0.5% risk on entry days. Pass/fail **numeric** vs SPY = **TODO** (not locked from Day-1). Qualitative: sleeve should not be the reason the book lags SPY in the wrong direction.

**Vs bleeders:** 20d P&amp;L vs the sibling that was **BENCHED** to PROMOTE A. Must be better, not merely different.

**Vs remaining agents:** overlap vs MeanReversion on dip days is OK; shorts vs ShortMomentum should be fade-rally, not breakdown.

**Replay before any future implementation PR:** `bb_reversion_long` / `rsi_oversold_long` / `short_rally_downtrend` through production ATR-cap 4% **with the live short gate**.

---

## Paper-only constraints

- No live client. No crypto. No new options. **No strategy code in this PR.**
- Do not raise `DAILY_TRADE_CAP`.
- Do not list Technical as A’s first variant (REACTIVATED resurrection).
- Do not take VolatilityAgent / MoversAgent (B’s reserved on-ramps).
- Do not assume Friday learner retunes A.
- Must seed `agent_summary.json` `{ "active": false }` (Ops PR #3: missing-from-summary never **PROMOTE**s).

---

## Out of scope

- CryptoAgent
- New options structures
- Lifting short gate
- Always-on ship
- PROTECTING this agent
- Improver auto-apply
