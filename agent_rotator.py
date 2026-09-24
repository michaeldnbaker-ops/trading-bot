"""
agent_rotator.py — v1.4 (2026-04-24)
────────────────────────────────────
Rotates underperforming agents out and promotes better alternatives.

CHANGE LOG (v1.4):
  • FIX: don't promote an agent that was benched earlier in the same cycle.
    AGENT_VARIANTS lists sibling ensemble agents as fallbacks. After v1.2
    enabled real benching, the fourth bench (BreakoutAgent) found
    TechnicalAgent — already benched two iterations earlier — listed as a
    variant and "promoted" it back to active. Net effect: the worst loser
    we just benched got resurrected by the next iteration.
    Track newly_benched in this cycle and exclude it from replacement
    candidates. Also clear benched_at when a promotion happens so state
    stays consistent.

CHANGE LOG (v1.3):
  • FIX: KeyError when benching an agent with no existing summary entry.
    Pre-v1.2 the rotator bailed before reaching this code path on a clean
    summary, so the bug was latent. v1.2 unblocked benching, exposing it.
    Fix mirrors the replacement-promotion path: ensure the summary entry
    exists (creating a blank one if needed) before mutating it.

CHANGE LOG (v1.2):
  • FIX: active_count was counted from agent_summary.json. An empty/clean
    summary (no benched agents) made active_count=0, and the MIN_ACTIVE_AGENTS
    safety floor immediately tripped on the first flagged agent — so nothing
    ever got benched on a fresh install. Now we count active agents from the
    EvalReport roster (excluding the MetaAgent wrapper), which reflects real
    state regardless of what's in agent_summary.json.
  • Bench worst-first: sort report.flagged_agents by 20-day P&L ascending
    so the rotator spends its bench-budget on the biggest losers instead of
    whatever order the dict happened to insert.

CHANGE LOG (v1.1):
  • Removed TechnicalAgent from PROTECTED_AGENTS — its protection wasn't
    earning its keep (9 % win rate, persistently worst-quartile P&L).
  • Now compatible with agent_evaluator v2.0 (ledger-backed). No code
    change needed here; the import surface and EvalReport shape are
    unchanged.

Rotation logic:
  1. Read the latest EvalReport from agent_evaluator.
  2. For each flagged agent (relative underperform OR N<10 absolute
     drain — see agent_evaluator thresholds), bench it for BENCH_DAYS.
     MIN_ACTIVE_AGENTS is the only bench skip besides PROTECTED_AGENTS.
     Drain FLAGs are not silently overridden.
  3. PROMOTE is variant substitution only. Prefer the inactive variant
     with the best 20d expectancy-after-costs. A known negative-
     expectancy variant is never promoted (bench without replacement).
  4. Log the rotation event to logs/rotation_log.jsonl.
  5. Update agent_summary.json accordingly.
  6. Re-activate benched agents after BENCH_DAYS only when after-cost
     expectancy is positive over MIN_TRADES_TO_EVALUATE (20d window).
     Otherwise they stay BENCHED and the cycle logs why. A pin
     (benched_at far in the future, and/or pinned_reason) stays benched.

"Better alternatives" in this system means an agent variant with different
parameters (e.g. TechnicalAgent_v2, TechnicalAgent_conservative).
If no variant exists, the original agent is benched and the ensemble simply
runs with fewer voices until it's recalled.

Add variant agent names to AGENT_VARIANTS to enable automatic substitution.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import logging

from agent_evaluator import MIN_TRADES_TO_EVALUATE, AgentEvaluator, EvalReport
from performance_logger import PerformanceLogger, LOGS_DIR, SUMMARY

log = logging.getLogger("AgentRotator")

# ── Config ──────────────────────────────────────────────────────────────────
BENCH_DAYS          = 3      # how long a flagged agent sits out
# Same sample floor the evaluator uses before a P&L number means anything.
# Resting BENCH_DAYS is not evidence; reactivation needs a real sample.
REACTIVATION_MIN_TRADES = MIN_TRADES_TO_EVALUATE
ROTATION_LOG        = LOGS_DIR / "rotation_log.jsonl"
MIN_ACTIVE_AGENTS   = 2      # never bench below this count (safety floor)
# Drain FLAGs (N<10 absolute-drain) use this same floor. There is no
# second override path that looks like a bench and then no-ops.

# ── Full 12-agent roster with cross-substitution logic ──────────────────────
# When an agent underperforms, the rotator promotes its best substitute.
# Substitutes are chosen from agents in the same category that ARE performing.
# Format: agent_name → [preferred substitutes in priority order]
AGENT_VARIANTS: dict[str, list[str]] = {
    # Upswing agents — substitute within category
    "TechnicalAgent":      ["MomentumAgent", "BreakoutAgent"],
    "MomentumAgent":       ["BreakoutAgent", "TechnicalAgent"],
    "BreakoutAgent":       ["MomentumAgent", "TechnicalAgent"],

    # Downswing agents — substitute within category
    "BearishPatternAgent": ["ShortMomentumAgent"],
    "ShortMomentumAgent":  ["BearishPatternAgent"],

    # Catalyst agents — substitute within category
    "EarningsAgent":       ["MacroAgent", "NewsAgent"],
    "MacroAgent":          ["EarningsAgent", "SentimentAgent"],

    # Flow/sentiment agents
    "NewsAgent":           ["SentimentAgent", "OptionsFlowAgent"],
    "SentimentAgent":      ["NewsAgent", "OptionsFlowAgent"],
    "OptionsFlowAgent":    ["NewsAgent", "SentimentAgent"],

    # Timing agents
    "PremarketAgent":      ["SectorRotationAgent"],
    "SectorRotationAgent": ["PremarketAgent"],

    # Soft-watch / later additions — bench without promoting a sibling
    # unless a real variant is listed. Empty on purpose (no override-theater).
    "MeanReversionAgent":  [],
}

# Agents that are NEVER benched — they provide critical infrastructure.
# NOTE (v1.1): TechnicalAgent was removed — its 9% win rate didn't justify
# protection. NewsAgent and SentimentAgent stay protected because their
# signals feed multiple downstream evaluators beyond P&L attribution.
# Short specialists are PROTECTED. On 2026-07-29 the market fell ~1000pts
# and the book was 100% long with zero shorts — because rotation had
# benched BearishPatternAgent AND ShortMomentumAgent on P&L earned under
# the old broken geometry. Benching every short-capable agent also makes
# the 2-agent short-consensus rule unsatisfiable, so the ensemble becomes
# structurally long-only exactly when downside protection matters most.
# Weight them down if they underperform; never bench them to zero.
PROTECTED_AGENTS = {"NewsAgent", "SentimentAgent",
                    "BearishPatternAgent", "ShortMomentumAgent"}


def is_pinned_bench(info: dict, benched_at: datetime, now: datetime) -> bool:
    """Pin convention: far-future benched_at and/or pinned_reason.

    Improver notes record a manual pin as benched_at in 2099 plus
    pinned_reason on the agent_summary.json row. Either signal keeps
    the agent BENCHED. A normal 3-day bench timestamp does not.
    """
    if str(info.get("pinned_reason") or "").strip():
        return True
    if benched_at - now >= timedelta(days=365):
        return True
    return False


def reactivation_decision(stats, min_trades: int | None = None) -> tuple[bool, str]:
    """(reactivate, reason) from the evaluator's 20d after-cost expectancy.

    Positive means strictly greater than zero. The trade count is the
    evaluator's MIN_TRADES_TO_EVALUATE unless a test overrides it.
    """
    need = REACTIVATION_MIN_TRADES if min_trades is None else min_trades
    if stats is None:
        return False, f"no expectancy sample (need ≥{need} trades)"
    trades = int(getattr(stats, "trades_20d", 0) or 0)
    exp = getattr(stats, "expectancy_after_costs_20d", None)
    if exp is None:
        exp = getattr(stats, "expectancy_after_costs", None)
    if trades < need or exp is None:
        shown = "n/a" if exp is None else f"{float(exp):+.2f}"
        return False, (
            f"after-cost expectancy {shown} over {trades} trades "
            f"(need positive expectancy and ≥{need} trades)"
        )
    if float(exp) <= 0:
        return False, (
            f"after-cost expectancy {float(exp):+.2f} over {trades} trades "
            f"is not positive"
        )
    return True, f"after-cost expectancy {float(exp):+.2f} over {trades} trades"


class AgentRotator:
    """Reads the latest evaluation and rotates agents as needed."""

    def __init__(self):
        self.logger   = PerformanceLogger()
        self.evaluator = AgentEvaluator()

    # ── Public API ─────────────────────────────────────────────────────────

    def run_rotation(self, dry_run: bool = False) -> dict:
        """
        Execute one rotation cycle.
        dry_run=True: prints what would happen without modifying anything.
        Returns a dict summarising actions taken (or planned if dry_run).
        """
        report  = self.evaluator.evaluate()
        summary = self.logger.get_summary()
        now     = datetime.now(timezone.utc)

        actions: list[str] = []
        agent_stats = {a.name: a for a in report.agents}

        # ── Step 1: Re-activate rested agents with positive expectancy ────
        # Time on the bench is not a performance check. REACTIVATED only
        # when the evaluator's 20d after-cost expectancy is positive over
        # REACTIVATION_MIN_TRADES. Pins (far-future benched_at and/or
        # pinned_reason) stay BENCHED regardless of the numbers.
        for name, info in summary.items():
            if info.get("active", True):
                continue
            benched_at_str = info.get("benched_at")
            if not benched_at_str:
                continue
            benched_at = datetime.fromisoformat(benched_at_str)
            if benched_at.tzinfo is None:
                benched_at = benched_at.replace(tzinfo=timezone.utc)
            if is_pinned_bench(info, benched_at, now):
                why = info.get("pinned_reason") or "benched_at far future"
                action = f"BENCHED {name} stays benched — pinned ({why})"
                actions.append(action)
                log.info(action)
                continue
            if (now - benched_at).days < BENCH_DAYS:
                continue
            ok, why = reactivation_decision(agent_stats.get(name))
            if not ok:
                action = f"BENCHED {name} stays benched — {why}"
                actions.append(action)
                log.info(action)
                continue
            if not dry_run:
                summary[name]["active"]     = True
                summary[name]["benched_at"] = None
            days = (now - benched_at).days
            action = f"REACTIVATED {name} (benched {days}d ago; {why})"
            actions.append(action)
            self._write_rotation_event(name, "REACTIVATED", action, dry_run)

        # ── Step 2: Bench underperforming agents ───────────────────────────
        # Active count must reflect the FULL roster, not just deviations
        # tracked in agent_summary.json. An empty summary means "everyone
        # is at default-active", but summary.values() would count zero —
        # tripping the MIN_ACTIVE_AGENTS floor on the first flagged agent.
        # Use the EvalReport roster (excluding the MetaAgent wrapper).
        real_agents = [a for a in report.agents if a.name != "MetaAgent"]
        active_count = sum(1 for a in real_agents if a.active)

        # Bench worst-first: spend the bench-budget on the biggest 20-day
        # losers, not on whatever order the flagged dict happened to use.
        flagged_sorted = sorted(
            report.flagged_agents,
            key=lambda name: (
                agent_stats[name].pnl_20d_after_costs
                if name in agent_stats else 0.0
            ),
        )

        # Track agents benched in THIS cycle so we don't accidentally
        # promote them as a sibling's replacement (see v1.4 changelog).
        newly_benched: set[str] = set()

        for agent_name in flagged_sorted:
            # Never bench protected core agents
            if agent_name in PROTECTED_AGENTS:
                actions.append(f"PROTECTED {agent_name} — core agent, reducing weight instead of benching")
                continue

            if active_count <= MIN_ACTIVE_AGENTS:
                actions.append(
                    f"SKIPPED bench of {agent_name} — already at minimum active agents ({MIN_ACTIVE_AGENTS})"
                )
                break

            # Find best available replacement (excluding agents we just
            # benched in this cycle — they're losers, not promotion targets)
            replacement = self._find_replacement(
                agent_name, summary, exclude=newly_benched, report=report,
            )

            if not dry_run:
                # Ensure the entry exists before mutating it. On a freshly
                # reset agent_summary.json (e.g. {}), the agent has never
                # been seen by the rotator before, so summary[agent_name]
                # would KeyError without this seed.
                summary[agent_name] = summary.get(agent_name) or self._blank_agent_entry()
                summary[agent_name]["active"]     = False
                summary[agent_name]["benched_at"] = now.isoformat()
                if replacement:
                    summary[replacement] = summary.get(replacement) or self._blank_agent_entry()
                    summary[replacement]["active"]     = True
                    # Clear the bench timestamp when re-activating, so we
                    # don't leave inconsistent active=true / benched_at=set state.
                    summary[replacement]["benched_at"] = None

            newly_benched.add(agent_name)
            action = (
                f"BENCHED {agent_name} → PROMOTED {replacement}"
                if replacement else
                f"BENCHED {agent_name} (no replacement available; ensemble running short)"
            )
            actions.append(action)
            self._write_rotation_event(agent_name, "BENCHED", action, dry_run, replacement=replacement)
            active_count -= 1

        # ── Step 3: Persist updated summary ───────────────────────────────
        if not dry_run and actions:
            with open(SUMMARY, "w") as f:
                json.dump(summary, f, indent=2)

        result = {
            "timestamp": now.isoformat(),
            "dry_run":   dry_run,
            "actions":   actions,
            "top_agent": report.top_agent,
            "flagged":   report.flagged_agents,
        }

        # Print summary
        print(f"\n{'[DRY RUN] ' if dry_run else ''}Rotation cycle — {now.strftime('%Y-%m-%d %H:%M UTC')}")
        if actions:
            for a in actions:
                print(f"  • {a}")
        else:
            print("  No rotations needed — all agents performing within threshold.")

        return result

    # ── Internals ──────────────────────────────────────────────────────────

    def _find_replacement(
        self,
        agent_name: str,
        summary: dict,
        exclude: set[str] | None = None,
        report: EvalReport | None = None,
    ) -> str | None:
        """Return the best available (inactive or unknown) variant.

        `exclude` blocks agents benched earlier in this cycle.
        Expectancy-after-costs (20d) ranks candidates:
          • known positive expectancy first (best E wins)
          • unknown / no 20d trades next (trial)
          • known negative expectancy is never promoted — that would
            be override-theater (swap one loser for another)
        """
        exclude = exclude or set()
        variants = AGENT_VARIANTS.get(agent_name, [])
        candidates: list[str] = []
        for variant in variants:
            if variant in exclude:
                continue
            entry = summary.get(variant)
            if entry is None or not entry.get("active", False):
                candidates.append(variant)
        if not candidates:
            return None

        stats = {a.name: a for a in (report.agents if report else [])}

        def _rank(name: str) -> tuple[int, float]:
            st = stats.get(name)
            if st is None or st.trades_20d == 0 or st.expectancy_after_costs_20d is None:
                return (1, 0.0)  # unknown — eligible trial
            exp = float(st.expectancy_after_costs_20d)
            if exp < 0:
                return (0, exp)  # known loser — last resort, then rejected
            return (2, exp)

        ranked = sorted(candidates, key=_rank, reverse=True)
        best = ranked[0]
        if _rank(best)[0] == 0:
            return None
        return best

    @staticmethod
    def _blank_agent_entry() -> dict:
        return {
            "total_pnl":    0.0,
            "trade_count":  0,
            "wins":         0,
            "losses":       0,
            "active":       True,
            "last_updated": None,
            "benched_at":   None,
        }

    @staticmethod
    def _write_rotation_event(
        agent_name: str,
        event_type: str,
        description: str,
        dry_run: bool,
        replacement: str | None = None,
    ):
        if dry_run:
            return
        record = {
            "timestamp":   datetime.now(timezone.utc).isoformat(),
            "event":       event_type,
            "agent":       agent_name,
            "replacement": replacement,
            "description": description,
        }
        with open(ROTATION_LOG, "a") as f:
            f.write(json.dumps(record) + "\n")


# ── CLI ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    dry = "--dry-run" in sys.argv
    rotator = AgentRotator()
    rotator.run_rotation(dry_run=dry)
