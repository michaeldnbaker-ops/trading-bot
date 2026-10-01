"""Broker-fill scoring, 1/N split, option x100, short sign, reconcile.

No live broker. Paper-only and CRYPTO_TRADING_ENABLED stay as they are.
"""

from __future__ import annotations

import inspect
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_evaluator import AgentEvaluator, AgentStats, EvalReport
from agent_rotator import AgentRotator
from broker_fills import (
    RoundTrip,
    _ReadOnly,
    agent_pnl_deltas,
    build_round_trips,
    compare_ledger,
    realized_on_date,
    realized_pnl,
    split_amount,
)
from report_data import reconcile_line, unexplained_day_gap
from test_learning_loop import TODAY, _trade


def _fills_round_trip(symbol, side_open, qty, entry, exit_, opened, closed):
    open_side = "buy" if side_open == "LONG" else "sell"
    close_side = "sell" if side_open == "LONG" else "buy"
    return [
        {"symbol": symbol, "side": open_side, "qty": qty, "price": entry,
         "transaction_time": opened},
        {"symbol": symbol, "side": close_side, "qty": qty, "price": exit_,
         "transaction_time": closed},
    ]


class SplitAndSign(unittest.TestCase):
    def test_one_over_n_absorbs_remainder(self):
        parts = split_amount(-300.0, 3)
        self.assertEqual(len(parts), 3)
        self.assertAlmostEqual(sum(parts), -300.0)
        self.assertTrue(all(abs(p - (-100)) < 0.02 for p in parts))

    def test_co_signed_evaluator_splits_ledger_pnl(self):
        trades = [
            _trade(
                "MetaAgent(NewsAgent, BreakoutAgent, MomentumAgent)",
                -300.0, days_ago=1, symbol="AMD",
            )
        ]
        ev = AgentEvaluator()
        with patch("agent_evaluator._today_et_date", return_value=TODAY), \
             patch("trade_ledger.epoch_trades", return_value=trades), \
             patch("agent_evaluator._agent_active_state", return_value={}):
            report = ev.evaluate()
        by = {a.name: a for a in report.agents}
        names = ["NewsAgent", "BreakoutAgent", "MomentumAgent"]
        self.assertEqual(sorted(by), sorted(names))
        self.assertAlmostEqual(sum(by[n].pnl_20d for n in names), -300.0)
        self.assertTrue(all(by[n].trades_20d == 1 for n in names))
        # One $5 round-trip friction (100 sh * $50 * 10 bps), not $5 each.
        self.assertAlmostEqual(
            sum(by[n].pnl_20d_after_costs for n in names), -305.0, places=2,
        )

    def test_short_round_trip_is_positive_when_price_falls(self):
        trips = build_round_trips(_fills_round_trip(
            "IBM", "SHORT", 10, 50, 40,
            "2026-09-01T14:00:00Z", "2026-09-02T14:00:00Z",
        ))
        self.assertEqual(len(trips), 1)
        self.assertEqual(trips[0].side, "SHORT")
        self.assertAlmostEqual(trips[0].realized_pnl, 100.0)
        # The long formula on this cover would book a loss.
        self.assertAlmostEqual((40 - 50) * 10, -100.0)
        self.assertGreater(trips[0].realized_pnl, 0)

    def test_option_premium_uses_contract_multiplier(self):
        symbol = "AAPL251219C00100000"
        trips = build_round_trips(_fills_round_trip(
            symbol, "LONG", 2, 1.50, 2.50,
            "2026-09-01T14:00:00Z", "2026-09-02T14:00:00Z",
        ))
        self.assertEqual(len(trips), 1)
        # (2.50 - 1.50) * 2 contracts * 100. Without x100 this is $2.
        self.assertAlmostEqual(trips[0].realized_pnl, 200.0)
        self.assertAlmostEqual(realized_pnl("LONG", 1.50, 2.50, 2, symbol), 200.0)
        self.assertAlmostEqual(realized_pnl("LONG", 1.50, 2.50, 2, "AAPL"), 2.0)

    def test_partial_fills_sum_to_one_round_trip(self):
        fills = [
            {"symbol": "CSCO", "side": "buy", "qty": 6, "price": 10,
             "transaction_time": "2026-09-01T14:00:00Z"},
            {"symbol": "CSCO", "side": "buy", "qty": 4, "price": 12,
             "transaction_time": "2026-09-01T14:01:00Z"},
            {"symbol": "CSCO", "side": "sell", "qty": 10, "price": 15,
             "transaction_time": "2026-09-02T14:00:00Z"},
        ]
        trips = build_round_trips(fills)
        self.assertEqual(len(trips), 2)
        self.assertAlmostEqual(sum(t.realized_pnl for t in trips), 42.0)


class BrokerFillScoring(unittest.TestCase):
    def test_evaluator_uses_fill_not_signal_pnl(self):
        trade = _trade(
            "MetaAgent(NewsAgent, BreakoutAgent)", 999.0,
            days_ago=1, symbol="AMD", shares=10, entry=100.0,
        )
        trip = RoundTrip(
            symbol="AMD", side="LONG", qty=10, entry_price=102.0, exit_price=108.0,
            entry_time="2026-09-19T14:00:00Z", exit_time="2026-09-19T18:00:00Z",
            realized_pnl=60.0,
        )
        ev = AgentEvaluator()
        with patch("agent_evaluator._today_et_date", return_value=TODAY), \
             patch("trade_ledger.epoch_trades", return_value=[trade]), \
             patch("agent_evaluator._agent_active_state", return_value={}):
            report = ev.evaluate(round_trips=[trip])
        by = {a.name: a for a in report.agents}
        self.assertAlmostEqual(by["NewsAgent"].pnl_20d, 30.0)
        self.assertAlmostEqual(by["BreakoutAgent"].pnl_20d, 30.0)
        self.assertAlmostEqual(
            by["NewsAgent"].pnl_20d + by["BreakoutAgent"].pnl_20d, 60.0,
        )
        # Not the ledger's $999, and not $999 copied onto each leaf.
        self.assertNotAlmostEqual(by["NewsAgent"].pnl_20d, 999.0)
        self.assertIn("broker-fill", report.scoring_note)

    def test_unmatched_trade_falls_back_to_ledger_and_logs(self):
        trade = _trade("NewsAgent", -40.0, days_ago=1, symbol="CAAP")
        ev = AgentEvaluator()
        with patch("agent_evaluator._today_et_date", return_value=TODAY), \
             patch("trade_ledger.epoch_trades", return_value=[trade]), \
             patch("agent_evaluator._agent_active_state", return_value={}), \
             self.assertLogs("BrokerFills", level="WARNING") as logs:
            report = ev.evaluate(round_trips=[])
        by = {a.name: a for a in report.agents}
        self.assertAlmostEqual(by["NewsAgent"].pnl_20d, -40.0)
        self.assertTrue(any("unmatched" in line and "CAAP" in line for line in logs.output))


class ReconcileLikeWithLike(unittest.TestCase):
    def test_sep30_definitional_gap_is_not_a_booking_gap(self):
        # Broker day -$374, no closes, option marks -$40. Old line reported
        # ~$335 unexplained. Both realized sides are 0.
        self.assertAlmostEqual(unexplained_day_gap(0, 0, -374), 0)
        line = reconcile_line(-374, 0, 0, -334)
        self.assertIn("gap $0", line)
        self.assertIn("Unrealized change today $-334", line)
        self.assertIn("mark-to-market $-374", line)
        self.assertNotIn("unexplained booking gap", line)

    def test_oct1_lifetime_realized_is_not_compared_to_day_mark(self):
        # Closes summed to about +$914 lifetime. Day mark was -$358.
        # Old gap abs(-358 - 914 - (-110)) ~= $1,162. Same realized
        # numbers are a booking gap of 0; the marks sit on the other line.
        old_gap = abs(-358 - 914 - (-110))
        self.assertAlmostEqual(old_gap, 1162)
        self.assertAlmostEqual(unexplained_day_gap(914, 914, -110), 0)
        line = reconcile_line(-358, 914, 914, -110)
        self.assertIn("gap $0", line)
        self.assertNotIn("1,162", line)
        self.assertNotIn("unexplained booking gap", line)

    def test_real_booking_gap_still_stands_out(self):
        # Under the $250 tolerance the dollars are still on the line.
        quiet = reconcile_line(-358, 700, 914, -110)
        self.assertIn("gap $214", quiet)
        self.assertNotIn("unexplained booking gap", quiet)
        # Past the tolerance, the booking gap is the warning — not the
        # mark-to-market remainder.
        loud = reconcile_line(-358, 500, 914, -110)
        self.assertIn("unexplained booking gap", loud)
        self.assertIn("$414", loud)
        self.assertAlmostEqual(unexplained_day_gap(500, 914, -110), 414)


class OptionAndShortNotes(unittest.TestCase):
    def test_missing_x100_and_short_sign_are_named(self):
        symbol = "AAPL251219C00100000"
        opt = _trade("NewsAgent", 1.0, days_ago=1, symbol=symbol, shares=1, entry=2.0)
        opt.opened_at_et = "2026-09-01 10:00:00"
        opt.exit_price = 3.0
        opt.realized_pnl = 1.0  # (3-2)*1, premium not ×100
        short = _trade("ShortMomentumAgent", -100.0, days_ago=1, symbol="IBM",
                       shares=10, entry=50.0)
        short.side = "SHORT"
        short.opened_at_et = "2026-09-01 10:00:00"
        short.exit_price = 40.0
        short.realized_pnl = (40 - 50) * 10  # long formula on a cover
        fills = _fills_round_trip(
            symbol, "LONG", 1, 2.0, 3.0,
            "2026-09-01T14:00:00Z", "2026-09-02T14:00:00Z",
        ) + _fills_round_trip(
            "IBM", "SHORT", 10, 50, 40,
            "2026-09-01T14:00:00Z", "2026-09-02T14:00:00Z",
        )
        rows = compare_ledger([opt, short], build_round_trips(fills))
        by = {r.symbol: r for r in rows}
        self.assertAlmostEqual(by[symbol].broker_pnl, 100.0)
        self.assertIn("OPTION x100", by[symbol].note)
        self.assertAlmostEqual(by["IBM"].broker_pnl, 100.0)
        self.assertIn("SIGN", by["IBM"].note)
        deltas = agent_pnl_deltas(rows)
        # Ledger booked the short as a loss; the fill was a gain.
        self.assertGreater(deltas["ShortMomentumAgent"], 0)
        self.assertGreater(deltas["NewsAgent"], 0)

    def test_ledger_exit_while_broker_still_holds(self):
        trade = _trade("BreakoutAgent", 50.0, days_ago=1, symbol="HOOD")
        rows = compare_ledger([trade], [], broker_open={"HOOD"})
        self.assertIsNone(rows[0].broker_pnl)
        self.assertIn("LEDGER EXITED EARLY", rows[0].note)


class SameNumberAndCryptoGate(unittest.TestCase):
    def test_rotator_quotes_the_report_it_was_given(self):
        agents = [
            AgentStats(
                name="MeanReversionAgent", pnl_20d_after_costs=-807.50,
                trades_20d=2, flagged=True, active=True,
            ),
            AgentStats(name="NewsAgent", pnl_20d_after_costs=50, trades_20d=12, active=True),
            AgentStats(name="BreakoutAgent", pnl_20d_after_costs=40, trades_20d=12, active=True),
        ]
        report = EvalReport(
            generated_at="test",
            agents=agents,
            flagged_agents=["MeanReversionAgent"],
        )
        rotator = AgentRotator()
        with patch.object(
            rotator.evaluator, "evaluate",
            side_effect=AssertionError("second evaluate"),
        ):
            result = rotator.run_rotation(dry_run=True, report=report)
        benches = [a for a in result["actions"] if a.startswith("BENCHED MeanReversionAgent")]
        self.assertEqual(len(benches), 1, result["actions"])
        self.assertIn("-807.50", benches[0])
        self.assertIn("over 2 trades", benches[0])

    def test_crypto_is_gated_and_excluded_from_the_average(self):
        import session_gates
        self.assertIs(session_gates.CRYPTO_TRADING_ENABLED, False)
        trades = [
            _trade("CryptoAgent", 500.0, days_ago=1, symbol="BTC/USD"),
            _trade("NewsAgent", 50.0, days_ago=1, symbol="AMD"),
        ]
        ev = AgentEvaluator()
        with patch("agent_evaluator._today_et_date", return_value=TODAY), \
             patch("trade_ledger.epoch_trades", return_value=trades), \
             patch("agent_evaluator._agent_active_state", return_value={
                 "CryptoAgent": {"active": True},
                 "NewsAgent": {"active": True},
             }):
            report = ev.evaluate()
        by = {a.name: a for a in report.agents}
        self.assertTrue(by["CryptoAgent"].gated)
        self.assertFalse(by["CryptoAgent"].active)
        self.assertNotIn("CryptoAgent", report.flagged_agents)
        self.assertIn("gated", report.summary_text())
        news_after = by["NewsAgent"].pnl_20d_after_costs
        self.assertAlmostEqual(report.ensemble_avg_20d, news_after)
        self.assertNotAlmostEqual(
            report.ensemble_avg_20d,
            (news_after + by["CryptoAgent"].pnl_20d_after_costs) / 2,
        )

    def test_scorecard_shows_gated_and_the_eval_number(self):
        import daily_reporter as dr
        tmp = Path(tempfile.mkdtemp())
        (tmp / "agent_summary.json").write_text(json.dumps({
            "CryptoAgent": {"active": True, "total_pnl": 100.0, "trade_count": 4},
            "MeanReversionAgent": {"active": True, "total_pnl": 0.0, "trade_count": 0},
        }))
        (tmp / "latest_eval.json").write_text(json.dumps({
            "agents": [{
                "name": "MeanReversionAgent",
                "active": True,
                "pnl_20d_after_costs": 584.0,
                "trades_20d": 1,
                "expectancy_after_costs_20d": 584.0,
            }]
        }))
        orig = dr.LOGS_DIR
        dr.LOGS_DIR = tmp
        try:
            with patch.object(dr, "_load_meta_weights", return_value={
                "MeanReversionAgent": 0.4,
            }):
                roster = dr.scorecard_agent_roster({
                    "agent_attribution": [
                        {"agent": "MeanReversionAgent", "total_pnl": -807.5,
                         "pnl_after_costs": -807.5},
                    ]
                })
        finally:
            dr.LOGS_DIR = orig
        by = {r["name"]: r for r in roster}
        self.assertEqual(by["CryptoAgent"]["status"], "gated")
        self.assertAlmostEqual(by["CryptoAgent"]["weight"], 0.0)
        self.assertAlmostEqual(by["MeanReversionAgent"]["pnl"], 584.0)
        self.assertNotAlmostEqual(by["MeanReversionAgent"]["pnl"], -807.5)


class DiagnosticReadOnly(unittest.TestCase):
    def test_client_is_paper_and_module_does_not_write(self):
        import broker_fills
        import diagnose_ledger_vs_broker as diag
        self.assertIn("paper=True", inspect.getsource(broker_fills.make_paper_client))
        blob = inspect.getsource(diag)
        self.assertNotIn("save_ledger", blob)
        self.assertNotIn("submit_order", blob)
        self.assertNotIn("close_position", blob)

    def test_run_compares_fills_without_touching_the_network(self):
        import diagnose_ledger_vs_broker as diag

        class _Client:
            paper = True

            def get_account_activities(self, *args, **kwargs):
                return _fills_round_trip(
                    "AMGN", "LONG", 10, 100, 110,
                    "2026-09-01T15:00:00Z", "2026-10-01T15:00:00Z",
                )

            def get_all_positions(self):
                return []

            def submit_order(self, *args, **kwargs):
                raise AssertionError("diagnostic submitted an order")

        trade = _trade("NewsAgent", 591.0, days_ago=1, symbol="AMGN",
                       shares=10, entry=100.0)
        trade.opened_at_et = "2026-09-01 11:00:00"
        trade.exit_at_et = "2026-10-01 11:00:00"
        trade.realized_pnl = 591.0
        text = diag.run(client=_Client(), trades=[trade], day="2026-10-01")
        self.assertIn("READ-ONLY", text)
        self.assertIn("AMGN", text)
        self.assertIn("ledger too high", text)
        self.assertIn("booking gap", text)
        wrapped = _ReadOnly(_Client())
        with self.assertRaises(RuntimeError):
            wrapped.submit_order()

    def test_realized_on_date_uses_exit_et(self):
        trip = RoundTrip(
            symbol="CSCO", side="LONG", qty=1, entry_price=1, exit_price=2,
            entry_time="2026-09-30T15:00:00Z",
            exit_time="2026-10-01T20:30:00Z",  # 16:30 ET Oct 1
            realized_pnl=447.0,
        )
        self.assertAlmostEqual(realized_on_date([trip], "2026-10-01"), 447.0)
        self.assertAlmostEqual(realized_on_date([trip], "2026-09-30"), 0.0)


class GatesUntouched(unittest.TestCase):
    def test_crypto_gate_and_paper_flag_stay_off_and_on(self):
        import session_gates
        self.assertIs(session_gates.CRYPTO_TRADING_ENABLED, False)
        self.assertIs(session_gates.PAPER_ONLY, True)
        import order_executor
        src = inspect.getsource(order_executor.OrderExecutor.__init__)
        self.assertIn("paper=True", src)


if __name__ == "__main__":
    unittest.main()
