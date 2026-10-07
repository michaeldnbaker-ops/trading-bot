"""Broker-fill scoring, 1/N split, option x100, short sign, reconcile.

No live broker. Paper-only and CRYPTO_TRADING_ENABLED stay as they are.
"""

from __future__ import annotations

import inspect
import json
import tempfile
import unittest
from datetime import datetime
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
    scoring_skip_reason,
    split_amount,
    split_lot_flags,
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

            def get(self, path, params=None):
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


class PhantomClosedLot(unittest.TestCase):
    """SPY 88374c40e400 and the durable closed+open lot rule.

    Learning Loop 2026-10-05. The ledger row is not deleted.
    """

    def _phantom(self, pnl=258.57, entry=729.79, shares=11, symbol="SPY"):
        trade = _trade(
            "BreakoutAgent", pnl, days_ago=1, symbol=symbol, shares=shares, entry=entry,
        )
        trade.trade_id = "88374c40e400"
        trade.status = "target"
        trade.exit_price = 753.30
        return trade

    def _eval(self, trades, round_trips=None, broker_positions=None):
        ev = AgentEvaluator()
        kwargs = {}
        if round_trips is not None:
            kwargs["round_trips"] = round_trips
        if broker_positions is not None:
            kwargs["broker_positions"] = broker_positions
        with patch("agent_evaluator._today_et_date", return_value=TODAY), \
             patch("trade_ledger.epoch_trades", return_value=trades), \
             patch("agent_evaluator._agent_active_state", return_value={}):
            return ev.evaluate(**kwargs)

    def test_flagged_spy_row_is_excluded_even_if_a_fill_matches(self):
        phantom = self._phantom()
        real = _trade("NewsAgent", 50.0, days_ago=1, symbol="AMD")
        trip = RoundTrip(
            symbol="SPY", side="LONG", qty=11, entry_price=729.79, exit_price=753.30,
            entry_time="2026-09-19T14:00:00Z", exit_time="2026-09-19T18:00:00Z",
            realized_pnl=258.57,
        )
        report = self._eval([phantom, real], round_trips=[trip])
        by = {a.name: a for a in report.agents}
        self.assertNotIn("BreakoutAgent", by)
        self.assertAlmostEqual(by["NewsAgent"].pnl_20d, 50.0)
        self.assertIn("phantom", report.scoring_note)
        rows = compare_ledger([phantom], [trip])
        self.assertFalse(rows[0].score)
        self.assertEqual(rows[0].slices, [])
        self.assertIn("PHANTOM", rows[0].note)
        self.assertIn("88374c40e400", scoring_skip_reason(phantom, [phantom]) or "")

    def test_durable_rule_skips_a_matching_open_lot_and_not_a_different_one(self):
        closed = _trade(
            "BreakoutAgent", 100.0, days_ago=1, symbol="SPY", shares=11, entry=729.79,
        )
        closed.trade_id = "deadbeefdead"
        twin = _trade(
            "NewsAgent", 0.0, days_ago=0, symbol="SPY", shares=11, entry=729.79, open_=True,
        )
        twin.trade_id = "openlotopen1"
        other = _trade("NewsAgent", 40.0, days_ago=2, symbol="QQQ", shares=5, entry=400)
        report = self._eval([closed, twin, other], round_trips=[])
        by = {a.name: a for a in report.agents}
        self.assertNotIn("BreakoutAgent", by)
        self.assertAlmostEqual(by["NewsAgent"].pnl_20d, 40.0)
        flags = split_lot_flags([closed, twin])
        self.assertEqual(len(flags), 1)
        self.assertTrue(flags[0]["message"].startswith("CRITICAL:"))
        self.assertIn("deadbeefdead", flags[0]["message"])
        self.assertIn("100.00", flags[0]["message"])

        different = _trade(
            "BreakoutAgent", 80.0, days_ago=1, symbol="SPY", shares=11, entry=700.0,
        )
        different.trade_id = "otherentry01"
        report = self._eval([different, twin], round_trips=[])
        by = {a.name: a for a in report.agents}
        self.assertAlmostEqual(by["BreakoutAgent"].pnl_20d, 80.0)
        self.assertEqual(split_lot_flags([different, twin]), [])

    def test_broker_lot_match_flags_without_deleting_history(self):
        closed = _trade(
            "BreakoutAgent", 258.57, days_ago=1, symbol="SPY", shares=11, entry=729.79,
        )
        closed.trade_id = "brokeronly01"
        closed.status = "target"
        held = {"symbol": "SPY", "qty": "11", "avg_entry_price": "729.79"}
        self.assertIsNotNone(scoring_skip_reason(closed, [closed], [held]))
        flags = split_lot_flags([closed], [held])
        self.assertEqual([f["trade_id"] for f in flags], ["brokeronly01"])
        # The closed object is unchanged — history is not rewritten.
        self.assertEqual(closed.status, "target")
        self.assertAlmostEqual(closed.realized_pnl, 258.57)
        # A different size is a different lot.
        other_size = {"symbol": "SPY", "qty": 20, "avg_entry_price": 729.79}
        self.assertIsNone(scoring_skip_reason(closed, [closed], [other_size]))
        self.assertEqual(split_lot_flags([closed], [other_size]), [])
        # The known id stays out of scoring after the live lot is gone.
        phantom = self._phantom()
        self.assertIsNotNone(scoring_skip_reason(phantom, [phantom], []))
        self.assertEqual(split_lot_flags([phantom], []), [])


class GatesUntouched(unittest.TestCase):
    def test_crypto_gate_and_paper_flag_stay_off_and_on(self):
        import session_gates
        self.assertIs(session_gates.CRYPTO_TRADING_ENABLED, False)
        self.assertIs(session_gates.PAPER_ONLY, True)
        import order_executor
        src = inspect.getsource(order_executor.OrderExecutor.__init__)
        self.assertIn("paper=True", src)


class OrderIdFills(unittest.TestCase):
    """REST paging, order-id match, close price, option attribution."""

    def test_activity_pages_walk_oldest_first_until_a_short_page(self):
        from broker_fills import fetch_fills

        class _Client:
            paper = True

            def __init__(self):
                self.calls = []

            def get(self, path, params=None):
                self.calls.append((path, dict(params or {})))
                token = (params or {}).get("page_token")
                if not token:
                    return [
                        {
                            "id": f"p{i:03d}",
                            "symbol": "AMD",
                            "side": "buy",
                            "qty": 1,
                            "price": 10 + i * 0.01,
                            "transaction_time": f"2026-07-01T14:{i % 60:02d}:{i // 60:02d}Z",
                            "order_id": f"o{i:03d}",
                        }
                        for i in range(100)
                    ]
                if token != "p099":
                    raise AssertionError(token)
                return [{
                    "id": "p100",
                    "symbol": "AMD",
                    "side": "sell",
                    "qty": 1,
                    "price": 12,
                    "transaction_time": "2026-07-02T15:00:00Z",
                    "order_id": "o100",
                }]

            def get_orders(self, *args, **kwargs):
                raise AssertionError("activities came back; closed orders must not be used")

        client = _Client()
        fills, source = fetch_fills(client, after="2026-06-01")
        self.assertEqual(source, "activities")
        self.assertEqual(len(fills), 101)
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(client.calls[0][0], "/account/activities/FILL")
        self.assertEqual(client.calls[0][1]["direction"], "asc")
        self.assertEqual(client.calls[0][1]["after"], "2026-06-01")
        self.assertNotIn("page_token", client.calls[0][1])
        self.assertEqual(client.calls[1][1]["page_token"], "p099")
        self.assertEqual(fills[-1]["order_id"], "o100")
        from broker_fills import activity_after_date

        class _Row:
            opened_at_et = "2026-07-02 09:31:00"

        self.assertEqual(activity_after_date([_Row()]), "2026-06-02")

    def test_empty_activities_warn_and_fall_back_without_secrets(self):
        from broker_fills import fetch_fills

        class _Down:
            paper = True

            def get(self, path, params=None):
                secret = "SUPERSECRETKEY"
                raise RuntimeError(f"activities failed {secret}")

            def get_orders(self, *args, **kwargs):
                return []

        class _Missing:
            paper = True

            def get_orders(self, *args, **kwargs):
                return [{
                    "symbol": "IBM", "side": "buy", "qty": 1, "price": 10,
                    "filled_at": "2026-08-07T14:00:00Z", "id": "closed-1",
                }]

        with patch.dict("os.environ", {"ALPACA_API_KEY": "SUPERSECRETKEY"}), \
             self.assertLogs("BrokerFills", level="WARNING") as logs:
            fills, source = fetch_fills(_Down(), after="2026-06-01")
        self.assertEqual(fills, [])
        self.assertEqual(source, "closed_orders")
        blob = "\n".join(logs.output)
        self.assertIn("falling back to closed orders", blob)
        self.assertNotIn("SUPERSECRETKEY", blob)
        self.assertIn("***", blob)

        with self.assertLogs("BrokerFills", level="WARNING") as logs:
            fills, source = fetch_fills(_Missing(), after="2026-06-01")
        self.assertEqual(source, "closed_orders")
        self.assertEqual(fills[0]["symbol"], "IBM")
        self.assertTrue(any("falling back to closed orders" in line for line in logs.output))

    def test_order_id_beats_fifo_when_an_older_lot_is_still_held(self):
        from broker_fills import assign_round_trips, match_method_counts
        july = {
            "symbol": "COIN", "side": "buy", "qty": 10, "price": 250,
            "time": "2026-07-08T14:00:00Z", "order_id": "july-lot",
        }
        opened = {
            "symbol": "COIN", "side": "buy", "qty": 10, "price": 200,
            "time": "2026-09-18T14:00:00Z", "order_id": "news-entry",
        }
        closed = {
            "symbol": "COIN", "side": "sell", "qty": 10, "price": 180,
            "time": "2026-09-18T18:00:00Z", "order_id": "news-exit",
        }
        trade = _trade("NewsAgent", 50.0, days_ago=1, symbol="COIN", shares=10, entry=200)
        trade.trade_id = "coin-news"
        trade.opened_at_et = "2026-09-18 10:00:00"
        trade.entry_order_id = "news-entry"
        trade.exit_order_id = "news-exit"
        trade.status = "stop"
        trade.realized_pnl = 50.0
        fills = [july, opened, closed]

        fifo = build_round_trips(fills)
        self.assertEqual(len(fifo), 1)
        self.assertAlmostEqual(fifo[0].entry_price, 250)
        self.assertAlmostEqual(fifo[0].realized_pnl, -700.0)
        missed = assign_round_trips([trade], fifo)
        self.assertEqual(missed["coin-news"], [])

        matched = build_round_trips(fills, trades=[trade])
        self.assertEqual(len(matched), 1)
        self.assertEqual(matched[0].order_ids, ("news-entry", "news-exit"))
        self.assertAlmostEqual(matched[0].entry_price, 200)
        self.assertAlmostEqual(matched[0].exit_price, 180)
        self.assertAlmostEqual(matched[0].realized_pnl, -200.0)
        claimed = assign_round_trips([trade], matched)
        self.assertEqual(len(claimed["coin-news"]), 1)
        self.assertEqual(match_method_counts(), {"order_id": 1, "fifo": 0, "unmatched": 0})

        ev = AgentEvaluator()
        with patch("agent_evaluator._today_et_date", return_value=datetime(2026, 10, 7)), \
             patch("trade_ledger.epoch_trades", return_value=[trade]), \
             patch("agent_evaluator._agent_active_state", return_value={}):
            report = ev.evaluate(round_trips=matched)
        by = {a.name: a for a in report.agents}
        self.assertAlmostEqual(by["NewsAgent"].pnl_20d, -200.0)
        self.assertNotAlmostEqual(by["NewsAgent"].pnl_20d, 50.0)
        self.assertIn("Order-id matches: 1", report.scoring_note)
        self.assertIn("FIFO fallback: 0", report.scoring_note)

    def test_close_records_avg_fill_not_the_trigger_or_a_later_quote(self):
        import trade_ledger as tl
        trade = tl.Trade(
            trade_id="bdf2554366ee", opened_at_et="2026-10-01 10:00:00",
            symbol="UAL", side="LONG", primary_agent="BreakoutAgent",
            contributors="", entry_price=80.0, target_price=93.47, stop_price=70.0,
            risk_dollar=320.0, shares=10, status="open",
        )
        activities = [
            {"symbol": "UAL", "side": "sell", "qty": 4, "price": 40.0,
             "transaction_time": "2026-10-06T15:00:00Z", "order_id": "trail"},
            {"symbol": "UAL", "side": "sell", "qty": 6, "price": 50.0,
             "transaction_time": "2026-10-06T15:00:02Z", "order_id": "trail"},
            {"symbol": "UAL", "side": "sell", "qty": 1, "price": 90.0,
             "transaction_time": "2026-10-06T19:00:00Z", "order_id": "later-print"},
            {"symbol": "UAL", "side": "buy", "qty": 10, "price": 95.0,
             "transaction_time": "2026-10-07T14:00:00Z", "order_id": "later-buy"},
        ]
        price, when, oid = tl.closing_fill(trade, activities)
        self.assertAlmostEqual(price, 46.0)
        self.assertEqual(oid, "trail")
        self.assertEqual(tl.broker_time_to_et(when), "2026-10-06 11:00:02")
        self.assertNotAlmostEqual(price, 93.47)
        self.assertNotAlmostEqual(price, 90.0)

        # The entry buy is not an exit, even when it is the fill someone
        # would book as a target. The trail's average is the loss.
        fresh = tl.Trade(
            trade_id="bdf2554366ee", opened_at_et="2026-10-01 10:00:00",
            symbol="UAL", side="LONG", primary_agent="BreakoutAgent",
            contributors="", entry_price=80.0, target_price=93.47, stop_price=70.0,
            risk_dollar=320.0, shares=10, status="open",
            entry_order_id="ual-entry",
        )
        entry = {
            "symbol": "UAL", "side": "buy", "id": "ual-entry",
            "filled_avg_price": 93.47, "filled_at": "2026-10-01T14:00:00Z",
        }
        self.assertFalse(tl.book_broker_close(fresh, entry))
        self.assertTrue(fresh.is_open)
        self.assertIsNone(fresh.exit_price)
        exit_order = {
            "symbol": "UAL", "side": "sell", "id": "trail",
            "filled_avg_price": 42.739, "filled_at": "2026-10-06T18:05:00Z",
        }
        self.assertTrue(tl.book_broker_close(fresh, exit_order))
        self.assertAlmostEqual(fresh.exit_price, 42.739)
        self.assertAlmostEqual(fresh.realized_pnl, -372.61)
        self.assertEqual(fresh.exit_at_et, "2026-10-06 14:05:00")
        self.assertEqual(fresh.exit_order_id, "trail")
        self.assertEqual(fresh.status, "stop")
        self.assertEqual(fresh.exit_reason, "broker avg fill")
        self.assertNotAlmostEqual(fresh.realized_pnl, 134.70)

    def test_option_round_trip_is_attributed_to_the_agent(self):
        import inspect
        import trade_ledger as tl
        from options_executor import _record_option_open, _stamp_option_exit_order, execute_options_trade
        from plain_report import closed_trades_from_books, trade_lines

        src = inspect.getsource(execute_options_trade)
        self.assertIn("_record_option_open", src)
        self.assertIn("order_id=", src)

        tmp = Path(tempfile.mkdtemp())
        orig = tl.LEDGER
        tl.LEDGER = tmp / "paper_trades.csv"
        try:
            tid = _record_option_open(
                {"agent": "MetaAgent(NewsAgent)"},
                {"symbol": "PTC261120C00195000"},
                3, 2.0, "opt-buy",
            )
            row = tl.load_ledger()[tid]
            self.assertEqual(row.entry_order_id, "opt-buy")
            self.assertEqual(row.leaf_agents, ["NewsAgent"])
            self.assertTrue(row.is_open)
            _stamp_option_exit_order("PTC261120C00195000", "opt-sell")
            row = tl.load_ledger()[tid]
            self.assertEqual(row.exit_order_id, "opt-sell")
        finally:
            tl.LEDGER = orig

        symbol = "PTC261120C00195000"
        trade = _trade("MetaAgent(NewsAgent)", 0.0, days_ago=1, symbol=symbol, shares=3, entry=2.0)
        trade.trade_id = "ptc-news"
        trade.opened_at_et = "2026-10-05 10:00:00"
        trade.status = "stop"
        trade.entry_order_id = "opt-buy"
        trade.exit_order_id = "opt-sell"
        fills = [
            {"symbol": symbol, "side": "buy", "qty": 3, "price": 2.0,
             "time": "2026-10-05T14:00:00Z", "order_id": "opt-buy"},
            {"symbol": symbol, "side": "sell", "qty": 3, "price": 1.2,
             "time": "2026-10-06T14:00:00Z", "order_id": "opt-sell"},
        ]
        trips = build_round_trips(fills, trades=[trade])
        self.assertEqual(len(trips), 1)
        self.assertAlmostEqual(trips[0].realized_pnl, -240.0)
        named = closed_trades_from_books(trips, [trade])
        self.assertEqual(named[0].strategies, ("News",))
        lines = trade_lines(named)
        self.assertFalse(any("not available" in line for line in lines))
        # plain_report writes losses as "down $240.00", not a signed figure.
        self.assertTrue(any(
            line.startswith("News:") and "down $240.00" in line for line in lines
        ))
        ev = AgentEvaluator()
        with patch("agent_evaluator._today_et_date", return_value=datetime(2026, 10, 7)), \
             patch("trade_ledger.epoch_trades", return_value=[trade]), \
             patch("agent_evaluator._agent_active_state", return_value={}):
            report = ev.evaluate(round_trips=trips)
        by = {a.name: a for a in report.agents}
        self.assertAlmostEqual(by["NewsAgent"].pnl_20d, -240.0)

    def test_backfill_is_dry_run_until_apply_and_old_csv_loads(self):
        import importlib.util
        import trade_ledger as tl
        spec = importlib.util.spec_from_file_location(
            "backfill_order_ids",
            Path(__file__).resolve().parent / "scripts" / "backfill_order_ids.py",
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        tmp = Path(tempfile.mkdtemp())
        ledger = tmp / "paper_trades.csv"
        log_path = tmp / "scheduler.log"
        header = (
            "trade_id,opened_at_et,symbol,side,primary_agent,contributors,"
            "entry_price,target_price,stop_price,risk_dollar,shares,status,"
            "exit_price,exit_at_et,exit_reason,realized_pnl,unrealized_pnl,"
            "current_price,last_updated_et"
        )
        ledger.write_text(
            header + "\n"
            "abc,2026-09-18 10:06:09,COIN,LONG,NewsAgent,,200,210,190,320,10,open,,,,,,,\n",
            encoding="utf-8",
        )
        log_path.write_text(
            "2026-09-18 10:06:09,237 [INFO] ✅ ORDER SUBMITTED: COIN LONG "
            "$1000 | qty=10 fill=200 | order_id=entry-1 | agent=NewsAgent\n"
            "2026-09-18 10:06:12,100 [INFO] 🪤 TRAIL SET: COIN exit trails 3.5% "
            "behind high-water mark (order exit-1) exit_order_id=exit-1 — upside uncapped\n",
            encoding="utf-8",
        )
        orig = tl.LEDGER
        tl.LEDGER = ledger
        try:
            before = ledger.read_text(encoding="utf-8")
            loaded = tl.load_ledger()
            self.assertEqual(loaded["abc"].entry_order_id, "")
            self.assertEqual(loaded["abc"].exit_order_id, "")
            self.assertEqual(mod.run([], ledger_path=ledger, log_path=log_path), 0)
            self.assertEqual(ledger.read_text(encoding="utf-8"), before)
            self.assertFalse(list(tmp.glob("*.bak-*")))
            self.assertEqual(mod.run(["--apply"], ledger_path=ledger, log_path=log_path), 0)
            backups = list(tmp.glob("paper_trades.csv.bak-*"))
            self.assertEqual(len(backups), 1)
            row = tl.load_ledger()["abc"]
            self.assertEqual(row.entry_order_id, "entry-1")
            self.assertEqual(row.exit_order_id, "exit-1")
            self.assertIn("entry_order_id", ledger.read_text(encoding="utf-8").splitlines()[0])
        finally:
            tl.LEDGER = orig

    def test_fixture_dry_run_moves_news_breakout_and_intermarket(self):
        """Ledger (activities missing) vs order-id on the full fill set."""
        before, after = _fixture_scoreboard()
        self.assertEqual(before["NewsAgent"]["20d"], 50.0)
        self.assertEqual(before["NewsAgent"]["all"], 450.0)
        self.assertEqual(before["BreakoutAgent"]["20d"], -40.0)
        self.assertEqual(before["BreakoutAgent"]["all"], -265.0)
        self.assertEqual(before["IntermarketAgent"]["20d"], -80.0)
        self.assertEqual(before["IntermarketAgent"]["all"], -380.0)
        self.assertEqual(after["NewsAgent"]["20d"], -200.0)
        self.assertEqual(after["NewsAgent"]["all"], -300.0)
        self.assertEqual(after["BreakoutAgent"]["20d"], 50.0)
        self.assertEqual(after["BreakoutAgent"]["all"], 850.0)
        self.assertEqual(after["IntermarketAgent"]["20d"], 50.0)
        self.assertEqual(after["IntermarketAgent"]["all"], -100.0)
        self.assertIn("Order-id matches: 6", after["note"])


def _closed(agent, pnl, symbol, opened, shares, entry, entry_id, exit_id, trade_id):
    trade = _trade(agent, pnl, days_ago=1, symbol=symbol, shares=shares, entry=entry)
    trade.trade_id = trade_id
    trade.opened_at_et = opened
    trade.status = "stop"
    trade.realized_pnl = pnl
    trade.entry_order_id = entry_id
    trade.exit_order_id = exit_id
    return trade


def _open(agent, symbol, opened, shares, entry, order_id, trade_id):
    trade = _trade(agent, 0.0, days_ago=0, symbol=symbol, shares=shares, entry=entry, open_=True)
    trade.trade_id = trade_id
    trade.opened_at_et = opened
    trade.entry_order_id = order_id
    return trade


def _fill(symbol, side, qty, price, when, order_id):
    return {
        "symbol": symbol, "side": side, "qty": qty, "price": price,
        "time": when, "order_id": order_id,
    }


def _fixture_book():
    trades = [
        _open("BreakoutAgent", "COIN", "2026-07-08 10:00:00", 10, 250, "july-lot", "july-coin"),
        _closed("NewsAgent", 50.0, "COIN", "2026-09-18 10:00:00", 10, 200,
                "news-entry", "news-exit", "coin-news"),
        _closed("NewsAgent", 400.0, "META", "2026-07-20 10:00:00", 10, 50,
                "meta-entry", "meta-exit", "meta-news"),
        _closed("BreakoutAgent", -225.0, "AMD", "2026-07-15 10:00:00", 20, 100,
                "amd-e", "amd-x", "amd-july"),
        _closed("BreakoutAgent", -40.0, "AMD", "2026-09-21 10:00:00", 5, 100,
                "amd2-e", "amd2-x", "amd-sept"),
        _open("IntermarketAgent", "QQQ", "2026-07-20 10:00:00", 5, 500, "q-old", "qqq-open"),
        _closed("IntermarketAgent", -80.0, "QQQ", "2026-09-22 10:00:00", 5, 400,
                "q-e", "q-x", "qqq-sept"),
        _closed("IntermarketAgent", -300.0, "IWM", "2026-07-10 10:00:00", 4, 200,
                "iwm-e", "iwm-x", "iwm-july"),
    ]
    fills = [
        _fill("COIN", "buy", 10, 250, "2026-07-08T14:00:00Z", "july-lot"),
        _fill("COIN", "buy", 10, 200, "2026-09-18T14:00:00Z", "news-entry"),
        _fill("COIN", "sell", 10, 180, "2026-09-18T18:00:00Z", "news-exit"),
        _fill("META", "buy", 10, 50, "2026-07-20T14:00:00Z", "meta-entry"),
        _fill("META", "sell", 10, 40, "2026-07-21T14:00:00Z", "meta-exit"),
        _fill("AMD", "buy", 20, 100, "2026-07-15T14:00:00Z", "amd-e"),
        _fill("AMD", "sell", 20, 140, "2026-07-16T14:00:00Z", "amd-x"),
        _fill("AMD", "buy", 5, 100, "2026-09-21T14:00:00Z", "amd2-e"),
        _fill("AMD", "sell", 5, 110, "2026-09-22T14:00:00Z", "amd2-x"),
        _fill("QQQ", "buy", 5, 500, "2026-07-20T14:00:00Z", "q-old"),
        _fill("QQQ", "buy", 5, 400, "2026-09-22T14:00:00Z", "q-e"),
        _fill("QQQ", "sell", 5, 410, "2026-09-23T14:00:00Z", "q-x"),
        _fill("IWM", "buy", 4, 200, "2026-07-10T14:00:00Z", "iwm-e"),
        _fill("IWM", "sell", 4, 162.5, "2026-07-11T14:00:00Z", "iwm-x"),
    ]
    return trades, fills


def _board(report) -> dict:
    out = {}
    for agent in report.agents:
        out[agent.name] = {"20d": agent.pnl_20d, "all": agent.pnl_alltime}
    out["note"] = report.scoring_note
    return out


def _fixture_scoreboard():
    from datetime import datetime as dt
    trades, fills = _fixture_book()
    today = dt(2026, 10, 7)
    ev = AgentEvaluator()
    with patch("agent_evaluator._today_et_date", return_value=today), \
         patch("trade_ledger.epoch_trades", return_value=trades), \
         patch("agent_evaluator._agent_active_state", return_value={}):
        before = _board(ev.evaluate())
        trips = build_round_trips(fills, trades=trades)
        after = _board(ev.evaluate(round_trips=trips))
    return before, after


if __name__ == "__main__":
    unittest.main()
