# Spec C — News / Premarket quality (paper)

**Status:** DRAFT — research HOLD. **No implementation in this PR.** Paper Alpaca only. **No crypto.**

**Contract:** `ROTATION-CONTRACT.md`. **Vocabulary:** **BENCHED / PROMOTED / REACTIVATED / FLAG.** Do **not** use KEEP/DISABLE.

**Path:** `PremarketAgent` is **not** PROTECTED — **BENCHED** → **PROMOTED** `PremarketAgent_strict`. `NewsAgent` **is** PROTECTED — **cannot** BENCH; quality is **in-place**.

---

## 1. Why this exists

`PremarketAgent` is a **ghost / bad-open** source (Ops). `NewsAgent` is **PROTECTED** and also a ghost source — **cannot** be BENCHED, so quality cannot ship as `NewsAgent_quality` via `_find_replacement`.

---

## 2. Two sleeves, two rotation rules

### 2.1 Premarket — BENCHED → PROMOTED

| Field | Value |
|---|---|
| **Roster name** | `PremarketAgent_strict` |
| **`AGENT_VARIANTS` parent** | `"PremarketAgent": ["PremarketAgent_strict"]` |
| **Seed** | `agent_summary.json` `{ "active": false }` — required. Missing-from-summary is **not** a promote (Ops PR #3) |
| **Promote-in** | When `PremarketAgent` is **BENCHED** and this name is already seeded `active: false` |
| **After 3 days** | Parent **REACTIVATED**. Both may run. Expected 3-day window, not a permanent replace. |
| **`PROTECTED_AGENTS`** | **No** |

### 2.2 News — in-place (PROTECTED)

| Field | Value |
|---|---|
| **Roster name** | Still `NewsAgent` |
| **Cannot** | BENCH `NewsAgent`; **PROMOTED** `NewsAgent_quality` **not** the path |
| **Can** | **FLAG** in `docs/logs/agent_report.md`; Ops-gated **in-place** patch |
| **`PROTECTED_AGENTS`** | **Yes** |

If Ops later **unprotects** News, a `NewsAgent_quality` **PROMOTED** variant is a follow-on spec, not this draft.

---

## 3. PremarketAgent_strict — intended behavior (research)

Same 04:00–09:30 ET window as `premarket_agent.py`. **Intended** (not coded):

- Require **volume** (or spread) vs thin prints — exact cutoff **TODO** (Ops scorecard).
- Skip **first minutes** after the print if opens are systematically bad — exact skip **TODO**.
- Cap **new names per morning** — exact cap **TODO**.
- Same `agent_risk_bridge` 7 gates + `MAX_POSITIONS`.

**Does not:** assume `get_agent_adjustment("PremarketAgent_strict")` exists or is called.

---

## 4. News in-place — intended behavior (research)

`news_agent.py` today: `NEWS_API_KEY`, Finnhub, RSS, `NEWS_LOOKBACK_HOURS=24`, `NEWS_MIN_ARTICLES=2`, **same** `confidence` for all tickers in a `direction` bucket.

**Intended** (not coded): **one ticker per `direction` per tick**; **source-weight** headlines; **do not** fire on a single RSS item — exact article floor **TODO**.

Still **PROTECTED**. Still **FLAG**-able. Still **cannot** be BENCHED.

---

## 5. Qualitative kill / remain-active / FLAG (numeric TODO)

**Kill** in this contract = **BENCHED** (Premarket family) or **FLAG without BENCH** (News). **Numeric boxes = TODO.** Do **not** invent ghost-rate, article-floor, or weight cutoffs from Day-1.

### PremarketAgent_strict

**Remain active (do not BENCH) while, qualitatively:**

- Vs **BENCHED sibling:** ghost / bad-open **rate** is **better** than parent `PremarketAgent` in the same window.
- Vs **SPY:** sleeve **does not uniquely** cause the book to **underperform SPY** on days Premarket is the ghost source.
- Vs **regime:** in **BULL / SIDEWAYS**, opens that **survive the first print** are not systematically faded vs SPY.

**FLAG when, qualitatively:**

- Ghost / bad-open **rate** is **worse** than parent or book average (evaluator window).
- Sleeve **P&L is negative** and **worse than book average** with enough trades (existing evaluator: 20d / 20% / ≥10 trades — **code today**, not a new Day-1 cutoff).

**BENCH when:** FLAG **and** not PROTECTED — then **PROMOTED** next seeded `active: false` variant if any; else empty roster slot until **REACTIVATED**.

### NewsAgent (in-place)

**Remain as PROTECTED while, qualitatively:**

- Quality patch **reduces** ghost rate vs pre-patch News.
- News is **not** the unique SPY-lag driver after the patch.

**FLAG when, qualitatively:** ghost rate or P&L vs book still **worse than average** after the patch.

**Cannot BENCH.** Improver cannot retire News or skip REACTIVATED.

> **Day-1 footnote (2026-09-11) — NOT LOCKED / pending several market days.** Premarket **1.15 / $0** with **entries 0** is **not** Spec C evidence (not a quality FLAG, not a promote). Bot vs SPY (1d −0.07% | 20d −6.92% vs −1.75% | since −17.71% vs +3.30%) is ensemble context only. Today rotator events = **none**. `MomentumAgent` already benched is not a C parent. Revisit numeric boxes after ≥5 market days. See [ROTATION-CONTRACT.md](ROTATION-CONTRACT.md).

---

## 6. Promotion path (Premarket only)

```
FLAG PremarketAgent (evaluator)
  → BENCHED 3 days
  → PROMOTED PremarketAgent_strict (first seeded active: false AGENT_VARIANTS child; must already be in agent_summary.json)
  → after BENCH_DAYS: PremarketAgent REACTIVATED
  (expected: parent returns; strict sleeve may stay active unless FLAG fires again)
```

News: **no** this path.

---

## 7. Implementation HOLD

Do **not** add `PremarketAgent_strict`, `AGENT_VARIANTS` row, or News patches in this PR.

**Ship order (later):** (1) `PremarketAgent_strict` + variants row + **seed** `agent_summary.json` `{ "active": false }` — **PROMOTED** only after Premarket **BENCHED**. Missing-from-summary never promotes (Ops PR #3). (2) News in-place — Ops + PROTECTED review. (3) Do **not** wait on Improver or unused `get_agent_adjustment`.

---

## 8. Out of scope

Options, crypto, RegimeEquity, ShortMeanReversion, `strategy_learner` conf deltas, Improver as actuator.
