"""
agent_evaluator.py — v2.1 (2026-09-20)
──────────────────────────────────────
Ranks leaf agents by P&L after paper friction. Ledger is the source of
truth. MetaAgent(...) wrappers are unwrapped before anything is judged.

CHANGE LOG (v2.1):
  • Leaf unwrap uses trade_ledger.expand_agent_names / leaf_agents so
    roster leaves get real P&L (wrapper rows used to hold the only
    non-zero entry).
  • FLAG / top-agent / averages use after-cost P&L and 20d
    expectancy-after-costs — the same numbers the scorecard and rotator
    PROMOTE path read.
  • N<10 absolute-drain FLAG: small samples still skip the relative
    20% test, but a severe after-cost drain can FLAG → BENCH.

CHANGE LOG (v2.0):
  • Replaced PerformanceLogger dependency with trade_ledger.
  • Agents are discovered dynamically from the ledger (no more stale
    hard-coded ENSEMBLE_AGENTS list — if it's traded, it's evaluated).
  • Each trade counts toward both its primary_agent and its contributors
    (consistent with trade_ledger.per_agent_attribution).
  • Output shape unchanged so agent_rotator.py works without modification.

Evaluation windows:
  • 5-day  rolling  — short-term momentum signal
  • 20-day rolling  — medium-term signal (primary rotation driver)
  • All-time        — lifetime context

Learning Loop thresholds (paper only — FLAG → rotator BENCH / PROMOTE):

  UNDERPERFORM_THRESHOLD = 0.20
      Relative FLAG when N≥10, 20d after-cost P&L is negative, and 5d
      or 20d after-cost P&L is >20% worse than the active-ensemble avg.

  MIN_TRADES_TO_EVALUATE = 10
      Relative FLAG needs ≥10 trades in the 20d window. Small-N books
      are usually noise (2026-08-12 benched winners at N=5–8).

  ABSOLUTE_DRAIN_MIN_TRADES = 2
  ABSOLUTE_DRAIN_FLAG_USD = -800
  ABSOLUTE_DRAIN_PER_TRADE_USD = -150
      Exception to the N<10 skip (MeanReversion/Technical soft-watch
      pattern: large 20d losses, N<10, previously never FLAG'd).
      FLAG when 20d after-cost P&L is worse than -$800 AND average
      after-cost P&L is worse than -$150/trade, with at least 2 trades.
      A single stop-out does not FLAG. Profitable small-N never FLAG.

  Paper friction (trade_ledger.ROUND_TRIP_COST_*):
      10 bps round-trip (5/side), $2 floor. Alpaca paper is $0
      commission; kill/PROMOTE still use after-cost numbers.

  MIN_ACTIVE_AGENTS = 2 lives in agent_rotator. Drain FLAGs are real;
      the rotator skips BENCH only at that floor / PROTECTED_AGENTS.
      No silent override of a drain FLAG.

Usage:
  from agent_evaluator import AgentEvaluator
  ev     = AgentEvaluator()
  report = ev.evaluate()
  print(report.summary_text())
  ev.save_report(report)         # writes logs/latest_eval.json
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import trade_ledger as _ledger

# ── Tuning knobs ────────────────────────────────────────────────────────────
UNDERPERFORM_THRESHOLD = 0.20   # 20 % below ensemble avg triggers flag
# An agent needs ≥ N trades before its P&L means anything. Raised 3 -> 10
# on 2026-08-12 after the rotator benched three PROFITABLE agents and
# flagged the best one in the book:
#   TechnicalAgent  +$2,132 over 5 trades  -> BENCHED
#   PremarketAgent    +$444 over 6 trades  -> BENCHED
#   CryptoAgent       +$433 over 8 trades  -> BENCHED
#   BreakoutAgent   +$3,328 over 6 trades  -> flagged to bench
#   NewsAgent       -$1,366 over 27 trades -> left active
# At 3-8 trades a 5-day P&L window is noise, and the rotator was making
# on/off decisions from it — switching off winners and keeping the one
# agent with a statistically real loss. 10 trades is still a low bar, but
# it is the point where a losing streak stops being a coin flip.
MIN_TRADES_TO_EVALUATE  = 10    # agent needs ≥ N trades before being judged
ROTATION_COOLDOWN_DAYS  = 2     # don't rotate same agent twice within N days

# N<10 absolute-drain FLAG (after costs). Both bars must fire.
# ~2.5× $320 risk ($800) total, and worse than -$150/trade so a few
# scratches around one stop do not FLAG. See module docstring.
ABSOLUTE_DRAIN_MIN_TRADES = 2
ABSOLUTE_DRAIN_FLAG_USD = -800.0
ABSOLUTE_DRAIN_PER_TRADE_USD = -150.0


# ── Data classes ────────────────────────────────────────────────────────────
@dataclass
class AgentStats:
    name:         str
    pnl_5d:       float = 0.0
    pnl_20d:      float = 0.0
    pnl_alltime:  float = 0.0
    pnl_5d_after_costs:  float = 0.0
    pnl_20d_after_costs: float = 0.0
    pnl_alltime_after_costs: float = 0.0
    cost_5d:      float = 0.0
    cost_20d:     float = 0.0
    cost_total:   float = 0.0
    expectancy_after_costs_5d:  Optional[float] = None
    expectancy_after_costs_20d: Optional[float] = None
    expectancy_after_costs:     Optional[float] = None
    trades_5d:    int   = 0
    trades_20d:   int   = 0
    trades_total: int   = 0
    wins_total:   int   = 0
    losses_total: int   = 0
    active:       bool  = True
    flagged:      bool  = False
    flag_reason:  str   = ""

    @property
    def win_rate(self) -> Optional[float]:
        closed = self.wins_total + self.losses_total
        if closed == 0:
            return None
        return round(self.wins_total / closed, 3)

    @property
    def avg_pnl_per_trade(self) -> Optional[float]:
        if self.trades_total == 0:
            return None
        return round(self.pnl_alltime / self.trades_total, 2)

    @property
    def avg_pnl_after_costs_20d(self) -> Optional[float]:
        if self.trades_20d == 0:
            return None
        return round(self.pnl_20d_after_costs / self.trades_20d, 2)


@dataclass
class EvalReport:
    generated_at:   str
    agents:         list[AgentStats]
    flagged_agents: list[str]     = field(default_factory=list)
    top_agent:      Optional[str] = None
    ensemble_avg_5d:  float = 0.0
    ensemble_avg_20d: float = 0.0

    def summary_text(self) -> str:
        lines = [
            f"=== Agent Performance Evaluation — {self.generated_at} ===",
            f"Ensemble avg P&L  │  5-day: ${self.ensemble_avg_5d:,.2f}  │  20-day: ${self.ensemble_avg_20d:,.2f}",
            "",
            f"{'Agent':<20} {'5d net':>10} {'20d net':>11} {'E[20d]':>9} {'All-Time':>11} {'Trades':>7} {'Win%':>7} {'Status':>10}",
            "─" * 96,
        ]
        for a in sorted(self.agents, key=lambda x: x.pnl_20d_after_costs, reverse=True):
            win_pct = f"{a.win_rate*100:.0f}%" if a.win_rate is not None else "—"
            status  = "⚠ FLAG" if a.flagged else ("✓ active" if a.active else "● bench")
            exp = (
                f"{a.expectancy_after_costs_20d:>+9.2f}"
                if a.expectancy_after_costs_20d is not None else f"{'—':>9}"
            )
            lines.append(
                f"{a.name:<20} {a.pnl_5d_after_costs:>+10,.2f} {a.pnl_20d_after_costs:>+11,.2f} "
                f"{exp} {a.pnl_alltime_after_costs:>+11,.2f} {a.trades_total:>7} {win_pct:>7} {status:>10}"
            )
            if a.flag_reason:
                lines.append(f"  └─ {a.flag_reason}")
        if self.top_agent:
            lines += ["", f"🏆  Top performer (20-day): {self.top_agent}"]
        if self.flagged_agents:
            lines += ["", f"⚠   Flagged for review: {', '.join(self.flagged_agents)}"]
        return "\n".join(lines)


# ── Helpers ─────────────────────────────────────────────────────────────────
def _today_et_date() -> datetime:
    """Naive datetime at midnight ET today (ledger timestamps are ET strings)."""
    et_now = datetime.now(_ledger.ET)
    return et_now.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)


def _trade_opened_dt(t) -> Optional[datetime]:
    """Parse a trade.opened_at_et string to naive datetime; return None if malformed."""
    try:
        return datetime.strptime(t.opened_at_et[:19], "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def _pnl_for_trade(t) -> float:
    """Use realized for closed trades, unrealized for open trades."""
    return t.realized_pnl if not t.is_open else t.unrealized_pnl


def _after_cost(pnl: float, cost: float) -> float:
    return round(float(pnl) - float(cost), 2)


def flag_reasons(
    st: AgentStats,
    avg_5d: float,
    avg_20d: float,
    *,
    min_trades: int = MIN_TRADES_TO_EVALUATE,
    underperform: float = UNDERPERFORM_THRESHOLD,
    drain_min_trades: int = ABSOLUTE_DRAIN_MIN_TRADES,
    drain_usd: float = ABSOLUTE_DRAIN_FLAG_USD,
    drain_per_trade: float = ABSOLUTE_DRAIN_PER_TRADE_USD,
) -> list[str]:
    """Return FLAG reasons for one active leaf. Empty = do not FLAG.

    Relative test (N≥min_trades) uses after-cost P&L vs ensemble averages.
    Absolute-drain test (N<min_trades) is the MeanReversion/Technical
    exception: severe after-cost 20d losses still FLAG.
    """
    if st.pnl_20d_after_costs > 0:
        return []

    reasons: list[str] = []
    n20 = st.trades_20d
    if n20 >= min_trades:
        if avg_5d > 0:
            if st.pnl_5d_after_costs < avg_5d * (1 - underperform):
                reasons.append(
                    f"5-day after-cost P&L ${st.pnl_5d_after_costs:+,.2f} is "
                    f">20% below ensemble avg ${avg_5d:+,.2f}"
                )
        elif avg_5d < 0:
            if st.pnl_5d_after_costs < avg_5d * (1 + underperform):
                reasons.append(
                    f"5-day after-cost P&L ${st.pnl_5d_after_costs:+,.2f} is "
                    f">20% worse than ensemble avg ${avg_5d:+,.2f}"
                )

        if avg_20d > 0:
            if st.pnl_20d_after_costs < avg_20d * (1 - underperform):
                reasons.append(
                    f"20-day after-cost P&L ${st.pnl_20d_after_costs:+,.2f} is "
                    f">20% below ensemble avg ${avg_20d:+,.2f}"
                )
        elif avg_20d < 0:
            if st.pnl_20d_after_costs < avg_20d * (1 + underperform):
                reasons.append(
                    f"20-day after-cost P&L ${st.pnl_20d_after_costs:+,.2f} is "
                    f">20% worse than ensemble avg ${avg_20d:+,.2f}"
                )
        return reasons

    if n20 < drain_min_trades:
        return []
    avg_trade = st.pnl_20d_after_costs / n20
    if st.pnl_20d_after_costs <= drain_usd and avg_trade <= drain_per_trade:
        reasons.append(
            f"absolute drain: 20-day after-cost P&L ${st.pnl_20d_after_costs:+,.2f} "
            f"over {n20} trades (avg ${avg_trade:+,.2f}/trade) exceeds "
            f"${drain_usd:,.0f} / ${drain_per_trade:,.0f}-per-trade bars "
            f"(N<{min_trades})"
        )
    return reasons


def _agent_active_state() -> dict[str, dict]:
    """
    Read agent_summary.json (written by agent_rotator) so we know who is
    currently benched. If the file is missing or malformed, every agent is
    treated as active.
    """
    summary_path = _ledger.LOGS_DIR / "agent_summary.json"
    if not summary_path.exists():
        return {}
    try:
        with open(summary_path) as f:
            return json.load(f) or {}
    except Exception:
        return {}


# ── Main evaluator ──────────────────────────────────────────────────────────
class AgentEvaluator:
    """Reads the ledger and produces a ranked EvalReport."""

    def evaluate(self) -> EvalReport:
        # Epoch-filtered: pre-2026-07-02 trades were distorted by the
        # duplicate-entry bug, so they'd have agents benched for the bug's
        # sins rather than their own. Full history remains in the ledger
        # and in reports; it just doesn't vote on rotation anymore.
        all_trades = _ledger.epoch_trades()
        active_state = _agent_active_state()

        # ── Window cutoffs (calendar days, ET) ────────────────────────────
        today_midnight = _today_et_date()
        cutoff_5d  = today_midnight - timedelta(days=5)
        cutoff_20d = today_midnight - timedelta(days=20)

        # ── Per-agent aggregation ─────────────────────────────────────────
        # Leaf names only — same unwrap as trade_ledger.per_agent_attribution.
        agg: dict[str, dict] = {}

        for t in all_trades:
            # Closed trades only. Marking open winners into the 5d/20d
            # windows is how a losing agent kept a full weight while its
            # closed record bled (meta_agent already dropped unrealized
            # for that reason). Rotation, FLAG, and size-tilt read this
            # report, so an open mark must not vote.
            if getattr(t, "is_open", False):
                continue
            opened = _trade_opened_dt(t)
            pnl    = _pnl_for_trade(t)
            cost   = _ledger.round_trip_cost(t)
            _names = list(getattr(t, "leaf_agents", None) or [])
            if not _names:
                # Older Trade stubs / tests without leaf_agents.
                _names = _ledger.leaf_agent_names(
                    getattr(t, "primary_agent", ""),
                    getattr(t, "contributors", ""),
                )
            for agent in _names:
                d = agg.setdefault(agent, {
                    "pnl_5d":       0.0, "pnl_20d":      0.0, "pnl_alltime":  0.0,
                    "cost_5d":      0.0, "cost_20d":     0.0, "cost_total":   0.0,
                    "trades_5d":    0,   "trades_20d":   0,   "trades_total": 0,
                    "wins_total":   0,   "losses_total": 0,
                })

                d["pnl_alltime"]  += pnl
                d["cost_total"]   += cost
                d["trades_total"] += 1

                # Win/loss only counts CLOSED trades — open positions are
                # too noisy to call wins or losses yet.
                if not t.is_open:
                    if t.realized_pnl >= 0:
                        d["wins_total"] += 1
                    else:
                        d["losses_total"] += 1

                if opened is None:
                    continue
                if opened >= cutoff_5d:
                    d["pnl_5d"]    += pnl
                    d["cost_5d"]   += cost
                    d["trades_5d"] += 1
                if opened >= cutoff_20d:
                    d["pnl_20d"]    += pnl
                    d["cost_20d"]   += cost
                    d["trades_20d"] += 1

        # ── Build AgentStats list ─────────────────────────────────────────
        stats_list: list[AgentStats] = []
        for name, d in agg.items():
            active_info = active_state.get(name, {})
            pnl_5d = round(d["pnl_5d"], 2)
            pnl_20d = round(d["pnl_20d"], 2)
            pnl_all = round(d["pnl_alltime"], 2)
            cost_5d = round(d["cost_5d"], 2)
            cost_20d = round(d["cost_20d"], 2)
            cost_all = round(d["cost_total"], 2)
            after_5 = _after_cost(pnl_5d, cost_5d)
            after_20 = _after_cost(pnl_20d, cost_20d)
            after_all = _after_cost(pnl_all, cost_all)
            st = AgentStats(
                name         = name,
                pnl_5d       = pnl_5d,
                pnl_20d      = pnl_20d,
                pnl_alltime  = pnl_all,
                pnl_5d_after_costs  = after_5,
                pnl_20d_after_costs = after_20,
                pnl_alltime_after_costs = after_all,
                cost_5d      = cost_5d,
                cost_20d     = cost_20d,
                cost_total   = cost_all,
                expectancy_after_costs_5d  = (
                    round(after_5 / d["trades_5d"], 4) if d["trades_5d"] else None
                ),
                expectancy_after_costs_20d = (
                    round(after_20 / d["trades_20d"], 4) if d["trades_20d"] else None
                ),
                expectancy_after_costs     = (
                    round(after_all / d["trades_total"], 4) if d["trades_total"] else None
                ),
                trades_5d    = d["trades_5d"],
                trades_20d   = d["trades_20d"],
                trades_total = d["trades_total"],
                wins_total   = d["wins_total"],
                losses_total = d["losses_total"],
                active       = active_info.get("active", True),
            )
            stats_list.append(st)

        # ── Ensemble averages (active leaf agents only) ───────────────────
        # After-cost P&L so FLAG/PROMOTE and the scorecard share one number.
        active = [
            s for s in stats_list
            if s.active and not _ledger.is_wrapper_agent_name(s.name)
        ]
        n = len(active) or 1
        avg_5d  = sum(s.pnl_5d_after_costs  for s in active) / n
        avg_20d = sum(s.pnl_20d_after_costs for s in active) / n

        # ── Flag underperformers ──────────────────────────────────────────
        flagged: list[str] = []
        for st in active:
            reasons = flag_reasons(st, avg_5d, avg_20d)
            if reasons:
                st.flagged     = True
                st.flag_reason = " | ".join(reasons)
                flagged.append(st.name)

        # ── Top performer by 20-day after-cost P&L ────────────────────────
        ranked = sorted(
            [s for s in active if s.trades_20d >= MIN_TRADES_TO_EVALUATE],
            key=lambda x: x.pnl_20d_after_costs, reverse=True,
        )
        top = ranked[0].name if ranked else None

        return EvalReport(
            generated_at     = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            agents           = stats_list,
            flagged_agents   = flagged,
            top_agent        = top,
            ensemble_avg_5d  = round(avg_5d,  2),
            ensemble_avg_20d = round(avg_20d, 2),
        )

    def save_report(self, report: EvalReport) -> Path:
        """Save the latest eval report as JSON for downstream tools to read."""
        report_path = _ledger.LOGS_DIR / "latest_eval.json"
        data = {
            "generated_at":     report.generated_at,
            "top_agent":        report.top_agent,
            "flagged_agents":   report.flagged_agents,
            "ensemble_avg_5d":  report.ensemble_avg_5d,
            "ensemble_avg_20d": report.ensemble_avg_20d,
            "agents": [
                {
                    "name":          a.name,
                    "pnl_5d":        a.pnl_5d,
                    "pnl_20d":       a.pnl_20d,
                    "pnl_alltime":   a.pnl_alltime,
                    "pnl_5d_after_costs":  a.pnl_5d_after_costs,
                    "pnl_20d_after_costs": a.pnl_20d_after_costs,
                    "pnl_alltime_after_costs": a.pnl_alltime_after_costs,
                    "cost_5d":       a.cost_5d,
                    "cost_20d":      a.cost_20d,
                    "cost_total":    a.cost_total,
                    "expectancy_after_costs_5d":  a.expectancy_after_costs_5d,
                    "expectancy_after_costs_20d": a.expectancy_after_costs_20d,
                    "expectancy_after_costs":     a.expectancy_after_costs,
                    "trades_5d":     a.trades_5d,
                    "trades_20d":    a.trades_20d,
                    "trades_total":  a.trades_total,
                    "wins_total":    a.wins_total,
                    "losses_total":  a.losses_total,
                    "win_rate":      a.win_rate,
                    "active":        a.active,
                    "flagged":       a.flagged,
                    "flag_reason":   a.flag_reason,
                }
                for a in report.agents
            ],
        }
        with open(report_path, "w") as f:
            json.dump(data, f, indent=2)
        return report_path


# ── CLI usage ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    ev     = AgentEvaluator()
    report = ev.evaluate()
    print(report.summary_text())
    path = ev.save_report(report)
    print(f"\nReport saved → {path}")
