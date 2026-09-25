"""Hard per-trade caps, duplicate-entry guard, and evaluation-bias fixes.

No broker and no network. Paper only.
"""

from __future__ import annotations

import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from risk_caps import (
    completed_bar_value,
    dynamic_risk_shares,
    entry_blocked,
    option_contracts,
)
from exposure import signed_exposure


ET = ZoneInfo("America/New_York")


class DynamicRiskShares(unittest.TestCase):
    def test_notional_binds_before_dollar_risk(self):
        # $100 entry, $4 stop (4%). $320 risk would buy 80 shares ($8,000).
        # $1,500 notional buys 15. Dollar risk of that position is $60.
        shares = dynamic_risk_shares(100, 96, notional_cap=1500, risk_cap=320)
        self.assertEqual(shares, 15)
        self.assertLessEqual(shares * 4, 320)
        self.assertLessEqual(shares * 100, 1500)

    def test_dollar_cap_binds_when_notional_is_loose(self):
        # The pre-clamp book: ~$8k notional from a $320 budget at a 4% stop
        # is exactly the cap, and a wider budget must not buy more.
        shares = dynamic_risk_shares(100, 96, notional_cap=50_000, risk_cap=320)
        self.assertEqual(shares, 80)
        self.assertEqual(shares * 4, 320)

    def test_one_share_above_notional_is_rejected(self):
        # MSTR-style: price above the notional cap. max(1, int()) used to
        # buy 1 share anyway.
        shares = dynamic_risk_shares(1800, 1728, notional_cap=1500, risk_cap=320)
        self.assertEqual(shares, 0)

    def test_one_share_above_risk_is_rejected(self):
        # One share whose stop is $400 away cannot be forced through.
        shares = dynamic_risk_shares(2000, 1600, notional_cap=5000, risk_cap=320)
        self.assertEqual(shares, 0)

    def test_wide_stop_shrinks_shares(self):
        # $50 name, $10 stop. Risk cap buys 32; $1,500 notional buys 30.
        shares = dynamic_risk_shares(50, 40, notional_cap=1500, risk_cap=320)
        self.assertEqual(shares, 30)
        self.assertLessEqual(shares * 10, 320)
        self.assertLessEqual(shares * 50, 1500)

    def test_never_rounds_up(self):
        # 1500/180 = 8.33 -> 8, not 9. 9 * 180 = 1620, over the cap.
        shares = dynamic_risk_shares(180, 172.8, notional_cap=1500, risk_cap=320)
        self.assertEqual(shares, 8)
        self.assertLessEqual(shares * 180, 1500)

    def test_bad_prices_are_zero(self):
        self.assertEqual(dynamic_risk_shares(0, 1, notional_cap=1500, risk_cap=320), 0)
        self.assertEqual(dynamic_risk_shares(100, 100, notional_cap=1500, risk_cap=320), 0)
        self.assertEqual(dynamic_risk_shares(100, 96, notional_cap=0, risk_cap=320), 0)


class OptionRiskCap(unittest.TestCase):
    def test_premium_inside_cap(self):
        # $3 ask -> $300 per contract. One fits in $320, two do not.
        self.assertEqual(option_contracts(3.0, 320), 1)

    def test_one_contract_over_cap_is_zero(self):
        # MSTR-like premium. Do not force 1 contract.
        self.assertEqual(option_contracts(4.0, 320), 0)
        self.assertEqual(option_contracts(15.0, 1000), 0)

    def test_pct_budget_still_floors(self):
        self.assertEqual(option_contracts(2.0, 320), 1)
        self.assertEqual(option_contracts(1.0, 320), 3)


class DuplicateGuard(unittest.TestCase):
    def test_flat_book_is_clear(self):
        self.assertIsNone(entry_blocked("AMD", [], []))

    def test_existing_shares_block(self):
        reason = entry_blocked("AMD", [{"symbol": "AMD", "qty": "4"}], [])
        self.assertIn("AMD", reason)

    def test_option_on_underlying_blocks_equity(self):
        reason = entry_blocked(
            "AMD",
            [{"symbol": "AMD250117C00160000", "qty": "1"}],
            [],
        )
        self.assertIn("AMD", reason)

    def test_different_underlying_does_not_match_prefix(self):
        # 'A' must not swallow AAPL.
        self.assertIsNone(entry_blocked(
            "A",
            [{"symbol": "AAPL250117C00150000", "qty": "1"}],
            [],
        ))

    def test_open_order_blocks(self):
        reason = entry_blocked("MXL", [], [{"symbol": "MXL", "qty": "10"}])
        self.assertIn("open order", reason)

    def test_execute_refuses_second_attempt_and_a_live_position(self):
        from order_executor import OrderExecutor

        def _signal(symbol="AAPL"):
            return {
                "symbol": symbol, "direction": "long", "entry_price": 100.0,
                "stop_loss_price": 96.0, "target_price": 110.0, "agent": "Test",
                "position_sizing": {"total_cost": 1500.0},
            }

        ex = OrderExecutor()
        ex.reset_entry_claims_for_tests()
        client = MagicMock()
        client.get_all_positions.return_value = []
        client.get_orders.return_value = []
        ex._client = client
        ex._submit_equity_bracket = MagicMock(return_value={
            "status": "submitted", "order_id": "1", "symbol": "AAPL",
            "direction": "long", "qty": 15, "fill_price": 100.0,
        })
        ex._record_ledger = MagicMock()
        with patch("session_gates.is_rth", return_value=True), \
             patch("trade_ledger.has_open_position", return_value=False):
            first = ex.execute(_signal())
            second = ex.execute(_signal())
        self.assertEqual(first["status"], "submitted")
        self.assertEqual(second["status"], "duplicate")
        ex._submit_equity_bracket.assert_called_once()

        ex.reset_entry_claims_for_tests()
        client.get_all_positions.return_value = [
            MagicMock(symbol="MSTR", qty="1"),
        ]
        with patch("session_gates.is_rth", return_value=True), \
             patch("trade_ledger.has_open_position", return_value=False):
            held = ex.execute(_signal("MSTR"))
        self.assertEqual(held["status"], "duplicate")
        self.assertIn("MSTR", held["reason"])


class CompletedBar(unittest.TestCase):
    def test_rth_uses_prior_bar(self):
        now = datetime(2026, 9, 25, 11, 0, tzinfo=ET)
        last_ts = datetime(2026, 9, 25, 16, 0, tzinfo=ET)
        self.assertEqual(completed_bar_value(9.0, 4.0, last_ts, now), 4.0)

    def test_after_close_keeps_today(self):
        now = datetime(2026, 9, 25, 16, 5, tzinfo=ET)
        last_ts = datetime(2026, 9, 25, 16, 0, tzinfo=ET)
        self.assertEqual(completed_bar_value(9.0, 4.0, last_ts, now), 9.0)

    def test_prior_session_is_kept(self):
        now = datetime(2026, 9, 25, 11, 0, tzinfo=ET)
        last_ts = datetime(2026, 9, 24, 16, 0, tzinfo=ET)
        self.assertEqual(completed_bar_value(4.0, 3.0, last_ts, now), 4.0)


class LogBackfillShares(unittest.TestCase):
    def test_shares_follow_stop_distance_not_notional(self):
        from trade_ledger import parse_paper_trade_line
        line = (
            "2026-09-25 10:15:00,123 [INFO] PAPER TRADE: AMD LONG "
            "entry=$160.00 target=$180.00 stop=$153.60 "
            "agent=MetaAgent(MomentumAgent)"
        )
        trade = parse_paper_trade_line(line)
        self.assertIsNotNone(trade)
        # $320 risk / $6.40 stop = 50 shares. The old formula did 320/160 = 2.
        self.assertAlmostEqual(trade.shares, 50.0)
        self.assertAlmostEqual(trade.risk_dollar, 320.0)
        self.assertGreater(trade.shares * trade.entry_price, 320.0)


class ExposureAndEval(unittest.TestCase):
    def test_spxu_is_inverse(self):
        self.assertEqual(signed_exposure("SPXU", 1000), -3000)
        self.assertEqual(signed_exposure("TQQQ", 1000), 3000)

    def test_open_marks_do_not_vote(self):
        from datetime import timedelta
        from agent_evaluator import AgentEvaluator
        from test_learning_loop import TODAY, _trade

        closed = _trade("TechnicalAgent", -100.0, days_ago=1, symbol="AMD")
        opened = (TODAY - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
        winner = _trade(
            "TechnicalAgent", 5_000.0, days_ago=1, symbol="MSTR", open_=True,
        )
        winner.opened_at_et = opened
        ev = AgentEvaluator()
        with patch("agent_evaluator._today_et_date", return_value=TODAY), \
             patch("trade_ledger.epoch_trades", return_value=[closed, winner]), \
             patch("agent_evaluator._agent_active_state", return_value={}):
            report = ev.evaluate()
        by = {a.name: a for a in report.agents}
        self.assertAlmostEqual(by["TechnicalAgent"].pnl_20d, -100.0)
        self.assertEqual(by["TechnicalAgent"].trades_20d, 1)


if __name__ == "__main__":
    unittest.main()
