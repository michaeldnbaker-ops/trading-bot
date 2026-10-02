"""L-2026-10-01b turnaround roster.

Active book is NewsAgent and BreakoutAgent. Code-disabled agents cannot
be reactivated by the rotator. Crypto stays hard-off.
"""

from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from agent_evaluator import AgentStats, EvalReport
from agent_rotator import (
    ACTIVE_SIGNAL_AGENTS,
    DISABLED_AGENTS,
    MIN_ACTIVE_AGENTS,
    PROTECTED_AGENTS,
    AgentRotator,
    agent_may_signal,
    roster_lines,
)


# Every name that has ever been on this bot, including the surge scanner
# and crypto. The live set is the subset agent_may_signal accepts.
FULL_ROSTER = [
    "TechnicalAgent",
    "NewsAgent",
    "SentimentAgent",
    "MomentumAgent",
    "BreakoutAgent",
    "BearishPatternAgent",
    "ShortMomentumAgent",
    "EarningsAgent",
    "MacroAgent",
    "PremarketAgent",
    "SectorRotationAgent",
    "OptionsFlowAgent",
    "VolatilityAgent",
    "IntermarketAgent",
    "MoversAgent",
    "MeanReversionAgent",
    "AlpacaSurgeAgent",
    "AlpacaSurgeDetector",
    "CryptoAgent",
]

PINNED_OFF = [
    "MoversAgent",
    "TechnicalAgent",
    "SentimentAgent",
    "EarningsAgent",
    "OptionsFlowAgent",
    "MomentumAgent",
    "PremarketAgent",
    "MeanReversionAgent",
]


def _stats(**kw) -> AgentStats:
    defaults = dict(
        name="ShortMomentumAgent",
        pnl_20d_after_costs=400.0,
        trades_20d=20,
        expectancy_after_costs_20d=20.0,
        expectancy_after_costs=20.0,
        active=False,
    )
    defaults.update(kw)
    return AgentStats(**defaults)


class TurnaroundRoster(unittest.TestCase):
    def test_active_set_is_news_and_breakout(self):
        self.assertEqual(
            ACTIVE_SIGNAL_AGENTS, frozenset({"NewsAgent", "BreakoutAgent"}),
        )
        live = {name for name in FULL_ROSTER if agent_may_signal(name)}
        self.assertEqual(live, {"NewsAgent", "BreakoutAgent"})

    def test_pinned_names_stay_off_the_book(self):
        for name in PINNED_OFF:
            self.assertFalse(agent_may_signal(name), name)
            self.assertNotIn(name, DISABLED_AGENTS)

    def test_protected_agents_empty(self):
        self.assertEqual(PROTECTED_AGENTS, set())

    def test_crypto_still_off(self):
        import session_gates
        self.assertIs(session_gates.CRYPTO_TRADING_ENABLED, False)
        self.assertFalse(session_gates.crypto_entries_allowed())
        self.assertFalse(agent_may_signal("CryptoAgent"))

    def test_disabled_set(self):
        self.assertEqual(DISABLED_AGENTS, frozenset({
            "ShortMomentumAgent",
            "BearishPatternAgent",
            "SectorRotationAgent",
            "IntermarketAgent",
            "MacroAgent",
            "VolatilityAgent",
            "AlpacaSurgeAgent",
            "AlpacaSurgeDetector",
        }))
        for name in DISABLED_AGENTS:
            self.assertFalse(agent_may_signal(name), name)

    def test_rotator_cannot_reactivate_disabled_agent(self):
        name = "ShortMomentumAgent"
        benched = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        summary = {name: {"active": False, "benched_at": benched}}
        rotator = AgentRotator()
        report = EvalReport(
            generated_at="test",
            agents=[_stats(name=name, active=False)],
            flagged_agents=[],
        )
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "agent_summary.json"
            with patch.object(rotator.evaluator, "evaluate", return_value=report), \
                 patch.object(rotator.logger, "get_summary", return_value=summary), \
                 patch.object(rotator, "_write_rotation_event"), \
                 patch("agent_rotator.SUMMARY", path):
                result = rotator.run_rotation(dry_run=False)
            self.assertFalse(path.exists())
        self.assertFalse(any("REACTIVATED" in a for a in result["actions"]), result)
        self.assertTrue(any("cannot reactivate" in h for h in result["holds"]), result)
        self.assertFalse(summary[name]["active"])
        self.assertEqual(summary[name]["benched_at"], benched)

    def test_running_short_does_not_promote_disabled_variant(self):
        agents = [
            _stats(name="PremarketAgent", pnl_20d_after_costs=-900,
                   expectancy_after_costs_20d=-40, active=True),
            _stats(name="NewsAgent", pnl_20d_after_costs=80, active=True),
            _stats(name="BreakoutAgent", pnl_20d_after_costs=50, active=True),
            _stats(name="SectorRotationAgent", pnl_20d_after_costs=500,
                   expectancy_after_costs_20d=40, trades_20d=20, active=False),
        ]
        summary = {
            "PremarketAgent": {"active": True},
            "NewsAgent": {"active": True},
            "BreakoutAgent": {"active": True},
            "SectorRotationAgent": {
                "active": False,
                "benched_at": (datetime.now(timezone.utc) - timedelta(days=30)).isoformat(),
            },
        }
        rotator = AgentRotator()
        report = EvalReport(
            generated_at="test",
            agents=agents,
            flagged_agents=["PremarketAgent"],
        )
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "agent_summary.json"
            with patch.object(rotator.evaluator, "evaluate", return_value=report), \
                 patch.object(rotator.logger, "get_summary", return_value=summary), \
                 patch.object(rotator, "_write_rotation_event"), \
                 patch("agent_rotator.SUMMARY", path):
                result = rotator.run_rotation(dry_run=False)
        joined = " ".join(result["actions"])
        self.assertIn("BENCHED PremarketAgent (no replacement", joined)
        self.assertIn("ensemble running short", joined)
        self.assertNotIn("PROMOTED", joined)
        self.assertFalse(summary["SectorRotationAgent"]["active"])

    def test_two_agent_floor_does_not_bench_or_fill(self):
        agents = [
            _stats(name="NewsAgent", pnl_20d_after_costs=-500, active=True),
            _stats(name="BreakoutAgent", pnl_20d_after_costs=-400, active=True),
            _stats(name="BearishPatternAgent", pnl_20d_after_costs=900,
                   expectancy_after_costs_20d=30, trades_20d=20, active=True),
            _stats(name="VolatilityAgent", pnl_20d_after_costs=-2000,
                   active=True),
        ]
        benched = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        summary = {
            "NewsAgent": {"active": True},
            "BreakoutAgent": {"active": True},
            "MacroAgent": {"active": False, "benched_at": benched},
        }
        rotator = AgentRotator()
        report = EvalReport(
            generated_at="test",
            agents=agents,
            flagged_agents=["NewsAgent", "BreakoutAgent", "VolatilityAgent"],
        )
        with patch.object(rotator.evaluator, "evaluate", return_value=report), \
             patch.object(rotator.logger, "get_summary", return_value=summary), \
             patch.object(rotator, "_write_rotation_event"):
            result = rotator.run_rotation(dry_run=True)
        self.assertFalse(any(a.startswith("BENCHED") for a in result["actions"]), result)
        self.assertFalse(any("REACTIVATED" in a or "PROMOTED" in a for a in result["actions"]))
        self.assertTrue(any("minimum active agents" in a for a in result["actions"]), result)
        self.assertTrue(any("cannot reactivate" in h for h in result["holds"]), result)
        self.assertFalse(summary["MacroAgent"]["active"])
        self.assertEqual(MIN_ACTIVE_AGENTS, 2)
        self.assertIn("actions", result)

    def test_find_replacement_skips_disabled_and_pinned(self):
        rotator = AgentRotator()
        self.assertIsNone(rotator._find_replacement(
            "BearishPatternAgent",
            {"ShortMomentumAgent": {"active": False}},
        ))
        # Technical's positive sibling is pinned; Breakout's expectancy is
        # negative, so the short-book fill must not be that pinned name.
        report = EvalReport(
            generated_at="test",
            agents=[
                _stats(name="MomentumAgent", expectancy_after_costs_20d=16.0,
                       trades_20d=12, active=False),
                _stats(name="BreakoutAgent", expectancy_after_costs_20d=-30.0,
                       trades_20d=12, active=False),
            ],
            flagged_agents=[],
        )
        summary = {
            "MomentumAgent": {
                "active": False,
                "pinned_reason": "manual hold",
                "benched_at": "2099-01-01T00:00:00+00:00",
            },
            "BreakoutAgent": {"active": False},
        }
        self.assertIsNone(rotator._find_replacement(
            "TechnicalAgent", summary, report=report,
        ))

    def test_dry_run_prints_roster(self):
        rotator = AgentRotator()
        report = EvalReport(generated_at="test", agents=[], flagged_agents=[])
        buf = io.StringIO()
        with redirect_stdout(buf), \
             patch.object(rotator.evaluator, "evaluate", return_value=report), \
             patch.object(rotator.logger, "get_summary", return_value={}), \
             patch.object(rotator, "_write_rotation_event"):
            rotator.run_rotation(dry_run=True)
        text = buf.getvalue()
        for line in roster_lines():
            self.assertIn(line, text)
        self.assertIn("Roster — active: BreakoutAgent, NewsAgent", text)
        self.assertIn("Roster — protected: (none)", text)
        self.assertIn("[DRY RUN]", text)

    def test_ensemble_and_surge_call_the_gate(self):
        src = Path(__file__).resolve().parent.joinpath("ensemble.py").read_text(encoding="utf-8")
        self.assertIn("agent_may_signal(agent.name)", src)
        self.assertIn('agent_may_signal("AlpacaSurgeAgent")', src)
        self.assertIn('agent_may_signal("AlpacaSurgeDetector")', src)


if __name__ == "__main__":
    unittest.main()
