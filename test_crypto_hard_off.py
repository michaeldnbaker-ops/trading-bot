"""Crypto new-entry hard-off. No broker, no network.

Locks the session_gates sleeve so a cron tick cannot open BTC/ETH/SOL
while existing positions can still be exited.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd


def _equity_signal(**overrides):
    sig = {
        "symbol": "AAPL",
        "direction": "long",
        "entry_price": 100.0,
        "stop_loss_price": 96.0,
        "target_price": 110.0,
        "agent": "Test",
        "position_size_usd": 50_000.0,
    }
    sig.update(overrides)
    return sig


class CryptoHardOff(unittest.TestCase):
    def test_flag_defaults_off(self):
        import session_gates
        self.assertIs(session_gates.CRYPTO_TRADING_ENABLED, False)
        self.assertFalse(session_gates.crypto_entries_allowed())

    def test_generate_signals_returns_empty_without_analysis(self):
        from crypto_agent import CryptoAgent
        agent = CryptoAgent()
        with patch.object(CryptoAgent, "_analyze", side_effect=AssertionError("fetched")):
            self.assertEqual(agent.generate_signals(), [])

    def test_scheduler_refuses_new_entries(self):
        from crypto_scheduler import run_crypto_tick
        with patch("crypto_agent.CryptoAgent.generate_signals",
                   side_effect=AssertionError("generated")):
            self.assertEqual(run_crypto_tick(), [])

    def test_exits_still_close_when_entries_disabled(self):
        from crypto_scheduler import manage_crypto_exits
        trade = SimpleNamespace(symbol="SOL/USD", target_price=0.01, stop_price=10**9)
        executor = MagicMock()
        executor._client = MagicMock()
        ticker = MagicMock()
        ticker.history.return_value = pd.DataFrame({"Close": [100.0]})
        with patch("trade_ledger.open_positions", return_value=[trade]), \
             patch("yfinance.Ticker", return_value=ticker), \
             patch("order_executor.get_executor", return_value=executor):
            manage_crypto_exits()
        executor._client.close_position.assert_called_once_with("SOLUSD")

    def test_executor_blocks_crypto_symbols(self):
        from order_executor import OrderExecutor
        from session_gates import is_crypto_symbol
        ex = OrderExecutor()
        ex._submit_crypto = MagicMock(side_effect=AssertionError("submitted"))
        symbols = ["BTC/USD", "ETH/USD", "SOL/USD", "BTCUSD", "ETH-USD", "SOLUSD"]
        for symbol in symbols:
            self.assertTrue(is_crypto_symbol(symbol), symbol)
            result = ex.execute(_equity_signal(symbol=symbol))
            self.assertEqual(result["status"], "blocked", symbol)
            self.assertIn("crypto", result["reason"])
        ex._submit_crypto.assert_not_called()

    def test_equity_notional_clamp_unchanged_during_rth(self):
        from order_executor import MAX_NOTIONAL_USD, OrderExecutor
        ex = OrderExecutor()
        ex.reset_entry_claims_for_tests()
        client = MagicMock()
        client.get_all_positions.return_value = []
        client.get_orders.return_value = []
        ex._client = client
        captured = {}

        def _fake_submit(symbol, direction, entry, stop, target, pos_usd):
            captured["pos_usd"] = pos_usd
            return {"status": "submitted", "order_id": "x", "symbol": symbol,
                    "direction": direction, "qty": 1}

        ex._submit_equity_bracket = _fake_submit
        ex._record_ledger = lambda *a, **k: None
        with patch("session_gates.is_rth", return_value=True), \
             patch("trade_ledger.has_open_position", return_value=False):
            result = ex.execute(_equity_signal())
        self.assertEqual(result["status"], "submitted")
        self.assertEqual(captured["pos_usd"], MAX_NOTIONAL_USD)
        self.assertLess(captured["pos_usd"], 50_000)


if __name__ == "__main__":
    unittest.main()
