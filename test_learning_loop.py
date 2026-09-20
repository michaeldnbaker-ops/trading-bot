"""Tests for paper Learning Loop: leaf P&L, expectancy-after-costs, N<10 drain FLAG.

No broker, no network. FLAG → BENCH still respects MIN_ACTIVE_AGENTS.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from trade_ledger import (
    Trade,
    expand_agent_names,
    expectancy_after_costs,
    leaf_agent_names,
    round_trip_cost,
)
from agent_evaluator import (
    ABSOLUTE_DRAIN_FLAG_USD,
    ABSOLUTE_DRAIN_MIN_TRADES,
    ABSOLUTE_DRAIN_PER_TRADE_USD,
    MIN_TRADES_TO_EVALUATE,
    AgentEvaluator,
    AgentStats,
    EvalReport,
    flag_reasons,
)
from agent_rotator import MIN_ACTIVE_AGENTS, AgentRotator


TODAY = datetime(2026, 9, 20)


def _trade(
    primary: str,
    pnl: float,
    days_ago: int = 1,
    *,
    contrib: str = "",
    shares: float = 100.0,
    entry: float = 50.0,
    open_: bool = False,
    symbol: str = "AMD",
) -> Trade:
    opened = (TODAY - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")
    return Trade(
        trade_id=f"{primary}-{symbol}-{days_ago}-{pnl}",
        opened_at_et=opened,
        symbol=symbol,
        side="LONG",
        primary_agent=primary,
        contributors=contrib,
        entry_price=entry,
        target_price=entry * 1.05,
        stop_price=entry * 0.95,
        risk_dollar=320.0,
        shares=shares,
        status="open" if open_ else "stop",
        realized_pnl=0.0 if open_ else pnl,
        unrealized_pnl=pnl if open_ else 0.0,
    )


def _stats(**kw) -> AgentStats:
    defaults = dict(
        name="MeanReversionAgent",
        pnl_20d=-900.0,
        pnl_20d_after_costs=-920.0,
        trades_20d=4,
        active=True,
    )
    defaults.update(kw)
    return AgentStats(**defaults)


class LeafUnwrap(unittest.TestCase):
    def test_wrapper_and_comma_list(self):
        self.assertEqual(
            expand_agent_names("MetaAgent(MeanReversionAgent, TechnicalAgent)"),
            ["MeanReversionAgent", "TechnicalAgent"],
        )
        self.assertEqual(
            expand_agent_names("NewsAgent, OptionsFlowAgent"),
            ["NewsAgent", "OptionsFlowAgent"],
        )
        self.assertEqual(
            leaf_agent_names("MetaAgent", "NewsAgent, OptionsFlowAgent"),
            ["NewsAgent", "OptionsFlowAgent"],
        )

    def test_trade_leaf_agents_from_compound_primary(self):
        t = _trade("MetaAgent(MeanReversionAgent, TechnicalAgent)", -400)
        self.assertEqual(t.leaf_agents, ["MeanReversionAgent", "TechnicalAgent"])
        self.assertEqual(t.primary_leaves, ["MeanReversionAgent", "TechnicalAgent"])
        self.assertEqual(t.contributor_leaves, [])


class LeafAttribution(unittest.TestCase):
    def test_wrapper_row_credits_leaves_not_meta(self):
        t = _trade("MetaAgent(MeanReversionAgent, TechnicalAgent)", -400.0)
        import trade_ledger as tl
        with patch.object(tl, "all_trades", return_value=[t]):
            rows = tl.per_agent_attribution()
        names = [r["agent"] for r in rows]
        self.assertIn("MeanReversionAgent", names)
        self.assertIn("TechnicalAgent", names)
        self.assertTrue(all(not n.startswith("MetaAgent") for n in names))
        by = {r["agent"]: r for r in rows}
        self.assertAlmostEqual(by["MeanReversionAgent"]["total_pnl"], -400.0)
        self.assertAlmostEqual(by["TechnicalAgent"]["total_pnl"], -400.0)
        self.assertLess(by["MeanReversionAgent"]["pnl_after_costs"], -400.0)
        self.assertIsNotNone(by["MeanReversionAgent"]["expectancy_after_costs"])

    def test_scorecard_uses_attribution_not_summary_zero(self):
        import daily_reporter as dr
        tmp = Path(tempfile.mkdtemp())
        (tmp / "agent_summary.json").write_text(json.dumps({
            "MeanReversionAgent": {"active": True, "total_pnl": 0.0, "trade_count": 0},
            "TechnicalAgent": {"active": True, "total_pnl": 0.0, "trade_count": 0},
        }))
        orig = dr.LOGS_DIR
        dr.LOGS_DIR = tmp
        try:
            with patch.object(dr, "_load_meta_weights", return_value={
                "MeanReversionAgent": 0.40,
                "TechnicalAgent": 0.40,
            }):
                roster = dr.scorecard_agent_roster({
                    "agent_attribution": [
                        {"agent": "MeanReversionAgent", "total_pnl": -1200.0,
                         "pnl_after_costs": -1210.0,
                         "expectancy_after_costs": -302.5},
                        {"agent": "TechnicalAgent", "total_pnl": -900.0,
                         "pnl_after_costs": -908.0,
                         "expectancy_after_costs": -227.0},
                    ]
                })
        finally:
            dr.LOGS_DIR = orig
        by = {r["name"]: r for r in roster}
        self.assertAlmostEqual(by["MeanReversionAgent"]["pnl"], -1210.0)
        self.assertAlmostEqual(by["TechnicalAgent"]["pnl"], -908.0)
        self.assertAlmostEqual(by["MeanReversionAgent"]["expectancy_after_costs"], -302.5)
        self.assertNotEqual(by["MeanReversionAgent"]["pnl"], 0)


class ExpectancyAfterCosts(unittest.TestCase):
    def test_helper_subtracts_aligned_costs(self):
        exp = expectancy_after_costs([100.0, -50.0], [2.0, 2.0])
        self.assertAlmostEqual(exp, (98.0 + -52.0) / 2)

    def test_round_trip_respects_floor_and_bps(self):
        cheap = _trade("NewsAgent", 10, shares=1, entry=10.0)  # $10 notional
        self.assertAlmostEqual(round_trip_cost(cheap), 2.0)
        fat = _trade("NewsAgent", 10, shares=1000, entry=50.0)  # $50k
        self.assertAlmostEqual(round_trip_cost(fat), 50.0)  # 10 bps

    def test_evaluator_exposes_expectancy_on_leaves(self):
        trades = [
            _trade("MetaAgent(MeanReversionAgent)", -200.0, days_ago=1),
            _trade("MetaAgent(MeanReversionAgent)", -200.0, days_ago=2),
            _trade("NewsAgent", 50.0, days_ago=1),
        ]
        ev = AgentEvaluator()
        with patch("agent_evaluator._today_et_date", return_value=TODAY), \
             patch("trade_ledger.epoch_trades", return_value=trades), \
             patch("agent_evaluator._agent_active_state", return_value={}):
            report = ev.evaluate()
        by = {a.name: a for a in report.agents}
        self.assertIn("MeanReversionAgent", by)
        self.assertNotIn("MetaAgent", by)
        mr = by["MeanReversionAgent"]
        self.assertEqual(mr.trades_20d, 2)
        self.assertLess(mr.pnl_20d_after_costs, mr.pnl_20d)
        self.assertIsNotNone(mr.expectancy_after_costs_20d)
        self.assertAlmostEqual(
            mr.expectancy_after_costs_20d,
            mr.pnl_20d_after_costs / mr.trades_20d,
        )


class AbsoluteDrainFlag(unittest.TestCase):
    def test_thresholds_are_documented_constants(self):
        self.assertEqual(MIN_TRADES_TO_EVALUATE, 10)
        self.assertEqual(ABSOLUTE_DRAIN_MIN_TRADES, 2)
        self.assertEqual(ABSOLUTE_DRAIN_FLAG_USD, -800.0)
        self.assertEqual(ABSOLUTE_DRAIN_PER_TRADE_USD, -150.0)

    def test_small_n_scratch_does_not_flag(self):
        st = _stats(pnl_20d_after_costs=-400.0, trades_20d=3)
        self.assertEqual(flag_reasons(st, avg_5d=0.0, avg_20d=-100.0), [])

    def test_single_trade_does_not_flag(self):
        st = _stats(pnl_20d_after_costs=-2000.0, trades_20d=1)
        self.assertEqual(flag_reasons(st, avg_5d=0.0, avg_20d=-100.0), [])

    def test_profitable_small_n_does_not_flag(self):
        # 2026-08-12 pattern: Technical +$2,132 over 5 trades
        st = _stats(
            name="TechnicalAgent",
            pnl_20d=2132.0,
            pnl_20d_after_costs=2120.0,
            trades_20d=5,
        )
        self.assertEqual(flag_reasons(st, avg_5d=500.0, avg_20d=800.0), [])

    def test_severe_small_n_drain_flags(self):
        st = _stats(
            name="MeanReversionAgent",
            pnl_20d=-1000.0,
            pnl_20d_after_costs=-1020.0,
            trades_20d=4,
        )
        reasons = flag_reasons(st, avg_5d=0.0, avg_20d=-50.0)
        self.assertTrue(reasons)
        self.assertIn("absolute drain", reasons[0])
        self.assertIn("N<10", reasons[0])

    def test_relative_flag_still_requires_n_10(self):
        st = _stats(
            name="NewsAgent",
            pnl_5d_after_costs=-500.0,
            pnl_20d_after_costs=-500.0,
            trades_20d=12,
        )
        reasons = flag_reasons(st, avg_5d=-100.0, avg_20d=-100.0)
        self.assertTrue(any("20-day after-cost" in r for r in reasons))
        self.assertFalse(any("absolute drain" in r for r in reasons))

    def test_evaluate_flags_mean_reversion_drain(self):
        trades = [
            _trade("MetaAgent(MeanReversionAgent)", -280.0, days_ago=i + 1)
            for i in range(4)
        ] + [
            _trade("NewsAgent", 40.0, days_ago=1),
            _trade("NewsAgent", 40.0, days_ago=2),
        ]
        ev = AgentEvaluator()
        with patch("agent_evaluator._today_et_date", return_value=TODAY), \
             patch("trade_ledger.epoch_trades", return_value=trades), \
             patch("agent_evaluator._agent_active_state", return_value={}):
            report = ev.evaluate()
        self.assertIn("MeanReversionAgent", report.flagged_agents)
        mr = next(a for a in report.agents if a.name == "MeanReversionAgent")
        self.assertTrue(mr.flagged)
        self.assertIn("absolute drain", mr.flag_reason)


class DrainFlagFlowsToBench(unittest.TestCase):
    def _run(self, report: EvalReport, summary: dict) -> dict:
        rotator = AgentRotator()
        with patch.object(rotator.evaluator, "evaluate", return_value=report), \
             patch.object(rotator.logger, "get_summary", return_value=summary), \
             patch.object(rotator, "_write_rotation_event"):
            return rotator.run_rotation(dry_run=True)

    def test_drain_flag_benches_and_keeps_min_active(self):
        agents = [
            _stats(name="MeanReversionAgent", pnl_20d_after_costs=-1200,
                   trades_20d=4, flagged=True),
            _stats(name="TechnicalAgent", pnl_20d_after_costs=-1100,
                   trades_20d=5, flagged=True),
            _stats(name="NewsAgent", pnl_20d_after_costs=50, trades_20d=12),
        ]
        report = EvalReport(
            generated_at="test",
            agents=agents,
            flagged_agents=["MeanReversionAgent", "TechnicalAgent"],
        )
        summary = {
            "MeanReversionAgent": {"active": True},
            "TechnicalAgent": {"active": True},
            "NewsAgent": {"active": True},
        }
        result = self._run(report, summary)
        benches = [a for a in result["actions"] if a.startswith("BENCHED")]
        skipped = [a for a in result["actions"] if a.startswith("SKIPPED")]
        self.assertEqual(len(benches), 1, result["actions"])
        self.assertTrue(any("minimum active agents" in a for a in skipped))
        self.assertEqual(MIN_ACTIVE_AGENTS, 2)

    def test_promote_prefers_positive_expectancy(self):
        agents = [
            _stats(name="TechnicalAgent", pnl_20d_after_costs=-900,
                   trades_20d=12, flagged=True,
                   expectancy_after_costs_20d=-80.0),
            _stats(name="MomentumAgent", pnl_20d_after_costs=200,
                   trades_20d=12, active=False,
                   expectancy_after_costs_20d=16.0),
            _stats(name="BreakoutAgent", pnl_20d_after_costs=-400,
                   trades_20d=12, active=False,
                   expectancy_after_costs_20d=-30.0),
            _stats(name="NewsAgent", pnl_20d_after_costs=80, trades_20d=12),
            _stats(name="SentimentAgent", pnl_20d_after_costs=40, trades_20d=12),
        ]
        report = EvalReport(
            generated_at="test",
            agents=agents,
            flagged_agents=["TechnicalAgent"],
        )
        summary = {
            "TechnicalAgent": {"active": True},
            "MomentumAgent": {"active": False},
            "BreakoutAgent": {"active": False},
            "NewsAgent": {"active": True},
            "SentimentAgent": {"active": True},
        }
        result = self._run(report, summary)
        self.assertTrue(
            any("BENCHED TechnicalAgent → PROMOTED MomentumAgent" in a
                for a in result["actions"]),
            result["actions"],
        )

    def test_promote_refuses_known_negative_expectancy(self):
        agents = [
            _stats(name="TechnicalAgent", pnl_20d_after_costs=-900,
                   trades_20d=12, flagged=True,
                   expectancy_after_costs_20d=-80.0),
            _stats(name="MomentumAgent", pnl_20d_after_costs=-400,
                   trades_20d=12, active=False,
                   expectancy_after_costs_20d=-30.0),
            _stats(name="BreakoutAgent", pnl_20d_after_costs=-500,
                   trades_20d=12, active=False,
                   expectancy_after_costs_20d=-40.0),
            _stats(name="NewsAgent", pnl_20d_after_costs=80, trades_20d=12),
            _stats(name="SentimentAgent", pnl_20d_after_costs=40, trades_20d=12),
        ]
        report = EvalReport(
            generated_at="test",
            agents=agents,
            flagged_agents=["TechnicalAgent"],
        )
        summary = {
            "TechnicalAgent": {"active": True},
            "MomentumAgent": {"active": False},
            "BreakoutAgent": {"active": False},
            "NewsAgent": {"active": True},
            "SentimentAgent": {"active": True},
        }
        result = self._run(report, summary)
        self.assertTrue(
            any("BENCHED TechnicalAgent (no replacement" in a
                for a in result["actions"]),
            result["actions"],
        )
        self.assertFalse(any("PROMOTED" in a for a in result["actions"]))


class PaperOnlyUntouched(unittest.TestCase):
    def test_executor_still_hardwires_paper(self):
        import inspect
        try:
            import order_executor
        except ImportError as e:
            self.skipTest(f"order_executor deps missing: {e}")
        src = inspect.getsource(order_executor.OrderExecutor.__init__)
        self.assertIn("paper=True", src)
        self.assertNotIn("paper=PAPER_TRADING", src)


if __name__ == "__main__":
    unittest.main()
