"""Plain after-close email: cadence, wording, and what stays out of the score."""

from __future__ import annotations

import io
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

from broker_fills import RoundTrip
from plain_report import (
    FOOTER,
    GO_NO_GO,
    NEXT_CHANGES_PATH,
    PLAN_START,
    STARTING_EQUITY,
    TRIPWIRE,
    ClosedTrade,
    MarketView,
    alerts_line,
    build_email,
    closed_trades_from_books,
    direction_dollars,
    direction_pts,
    _load_history,
    equity_from_history_payload,
    is_day_preliminary,
    load_view,
    settled_close_for_day,
    go_no_go_status,
    is_alert_text,
    legacy_realized,
    main,
    read_next_changes,
    select_cadence,
    session_date_for_portfolio_bar,
    session_date_for_spy_bar,
    shows_week_line,
    spy_from_bars,
    trade_lines,
)
import weekly_reporter

ET = ZoneInfo("America/New_York")
FRI = date(2026, 10, 2)
THU = date(2026, 10, 1)
PRIOR_FRI = date(2026, 9, 25)


def fixture_view(**overrides) -> MarketView:
    view = MarketView(
        equity_by_day={
            FRI: 78671.07,
            THU: 78131.09,
            PRIOR_FRI: 78967.12,
        },
        spy_by_day={
            THU: 763.99,
            FRI: 769.64,
            PRIOR_FRI: 771.35,
        },
        positions=[],
        trades=None,
        live_equity=True,
        account_equity=1.0,   # ignored when the official close is present
        last_equity=1.0,
    )
    for key, value in overrides.items():
        setattr(view, key, value)
    return view


class Cadence(unittest.TestCase):
    def test_normal_day_is_daily(self):
        self.assertEqual(select_cadence(date(2026, 10, 1)), "daily")  # Thursday
        self.assertEqual(select_cadence(date(2026, 10, 7)), "daily")  # Wednesday
        self.assertIsNone(select_cadence(date(2026, 10, 3)))          # Saturday

    def test_friday_is_weekly(self):
        self.assertEqual(select_cadence(FRI), "weekly")

    def test_month_end_friday_is_monthly(self):
        day = date(2026, 10, 30)
        self.assertEqual(select_cadence(day), "monthly")
        self.assertTrue(shows_week_line(day))

    def test_month_end_not_friday_is_monthly_without_the_week(self):
        # Thursday 4/30; Friday 5/1 is a normal session, so this is not the weekly note.
        day = date(2026, 4, 30)
        self.assertEqual(select_cadence(day), "monthly")
        self.assertFalse(shows_week_line(day))
        self.assertEqual(select_cadence(date(2026, 9, 30)), "monthly")  # Wednesday

    def test_holiday_friday_moves_weekly_to_thursday(self):
        # Good Friday 2026-04-03. The week note goes out Thursday.
        self.assertIsNone(select_cadence(date(2026, 4, 3)))
        self.assertEqual(select_cadence(date(2026, 4, 2)), "weekly")
        # Christmas Friday 2026-12-25; Thursday 12/24 is the week note, not month-end.
        self.assertEqual(select_cadence(date(2026, 12, 24)), "weekly")
        # Month-end Thursday when Friday is New Year's Day: one monthly note, with the week.
        self.assertEqual(select_cadence(date(2026, 12, 31)), "monthly")
        self.assertTrue(shows_week_line(date(2026, 12, 31)))


class SubjectWording(unittest.TestCase):
    def test_fixture_daily_and_weekly(self):
        view = fixture_view()
        daily = build_email(FRI, "daily", view)
        weekly = build_email(FRI, "weekly", view)
        self.assertEqual(
            daily.subject,
            "Trading [PAPER] Daily 10/2/2026: UP $540, BEHIND S&P by 0.05 pts",
        )
        self.assertEqual(
            weekly.subject,
            "Trading [PAPER] Weekly 10/2/2026: DOWN $296, BEHIND S&P by 0.15 pts",
        )
        self.assertIn(
            "Today: the bot was up 0.69% and the S&P was up 0.74%, "
            "behind by 0.05 pts (up $540).",
            daily.body,
        )
        self.assertIn(
            "This week: the bot was down 0.37% and the S&P was down 0.22%, "
            "behind by 0.15 pts (down $296).",
            weekly.body,
        )
        self.assertLess(weekly.body.index("Today:"), weekly.body.index("This week:"))
        self.assertNotIn("This week:", daily.body)
        self.assertNotIn("This month:", daily.body)
        self.assertIn("The paper account was up $540 today, behind the S&P by 0.05 points.", daily.body)
        self.assertIn(
            "The paper account was down $296 this week, behind the S&P by 0.15 points.",
            weekly.body,
        )
        self.assertEqual(weekly.body.count("percentage points"), 1)
        self.assertIn(FOOTER, weekly.body)
        for banned in ("edge pts", "1/N", "rotator", "MetaAgent"):
            self.assertNotIn(banned, weekly.body)
            self.assertNotIn(banned, weekly.html)

    def test_direction_words_and_rounding(self):
        self.assertEqual(direction_dollars(Decimal("10.4")), "UP $10")
        self.assertEqual(direction_dollars(Decimal("10.5")), "UP $11")
        self.assertEqual(direction_dollars(Decimal("-10.5")), "DOWN $11")
        self.assertEqual(direction_dollars(Decimal("-0.4")), "DOWN $0")
        self.assertEqual(direction_pts(Decimal("0.004")), "AHEAD S&P by 0.00 pts")
        self.assertEqual(direction_pts(Decimal("0.005")), "AHEAD S&P by 0.01 pts")
        self.assertEqual(direction_pts(Decimal("-0.004")), "BEHIND S&P by 0.00 pts")
        self.assertEqual(direction_pts(Decimal("-1.2")), "BEHIND S&P by 1.20 pts")
        # A clean ahead/up day, separate from the fixture.
        view = MarketView(
            equity_by_day={date(2026, 10, 1): 10100.0, date(2026, 9, 30): 10000.0},
            spy_by_day={date(2026, 10, 1): 100.0, date(2026, 9, 30): 100.0},
        )
        email = build_email(date(2026, 10, 1), "daily", view)
        self.assertEqual(
            email.subject,
            "Trading [PAPER] Daily 10/1/2026: UP $100, AHEAD S&P by 1.00 pts",
        )

    def test_late_official_bar_uses_account_and_last_equity(self):
        # Today's bar is missing, so the live equity is labeled preliminary.
        # The prior history close matches last_equity in this fixture, so
        # the dollars still match the settled note.
        official = fixture_view()
        late = fixture_view(
            equity_by_day={THU: 78131.09, PRIOR_FRI: 78967.12},
            account_equity=78671.07,
            last_equity=78131.09,
            live_equity=True,
        )
        self.assertFalse(is_day_preliminary(FRI, official))
        self.assertTrue(is_day_preliminary(FRI, late))
        for cadence in ("daily", "weekly"):
            a = build_email(FRI, cadence, official)
            b = build_email(FRI, cadence, late)
            self.assertNotIn("preliminary", a.subject)
            self.assertNotIn("preliminary", a.body.lower())
            self.assertIn("(preliminary)", b.subject)
            self.assertIn("preliminary", b.body.lower())
            for word in ("provisional", "estimate", "unofficial", "intraday", "last_equity"):
                self.assertNotIn(word, b.body.lower())
            self.assertIn("equity $78,671.07 (preliminary)", b.body)
            self.assertIn("$3,671.07 above the $75,000 line", b.body)
            self.assertIn("All-time drawdown from the $100,000 start: $21,328.93.", b.body)
        self.assertIn("UP $540 (preliminary)", build_email(FRI, "daily", late).subject)
        self.assertIn("DOWN $296 (preliminary)", build_email(FRI, "weekly", late).subject)
        self.assertIn("(up $540, preliminary)", build_email(FRI, "daily", late).body)

    def test_missing_broker_does_not_invent_numbers(self):
        email = build_email(date(2026, 10, 1), "daily", MarketView())
        self.assertTrue(email.subject.endswith("unavailable"))
        self.assertNotIn("UP $", email.subject)
        self.assertNotIn("DOWN $", email.subject)
        self.assertIn("unavailable", email.body)
        self.assertNotIn("UP $0", email.body)
        self.assertNotIn("$0.00 above", email.body)


class PlanAndOldLosses(unittest.TestCase):
    def test_constants(self):
        self.assertEqual(PLAN_START, date(2026, 10, 5))
        self.assertEqual(GO_NO_GO, date(2026, 11, 13))
        self.assertEqual(TRIPWIRE, 75_000.0)
        self.assertEqual(STARTING_EQUITY, 100_000.0)
        self.assertEqual(NEXT_CHANGES_PATH.name, "weekly_next_changes.md")

    def test_before_plan_start_says_the_plan_has_not_started(self):
        view = fixture_view(trades=[
            ClosedTrade(date(2026, 10, 1), 999, ("News",)),
            ClosedTrade(date(2026, 10, 5), 40, ("News",)),
        ])
        email = build_email(FRI, "weekly", view)
        self.assertIn("New plan starts Mon 10/5", email.body)
        self.assertNotIn("win rate", email.body)
        self.assertNotIn("11/13 test", email.body)
        self.assertNotIn("News:", email.body)
        self.assertNotIn("999", email.body)

    def test_plan_start_trade_filter(self):
        trades = [
            ClosedTrade(date(2026, 10, 2), 500, ("News",)),       # before the plan
            ClosedTrade(date(2026, 10, 4), 80, ("Breakout",)),    # Sunday before Mon 10/5
            ClosedTrade(date(2026, 10, 5), 40, ("News",)),
            ClosedTrade(date(2026, 10, 6), -10, ("Breakout",)),
            ClosedTrade(date(2026, 10, 6), 5, ("Momentum",)),     # not a plan strategy
        ]
        email = build_email(date(2026, 10, 7), "daily", MarketView(trades=trades))
        self.assertNotIn("New plan starts", email.body)
        self.assertIn("The 11/13 test is still open.", email.body)
        self.assertIn("Trades opened since Mon 10/5: 2 closed, win rate 50%.", email.body)
        self.assertIn("News: 1 trade, win rate 100%, up $40.00.", email.body)
        self.assertIn("Breakout: 1 trade, win rate 0%, down $10.00.", email.body)
        self.assertNotIn("500", email.body)
        self.assertNotIn("80", email.body)
        self.assertEqual(go_no_go_status(date(2026, 11, 13)), "The 11/13 test ends today.")
        self.assertEqual(go_no_go_status(date(2026, 11, 16)), "The 11/13 test has ended.")

    def test_broker_fill_open_date_not_ledger_dollars(self):
        trips = [
            RoundTrip("AAA", "LONG", 1, 10, 11, "2026-10-01T14:00:00Z",
                      "2026-10-02T18:00:00Z", 500.0),
            RoundTrip("BBB", "LONG", 1, 10, 12, "2026-10-05T14:00:00Z",
                      "2026-10-06T18:00:00Z", 40.0),
            RoundTrip("CCC", "LONG", 1, 10, 9, "2026-10-06T14:00:00Z",
                      "2026-10-07T18:00:00Z", -10.0),
        ]

        def row(tid, symbol, agent, opened):
            return SimpleNamespace(
                trade_id=tid, symbol=symbol, side="LONG", shares=1,
                opened_at_et=opened, is_open=False,
                primary_agent=agent, contributors="",
                realized_pnl=99999.0,
            )

        named = closed_trades_from_books(trips, [
            row("1", "AAA", "NewsAgent", "2026-10-01 10:00:00"),
            row("2", "BBB", "NewsAgent", "2026-10-05 10:00:00"),
            row("3", "CCC", "BreakoutAgent", "2026-10-06 10:00:00"),
        ])
        lines = "\n".join(trade_lines(named))
        self.assertIn("2 closed, win rate 50%", lines)
        self.assertNotIn("99999", lines)
        self.assertNotIn("500", lines)

    def test_old_losses_stay_out_of_the_score(self):
        view = fixture_view(trades=[
            ClosedTrade(date(2026, 9, 1), -4242.42, ()),
            ClosedTrade(date(2026, 10, 6), 40, ("News",)),
        ])
        email = build_email(FRI, "weekly", view)
        head, marker, tail = email.body.partition(
            "OLD LOSSES — already cut, not part of current score"
        )
        self.assertTrue(marker)
        self.assertNotIn("4,242.42", head)
        self.assertNotIn("21,328.93", head)
        self.assertNotIn("$100,000", head)
        self.assertIn("down $4,242.42", tail)
        self.assertIn("All-time drawdown from the $100,000 start: $21,328.93.", tail)
        self.assertIn("up $540", head)
        self.assertIn("down $296", head)
        # The same sentences are in the HTML, still under that heading.
        html_head, _, html_tail = email.html.partition("OLD LOSSES")
        self.assertNotIn("4,242.42", html_head)
        self.assertIn("4,242.42", html_tail)

    def test_legacy_pnl_not_available_when_a_trip_has_no_name(self):
        self.assertIsNone(legacy_realized(None))
        self.assertIsNone(legacy_realized([
            ClosedTrade(date(2026, 9, 1), -10, None),
        ]))
        email = build_email(FRI, "daily", fixture_view(trades=[
            ClosedTrade(date(2026, 9, 1), -10, None),
        ]))
        self.assertIn("Older strategies (not News or Breakout): not available.", email.body)


class NextChanges(unittest.TestCase):
    def _write(self, folder: str, text: str, modified: date) -> Path:
        path = Path(folder) / "weekly_next_changes.md"
        path.write_text(text, encoding="utf-8")
        stamp = datetime(modified.year, modified.month, modified.day, 15, 0, tzinfo=ET)
        os.utime(path, (stamp.timestamp(), stamp.timestamp()))
        return path

    def test_first_two_bullets_only_when_the_file_is_from_today(self):
        body = "\n".join([
            "# ideas",
            "not a bullet",
            "-no-space",
            "- Cut the open loser",
            "a note",
            "- Keep News smaller",
            "- Ignore this third line",
        ])
        with tempfile.TemporaryDirectory() as folder:
            fresh = self._write(folder, body, FRI)
            self.assertEqual(
                read_next_changes(fresh, FRI),
                ["Cut the open loser", "Keep News smaller"],
            )
            email = build_email(FRI, "weekly", fixture_view(), next_changes_path=fresh)
            self.assertIn("Next changes:\n- Cut the open loser\n- Keep News smaller", email.body)
            self.assertNotIn("Ignore this third line", email.body)
            self.assertNotIn("not a bullet", email.body)
            self.assertNotIn("no-space", email.body)

            stale = self._write(folder, body, THU)
            stale_email = build_email(FRI, "weekly", fixture_view(), next_changes_path=stale)
            self.assertNotIn("Next changes:", stale_email.body)
            self.assertNotIn("Cut the open loser", stale_email.body)

            daily = build_email(FRI, "daily", fixture_view(), next_changes_path=fresh)
            self.assertNotIn("Next changes:", daily.body)
            self.assertNotIn("Cut the open loser", daily.body)

            monthly = build_email(
                date(2026, 10, 30), "monthly", MarketView(), next_changes_path=fresh,
            )
            # File date is 10/2, report date is 10/30 — stale, so the section is omitted.
            self.assertNotIn("Next changes:", monthly.body)

    def test_missing_or_unreadable_file_omits_the_section(self):
        missing = Path(tempfile.gettempdir()) / "does-not-exist-next-changes.md"
        email = build_email(FRI, "weekly", fixture_view(), next_changes_path=missing)
        self.assertNotIn("Next changes:", email.body)
        self.assertEqual(read_next_changes(missing, FRI), [])

        class Boom:
            def is_file(self):
                return True

            def stat(self):
                raise OSError("unreadable")

        self.assertEqual(read_next_changes(Boom(), FRI), [])  # type: ignore[arg-type]


class AlertsAndStanding(unittest.TestCase):
    def test_alerts_line_only_when_non_empty(self):
        quiet = build_email(FRI, "daily", fixture_view(alerts=[]))
        self.assertNotIn("Alerts:", quiet.body)
        noisy = build_email(FRI, "daily", fixture_view(alerts=[
            "CRITICAL: naked-position kill-switch fired",
            "GHOST: ledger shows ABC open the broker does not hold",
            "all clear",  # not an alert; dropped
        ]))
        self.assertIn(
            "Alerts: CRITICAL: naked-position kill-switch fired; "
            "GHOST: ledger shows ABC open the broker does not hold",
            noisy.body,
        )
        self.assertNotIn("all clear", noisy.body)
        self.assertLess(noisy.body.index("Today's standing:"), noisy.body.index("Alerts:"))
        self.assertLess(noisy.body.index("Alerts:"), noisy.body.index("OLD LOSSES"))

    def test_filter_keeps_the_existing_findings(self):
        self.assertFalse(is_alert_text(
            "No anomalies detected. Bot ran healthy ticks, signals flowed."
        ))
        self.assertTrue(is_alert_text("CRITICAL: naked-position kill-switch fired"))
        self.assertTrue(is_alert_text("GHOST: ledger shows 1 open the broker does not hold"))
        self.assertTrue(is_alert_text("[WARN] anomaly:equity: equity is 4.2 sigma off"))
        self.assertFalse(is_alert_text("RECONCILE: broker realized today matches"))
        self.assertEqual(alerts_line([]), "")
        self.assertEqual(alerts_line(["all clear"]), "")

    def test_standing_line(self):
        view = fixture_view(positions=[
            {"unrealized_pl": 12.5},
            {"unrealized_pl": -2.5},
        ])
        email = build_email(FRI, "daily", view)
        self.assertIn(
            "Today's standing: equity $78,671.07, $3,671.07 above the $75,000 line, "
            "2 open positions, unrealized P/L up $10.00.",
            email.body,
        )
        below = build_email(
            date(2026, 10, 1),
            "daily",
            MarketView(
                equity_by_day={date(2026, 10, 1): 74000.0, date(2026, 9, 30): 74000.0},
                spy_by_day={date(2026, 10, 1): 100.0, date(2026, 9, 30): 100.0},
                positions=None,
            ),
        )
        self.assertIn("$1,000.00 below the $75,000 line", below.body)
        self.assertIn("open positions not loaded for past dates", below.body)
        live_missing = build_email(
            FRI, "daily", fixture_view(positions=None, live_equity=True),
        )
        self.assertIn("open positions unavailable", live_missing.body)
        self.assertNotIn("not loaded for past dates", live_missing.body)


def _utc(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp())


def real_history_payload() -> dict:
    """Shaped like Alpaca period=1A: leading zeros, then the real closes.

    The last three points are the closes the paper account actually returned,
    stamped on the next UTC day. base_value is the account start, not a bar.
    """
    return {
        "timestamp": [
            _utc(date(2026, 5, 1)),
            _utc(date(2026, 5, 2)),
            *[_utc(date(2026, 5, 3))] * 19,
            _utc(date(2026, 10, 1)),
            _utc(date(2026, 10, 2)),
            _utc(date(2026, 10, 3)),
        ],
        "equity": [0.0, 0.0, *([0.0] * 19), 78466.63, 78131.09, 78671.07],
        "profit_loss": [0.0, 0.0, *([0.0] * 19), -1533.37, -1868.91, -1328.93],
        "profit_loss_pct": [0.0, 0.0, *([0.0] * 19), -0.015, -0.019, -0.013],
        "base_value": 100000.0,
        "base_value_asof": "2026-04-30",
        "timeframe": "1D",
    }


class PortfolioStamp(unittest.TestCase):
    def test_daily_bar_is_the_previous_utc_date(self):
        payload = {"timestamp": ["2026-10-03T00:00:00Z"], "equity": [78671.07]}
        self.assertEqual(equity_from_history_payload(payload)[FRI], 78671.07)
        unix = int(datetime(2026, 10, 3, tzinfo=timezone.utc).timestamp())
        again = equity_from_history_payload({"timestamp": [unix], "equity": [1.5]})
        self.assertIn(FRI, again)
        # The portfolio shift is not applied to SPY bars.
        late = datetime(2026, 10, 3, 8, 0, tzinfo=timezone.utc)
        self.assertEqual(session_date_for_portfolio_bar(late), FRI)
        self.assertEqual(session_date_for_spy_bar(late), date(2026, 10, 3))
        self.assertEqual(
            spy_from_bars([{"t": "2026-10-02T04:00:00Z", "c": 769.64}])[FRI],
            769.64,
        )

    def test_real_history_payload_drops_zeros(self):
        got = equity_from_history_payload(real_history_payload())
        self.assertEqual(got, {
            date(2026, 9, 30): 78466.63,
            THU: 78131.09,
            FRI: 78671.07,
        })
        self.assertNotIn(date(2026, 4, 30), got)
        self.assertEqual(equity_from_history_payload({
            "timestamp": [_utc(date(2026, 10, 3))] * 21,
            "equity": [0.0] * 21,
            "base_value": None,
            "timeframe": "1D",
        }), {})

    def test_history_requests_one_year_then_three_months(self):
        calls = []

        def fake_get(url, headers, params=None):
            calls.append(dict(params or {}))
            self.assertNotIn("start", params or {})
            if params.get("period") == "1A":
                return {
                    "timestamp": [_utc(date(2026, 10, 3))] * 21,
                    "equity": [0.0] * 21,
                    "base_value": None,
                    "timeframe": "1D",
                }
            return real_history_payload()

        with patch("plain_report._get_json", side_effect=fake_get):
            got = _load_history({"APCA-API-KEY-ID": "x"})
        self.assertEqual(calls, [
            {"period": "1A", "timeframe": "1D"},
            {"period": "3M", "timeframe": "1D"},
        ])
        self.assertEqual(got[FRI], 78671.07)
        self.assertNotIn(date(2026, 4, 30), got)

        calls.clear()

        def one_year(url, headers, params=None):
            calls.append(dict(params or {}))
            return real_history_payload()

        with patch("plain_report._get_json", side_effect=one_year):
            got = _load_history({"APCA-API-KEY-ID": "x"})
        self.assertEqual(calls, [{"period": "1A", "timeframe": "1D"}])
        self.assertEqual(got[FRI], 78671.07)

    def test_past_date_preview_uses_the_history_close(self):
        view = MarketView(
            equity_by_day=equity_from_history_payload(real_history_payload()) | {
                PRIOR_FRI: 78967.12,
            },
            spy_by_day={THU: 763.99, FRI: 769.64, PRIOR_FRI: 771.35},
            positions=None,
            account_equity=None,
            last_equity=None,
            live_equity=False,
        )
        email = build_email(FRI, "weekly", view)
        self.assertEqual(
            email.subject,
            "Trading [PAPER] Weekly 10/2/2026: DOWN $296, BEHIND S&P by 0.15 pts",
        )
        self.assertIn(
            "Today's standing: equity $78,671.07, $3,671.07 above the $75,000 line, "
            "open positions not loaded for past dates.",
            email.body,
        )
        self.assertIn("All-time drawdown from the $100,000 start: $21,328.93.", email.body)
        self.assertNotIn("unavailable", email.subject)


class MonthlyShape(unittest.TestCase):
    def test_month_end_friday_includes_week_and_day(self):
        email = build_email(date(2026, 10, 30), "monthly", MarketView())
        self.assertLess(email.body.index("Today:"), email.body.index("This week:"))
        self.assertLess(email.body.index("This week:"), email.body.index("This month:"))

    def test_month_end_thursday_has_the_day_and_not_the_week(self):
        email = build_email(date(2026, 4, 30), "monthly", MarketView())
        self.assertIn("Today:", email.body)
        self.assertIn("This month:", email.body)
        self.assertNotIn("This week:", email.body)


class Cli(unittest.TestCase):
    def test_preview_prints_and_does_not_send(self):
        buf = io.StringIO()
        with patch("plain_report.load_view", return_value=fixture_view()), \
             patch("plain_report.deliver") as deliver, \
             patch("sys.stdout", buf):
            code = main(["--preview", "2026-10-02"], today=date(2026, 10, 3))
        self.assertEqual(code, 0)
        deliver.assert_not_called()
        text = buf.getvalue()
        self.assertIn(
            "Subject: Trading [PAPER] Weekly 10/2/2026: DOWN $296, BEHIND S&P by 0.15 pts",
            text,
        )
        self.assertIn("Today:", text)

    def test_send_now_uses_the_cadence_and_skips_a_closed_day(self):
        with patch("plain_report.load_view", return_value=fixture_view()), \
             patch("plain_report.deliver", return_value=True) as deliver:
            code = main(["--send-now"], today=FRI)
        self.assertEqual(code, 0)
        self.assertIn("Weekly", deliver.call_args[0][0].subject)
        self.assertNotIn("Daily", deliver.call_args[0][0].subject)

        buf = io.StringIO()
        with patch("plain_report.deliver") as deliver, patch("sys.stdout", buf):
            closed = main(["--send-now"], today=date(2026, 4, 3))
        self.assertEqual(closed, 0)
        deliver.assert_not_called()
        self.assertIn("market is closed", buf.getvalue())

    def test_preview_of_a_holiday_does_not_crash(self):
        buf = io.StringIO()
        with patch("plain_report.deliver") as deliver, patch("sys.stdout", buf):
            code = main(["--preview", "2026-04-03"], today=date(2026, 4, 3))
        self.assertEqual(code, 0)
        deliver.assert_not_called()
        self.assertIn("market is closed", buf.getvalue())

    def test_source_does_not_submit_orders(self):
        src = Path("plain_report.py").read_text(encoding="utf-8")
        for banned in (
            "submit_order", "cancel_order", "close_position",
            "close_all_positions", "requests.post", "requests.delete", "requests.patch",
        ):
            self.assertNotIn(banned, src)

    def test_weekly_send_now_is_a_noop_unless_the_flag_is_set(self):
        self.assertFalse(weekly_reporter.legacy_weekly_send_enabled())
        with patch.dict(os.environ, {"LEGACY_WEEKLY_EMAIL": "1"}):
            self.assertTrue(weekly_reporter.legacy_weekly_send_enabled())
        env = os.environ.copy()
        env.pop("LEGACY_WEEKLY_EMAIL", None)
        proc = subprocess.run(
            [sys.executable, "weekly_reporter.py", "--send-now"],
            cwd=str(Path(__file__).resolve().parent),
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("LEGACY_WEEKLY_EMAIL", proc.stdout)
        self.assertNotIn("sent", proc.stdout.lower())

    def test_preview_cli_runs_without_env(self):
        env = os.environ.copy()
        for key in ("ALPACA_API_KEY", "ALPACA_API_SECRET", "GMAIL_ADDRESS",
                    "GMAIL_APP_PASSWORD", "REPORT_TO_EMAIL"):
            env.pop(key, None)
        proc = subprocess.run(
            [sys.executable, "daily_reporter.py", "--preview", "2026-10-02"],
            cwd=str(Path(__file__).resolve().parent),
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)
        self.assertIn("unavailable", proc.stdout)
        self.assertNotIn("UP $", proc.stdout.split("Subject:", 1)[-1].splitlines()[0])


MON = date(2026, 10, 5)
# 10/5 settled close from portfolio history. Prior close is the number
# that makes that day -$391.
SETTLED_CLOSE = 78280.10
PRIOR_CLOSE = SETTLED_CLOSE + 391  # 78671.10


def _next_utc_midnight(session: date) -> str:
    """Portfolio-history 1D bars are stamped the next UTC midnight."""
    nxt = session + timedelta(days=1)
    return f"{nxt.isoformat()}T00:00:00Z"


class SettledDayPnL(unittest.TestCase):
    """Day P&L comes from portfolio-history closes, not the live account."""

    def _load(self, history, account, *, history_error=False):
        periods = []

        def fake_get(url, headers, params=None):
            if "portfolio/history" in url:
                period = (params or {}).get("period")
                periods.append(period)
                self.assertEqual((params or {}).get("timeframe"), "1D")
                if history_error:
                    raise RuntimeError("portfolio history unavailable")
                if callable(history):
                    return history(period)
                return history
            if url.endswith("/v2/account"):
                return account
            if url.endswith("/v2/positions"):
                return []
            raise RuntimeError("unexpected url")

        spy = {date(2026, 10, 2): 100.0, MON: 100.0}
        with patch.dict(os.environ, {
            "ALPACA_API_KEY": "test-key",
            "ALPACA_API_SECRET": "test-secret",
        }), patch("plain_report._get_json", side_effect=fake_get), \
             patch("plain_report._load_spy", return_value=spy), \
             patch("plain_report._load_trades", return_value=[]), \
             patch("plain_report.collect_alerts", return_value=[]):
            view = load_view(MON, live=True)
        return view, periods

    def test_settled_close_day_pnl_uses_prior_history_close(self):
        # Live equity would have printed DOWN $531. The settled day is -$391.
        history = {
            "timestamp": [_next_utc_midnight(date(2026, 10, 2)), _next_utc_midnight(MON)],
            "equity": [PRIOR_CLOSE, SETTLED_CLOSE],
            "timeframe": "1D",
        }
        self.assertEqual(settled_close_for_day(history, MON), SETTLED_CLOSE)
        view, periods = self._load(
            history,
            {"equity": PRIOR_CLOSE - 531, "last_equity": 77000.0},
        )
        self.assertEqual(periods, ["1A", "3M", "1M", "1W"])
        self.assertEqual(view.equity_by_day[MON], SETTLED_CLOSE)
        self.assertEqual(view.equity_by_day[date(2026, 10, 2)], PRIOR_CLOSE)
        self.assertFalse(is_day_preliminary(MON, view))
        email = build_email(MON, "daily", view)
        self.assertEqual(
            email.subject,
            "Trading [PAPER] Daily 10/5/2026: DOWN $391, BEHIND S&P by 0.50 pts",
        )
        self.assertNotIn("preliminary", email.subject.lower())
        self.assertNotIn("preliminary", email.body.lower())
        self.assertIn("down 0.50%", email.body)
        self.assertIn("(down $391).", email.body)
        self.assertIn("equity $78,280.10,", email.body)
        self.assertIn(
            "Since the new plan (Mon 10/5): the bot was down 0.50%",
            email.body,
        )
        self.assertNotIn("531", email.subject)
        self.assertNotIn("78,140", email.body)

    def test_session_et_stamp_is_also_a_settled_close(self):
        # A bar timestamped on the session's own ET date (4:00 PM ET) is
        # today's close even though the UTC-minus-one map lands on Sunday.
        history = {
            "timestamp": ["2026-10-03T00:00:00Z", "2026-10-05T20:00:00Z"],
            "equity": [PRIOR_CLOSE, SETTLED_CLOSE],
        }
        self.assertEqual(settled_close_for_day(history, MON), SETTLED_CLOSE)
        view, _periods = self._load(history, {"equity": 1.0, "last_equity": 1.0})
        email = build_email(MON, "daily", view)
        self.assertIn("DOWN $391", email.subject)
        self.assertNotIn("preliminary", email.subject.lower())

    def test_missing_today_bar_is_preliminary(self):
        # Latest stamp is Friday's close (Saturday 00:00 UTC), before Monday.
        history = {
            "timestamp": [_next_utc_midnight(date(2026, 10, 2))],
            "equity": [PRIOR_CLOSE],
            "timeframe": "1D",
        }
        self.assertIsNone(settled_close_for_day(history, MON))
        live = PRIOR_CLOSE - 142
        view, _periods = self._load(
            history,
            {"equity": live, "last_equity": 77000.0},
        )
        self.assertNotIn(MON, view.equity_by_day)
        self.assertTrue(is_day_preliminary(MON, view))
        email = build_email(MON, "daily", view)
        self.assertIn(
            "Trading [PAPER] Daily 10/5/2026: DOWN $142 (preliminary)",
            email.subject,
        )
        self.assertIn("down $142 (preliminary) today", email.body)
        self.assertIn("(down $142, preliminary)", email.body)
        self.assertIn("equity $78,529.10 (preliminary)", email.body)
        # last_equity would have been live 78529.10 minus 77000 = UP $1,529.
        self.assertNotIn("UP $1,529", email.subject)
        self.assertNotIn("UP $1,529", email.body)
        for section in ("Today:", "Since the new plan", "Today's standing:", "OLD LOSSES", FOOTER):
            self.assertIn(section, email.body)

    def test_null_or_zero_today_bar_is_preliminary(self):
        live = PRIOR_CLOSE - 142
        account = {"equity": live, "last_equity": 77000.0}
        for bad in (None, 0, 0.0):
            history = {
                "timestamp": [
                    _next_utc_midnight(date(2026, 10, 2)),
                    _next_utc_midnight(MON),
                ],
                "equity": [PRIOR_CLOSE, bad],
            }
            self.assertIsNone(settled_close_for_day(history, MON))
            view, _periods = self._load(history, account)
            email = build_email(MON, "daily", view)
            self.assertIn("DOWN $142 (preliminary)", email.subject)
            self.assertNotIn("DOWN $391", email.subject)

    def test_one_week_missing_today_overrides_a_longer_window(self):
        long = {
            "timestamp": [_next_utc_midnight(date(2026, 10, 2)), _next_utc_midnight(MON)],
            "equity": [PRIOR_CLOSE, SETTLED_CLOSE],
        }
        short = {
            "timestamp": [_next_utc_midnight(date(2026, 10, 2))],
            "equity": [PRIOR_CLOSE],
        }
        view, _periods = self._load(
            lambda period: short if period in ("1W", "1M") else long,
            {"equity": PRIOR_CLOSE - 142, "last_equity": 77000.0},
        )
        email = build_email(MON, "daily", view)
        self.assertIn("DOWN $142 (preliminary)", email.subject)
        self.assertNotIn(MON, view.equity_by_day)

    def test_all_zero_week_keeps_the_settled_longer_window(self):
        long = {
            "timestamp": [_next_utc_midnight(date(2026, 10, 2)), _next_utc_midnight(MON)],
            "equity": [PRIOR_CLOSE, SETTLED_CLOSE],
        }
        zeros = {
            "timestamp": [_next_utc_midnight(date(2026, 10, 2))] * 3,
            "equity": [0.0, 0.0, 0.0],
        }
        view, _periods = self._load(
            lambda period: zeros if period in ("1W", "1M") else long,
            {"equity": 1.0, "last_equity": 1.0},
        )
        email = build_email(MON, "daily", view)
        self.assertIn("DOWN $391", email.subject)
        self.assertNotIn("preliminary", email.subject.lower())
        self.assertEqual(view.equity_by_day[MON], SETTLED_CLOSE)

    def test_history_api_error_falls_back_without_crashing(self):
        view, periods = self._load(
            {},
            {"equity": 78000.0, "last_equity": PRIOR_CLOSE},
            history_error=True,
        )
        self.assertEqual(periods, ["1A", "3M", "1M", "1W"])
        self.assertEqual(view.equity_by_day, {})
        self.assertTrue(is_day_preliminary(MON, view))
        email = build_email(MON, "daily", view)
        # Old behavior: live equity minus last_equity, now labeled preliminary.
        self.assertIn("DOWN $671 (preliminary)", email.subject)
        self.assertIn("preliminary", email.body)
        self.assertIn("[PAPER]", email.subject)
        self.assertNotIn("DOWN $391", email.subject)
        for section in ("Today:", "Since the new plan", "Today's standing:", "OLD LOSSES", FOOTER):
            self.assertIn(section, email.body)


if __name__ == "__main__":
    unittest.main()
