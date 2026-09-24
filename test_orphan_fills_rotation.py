"""Orphan exits, unfilled entries, and expectancy-gated reactivation.

No broker and no alpaca-py. Paper-only and the crypto hard-off stay as they are.
"""

from __future__ import annotations

import io
import sys
import types
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from agent_evaluator import MIN_TRADES_TO_EVALUATE, AgentStats, EvalReport
from agent_rotator import (
    REACTIVATION_MIN_TRADES,
    AgentRotator,
    is_pinned_bench,
    reactivation_decision,
)

ET = ZoneInfo("America/New_York")


def _stats(**kw) -> AgentStats:
    defaults = dict(
        name="TechnicalAgent",
        pnl_20d_after_costs=200.0,
        trades_20d=12,
        expectancy_after_costs_20d=16.0,
        expectancy_after_costs=16.0,
        active=False,
    )
    defaults.update(kw)
    return AgentStats(**defaults)


def _pos(symbol, qty, upl, plpc=0.0, avg=1.0, order_type=""):
    return SimpleNamespace(
        symbol=symbol, qty=qty, unrealized_pl=upl, unrealized_plpc=plpc,
        avg_entry_price=avg,
    )


def _ord(symbol, side, qty, oid="o1", order_type="trailing_stop"):
    return SimpleNamespace(
        symbol=symbol, side=side, qty=qty, id=oid, order_type=order_type,
    )


class _Broker:
    def __init__(self, positions, orders=None):
        self.positions = positions
        self.orders = list(orders or [])
        self.cancelled = []
        self.closed = []

    def get_all_positions(self):
        return self.positions

    def get_orders(self, req=None):
        return self.orders

    def cancel_order_by_id(self, oid):
        self.cancelled.append(oid)

    def close_position(self, sym):
        self.closed.append(sym)

    def get_open_position(self, sym):
        for p in self.positions:
            if p.symbol == sym:
                return p
        raise RuntimeError(sym)


class OrphanExits(unittest.TestCase):
    def _run(self, positions, orders=None, ledger=None, trail=None, opt=None):
        from market_scheduler import reconcile_orphan_positions
        client = _Broker(positions, orders)
        trail = trail or MagicMock(return_value=(SimpleNamespace(id="trail"), None))
        opt = opt or MagicMock(return_value={
            "placed": False, "error": "unsupported", "stop_price": 0.5, "qty": 1,
        })
        with patch("order_executor._submit_trail_with_retry", trail), \
             patch("options_executor.submit_option_protective_stop", opt):
            actions = reconcile_orphan_positions(client, set(ledger or []))
        return client, actions, trail, opt

    def test_kept_winner_does_not_cancel_a_live_trail(self):
        client, actions, trail, _opt = self._run(
            [_pos("GPC", 10, 1060)],
            [_ord("GPC", "sell", 10, order_type="trailing_stop")],
        )
        self.assertEqual(client.cancelled, [])
        self.assertEqual(client.closed, [])
        trail.assert_not_called()
        self.assertIn("trailing stop still active", actions[0]["message"])
        self.assertNotIn("NO protective exit", actions[0]["message"])

    def test_kept_winner_replaces_a_missing_exit(self):
        client, actions, trail, _opt = self._run([_pos("NFLX", 8, 877)])
        self.assertEqual(client.cancelled, [])
        self.assertEqual(client.closed, [])
        trail.assert_called_once()
        self.assertEqual(trail.call_args[0][2], 8)  # qty from the broker position
        msg = actions[0]["message"]
        self.assertIn("re-placed trailing stop", msg)
        self.assertNotIn("still active", msg)

    def test_undersized_trail_is_replaced_at_broker_qty(self):
        client, actions, trail, _opt = self._run(
            [_pos("P", 12, 40)],
            [_ord("P", "sell", 1, oid="tiny")],
        )
        self.assertEqual(client.cancelled, ["tiny"])
        self.assertEqual(client.closed, [])
        self.assertEqual(trail.call_args[0][2], 12)
        self.assertIn("re-placed trailing stop", actions[0]["message"])
        self.assertNotIn("still active", actions[0]["message"])

    def test_loser_cancels_then_closes(self):
        client, actions, trail, _opt = self._run(
            [_pos("SUNB", 4, -20)],
            [_ord("SUNB", "sell", 4, oid="exit")],
        )
        self.assertEqual(client.cancelled, ["exit"])
        self.assertEqual(client.closed, ["SUNB"])
        trail.assert_not_called()
        self.assertEqual(actions[0]["action"], "closed")

    def test_option_orphan_gets_an_options_stop_not_a_trail(self):
        sym = "FPS261016C00035000"
        placed = MagicMock(return_value={
            "placed": True, "stop_price": 1.75, "qty": 2, "error": "",
        })
        client, actions, trail, opt = self._run(
            [_pos(sym, 2, 180, avg=3.5)], opt=placed,
        )
        trail.assert_not_called()
        opt.assert_called_once()
        self.assertEqual(client.cancelled, [])
        self.assertEqual(client.closed, [])
        msg = actions[0]["message"]
        self.assertIn("options stop", msg)
        self.assertNotIn("trailing stop still active", msg)

    def test_option_stop_refusal_is_a_warning_not_a_lie(self):
        sym = "NOK261016C00010000"
        client, actions, trail, _opt = self._run([_pos(sym, 9, 90, avg=0.4)])
        trail.assert_not_called()
        self.assertEqual(client.closed, [])
        msg = actions[0]["message"]
        self.assertIn("NO protective exit", msg)
        self.assertIn("broker refused", msg)
        self.assertNotIn("trailing stop still active", msg)
        self.assertEqual(actions[0]["level"], "warning")

    def test_option_with_a_covering_stop_is_left_alone(self):
        sym = "FPS261016C00035000"
        client, actions, trail, opt = self._run(
            [_pos(sym, 2, 50, avg=3.5)],
            [_ord(sym, "sell", 2, oid="ostop", order_type="stop")],
        )
        self.assertEqual(client.cancelled, [])
        trail.assert_not_called()
        opt.assert_not_called()
        msg = actions[0]["message"]
        self.assertIn("protective exit still active", msg)
        self.assertNotIn("trailing stop", msg)

    def test_ledgered_symbol_is_not_an_orphan(self):
        client, actions, trail, _opt = self._run(
            [_pos("AAPL", 5, 10)], ledger=["AAPL"],
        )
        self.assertEqual(actions, [])
        self.assertEqual(client.closed, [])
        trail.assert_not_called()


class UnfilledEntries(unittest.TestCase):
    def _executor(self):
        from order_executor import OrderExecutor
        ex = OrderExecutor()
        ex._client = object()
        return ex

    def test_unfilled_is_not_logged_as_submitted_or_ledgered(self):
        ex = self._executor()
        ex._submit_equity_bracket = lambda *a, **k: {
            "status": "unfilled", "symbol": "P", "order_id": "abc",
            "qty": 0, "cancelled": True,
        }
        ex._record_ledger = MagicMock()
        with patch("session_gates.is_rth", return_value=True), \
             self.assertLogs("OrderExecutor", level="INFO") as logs:
            result = ex.execute({
                "symbol": "P", "direction": "long", "entry_price": 100.0,
                "stop_loss_price": 96.0, "target_price": 110.0,
                "agent": "Test", "position_size_usd": 500.0,
            })
        self.assertEqual(result["status"], "unfilled")
        ex._record_ledger.assert_not_called()
        blob = "\n".join(logs.output)
        self.assertNotIn("ORDER SUBMITTED", blob)
        self.assertIn("ORDER UNFILLED", blob)

    def test_ledger_uses_fill_price_and_qty(self):
        ex = self._executor()
        captured = {}

        def fake_record(**kw):
            captured.update(kw)
            return "tid"

        with patch("trade_ledger.record_trade", fake_record):
            ex._record_ledger(
                {"symbol": "P", "direction": "long", "entry_price": 100.0,
                 "stop_loss_price": 96.0, "target_price": 110.0, "agent": "T"},
                {"status": "submitted", "qty": 12, "fill_price": 122.21,
                 "order_id": "o1"},
            )
        self.assertEqual(captured["entry_price"], 122.21)
        self.assertEqual(captured["shares"], 12)
        self.assertGreater(captured["risk_dollar"], 0)
        self.assertNotEqual(captured["entry_price"], 100.0)

    def test_zero_qty_is_not_ledgered(self):
        ex = self._executor()
        with patch("trade_ledger.record_trade") as rec:
            ex._record_ledger(
                {"symbol": "P", "direction": "long", "entry_price": 100.0,
                 "stop_loss_price": 96.0, "target_price": 110.0, "agent": "T"},
                {"status": "unfilled", "qty": 0},
            )
        rec.assert_not_called()

    def _install_alpaca_fakes(self):
        class _Req:
            def __init__(self, **kw):
                self.__dict__.update(kw)

        class _Side:
            BUY = "buy"
            SELL = "sell"

        class _Tif:
            DAY = "day"
            GTC = "gtc"

        req = types.ModuleType("alpaca.trading.requests")
        req.MarketOrderRequest = _Req
        req.TrailingStopOrderRequest = _Req
        req.GetOrdersRequest = _Req
        enums = types.ModuleType("alpaca.trading.enums")
        enums.OrderSide = _Side
        enums.TimeInForce = _Tif
        enums.QueryOrderStatus = SimpleNamespace(OPEN="open")
        trading = types.ModuleType("alpaca.trading")
        alpaca = types.ModuleType("alpaca")
        return {
            "alpaca": alpaca,
            "alpaca.trading": trading,
            "alpaca.trading.requests": req,
            "alpaca.trading.enums": enums,
        }

    def test_working_order_is_cancelled_when_nothing_fills(self):
        from order_executor import OrderExecutor

        class Client:
            def __init__(self):
                self.cancelled = []
                self.submitted = []

            def submit_order(self, req):
                self.submitted.append(req)
                return SimpleNamespace(id="entry-1")

            def get_order_by_id(self, oid):
                return SimpleNamespace(
                    id=oid, status="new", filled_qty=0, filled_avg_price=None)

            def get_open_position(self, symbol):
                raise RuntimeError("flat")

            def cancel_order_by_id(self, oid):
                self.cancelled.append(oid)

        client = Client()
        ex = OrderExecutor()
        ex._client = client
        with patch.dict(sys.modules, self._install_alpaca_fakes()), \
             patch("time.sleep", return_value=None):
            result = ex._submit_equity_bracket("P", "long", 100.0, 96.0, 110.0, 1200.0)
        self.assertEqual(result["status"], "unfilled")
        self.assertEqual(result["qty"], 0)
        self.assertEqual(client.cancelled, ["entry-1"])
        self.assertFalse(any(getattr(r, "trail_percent", None) for r in client.submitted))

    def test_late_fill_is_protected_at_broker_qty_and_price(self):
        from order_executor import OrderExecutor

        class Client:
            """1 share prints during the wait; the rest fills as we cancel."""

            def __init__(self):
                self.phase = "partial"
                self.cancelled = []
                self.submitted = []

            def submit_order(self, req):
                self.submitted.append(req)
                if getattr(req, "trail_percent", None) is not None:
                    return SimpleNamespace(id="trail-1")
                return SimpleNamespace(id="entry-1")

            def get_order_by_id(self, oid):
                if self.phase == "partial":
                    return SimpleNamespace(
                        id=oid, status="partially_filled",
                        filled_qty=1, filled_avg_price="122.21")
                return SimpleNamespace(
                    id=oid, status="filled",
                    filled_qty=12, filled_avg_price="122.21")

            def get_open_position(self, symbol):
                qty = "1" if self.phase == "partial" else "12"
                return SimpleNamespace(symbol=symbol, qty=qty)

            def cancel_order_by_id(self, oid):
                self.cancelled.append(oid)
                self.phase = "filled"

        client = Client()
        ex = OrderExecutor()
        ex._client = client
        with patch.dict(sys.modules, self._install_alpaca_fakes()), \
             patch("time.sleep", return_value=None):
            result = ex._submit_equity_bracket("P", "long", 100.0, 96.0, 110.0, 1500.0)
        self.assertEqual(result["status"], "submitted")
        self.assertEqual(result["qty"], 12)
        self.assertEqual(result["fill_price"], 122.21)
        self.assertIn("entry-1", client.cancelled)
        trails = [r for r in client.submitted if getattr(r, "trail_percent", None)]
        self.assertEqual(len(trails), 1)
        self.assertEqual(trails[0].qty, 12)


class BackstopSizesFromBroker(unittest.TestCase):
    def test_undersized_trail_is_replaced_with_position_qty(self):
        from order_executor import ensure_protective_exits, _protect_failed_at
        _protect_failed_at.clear()

        class _Pos:
            def __init__(self, symbol, qty):
                self.symbol, self.qty = symbol, qty
                self.unrealized_plpc = 0

        class _Ord:
            def __init__(self, symbol, side, qty, oid):
                self.symbol, self.side, self.qty, self.id = symbol, side, qty, oid

        class _Client:
            def __init__(self):
                self.submitted = []
                self.cancelled = []

            def get_all_positions(self):
                return [_Pos("P", 12)]

            def get_orders(self, _req=None):
                return [_Ord("P", "sell", 1, "tiny")]

            def get_open_position(self, symbol):
                return _Pos(symbol, 12)

            def cancel_order_by_id(self, oid):
                self.cancelled.append(oid)

            def submit_order(self, req):
                self.submitted.append(req)
                return SimpleNamespace(id="trail-full")

        client = _Client()

        class _Req:
            def __init__(self, **kw):
                self.__dict__.update(kw)

        class _Enums:
            class QueryOrderStatus:
                OPEN = "open"
            class OrderSide:
                SELL = "sell"
                BUY = "buy"
            class TimeInForce:
                GTC = "gtc"

        fake_requests = types.ModuleType("alpaca.trading.requests")
        fake_requests.TrailingStopOrderRequest = _Req
        fake_requests.GetOrdersRequest = _Req
        fake_enums = types.ModuleType("alpaca.trading.enums")
        fake_enums.QueryOrderStatus = _Enums.QueryOrderStatus
        fake_enums.OrderSide = _Enums.OrderSide
        fake_enums.TimeInForce = _Enums.TimeInForce
        with patch("order_executor.paper_only_violation", return_value=None), \
             patch.dict(sys.modules, {
                 "alpaca": types.ModuleType("alpaca"),
                 "alpaca.trading": types.ModuleType("alpaca.trading"),
                 "alpaca.trading.requests": fake_requests,
                 "alpaca.trading.enums": fake_enums,
             }), \
             patch("time.sleep", return_value=None):
            result = ensure_protective_exits(client)
        self.assertEqual(result["protected"], ["P"])
        self.assertEqual(client.cancelled, ["tiny"])
        self.assertEqual(client.submitted[0].qty, 12)


class ExpectancyReactivation(unittest.TestCase):
    def _run(self, summary, agents, dry_run=True):
        rotator = AgentRotator()
        report = EvalReport(generated_at="test", agents=agents, flagged_agents=[])
        with patch.object(rotator.evaluator, "evaluate", return_value=report), \
             patch.object(rotator.logger, "get_summary", return_value=summary), \
             patch.object(rotator, "_write_rotation_event") as ev:
            result = rotator.run_rotation(dry_run=dry_run)
        return result, summary, ev

    def test_positive_expectancy_reactivates_after_bench(self):
        name = "TechnicalAgent"
        benched = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        summary = {name: {"active": False, "benched_at": benched}}
        result, summary, ev = self._run(summary, [_stats(name=name)])
        self.assertTrue(any(a.startswith(f"REACTIVATED {name}") for a in result["actions"]))
        self.assertIn("after-cost expectancy", result["actions"][0])
        ev.assert_called()
        self.assertEqual(ev.call_args[0][1], "REACTIVATED")
        self.assertEqual(REACTIVATION_MIN_TRADES, MIN_TRADES_TO_EVALUATE)
        # dry_run must not clear the bench; a real cycle does.
        self.assertFalse(summary[name]["active"])
        import tempfile
        from pathlib import Path
        summary_live = {name: {"active": False, "benched_at": benched}}
        rotator = AgentRotator()
        report = EvalReport(
            generated_at="test", agents=[_stats(name=name)], flagged_agents=[])
        with tempfile.TemporaryDirectory() as d:
            with patch.object(rotator.evaluator, "evaluate", return_value=report), \
                 patch.object(rotator.logger, "get_summary", return_value=summary_live), \
                 patch.object(rotator, "_write_rotation_event"), \
                 patch("agent_rotator.SUMMARY", Path(d) / "agent_summary.json"):
                rotator.run_rotation(dry_run=False)
        self.assertTrue(summary_live[name]["active"])
        self.assertIsNone(summary_live[name]["benched_at"])

    def test_negative_expectancy_stays_benched(self):
        name = "TechnicalAgent"
        benched = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        summary = {name: {"active": False, "benched_at": benched}}
        buf = io.StringIO()
        with redirect_stdout(buf):
            result, summary, ev = self._run(summary, [
                _stats(name=name, expectancy_after_costs_20d=-4.5, trades_20d=12),
            ])
        self.assertEqual(result["actions"], [])
        self.assertTrue(any("stays benched" in h and "not positive" in h
                            for h in result["holds"]))
        text = buf.getvalue()
        self.assertIn("No rotations needed", text)
        self.assertIn("Holds — still benched", text)
        self.assertFalse(summary[name]["active"])
        ev.assert_not_called()

    def test_too_few_trades_stays_benched(self):
        name = "MomentumAgent"
        benched = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        summary = {name: {"active": False, "benched_at": benched}}
        result, _, ev = self._run(summary, [
            _stats(name=name, trades_20d=3, expectancy_after_costs_20d=20.0),
        ])
        self.assertEqual(result["actions"], [])
        self.assertTrue(any("stays benched" in h and "3 trades" in h
                            for h in result["holds"]))
        ev.assert_not_called()

    def test_bench_window_still_required(self):
        name = "BreakoutAgent"
        benched = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        summary = {name: {"active": False, "benched_at": benched}}
        result, summary, _ev = self._run(summary, [_stats(name=name)])
        self.assertFalse(any("REACTIVATED" in a for a in result["actions"]))
        self.assertFalse(summary[name]["active"])

    def test_pin_stays_benched_even_with_positive_expectancy(self):
        name = "MeanReversionAgent"
        summary = {name: {
            "active": False,
            "benched_at": "2099-01-01T00:00:00+00:00",
            "pinned_reason": "manual pin",
        }}
        now = datetime.now(timezone.utc)
        self.assertTrue(is_pinned_bench(
            summary[name],
            datetime.fromisoformat(summary[name]["benched_at"]),
            now,
        ))
        result, summary, ev = self._run(summary, [
            _stats(name=name, expectancy_after_costs_20d=25.0, trades_20d=20),
        ])
        self.assertEqual(result["actions"], [])
        self.assertTrue(any("pinned" in h and "stays benched" in h
                            for h in result["holds"]))
        self.assertFalse(summary[name]["active"])
        self.assertIsNotNone(summary[name]["benched_at"])
        ev.assert_not_called()

    def test_pinned_reason_alone_holds_the_bench(self):
        name = "PremarketAgent"
        benched = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        summary = {name: {
            "active": False, "benched_at": benched, "pinned_reason": "ops hold",
        }}
        result, summary, _ev = self._run(summary, [_stats(name=name)])
        self.assertEqual(result["actions"], [])
        self.assertTrue(any("pinned" in h for h in result["holds"]))
        self.assertFalse(summary[name]["active"])

    def test_pinned_only_does_not_write_summary(self):
        import tempfile
        from pathlib import Path
        name = "MeanReversionAgent"
        summary = {name: {
            "active": False,
            "benched_at": "2099-01-01T00:00:00+00:00",
            "pinned_reason": "manual pin",
        }}
        rotator = AgentRotator()
        report = EvalReport(
            generated_at="test",
            agents=[_stats(name=name, expectancy_after_costs_20d=25.0, trades_20d=20)],
            flagged_agents=[],
        )
        buf = io.StringIO()
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "agent_summary.json"
            with redirect_stdout(buf), \
                 patch.object(rotator.evaluator, "evaluate", return_value=report), \
                 patch.object(rotator.logger, "get_summary", return_value=summary), \
                 patch.object(rotator, "_write_rotation_event") as ev, \
                 patch("agent_rotator.SUMMARY", path):
                result = rotator.run_rotation(dry_run=False)
            self.assertFalse(path.exists())
        self.assertEqual(result["actions"], [])
        self.assertTrue(any("pinned" in h for h in result["holds"]))
        self.assertFalse(summary[name]["active"])
        ev.assert_not_called()
        text = buf.getvalue()
        self.assertIn("No rotations needed", text)
        self.assertIn("Holds — still benched", text)

    def test_decision_helper_requires_positive_sample(self):
        ok, _why = reactivation_decision(_stats(trades_20d=10, expectancy_after_costs_20d=0.01))
        self.assertTrue(ok)
        ok, why = reactivation_decision(_stats(trades_20d=10, expectancy_after_costs_20d=0.0))
        self.assertFalse(ok)
        self.assertIn("not positive", why)
        ok, why = reactivation_decision(None)
        self.assertFalse(ok)
        self.assertIn("no expectancy sample", why)


class ReconcileAndSchedule(unittest.TestCase):
    def test_options_day_pnl_is_removed_from_the_gap(self):
        from report_data import unexplained_day_gap
        # Broker day +1000, ledger realized +100, option intraday +800.
        # Without the option term the gap is $900 and looks like drift.
        self.assertAlmostEqual(unexplained_day_gap(1000, 100, 800), 100)
        self.assertAlmostEqual(unexplained_day_gap(1000, 100, 0), 900)

    def test_ghost_close_exit_at_is_et(self):
        # No network, no wall clock, no dotenv. The old test patched
        # requests.get; close_ghosts imports invariants, and a missing
        # dotenv was swallowed as closed=0.
        import trade_ledger as tl
        self.assertEqual(
            tl.broker_time_to_et("2026-09-24T18:05:00.000Z"),
            "2026-09-24 14:05:00",
        )
        self.assertEqual(
            tl.broker_time_to_et("2026-01-15T15:00:00+00:00"),
            "2026-01-15 10:00:00",
        )
        trade = tl.Trade(
            trade_id="t1", opened_at_et="2020-01-15 09:31:00",
            symbol="SUNB", side="LONG", primary_agent="Test", contributors="",
            entry_price=10.0, target_price=12.0, stop_price=9.0,
            risk_dollar=10.0, shares=1,
        )
        book = {
            "broker_syms": set(),
            "pending": set(),
            "fills": {"SUNB": (11.5, "2026-09-24T18:05:00.000Z")},
        }
        fixed_now = datetime(2026, 9, 24, 15, 0, tzinfo=tl.ET)
        with patch.object(tl, "load_ledger", return_value={"t1": trade}), \
             patch.object(tl, "save_ledger") as save, \
             patch.object(tl, "_fetch_ghost_book", side_effect=AssertionError("network")):
            out = tl.close_ghosts(book=book, now=fixed_now)
        self.assertNotIn("error", out, out)
        self.assertEqual(out["closed"], 1)
        self.assertEqual(trade.exit_at_et, "2026-09-24 14:05:00")
        self.assertEqual(trade.exit_price, 11.5)
        self.assertNotEqual(trade.exit_at_et[:13], "2026-09-24 18")
        save.assert_called_once()

    def test_earnings_skips_etfs_before_calendar(self):
        import earnings_agent as ea
        agent = ea.EarningsAgent(watchlist=["DIA", "QQQ", "XLV", "AAPL"])
        seen = []

        def _analyze(symbol):
            seen.append(symbol)
            return None

        with patch.object(ea, "_YF_OK", True), \
             patch.object(agent, "_analyze", _analyze):
            agent.generate_signals()
        self.assertEqual(seen, ["AAPL"])
        with patch.object(ea, "_YF_OK", True):
            self.assertIsNone(agent._analyze("QQQ"))
            self.assertIsNone(agent._analyze("DIA"))

    def test_minute_boundary_does_not_sleep_past_a_spill(self):
        from market_scheduler import seconds_until_next_minute, tick_spilled_into_new_minute
        now = datetime(2026, 9, 24, 10, 0, 40, tzinfo=ET)
        self.assertAlmostEqual(seconds_until_next_minute(now), 20.0)
        started = 10 * 60  # 10:00
        spilled = datetime(2026, 9, 24, 10, 1, 5, tzinfo=ET)
        self.assertTrue(tick_spilled_into_new_minute(started, spilled))
        same = datetime(2026, 9, 24, 10, 0, 50, tzinfo=ET)
        self.assertFalse(tick_spilled_into_new_minute(started, same))
        self.assertFalse(tick_spilled_into_new_minute(-1, spilled))

    def test_paper_only_and_crypto_gate_unchanged(self):
        import inspect
        import order_executor
        import session_gates
        self.assertIs(session_gates.CRYPTO_TRADING_ENABLED, False)
        self.assertIs(session_gates.PAPER_ONLY, True)
        src = inspect.getsource(order_executor.OrderExecutor.__init__)
        self.assertIn("paper=True", src)
        self.assertNotIn("DISABLED_AGENTS", inspect.getsource(
            __import__("agent_rotator")))


if __name__ == "__main__":
    unittest.main()
