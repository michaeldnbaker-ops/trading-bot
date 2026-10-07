"""New-entry kill switch and empty-roster stand-down.

Paper only. CRYPTO_TRADING_ENABLED stays false. No broker, no network.

When NEW_ENTRIES_ENABLED is false, or data/NO_NEW_ENTRIES exists, or the
turnaround book (News + Breakout) is fully benched, run_cycle still manages
open positions and does not generate signals or open equity/option entries.
An empty roster does not fall back to legacy agents.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

from ensemble import (
    PAPER_TRADING,
    Ensemble,
    active_signal_roster,
    new_entries_disabled_message,
    new_entries_disabled_reason,
    new_entries_enabled,
)
from session_gates import CRYPTO_TRADING_ENABLED


class _NullExec:
    _client = None


def _summary(path: Path, rows: dict) -> None:
    path.write_text(json.dumps(rows))


@contextmanager
def _flags(*, env: str = "true", flag: Path | None = None, summary: Path | None = None):
    """Pin env, file flag, and bench file so a developer summary cannot leak in."""
    missing = Path(tempfile.gettempdir()) / "no-such-NO_NEW_ENTRIES"
    missing_summary = Path(tempfile.gettempdir()) / "no-such-agent_summary.json"
    env_patch = patch.dict(os.environ, {"NEW_ENTRIES_ENABLED": env})
    flag_patch = patch("ensemble.NO_NEW_ENTRIES_PATH", flag if flag is not None else missing)
    summary_patch = patch(
        "ensemble.AGENT_SUMMARY_PATH",
        summary if summary is not None else missing_summary,
    )
    with env_patch, flag_patch, summary_patch:
        yield


@contextmanager
def _managed_cycle(ens: Ensemble):
    """Position-management hooks and the broker, with no network."""
    ens.regime.detect = lambda: {"NEUTRAL"}
    ens.risk.assess = lambda: {
        "halt_trading": False,
        "warnings": [],
        "vix": 16,
        "confidence_multiplier": 1.0,
    }
    ens._dynamic_universe = lambda *a, **k: []
    ens._normalize_geometry = lambda signal: signal
    with patch("order_executor.ensure_protective_exits", return_value={}) as protect, \
         patch("order_executor.widen_trails_on_survivors") as trails, \
         patch("order_executor.get_executor", return_value=_NullExec()), \
         patch("order_executor.execute_signal") as equity, \
         patch("trade_ledger.close_ghosts") as ghosts, \
         patch("trade_ledger.trades_on_date", return_value=[]), \
         patch("trade_ledger.open_positions", return_value=[]), \
         patch("trade_ledger.has_open_position", return_value=False), \
         patch("options_executor.manage_options_exits") as manage, \
         patch("options_executor.execute_options_trade",
               return_value={"status": "submitted"}) as options, \
         patch("trade_context.record_entry"), \
         patch.object(Ensemble, "_avoid_symbols", return_value=set()):
        yield {
            "protect": protect,
            "trails": trails,
            "ghosts": ghosts,
            "manage": manage,
            "options": options,
            "equity": equity,
        }


def _arm_signals(ens: Ensemble, signals: dict[str, list] | None = None) -> list[str]:
    """Record generate_signals. Unlisted agents raise if called."""
    signals = signals or {}
    called: list[str] = []

    def _ok(name: str):
        def _run():
            called.append(name)
            return list(signals.get(name) or [])
        return _run

    def _forbidden(name: str):
        def _run():
            raise AssertionError(f"{name}.generate_signals was called")
        return _run

    allowed = set(signals) or {"NewsAgent", "BreakoutAgent"}
    for agent in ens.agents:
        if agent.name in allowed:
            agent.generate_signals = _ok(agent.name)
        else:
            agent.generate_signals = _forbidden(agent.name)
    return called


def _approved_signal() -> dict:
    return {
        "agent": "NewsAgent",
        "symbol": "AAPL",
        "direction": "long",
        "confidence": 0.90,
        "raw_confidence": 0.90,
        "entry_price": 100.0,
        "stop_loss_price": 96.0,
        "target_price": 110.0,
    }


class EntryKillSwitch(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # One real ensemble. Agents are not asked to scan unless a test
        # replaces generate_signals.
        cls.ens = Ensemble()

    def test_flags_stay_paper_and_crypto_off(self):
        self.assertTrue(PAPER_TRADING)
        self.assertIs(CRYPTO_TRADING_ENABLED, False)
        with _flags(env="true"):
            self.assertTrue(new_entries_enabled())
            self.assertIsNone(new_entries_disabled_reason())
            self.assertEqual(
                active_signal_roster(),
                ["BreakoutAgent", "NewsAgent"],
            )

    def test_env_false_values_and_default(self):
        for off in ("false", "False", "0", "no", "off", "disabled"):
            with _flags(env=off):
                self.assertFalse(new_entries_enabled(), off)
                self.assertEqual(
                    new_entries_disabled_reason(),
                    "NEW_ENTRIES_ENABLED=false",
                )
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("NEW_ENTRIES_ENABLED", None)
            with _flags(env="true"):
                pass
            os.environ.pop("NEW_ENTRIES_ENABLED", None)
            with patch("ensemble.NO_NEW_ENTRIES_PATH", Path("/tmp/absent-NO_NEW_ENTRIES")), \
                 patch("ensemble.AGENT_SUMMARY_PATH", Path("/tmp/absent-agent_summary.json")):
                self.assertTrue(new_entries_enabled())
                self.assertIsNone(new_entries_disabled_reason())

    def test_env_blocks_entries_and_management_still_runs(self):
        called = _arm_signals(self.ens)
        with _flags(env="false"), _managed_cycle(self.ens) as hooks, \
             self.assertLogs("Ensemble", level="INFO") as logs:
            result = self.ens.run_cycle()
        self.assertEqual(result, [])
        self.assertEqual(called, [])
        hooks["protect"].assert_called_once()
        hooks["ghosts"].assert_called_once()
        hooks["manage"].assert_called_once()
        hooks["trails"].assert_called_once()
        hooks["options"].assert_not_called()
        hooks["equity"].assert_not_called()
        stand = [m for m in logs.output if "New entries disabled" in m]
        self.assertEqual(stand, [
            "INFO:Ensemble:" + new_entries_disabled_message("NEW_ENTRIES_ENABLED=false"),
        ])

    def test_file_flag_blocks_entries_without_restart(self):
        with tempfile.TemporaryDirectory() as d:
            flag = Path(d) / "NO_NEW_ENTRIES"
            flag.write_text("ops\n")
            called = _arm_signals(self.ens)
            with _flags(env="true", flag=flag), _managed_cycle(self.ens) as hooks, \
                 self.assertLogs("Ensemble", level="INFO") as logs:
                first = self.ens.run_cycle()
            self.assertEqual(first, [])
            self.assertEqual(called, [])
            hooks["protect"].assert_called()
            hooks["manage"].assert_called()
            hooks["options"].assert_not_called()
            hooks["equity"].assert_not_called()
            stand = [m for m in logs.output if "New entries disabled" in m]
            self.assertEqual(len(stand), 1)
            self.assertIn("data/NO_NEW_ENTRIES", stand[0])
            self.assertIn("managing open positions only", stand[0])

            # Deleting the file is enough. The next tick reads it again.
            flag.unlink()
            called = _arm_signals(self.ens)
            with _flags(env="true", flag=flag), _managed_cycle(self.ens) as hooks:
                self.ens.run_cycle()
            self.assertEqual(called, ["NewsAgent", "BreakoutAgent"])
            hooks["options"].assert_not_called()

    def test_env_and_file_log_once(self):
        with tempfile.TemporaryDirectory() as d:
            flag = Path(d) / "NO_NEW_ENTRIES"
            flag.write_text("\n")
            _arm_signals(self.ens)
            with _flags(env="false", flag=flag), _managed_cycle(self.ens), \
                 self.assertLogs("Ensemble", level="INFO") as logs:
                self.ens.run_cycle()
        stand = [m for m in logs.output if "New entries disabled" in m]
        self.assertEqual(len(stand), 1)
        self.assertIn("NEW_ENTRIES_ENABLED=false, data/NO_NEW_ENTRIES", stand[0])

    def test_empty_roster_blocks_without_fallback_or_crash(self):
        with tempfile.TemporaryDirectory() as d:
            summary = Path(d) / "agent_summary.json"
            _summary(summary, {
                "NewsAgent": {"active": False, "pinned_reason": "friday bench"},
                "BreakoutAgent": {"active": False, "benched_at": "2099-01-01T00:00:00+00:00"},
                "TechnicalAgent": {"active": True},
                "MomentumAgent": {"active": True},
                "MeanReversionAgent": {"active": True},
                "SentimentAgent": {"active": True},
            })
            with _flags(env="true", summary=summary):
                self.assertEqual(active_signal_roster(), [])
                self.assertEqual(new_entries_disabled_reason(), "empty roster")
            # Every agent, including the legacy book, raises if asked to signal.
            for agent in self.ens.agents:
                agent.generate_signals = lambda n=agent.name: (_ for _ in ()).throw(
                    AssertionError(f"fallback called {n}")
                )
            with _flags(env="true", summary=summary), _managed_cycle(self.ens) as hooks, \
                 self.assertLogs("Ensemble", level="INFO") as logs:
                result = self.ens.run_cycle()
        self.assertEqual(result, [])
        hooks["protect"].assert_called_once()
        hooks["ghosts"].assert_called_once()
        hooks["manage"].assert_called_once()
        hooks["trails"].assert_called_once()
        hooks["options"].assert_not_called()
        hooks["equity"].assert_not_called()
        stand = [m for m in logs.output if "New entries disabled" in m]
        self.assertEqual(stand, [
            "INFO:Ensemble:" + new_entries_disabled_message("empty roster"),
        ])
        self.assertNotIn("TechnicalAgent", " ".join(logs.output))
        self.assertNotIn("PROMOTED", " ".join(logs.output))

    def test_one_active_agent_still_signals(self):
        with tempfile.TemporaryDirectory() as d:
            summary = Path(d) / "agent_summary.json"
            _summary(summary, {
                "NewsAgent": {"active": False},
                "BreakoutAgent": {"active": True},
                "TechnicalAgent": {"active": True},
            })
            called = _arm_signals(self.ens, {"BreakoutAgent": []})
            with _flags(env="true", summary=summary):
                self.assertEqual(active_signal_roster(), ["BreakoutAgent"])
                self.assertIsNone(new_entries_disabled_reason())
            with _flags(env="true", summary=summary), _managed_cycle(self.ens):
                result = self.ens.run_cycle()
        self.assertEqual(result, [])
        self.assertEqual(called, ["BreakoutAgent"])

    def test_normal_mode_still_reaches_options_entry(self):
        sig = _approved_signal()
        called = _arm_signals(self.ens, {"NewsAgent": [sig], "BreakoutAgent": []})
        self.ens.meta.synthesize = lambda *a, **k: [dict(sig)]
        self.ens.bridge.evaluate_signal = lambda signal: {**signal, "approved": True}
        with _flags(env="true"), _managed_cycle(self.ens) as hooks, \
             self.assertLogs("Ensemble", level="INFO") as logs:
            result = self.ens.run_cycle()
        self.assertEqual(called, ["NewsAgent", "BreakoutAgent"])
        self.assertEqual(len(result), 1)
        hooks["options"].assert_called_once()
        hooks["equity"].assert_not_called()
        hooks["manage"].assert_called_once()
        self.assertFalse(any("New entries disabled" in m for m in logs.output))
        self.assertTrue(any("Total raw signals" in m for m in logs.output))

    def test_options_entry_refuses_and_exits_still_run(self):
        from options_executor import (
            execute_options_trade,
            manage_options_exits,
            sync_option_protective_stop,
        )
        signal = _approved_signal()
        with _flags(env="false"), \
             patch("options_executor._clients") as clients:
            blocked = execute_options_trade(signal)
        clients.assert_not_called()
        self.assertEqual(blocked["status"], "blocked")
        self.assertEqual(blocked["reason"], "NEW_ENTRIES_ENABLED=false")

        with tempfile.TemporaryDirectory() as d:
            flag = Path(d) / "NO_NEW_ENTRIES"
            flag.write_text("hold\n")
            with _flags(env="true", flag=flag), \
                 patch("options_executor._clients") as clients:
                blocked = execute_options_trade(signal)
        clients.assert_not_called()
        self.assertIn("data/NO_NEW_ENTRIES", blocked["reason"])

        client = MagicMock()
        client.get_all_positions.return_value = []
        with _flags(env="false"), \
             patch("options_executor._clients", return_value=(client, None)):
            manage_options_exits()
        client.get_all_positions.assert_called_once()

        # Stop sync is a broker update, not an entry. The flag does not skip it.
        sym = "FPS261016C00035000"
        pos = type("P", (), {})()
        pos.symbol = sym
        pos.qty = 1
        pos.avg_entry_price = 1.0
        pos.current_price = 1.2
        pos.unrealized_pl = 20.0
        orders = []
        with _flags(env="false"):
            kept = sync_option_protective_stop(client, pos, orders, mark=1.2)
        self.assertIn(kept["action"], {"placed", "refused", "keep", "raised"})
        self.assertNotEqual(kept["action"], "blocked")

    def test_equity_execute_refuses_when_file_present(self):
        from order_executor import OrderExecutor
        with tempfile.TemporaryDirectory() as d:
            flag = Path(d) / "NO_NEW_ENTRIES"
            flag.write_text("\n")
            ex = OrderExecutor()
            ex._client = object()
            ex._portfolio_equity = lambda: 100_000.0
            ex._submit_equity_bracket = MagicMock(
                side_effect=AssertionError("equity entry submitted"),
            )
            with _flags(env="true", flag=flag), \
                 patch("session_gates.is_rth", return_value=True):
                result = ex.execute(_approved_signal())
        self.assertEqual(result["status"], "blocked")
        self.assertIn("data/NO_NEW_ENTRIES", result["reason"])
        ex._submit_equity_bracket.assert_not_called()

    def test_unreadable_summary_does_not_empty_the_roster(self):
        with tempfile.TemporaryDirectory() as d:
            summary = Path(d) / "agent_summary.json"
            summary.write_text("{not json")
            with _flags(env="true", summary=summary), \
                 self.assertLogs("Ensemble", level="WARNING"):
                self.assertEqual(
                    active_signal_roster(),
                    ["BreakoutAgent", "NewsAgent"],
                )
                self.assertIsNone(new_entries_disabled_reason())


if __name__ == "__main__":
    logging.basicConfig(level="INFO")
    unittest.main()
