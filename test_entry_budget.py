"""After-10:00 ET entry budget. No broker, no network. Paper only.

After 10:00 ET, new non-crypto entries for the rest of the day are limited
to max(1, daily_trade_cap // 2), minus entries already opened at or after
10:00 ET, and still cannot exceed the daily cap. Before 10:00 only the
daily cap applies.

Both the early "cap reached" return and the per-tick "budget exhausted"
break call entry_slot_open with the same entries_remaining.
"""

from __future__ import annotations

import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from ensemble import (
    PAPER_TRADING,
    entry_slot_open,
    equity_entry_budget,
)
from session_gates import CRYPTO_TRADING_ENABLED
from trade_ledger import Trade


ET = ZoneInfo("America/New_York")
DAY = "2026-10-05"


def _row(symbol: str, hhmmss: str) -> Trade:
    opened = f"{DAY} {hhmmss}"
    return Trade(
        trade_id=f"{symbol}-{hhmmss}",
        opened_at_et=opened,
        symbol=symbol,
        side="LONG",
        primary_agent="TestAgent",
        contributors="",
        entry_price=10.0,
        target_price=12.0,
        stop_price=9.0,
        risk_dollar=100.0,
        shares=1.0,
    )


def _at(hhmmss: str) -> datetime:
    return datetime.strptime(f"{DAY} {hhmmss}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=ET)


class After10EntryBudget(unittest.TestCase):
    def test_flags_stay_paper_and_crypto_off(self):
        self.assertTrue(PAPER_TRADING)
        self.assertIs(CRYPTO_TRADING_ENABLED, False)

    def test_one_before_10_plus_one_after_blocks_second_after_10(self):
        # 2026-10-05: cap 3, RXO 09:53, then PCVX 11:12 and XP 11:13.
        # Limit is max(1, 3 // 2) = 1. The second after-10 entry is blocked.
        morning = [_row("RXO", "09:53:00")]
        first_slot = equity_entry_budget(morning, 3, _at("11:12:40"))
        self.assertEqual(first_slot.after_10_limit, 1)
        self.assertEqual(first_slot.opened_today, 1)
        self.assertEqual(first_slot.opened_after_10, 0)
        self.assertEqual(first_slot.entries_remaining, 1)
        self.assertTrue(entry_slot_open(first_slot.entries_remaining, 0))
        # Per-tick break uses that same remaining: one approval fills it.
        self.assertFalse(entry_slot_open(first_slot.entries_remaining, 1))

        both = morning + [_row("PCVX", "11:12:40")]
        blocked = equity_entry_budget(both, 3, _at("11:13:58"))
        self.assertEqual(blocked.opened_today, 2)
        self.assertEqual(blocked.opened_after_10, 1)
        self.assertEqual(blocked.entries_remaining, 0)
        # Early return and the per-tick path both see zero left.
        self.assertFalse(entry_slot_open(blocked.entries_remaining, 0))
        self.assertFalse(entry_slot_open(blocked.entries_remaining, 1))

        # 10:00:00 itself counts. 09:59:59 does not.
        on_hour = equity_entry_budget(
            morning + [_row("PCVX", "10:00:00")], 3, _at("10:01:00"),
        )
        self.assertEqual(on_hour.opened_after_10, 1)
        self.assertEqual(on_hour.entries_remaining, 0)
        edge = equity_entry_budget(
            [_row("RXO", "09:59:59")], 3, _at("11:00:00"),
        )
        self.assertEqual(edge.opened_after_10, 0)
        self.assertEqual(edge.entries_remaining, 1)

    def test_zero_before_10_allows_only_one_after_when_cap_is_3(self):
        empty = equity_entry_budget([], 3, _at("11:00:00"))
        self.assertEqual(empty.after_10_limit, 1)
        self.assertEqual(empty.opened_today, 0)
        self.assertEqual(empty.entries_remaining, 1)
        self.assertTrue(entry_slot_open(empty.entries_remaining, 0))
        self.assertFalse(entry_slot_open(empty.entries_remaining, 1))

        one_after = equity_entry_budget(
            [_row("PCVX", "11:12:40")], 3, _at("11:13:58"),
        )
        self.assertEqual(one_after.opened_after_10, 1)
        self.assertEqual(one_after.entries_remaining, 0)
        self.assertFalse(entry_slot_open(one_after.entries_remaining))

        # Crypto does not consume the equity budget.
        crypto = equity_entry_budget(
            [_row("BTC/USD", "11:05:00")], 3, _at("11:06:00"),
        )
        self.assertEqual(crypto.opened_today, 0)
        self.assertEqual(crypto.opened_after_10, 0)
        self.assertEqual(crypto.entries_remaining, 1)

        # Daily cap still binds when the morning already filled it,
        # even though the after-10 limit would have allowed one.
        full_morning = equity_entry_budget(
            [
                _row("AAA", "09:31:00"),
                _row("BBB", "09:40:00"),
                _row("CCC", "09:50:00"),
            ],
            3,
            _at("11:00:00"),
        )
        self.assertEqual(full_morning.opened_after_10, 0)
        self.assertEqual(full_morning.after_10_limit, 1)
        self.assertEqual(full_morning.entries_remaining, 0)

    def test_before_10_is_unaffected(self):
        none_yet = equity_entry_budget([], 3, _at("09:59:59"))
        self.assertIsNone(none_yet.after_10_limit)
        self.assertEqual(none_yet.entries_remaining, 3)
        self.assertTrue(entry_slot_open(none_yet.entries_remaining, 2))
        self.assertFalse(entry_slot_open(none_yet.entries_remaining, 3))

        one = equity_entry_budget([_row("RXO", "09:40:00")], 3, _at("09:53:00"))
        self.assertIsNone(one.after_10_limit)
        self.assertEqual(one.opened_today, 1)
        self.assertEqual(one.opened_after_10, 0)
        self.assertEqual(one.entries_remaining, 2)

        # A later open stamp does not clamp the morning. Only the clock does.
        later_stamp = equity_entry_budget(
            [_row("ZZZ", "11:00:00")], 3, _at("09:45:00"),
        )
        self.assertEqual(later_stamp.opened_after_10, 1)
        self.assertIsNone(later_stamp.after_10_limit)
        self.assertEqual(later_stamp.entries_remaining, 2)

        two = equity_entry_budget(
            [_row("RXO", "09:40:00"), _row("PCVX", "09:50:00")],
            3,
            _at("09:55:00"),
        )
        self.assertEqual(two.entries_remaining, 1)

        # Exactly 10:00 starts the afternoon budget. 09:59 does not.
        at_open = equity_entry_budget([], 3, _at("10:00:00"))
        self.assertEqual(at_open.after_10_limit, 1)
        self.assertEqual(at_open.entries_remaining, 1)


if __name__ == "__main__":
    unittest.main()
