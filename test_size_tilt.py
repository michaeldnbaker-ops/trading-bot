"""Evidence-gated per-agent size tilt. No broker, no network.

Qualifiers: 20-day after-cost expectancy > 0 on >= 10 trades AND all-time
after-cost expectancy > 0 on >= 30 trades. SIZE_TILT_ENABLED defaults off.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from agent_evaluator import AgentEvaluator, AgentStats, EvalReport
from trade_ledger import Trade

import size_tilt


TODAY = datetime(2026, 9, 20)


def _trade(primary: str, pnl: float, days_ago: int, *, symbol: str = "AAPL") -> Trade:
    opened = (TODAY - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")
    return Trade(
        trade_id=f"{primary}-{symbol}-{days_ago}-{pnl}-{id(pnl)}",
        opened_at_et=opened,
        symbol=symbol,
        side="LONG",
        primary_agent=primary,
        contributors="",
        entry_price=10.0,
        target_price=11.0,
        stop_price=9.6,
        risk_dollar=320.0,
        shares=1.0,
        status="stop",
        realized_pnl=pnl,
        unrealized_pnl=0.0,
    )


def _book(n_recent: int, n_old: int, pnl: float, agent: str = "BreakoutAgent") -> list[Trade]:
    """Recent trades sit inside the 20-day window; old trades are all-time only."""
    rows = []
    for i in range(n_recent):
        rows.append(_trade(agent, pnl, days_ago=1, symbol=f"R{i}"))
    for i in range(n_old):
        rows.append(_trade(agent, pnl, days_ago=21 + (i % 30), symbol=f"O{i}"))
    return rows


def _evaluate(trades, summary=None):
    ev = AgentEvaluator()
    with patch("agent_evaluator._today_et_date", return_value=TODAY), \
         patch("trade_ledger.epoch_trades", return_value=trades), \
         patch("agent_evaluator._agent_active_state", return_value=summary or {}):
        return ev.evaluate()


def _stats(**overrides) -> AgentStats:
    base = dict(
        name="BreakoutAgent",
        active=True,
        trades_20d=10,
        trades_total=30,
        expectancy_after_costs_20d=1.25,
        expectancy_after_costs=0.80,
        # Gross is negative on purpose: tilt must read after-cost expectancy.
        pnl_20d=-500.0,
        pnl_alltime=-900.0,
    )
    base.update(overrides)
    return AgentStats(**base)


def _signal(**overrides):
    sig = {
        "symbol": "AAPL",
        "direction": "long",
        "entry_price": 100.0,
        "stop_loss_price": 96.0,
        "target_price": 110.0,
        "agent": "BreakoutAgent",
        "position_size_usd": 50_000.0,
    }
    sig.update(overrides)
    return sig


class SizeTiltHarness(unittest.TestCase):
    def setUp(self):
        size_tilt.reset_for_tests()
        self.tmp = Path(tempfile.mkdtemp())
        self._env = patch.dict(os.environ, {"SIZE_TILT_ENABLED": "false"})
        self._path = patch.object(
            size_tilt, "qualifiers_path", return_value=self.tmp / "size_tilt_qualifiers.json",
        )
        self._summary = patch.object(size_tilt, "load_summary", return_value={})
        self._env.start()
        self._path.start()
        self._summary.start()

    def tearDown(self):
        self._summary.stop()
        self._path.stop()
        self._env.stop()
        size_tilt.reset_for_tests()

    def _stats_for(self, report, name):
        return next(a for a in report.agents if a.name == name)


class QualificationRules(SizeTiltHarness):
    def test_flag_defaults_off(self):
        os.environ.pop("SIZE_TILT_ENABLED", None)
        self.assertFalse(size_tilt.enabled())
        self.assertEqual(size_tilt.tilted_absolute_cap(1500), 2250.0)
        self.assertEqual(size_tilt.SIZE_TILT_MULTIPLIER, 1.5)

    def test_exactly_10_and_30_positive_after_cost_qualifies(self):
        report = _evaluate(_book(10, 20, pnl=10.0))
        stats = self._stats_for(report, "BreakoutAgent")
        self.assertEqual(stats.trades_20d, 10)
        self.assertEqual(stats.trades_total, 30)
        self.assertGreater(stats.expectancy_after_costs_20d, 0)
        self.assertGreater(stats.expectancy_after_costs, 0)
        # Gross can be anything; the gate is the after-cost field.
        self.assertGreater(stats.pnl_20d, stats.pnl_20d_after_costs)
        ok, reason = size_tilt.decide(stats, {})
        self.assertTrue(ok, reason)
        self.assertIn("qualifies", reason)

    def test_nine_trades_in_20d_does_not_qualify(self):
        report = _evaluate(_book(9, 21, pnl=10.0))
        stats = self._stats_for(report, "BreakoutAgent")
        self.assertEqual(stats.trades_20d, 9)
        self.assertEqual(stats.trades_total, 30)
        ok, reason = size_tilt.decide(stats, {})
        self.assertFalse(ok)
        self.assertIn("20d trades 9 < 10", reason)

    def test_twenty_nine_all_time_does_not_qualify(self):
        report = _evaluate(_book(10, 19, pnl=10.0))
        stats = self._stats_for(report, "BreakoutAgent")
        self.assertEqual(stats.trades_20d, 10)
        self.assertEqual(stats.trades_total, 29)
        ok, reason = size_tilt.decide(stats, {})
        self.assertFalse(ok)
        self.assertIn("all-time trades 29 < 30", reason)

    def test_expectancy_exactly_zero_does_not_qualify(self):
        # $2 round-trip floor on a $10 notional. Gross +2.00 → after-cost 0.
        report = _evaluate(_book(10, 20, pnl=2.0))
        stats = self._stats_for(report, "BreakoutAgent")
        self.assertEqual(stats.trades_20d, 10)
        self.assertEqual(stats.trades_total, 30)
        self.assertEqual(stats.expectancy_after_costs_20d, 0.0)
        self.assertEqual(stats.expectancy_after_costs, 0.0)
        self.assertGreater(stats.pnl_20d, 0)
        ok, reason = size_tilt.decide(stats, {})
        self.assertFalse(ok)
        self.assertIn("is not > 0", reason)

        only_20d = size_tilt.decide(_stats(expectancy_after_costs_20d=0.0), {})
        self.assertFalse(only_20d[0])
        self.assertIn("20d after-cost expectancy", only_20d[1])
        only_all = size_tilt.decide(_stats(expectancy_after_costs=0.0), {})
        self.assertFalse(only_all[0])
        self.assertIn("all-time after-cost expectancy", only_all[1])

    def test_after_cost_positive_qualifies_even_if_gross_is_negative(self):
        ok, reason = size_tilt.decide(_stats(), {})
        self.assertTrue(ok, reason)

    def test_benched_agent_never_tilted(self):
        report = _evaluate(
            _book(10, 20, pnl=10.0),
            {"BreakoutAgent": {"active": False}},
        )
        stats = self._stats_for(report, "BreakoutAgent")
        self.assertFalse(stats.active)
        ok, reason = size_tilt.decide(stats, {"active": False})
        self.assertFalse(ok)
        self.assertIn("benched", reason)
        payload = size_tilt.ensure_today(
            report, summary={"BreakoutAgent": {"active": False}},
        )
        self.assertNotIn("BreakoutAgent", payload["qualifiers"])
        os.environ["SIZE_TILT_ENABLED"] = "true"
        self.assertFalse(size_tilt.order_is_tilted("BreakoutAgent", "AAPL"))

    def test_pinned_agent_never_tilted(self):
        report = _evaluate(
            _book(10, 20, pnl=10.0),
            {"BreakoutAgent": {"active": True, "pinned_reason": "manual hold"}},
        )
        stats = self._stats_for(report, "BreakoutAgent")
        self.assertTrue(stats.active)
        row = {"active": True, "pinned_reason": "manual hold"}
        ok, reason = size_tilt.decide(stats, row)
        self.assertFalse(ok)
        self.assertIn("pinned", reason)
        payload = size_tilt.ensure_today(report, summary={"BreakoutAgent": row})
        self.assertNotIn("BreakoutAgent", payload["qualifiers"])
        self.assertIn("pinned", payload["reasons"]["BreakoutAgent"])
        os.environ["SIZE_TILT_ENABLED"] = "true"
        self.assertFalse(size_tilt.order_is_tilted("BreakoutAgent", "AAPL"))

    def test_crypto_agent_never_qualifies(self):
        report = _evaluate(_book(10, 20, pnl=10.0, agent="CryptoAgent"))
        stats = self._stats_for(report, "CryptoAgent")
        self.assertGreater(stats.expectancy_after_costs_20d, 0)
        ok, reason = size_tilt.decide(stats, {})
        self.assertFalse(ok)
        self.assertIn("crypto", reason)
        payload = size_tilt.ensure_today(report, summary={})
        self.assertNotIn("CryptoAgent", payload["qualifiers"])
        os.environ["SIZE_TILT_ENABLED"] = "true"
        # Even a stale qualifier entry cannot tilt crypto.
        size_tilt._cache["qualifiers"] = ["CryptoAgent", "BreakoutAgent"]
        self.assertFalse(size_tilt.order_is_tilted("CryptoAgent", "AAPL"))
        self.assertFalse(size_tilt.order_is_tilted("BreakoutAgent", "BTC/USD"))
        self.assertFalse(size_tilt.order_is_tilted("BreakoutAgent", "ETHUSD"))
        self.assertFalse(size_tilt.order_is_tilted("BreakoutAgent", "SOL/USD"))

    def test_wrapped_qualifier_tilts_mixed_ticket_does_not(self):
        os.environ["SIZE_TILT_ENABLED"] = "true"
        size_tilt._cache = {
            "date": size_tilt.today_iso(),
            "SIZE_TILT_ENABLED": True,
            "qualifiers": ["BreakoutAgent"],
            "reasons": {},
        }
        self.assertTrue(size_tilt.order_is_tilted("MetaAgent(BreakoutAgent)", "AAPL"))
        self.assertTrue(size_tilt.order_is_tilted("BreakoutAgent", "AAPL"))
        self.assertFalse(
            size_tilt.order_is_tilted("MetaAgent(BreakoutAgent, NewsAgent)", "AAPL")
        )

    def test_flag_off_does_not_tilt_a_qualifier(self):
        self.assertFalse(size_tilt.enabled())
        size_tilt._cache = {
            "date": size_tilt.today_iso(),
            "SIZE_TILT_ENABLED": False,
            "qualifiers": ["BreakoutAgent"],
            "reasons": {},
        }
        self.assertFalse(size_tilt.order_is_tilted("BreakoutAgent", "AAPL"))
        self.assertFalse(size_tilt.order_is_tilted("MetaAgent(BreakoutAgent)", "AAPL"))


class DailyRecord(SizeTiltHarness):
    def test_logs_and_writes_qualifiers_including_empty(self):
        report = _evaluate(_book(3, 11, pnl=10.0))  # 3 in 20d, 14 all-time
        os.environ["SIZE_TILT_ENABLED"] = "false"
        with self.assertLogs("SizeTilt", level="INFO") as logs:
            payload = size_tilt.ensure_today(report, summary={})
        self.assertEqual(payload["qualifiers"], [])
        self.assertIs(payload["SIZE_TILT_ENABLED"], False)
        self.assertIn("BreakoutAgent", payload["reasons"])
        blob = "\n".join(logs.output)
        self.assertIn("SIZE TILT daily", blob)
        self.assertIn("qualifiers=(none)", blob)
        self.assertIn("SIZE_TILT_ENABLED=False", blob)
        path = self.tmp / "size_tilt_qualifiers.json"
        disk = json.loads(path.read_text())
        self.assertEqual(disk["date"], payload["date"])
        self.assertEqual(disk["qualifiers"], [])
        self.assertIn("reasons", disk)
        self.assertIn("BreakoutAgent", disk["reasons"])
        # Same ET date does not recompute or re-log.
        size_tilt._cache = None
        # File is already today's — load it. Freeze the date to the payload.
        with patch.object(size_tilt, "today_iso", return_value=payload["date"]):
            again = size_tilt.ensure_today()
        self.assertEqual(again["qualifiers"], [])
        self.assertEqual(again["reasons"]["BreakoutAgent"], payload["reasons"]["BreakoutAgent"])

    def test_empty_reasons_not_cached_and_next_call_recomputes(self):
        empty = EvalReport(generated_at="t", agents=[])
        path = self.tmp / "size_tilt_qualifiers.json"
        with self.assertLogs("SizeTilt", level="WARNING") as logs:
            first = size_tilt.ensure_today(empty, summary={})
        self.assertTrue(any("empty" in line for line in logs.output))
        self.assertEqual(first["qualifiers"], [])
        self.assertEqual(first["reasons"], {})
        self.assertIsNone(size_tilt._cache)
        self.assertFalse(path.exists())

        os.environ["SIZE_TILT_ENABLED"] = "true"
        with patch.object(size_tilt, "ensure_today", return_value=first):
            self.assertFalse(size_tilt.order_is_tilted("BreakoutAgent", "AAPL"))
        self.assertIsNone(size_tilt._cache)
        self.assertFalse(path.exists())

        full = _evaluate(_book(10, 20, pnl=10.0))
        with patch.object(
            size_tilt, "_payload_from_report", wraps=size_tilt._payload_from_report,
        ) as built:
            second = size_tilt.ensure_today(full, summary={})
        self.assertEqual(built.call_count, 1)
        self.assertEqual(second["qualifiers"], ["BreakoutAgent"])
        self.assertEqual(size_tilt._cache["date"], second["date"])
        self.assertTrue(path.exists())

    def test_roster_agent_missing_from_reasons_not_cached_and_next_call_recomputes(self):
        """A name on the evaluator roster with no reason is not saved.

        Summary-only names are not the required set. This drops a roster
        agent after the payload is built.
        """
        report = _evaluate(_book(10, 20, pnl=10.0))
        report.agents.append(AgentStats(
            name="NewsAgent",
            active=True,
            trades_20d=10,
            trades_total=30,
            expectancy_after_costs_20d=1.0,
            expectancy_after_costs=1.0,
        ))
        path = self.tmp / "size_tilt_qualifiers.json"
        real = size_tilt._payload_from_report

        def drop_news(report, summary, day):
            payload = real(report, summary, day)
            payload["reasons"].pop("NewsAgent", None)
            payload["qualifiers"] = [
                name for name in payload["qualifiers"] if name != "NewsAgent"
            ]
            return payload

        with self.assertLogs("SizeTilt", level="WARNING") as logs, \
             patch.object(size_tilt, "_payload_from_report", side_effect=drop_news) as built:
            first = size_tilt.ensure_today(report, summary={})
            self.assertEqual(built.call_count, 1)
            self.assertEqual(first["qualifiers"], [])
            self.assertEqual(first["reasons"], {})
            self.assertIsNone(size_tilt._cache)
            self.assertFalse(path.exists())
            self.assertTrue(any("NewsAgent" in line for line in logs.output))

            os.environ["SIZE_TILT_ENABLED"] = "true"
            with patch.object(size_tilt, "ensure_today", return_value=first):
                self.assertFalse(size_tilt.order_is_tilted("BreakoutAgent", "AAPL"))
            self.assertIsNone(size_tilt._cache)

        with patch.object(
            size_tilt, "_payload_from_report", wraps=size_tilt._payload_from_report,
        ) as built:
            second = size_tilt.ensure_today(report, summary={})
        self.assertEqual(built.call_count, 1)
        self.assertEqual(second["qualifiers"], ["BreakoutAgent", "NewsAgent"])
        self.assertIn("NewsAgent", second["reasons"])
        self.assertIsNotNone(size_tilt._cache)
        self.assertTrue(path.exists())

    def test_live_summary_wrappers_and_earnings_do_not_block_save(self):
        """Real agent_summary.json shape must still write the day file.

        Active MetaAgent(...) rows and EarningsAgent (no trades, not scored)
        are not required. A 0-trade evaluator row is "no trades", not missing.
        """
        report = _evaluate(_book(10, 20, pnl=10.0))
        report.agents.append(AgentStats(name="QuietAgent", trades_total=0, active=True))
        report.agents.append(AgentStats(
            name="MetaAgent(BearishPatternAgent, MacroAgent, OptionsFlowAgent)",
            trades_total=4,
            active=True,
        ))
        summary = {
            "BreakoutAgent": {"active": True},
            "EarningsAgent": {"active": True},
            "MetaAgent(BearishPatternAgent, MacroAgent, OptionsFlowAgent)": {"active": True},
            "MetaAgent(BreakoutAgent, NewsAgent)": {"active": True},
            "QuietAgent": {"active": True},
        }
        path = self.tmp / "size_tilt_qualifiers.json"
        with self.assertNoLogs("SizeTilt", level="WARNING"):
            payload = size_tilt.ensure_today(report, summary=summary)
        self.assertEqual(payload["qualifiers"], ["BreakoutAgent"])
        self.assertEqual(payload["reasons"]["QuietAgent"], "no trades")
        self.assertNotIn("EarningsAgent", payload["reasons"])
        self.assertNotIn(
            "MetaAgent(BearishPatternAgent, MacroAgent, OptionsFlowAgent)",
            payload["reasons"],
        )
        self.assertTrue(path.exists())
        disk = json.loads(path.read_text())
        self.assertEqual(disk["qualifiers"], ["BreakoutAgent"])
        self.assertEqual(disk["reasons"]["QuietAgent"], "no trades")
        self.assertIsNotNone(size_tilt._cache)

        # Restart must reuse the file even when the live summary still has wrappers.
        size_tilt._cache = None
        with self.assertNoLogs("SizeTilt", level="WARNING"):
            again = size_tilt.ensure_today(summary=summary)
        self.assertEqual(again["qualifiers"], ["BreakoutAgent"])
        self.assertEqual(again["reasons"]["QuietAgent"], "no trades")


class NotionalClamp(SizeTiltHarness):
    def _execute(self, signal, equity=100_000.0):
        from order_executor import OrderExecutor
        ex = OrderExecutor()
        ex._client = object()
        ex._portfolio_equity = lambda: equity
        captured = {}

        def _fake_submit(symbol, direction, entry, stop, target, pos_usd):
            captured["pos_usd"] = pos_usd
            return {
                "status": "submitted", "order_id": "x", "symbol": symbol,
                "direction": direction, "qty": 1, "fill_price": entry,
            }

        ex._submit_equity_bracket = _fake_submit
        ex._record_ledger = lambda *a, **k: None
        with patch("session_gates.is_rth", return_value=True):
            result = ex.execute(signal)
        captured["result"] = result
        return captured

    def _seed_breakout(self):
        size_tilt._cache = {
            "date": size_tilt.today_iso(),
            "SIZE_TILT_ENABLED": True,
            "qualifiers": ["BreakoutAgent"],
            "reasons": {"BreakoutAgent": "qualifies"},
        }

    def test_qualifier_gets_2250_and_may_exceed_two_percent(self):
        os.environ["SIZE_TILT_ENABLED"] = "true"
        self._seed_breakout()
        captured = self._execute(_signal(agent="MetaAgent(BreakoutAgent)"))
        self.assertEqual(captured["result"]["status"], "submitted")
        self.assertEqual(captured["pos_usd"], 2250.0)
        # $100k × 2% = $2,000. The tilt is an exception up to $2,250.
        self.assertGreater(captured["pos_usd"], 100_000 * 0.02)
        self.assertLess(captured["pos_usd"], 50_000)

    def test_non_qualifier_stays_1500(self):
        os.environ["SIZE_TILT_ENABLED"] = "true"
        self._seed_breakout()
        captured = self._execute(_signal(agent="NewsAgent"))
        self.assertEqual(captured["pos_usd"], 1500.0)

    def test_flag_off_keeps_1500_for_a_qualifier(self):
        os.environ["SIZE_TILT_ENABLED"] = "false"
        self._seed_breakout()
        captured = self._execute(_signal(agent="BreakoutAgent"))
        self.assertEqual(captured["pos_usd"], 1500.0)

    def test_flag_off_order_path_does_not_evaluate_or_write(self):
        """Orders must not call ensure_today or the evaluator when the flag is off."""
        from agent_risk_bridge import AgentRiskBridge
        os.environ["SIZE_TILT_ENABLED"] = "false"
        path = self.tmp / "size_tilt_qualifiers.json"
        with patch.object(AgentRiskBridge, "_live_equity", return_value=None):
            bridge = AgentRiskBridge(account_balance=100_000)
        bridge.account_balance = 100_000
        equity = {
            "symbol": "AAPL",
            "direction": "long",
            "confidence": 0.90,
            "entry_price": 10.0,
            "stop_loss_price": 9.60,
            "target_price": 12.0,
            "agent": "BreakoutAgent",
            "instrument_type": "equity",
        }
        with patch.object(size_tilt, "ensure_today") as ensure, \
             patch("agent_evaluator.AgentEvaluator.evaluate") as evaluate:
            captured = self._execute(_signal(agent="BreakoutAgent"))
            sizing = bridge._compute_position_size(equity, "standard")
        ensure.assert_not_called()
        evaluate.assert_not_called()
        self.assertEqual(captured["pos_usd"], 1500.0)
        self.assertEqual(sizing["total_cost"], 2000.0)
        self.assertFalse(path.exists())
        self.assertIsNone(size_tilt._cache)

    def test_two_percent_cap_still_binds_non_qualifiers(self):
        # $50k × 2% = $1,000, which is tighter than the $1,500 absolute.
        os.environ["SIZE_TILT_ENABLED"] = "true"
        self._seed_breakout()
        non = self._execute(_signal(agent="NewsAgent"), equity=50_000.0)
        self.assertEqual(non["pos_usd"], 1000.0)
        qual = self._execute(_signal(agent="BreakoutAgent"), equity=50_000.0)
        self.assertEqual(qual["pos_usd"], 2250.0)

    def test_crypto_symbol_is_not_tilted(self):
        os.environ["SIZE_TILT_ENABLED"] = "true"
        self._seed_breakout()
        from order_executor import OrderExecutor
        ex = OrderExecutor()
        ex._client = object()
        ex._portfolio_equity = lambda: 100_000.0
        ex._submit_crypto = lambda *a, **k: (_ for _ in ()).throw(AssertionError("submitted"))
        with self.assertLogs("OrderExecutor", level="INFO") as logs:
            result = ex.execute(_signal(symbol="BTC/USD", agent="BreakoutAgent"))
        self.assertEqual(result["status"], "blocked")
        blob = "\n".join(logs.output)
        self.assertIn("abs_cap=$1500", blob)
        self.assertNotIn("size_tilt", blob)

    def test_benched_and_pinned_orders_stay_1500(self):
        os.environ["SIZE_TILT_ENABLED"] = "true"
        # Daily list omitted them. A direct order still uses the $1,500 path.
        size_tilt._cache = {
            "date": size_tilt.today_iso(),
            "SIZE_TILT_ENABLED": True,
            "qualifiers": [],
            "reasons": {
                "BreakoutAgent": "benched (active:false)",
                "NewsAgent": "pinned (pinned_reason=manual hold)",
            },
        }
        benched = self._execute(_signal(agent="BreakoutAgent"))
        pinned = self._execute(_signal(agent="NewsAgent"))
        self.assertEqual(benched["pos_usd"], 1500.0)
        self.assertEqual(pinned["pos_usd"], 1500.0)


class BridgeCeiling(SizeTiltHarness):
    def test_equity_notional_ceiling_rises_only_when_tilted(self):
        from agent_risk_bridge import AgentRiskBridge
        with patch.object(AgentRiskBridge, "_live_equity", return_value=None):
            bridge = AgentRiskBridge(account_balance=100_000)
        bridge.account_balance = 100_000
        sig = {
            "symbol": "AAPL",
            "direction": "long",
            "confidence": 0.90,
            "entry_price": 10.0,
            "stop_loss_price": 9.60,
            "target_price": 12.0,
            "agent": "BreakoutAgent",
            "instrument_type": "equity",
        }
        os.environ["SIZE_TILT_ENABLED"] = "true"
        with patch("size_tilt.order_is_tilted", return_value=False):
            plain = bridge._compute_position_size(sig, "standard")
        self.assertEqual(plain["total_cost"], 2000.0)
        with patch("size_tilt.order_is_tilted", return_value=True):
            tilted = bridge._compute_position_size(sig, "standard")
        self.assertEqual(tilted["total_cost"], 2250.0)
        # Risk budget is still what cuts a wide stop; the tilt only moves
        # the notional ceiling.
        wide = dict(sig, stop_loss_price=7.50)  # 25% stop; risk budget binds under $2,250
        with patch("size_tilt.order_is_tilted", return_value=True):
            wide_size = bridge._compute_position_size(wide, "standard")
        self.assertLess(wide_size["total_cost"], 2250.0)


class SchedulerUntouched(SizeTiltHarness):
    def test_eval_windows_unchanged_and_record_hooked(self):
        import inspect
        import market_scheduler as ms
        self.assertEqual(ms.EVAL_TIMES_ET, [(10, 0), (15, 30)])
        src = inspect.getsource(ms.run_eval_cycle)
        self.assertIn("ensure_today", src)


if __name__ == "__main__":
    unittest.main()
