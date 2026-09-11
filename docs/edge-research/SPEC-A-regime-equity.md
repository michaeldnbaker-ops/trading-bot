# SPEC-A — Regime-aware equity long/short (replace bleeders)

**Ops gap A (do first).** Paper Alpaca only. **Do not implement in this PR.**  
**Not options. Not crypto.**

---

## Hypothesis

The paper book is ~**−18% vs SPY up**. Ops names the chronic bleeders as **OptionsFlowAgent** and **TechnicalAgent**, with **BreakoutAgent** and **SectorRotationAgent** in the rotation-guide bleeder set.

Repo facts that match that diagnosis:

- `TechnicalAgent` has **no** `regime_affinity` / `regime_aversion`. It scores 5-minute RSI/MACD/BB/SMA and emits L/S at 0.55. Rotator already stripped its PROTECTED status (9% win-rate note in `agent_rotator.py`).
- `OptionsFlowAgent` is a yfinance P/C–IV **proxy**, not flow. It labels `instrument_type: options` and is in the path that put **paper calls** on XLE/SBUX/F. Ops has paused new options ideas; this spec **replaces the equity decision**, it does not add another options product.
- `BreakoutAgent` already averse to `HIGH_VOL` (`deep_research.py`: −$28/trade, PF 0.85) but remains a long-breakout voice in a book that `signal_research.py` ranked **below** buy-the-dip.
- `SectorRotationAgent` longs leaders / shorts laggards with **no HIGH_VOL** affinity; its shorts are then crushed by the ensemble bear-gate or leak as lagging-sector longs in the wrong tape.
- `MeanReversionAgent` is the one measured long add (PF ~1.67–1.68, 19/22 years) and is **half-wired** (not in `DEFAULT_WEIGHTS`).

**Claim:** one **equity** regime switcher — long only the rules that already won in-repo *in bull/neutral*, short only the rules that won in-repo *in BEAR/HIGH_VOL* — can **rotate in** after Technical / OptionsFlow / Breakout / SectorRotation are benched, compete for the daily cap of 3, and be **KEEP / BENCH / DISABLE**’d vs SPY on a stated clock.

This is a **variant**, not a 17th always-on equal-weight voice. Shared lifecycle: [ROTATION-CONTRACT.md](ROTATION-CONTRACT.md). Assume learn/rotate/weight work after Ops PR `bc-652b78ab`.

**If both A and B ship:** A is **long-only** while live; B owns fade-rally shorts (see SPEC-B). Do not double-fire the same short rule.

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

## MetaAgent / rotator — activate and deactivate

**Default: cold.** `RegimeEquityAgent` is in `Ensemble.agents` and `DEFAULT_WEIGHTS` but `agent_summary.json` ships `{ "active": false }`. Ensemble skips it every tick until promotion. MetaAgent must **not** treat a never-traded key as weight 1.0 — unproven live weight = `MIN_AGENT_WEIGHT` until 10 closed attributed trades (then ledger power curve).

**Activate (rotate in)** — rotator PROMOTES this name when it benches a sibling. First-choice mappings (replace today’s bleeder↔bleeder lists):

| Failing sleeve (benched) | `AGENT_VARIANTS` first substitute |
|---|---|
| TechnicalAgent | **RegimeEquityAgent** (then MomentumAgent) |
| OptionsFlowAgent | **RegimeEquityAgent** (then SentimentAgent — not News as a trade source) |
| BreakoutAgent | **RegimeEquityAgent** (then MeanReversionAgent) |
| SectorRotationAgent | **RegimeEquityAgent** |

Do **not** list Technical or OptionsFlow as substitutes *of* RegimeEquityAgent (avoids v1.4-style resurrection).

**Regime while live (MetaAgent, every tick):**

| Detector set | MetaAgent | Agent emit |
|---|---|---|
| BULL_TREND or NEUTRAL, not HIGH_VOL | Affinity boost on **longs** | Longs only |
| BEAR_TREND or HIGH_VOL | Affinity boost on **shorts** if A still owns shorts; else silent on shorts (SPEC-B) | No bull-tape shorts |
| Intersection with `regime_aversion` | Weight × (1 − REGIME_PENALTY), floor 0.20 | Prefer `[]` rather than fighting the penalty |
| Benched `active: false` | No signals, no slot | Skip |

**Deactivate:**

| Path | Trigger | State |
|---|---|---|
| BENCH | Evaluator 20d flag (negative and >20% worse than ensemble avg, ≥10 trades) | `active: false`, `benched_at=now`, eligible to return after `BENCH_DAYS` |
| DISABLE | Kill table below | `benched_at=2099-01-01` (Improver). **Not** a `_find_replacement` candidate. Ops re-enables |
| Weight mute | 20d closed P&L ≤ 0 after 10 trades | Stay active but `MIN_AGENT_WEIGHT` — not equal say |

Friday learner may nudge this name’s `confidence_threshold_delta` only. It must not set `active: true` or lift `block_shorts`.

PROTECTED agents are never benched to make room for A.

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
- **Not PROTECTED.** Do not add to `PROTECTED_AGENTS`.
- `DEFAULT_WEIGHTS["RegimeEquityAgent"] = 1.0` is only the **dict key** (so P&L is counted). Live weight starts at `MIN_AGENT_WEIGHT`. Also add `MeanReversionAgent` to `DEFAULT_WEIGHTS` if it stays (plumbing).
- `AGENT_VARIANTS` as in the activate table above.

---

## KEEP / BENCH / DISABLE

Clock starts on first **paper fill** after promotion. Broker = money, ledger = attribution. Not win rate.

| Verdict | Vs SPY (`report_data` 20d `edge`, and sleeve $ vs SPY $ on the same 0.5% risk budget) | Vs existing agents | Action |
|---|---|---|---|
| **KEEP** | 20d edge ≥ 0 while SPY 20d ≥ 0, **or** sleeve $ > 0 in a window where SPY 20d &lt; 0 | 20d P&amp;L **better** than the benched sibling(s) (Technical and/or OptionsFlow and/or Breakout) over the same dates; trade-day overlap with those siblings’ *would-have* signals &lt; 60% | Stay `active: true` |
| **BENCH** | Evaluator flag: 20d P&amp;L negative and &gt;20% worse than ensemble avg, ≥10 trades | Worse than ensemble avg but not worse than the benched bleeder (inconclusive replace) | 3-day rest; may rotate in again |
| **DISABLE** | 20d sleeve edge &lt; 0 **and** SPY 20d &gt; 0 after ≥10 closed trades (same “SPY up, we down” as the −18% book) | 20d P&amp;L **worse** than benched Technical+OptionsFlow combined, **or** ≥60% symbol-session overlap with them (rename, not replace), **or** any bull-tape short fill, **or** stop/risk cap violated | `benched_at=2099-01-01`; not a replacement candidate until Ops |

Do **not** KEEP solely because the ensemble got quieter. Must beat SPY **or** beat the replaced bleeder — preferably both.

---

## How to measure vs SPY and vs existing agents

**Vs SPY:** `report_data` 20d and since-start `edge`. Sleeve-level: sum attributed realized+unrealized / dollar-days risked vs buying SPY with the same 0.5% risk on entry days. Target: 20d edge ≥ 0 while SPY ≥ 0, or positive $ in SPY-down weeks.

**Vs bleeders:** 20d P&amp;L vs Technical + OptionsFlow (the replaced set). Must be better, not merely different.

**Vs keepers:** Jaccard overlap of trade days vs MeanReversion (longs should be similar *on dip days* — that is OK if Technical is gone) and vs ShortMomentum (shorts should be **fade-rally**, not breakdown — overlap &lt; 50%).

**Replay before paper:** run `bb_reversion_long` / `rsi_oversold_long` / `short_rally_downtrend` through production ATR-cap 4% **with the live short gate**. If shorts are not gated, do not ship (that is how the book already died).

---

## Paper-only constraints

- No live client. No crypto. No new options.
- Do not raise `DAILY_TRADE_CAP` to give this agent room.
- DISABLE does not auto-lift after 3 days. Do not re-promote Technical as A’s substitute.
- Later **options** variants of this sleeve (Ops D) stay cold until D is lifted; same rotate-in contract.

---

## Out of scope

- CryptoAgent
- New options structures / putting XLE-SBUX-F “right”
- Lifting short gate
- Always-on ship at weight 1.0
- PROTECTING this agent on day one
