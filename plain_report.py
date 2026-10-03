"""
plain_report.py
───────────────
The one after-close email. daily_reporter.py sends it at 4:35 PM ET.

One plain-English note per trading day, in three cadences:

  daily    every other session
  weekly   the last trading day of the week (Friday, or Thursday when
           Friday is an NYSE holiday). Replaces that day's daily note.
  monthly  the last trading day of the month. Replaces daily and weekly.

Money is the broker only: Alpaca portfolio history for official daily
closes, account equity / last_equity when today's 1D bar is not posted
yet, Alpaca positions for the open book, broker-fill round trips for
trade stats, and SPY close-to-close for the same dates. The ledger is
used only to name a round trip News or Breakout. It is never a dollar.

Alpaca's 1D portfolio bars are stamped the next UTC day. A Friday close
arrives with a Saturday UTC timestamp; the session date is that UTC
date minus one day.

This module only reads. It does not submit, cancel, or close orders.
With no .env and no network every figure is "unavailable" or
"not available". It does not invent a number.
"""

from __future__ import annotations

import html
import os
import smtplib
import ssl
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from zoneinfo import ZoneInfo

from session_gates import ET, is_nyse_session_day

# ── Config (one place) ───────────────────────────────────────────────────────

PLAN_START = date(2026, 10, 5)
GO_NO_GO = date(2026, 11, 13)
TRIPWIRE = 75_000.0
STARTING_EQUITY = 100_000.0
NEXT_CHANGES_PATH = Path(__file__).resolve().parent / "logs" / "weekly_next_changes.md"

PAPER_API = "https://paper-api.alpaca.markets"
DATA_API = "https://data.alpaca.markets"
PLAN_STRATEGIES = ("News", "Breakout")
_AGENT_TO_STRATEGY = {"NewsAgent": "News", "BreakoutAgent": "Breakout"}

FOOTER = (
    "Points (pts) are percentage points: the bot's percent return minus "
    "the S&P's percent return over the same dates."
)


# ── Small types ──────────────────────────────────────────────────────────────

@dataclass
class ClosedTrade:
    """One broker round trip. strategies None means we could not name it."""

    opened: date
    pnl: float
    strategies: tuple[str, ...] | None


@dataclass
class MarketView:
    """Broker facts for one report. Missing pieces stay None — never zero."""

    equity_by_day: dict[date, float] = field(default_factory=dict)
    account_equity: float | None = None
    last_equity: float | None = None
    spy_by_day: dict[date, float] = field(default_factory=dict)
    positions: list[dict] | None = None
    trades: list[ClosedTrade] | None = None
    alerts: list[str] = field(default_factory=list)
    # True only for today's live note, when the official 1D bar may be absent.
    live_equity: bool = False


@dataclass(frozen=True)
class PeriodStats:
    dollars: Decimal
    bot_pct: Decimal
    spy_pct: Decimal
    pts: Decimal


@dataclass(frozen=True)
class Email:
    subject: str
    body: str
    html: str


# ── Calendar ─────────────────────────────────────────────────────────────────

def _noon(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, 12, 0, tzinfo=ET)


def is_session(day: date) -> bool:
    """NYSE session, via session_gates (exchange calendar, else the holiday list)."""
    return bool(is_nyse_session_day(_noon(day)))


def previous_trading_day(day: date) -> date | None:
    cursor = day - timedelta(days=1)
    for _ in range(14):
        if is_session(cursor):
            return cursor
        cursor -= timedelta(days=1)
    return None


def _last_session_between(end: date, start: date) -> date | None:
    cursor = end
    while cursor >= start:
        if is_session(cursor):
            return cursor
        cursor -= timedelta(days=1)
    return None


def last_trading_day_of_week(day: date) -> date | None:
    """Friday, or the prior session when Friday is closed."""
    monday = day - timedelta(days=day.weekday())
    friday = monday + timedelta(days=4)
    return _last_session_between(friday, monday)


def last_trading_day_of_month(day: date) -> date | None:
    if day.month == 12:
        last = date(day.year, 12, 31)
    else:
        last = date(day.year, day.month + 1, 1) - timedelta(days=1)
    first = date(day.year, day.month, 1)
    return _last_session_between(last, first)


def select_cadence(day: date) -> str | None:
    """'daily', 'weekly', 'monthly', or None when the market is closed.

    Monthly wins when the session is also the week's note, so a month-end
    Friday (or a Thursday standing in for a holiday Friday) sends one email.
    """
    if not is_session(day):
        return None
    if day == last_trading_day_of_month(day):
        return "monthly"
    if day == last_trading_day_of_week(day):
        return "weekly"
    return "daily"


def shows_week_line(day: date) -> bool:
    """Monthly adds the week when this session is the week's note.

    That is Friday, or Thursday (or earlier) when Friday is a holiday.
    """
    return day == last_trading_day_of_week(day)


def _mdy(day: date) -> str:
    return f"{day.month}/{day.day}/{day.year}"


# ── Numbers ──────────────────────────────────────────────────────────────────

def D(value) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _q(value: Decimal, places: str) -> Decimal:
    return value.quantize(Decimal(places), rounding=ROUND_HALF_UP)


def round_dollars(value: Decimal) -> Decimal:
    return _q(value, "1")


def round_pts(value: Decimal) -> Decimal:
    return _q(value, "0.01")


def round_pct(value: Decimal) -> Decimal:
    return _q(value, "0.01")


def _up(rounded: Decimal, raw: Decimal) -> bool:
    return rounded > 0 or (rounded == 0 and raw >= 0)


def direction_dollars(dollars: Decimal) -> str:
    rounded = round_dollars(dollars)
    word = "UP" if _up(rounded, dollars) else "DOWN"
    return f"{word} ${abs(rounded):,.0f}"


def direction_pts(pts: Decimal) -> str:
    rounded = round_pts(pts)
    word = "AHEAD" if _up(rounded, pts) else "BEHIND"
    return f"{word} S&P by {abs(rounded):.2f} pts"


def dollar_phrase(dollars: Decimal) -> str:
    rounded = round_dollars(dollars)
    word = "up" if _up(rounded, dollars) else "down"
    return f"{word} ${abs(rounded):,.0f}"


def pct_phrase(pct: Decimal) -> str:
    rounded = round_pct(pct)
    if rounded > 0:
        return f"up {rounded:.2f}%"
    if rounded < 0:
        return f"down {abs(rounded):.2f}%"
    return "flat"


def pts_phrase(pts: Decimal) -> str:
    rounded = round_pts(pts)
    word = "ahead" if _up(rounded, pts) else "behind"
    return f"{word} by {abs(rounded):.2f} pts"


def money_cents(amount: Decimal) -> str:
    rounded = _q(amount, "0.01")
    if rounded > 0:
        return f"up ${rounded:,.2f}"
    if rounded < 0:
        return f"down ${abs(rounded):,.2f}"
    return "$0.00"


def level_dollars(amount: Decimal) -> str:
    return f"${_q(amount, '0.01'):,.2f}"


def period_stats(end_eq, start_eq, end_spy, start_spy) -> PeriodStats | None:
    if end_eq is None or start_eq is None or end_spy is None or start_spy is None:
        return None
    end_eq, start_eq = D(end_eq), D(start_eq)
    end_spy, start_spy = D(end_spy), D(start_spy)
    if start_eq <= 0 or start_spy <= 0 or end_eq <= 0 or end_spy <= 0:
        return None
    bot = (end_eq / start_eq - 1) * 100
    spy = (end_spy / start_spy - 1) * 100
    return PeriodStats(dollars=end_eq - start_eq, bot_pct=bot, spy_pct=spy, pts=bot - spy)


def equity_on(day: date | None, as_of: date, view: MarketView) -> Decimal | None:
    if day is None:
        return None
    if day in view.equity_by_day:
        value = view.equity_by_day[day]
        if value is None or D(value) <= 0:
            return None
        return D(value)
    if view.live_equity and day == as_of and view.account_equity is not None:
        if D(view.account_equity) <= 0:
            return None
        return D(view.account_equity)
    return None


def daily_start_equity(as_of: date, view: MarketView) -> Decimal | None:
    """Yesterday's close. Official bar, or last_equity when today's bar is late."""
    if as_of not in view.equity_by_day and view.live_equity and view.last_equity is not None:
        if D(view.last_equity) <= 0:
            return None
        return D(view.last_equity)
    return equity_on(previous_trading_day(as_of), as_of, view)


def spy_on(day: date | None, view: MarketView) -> Decimal | None:
    if day is None or day not in view.spy_by_day:
        return None
    value = view.spy_by_day[day]
    if value is None or D(value) <= 0:
        return None
    return D(value)


def _pair(as_of: date, start_day: date | None, view: MarketView, *, daily: bool) -> PeriodStats | None:
    end_eq = equity_on(as_of, as_of, view)
    if daily:
        start_eq = daily_start_equity(as_of, view)
        start_day = previous_trading_day(as_of)
    else:
        start_eq = equity_on(start_day, as_of, view)
    return period_stats(end_eq, start_eq, spy_on(as_of, view), spy_on(start_day, view))


def stats_for(cadence: str, as_of: date, view: MarketView) -> PeriodStats | None:
    if cadence == "daily":
        return _pair(as_of, None, view, daily=True)
    if cadence == "weekly":
        # Last session before this week's Monday: the prior week's close.
        anchor = previous_trading_day(_week_monday(as_of))
        return _pair(as_of, anchor, view, daily=False)
    anchor = previous_trading_day(date(as_of.year, as_of.month, 1))
    return _pair(as_of, anchor, view, daily=False)


def _week_monday(day: date) -> date:
    return day - timedelta(days=day.weekday())


def since_plan_stats(as_of: date, view: MarketView) -> PeriodStats | None:
    if as_of < PLAN_START:
        return None
    anchor = previous_trading_day(PLAN_START)
    return _pair(as_of, anchor, view, daily=False)


# ── Subject and sentences ────────────────────────────────────────────────────

def subject_line(cadence: str, as_of: date, view: MarketView) -> str:
    title = {"daily": "Daily", "weekly": "Weekly", "monthly": "Monthly"}[cadence]
    head = f"Trading [PAPER] {title} {_mdy(as_of)}: "
    stats = stats_for(cadence, as_of, view)
    if stats is None:
        return head + "unavailable"
    return head + f"{direction_dollars(stats.dollars)}, {direction_pts(stats.pts)}"


def opening_sentence(cadence: str, stats: PeriodStats | None) -> str:
    period = {"daily": "today", "weekly": "this week", "monthly": "this month"}[cadence]
    if stats is None:
        return f"The paper account's result for {period} is unavailable."
    rounded = round_dollars(stats.dollars)
    moved = "up" if _up(rounded, stats.dollars) else "down"
    pts_rounded = round_pts(stats.pts)
    if _up(pts_rounded, stats.pts):
        rel = f"ahead of the S&P by {abs(pts_rounded):.2f} points"
    else:
        rel = f"behind the S&P by {abs(pts_rounded):.2f} points"
    return (
        f"The paper account was {moved} ${abs(rounded):,.0f} {period}, {rel}."
    )


def format_period_line(label: str, stats: PeriodStats | None) -> str:
    if stats is None:
        return f"{label}: unavailable."
    return (
        f"{label}: the bot was {pct_phrase(stats.bot_pct)} and the S&P was "
        f"{pct_phrase(stats.spy_pct)}, {pts_phrase(stats.pts)} "
        f"({dollar_phrase(stats.dollars)})."
    )


def period_block(cadence: str, as_of: date, view: MarketView) -> str:
    today = _pair(as_of, None, view, daily=True)
    lines = [format_period_line("Today", today)]
    if cadence == "weekly" or (cadence == "monthly" and shows_week_line(as_of)):
        anchor = previous_trading_day(_week_monday(as_of))
        lines.append(format_period_line("This week", _pair(as_of, anchor, view, daily=False)))
    if cadence == "monthly":
        anchor = previous_trading_day(date(as_of.year, as_of.month, 1))
        lines.append(format_period_line("This month", _pair(as_of, anchor, view, daily=False)))
    return "\n".join(lines)


def go_no_go_status(as_of: date) -> str:
    if as_of < GO_NO_GO:
        return "The 11/13 test is still open."
    if as_of == GO_NO_GO:
        return "The 11/13 test ends today."
    return "The 11/13 test has ended."


def _win_rate(wins: int, n: int) -> str:
    if n <= 0:
        return "0%"
    rate = _q(Decimal(wins) / Decimal(n) * 100, "1")
    return f"{rate:.0f}%"


def _plan_names(trade: ClosedTrade) -> tuple[str, ...]:
    if not trade.strategies:
        return ()
    return tuple(name for name in trade.strategies if name in PLAN_STRATEGIES)


def trade_lines(trades: list[ClosedTrade] | None) -> list[str]:
    """Counts, win rate, and News / Breakout lines. Pre-plan trades are out."""
    if trades is None:
        return ["Trade results since the new plan: not available."]
    unknown = [t for t in trades if t.opened >= PLAN_START and t.strategies is None]
    if unknown:
        return ["Trade results since the new plan: not available."]
    kept = [t for t in trades if t.opened >= PLAN_START and _plan_names(t)]
    wins = sum(1 for t in kept if D(t.pnl) > 0)
    if kept:
        lines = [
            f"Trades opened since Mon 10/5: {len(kept)} closed, "
            f"win rate {_win_rate(wins, len(kept))}."
        ]
    else:
        lines = ["Trades opened since Mon 10/5: 0 closed."]
    for name in PLAN_STRATEGIES:
        rows = [t for t in kept if name in _plan_names(t)]
        if not rows:
            lines.append(f"{name}: 0 trades.")
            continue
        # A ticket signed by both plan strategies splits its dollars across them.
        pnl = Decimal(0)
        row_wins = 0
        for trade in rows:
            names = _plan_names(trade)
            share = D(trade.pnl) / Decimal(len(names))
            pnl += share
            if share > 0:
                row_wins += 1
        noun = "trade" if len(rows) == 1 else "trades"
        lines.append(
            f"{name}: {len(rows)} {noun}, win rate {_win_rate(row_wins, len(rows))}, "
            f"{money_cents(pnl)}."
        )
    return lines


def section_three(as_of: date, view: MarketView) -> str:
    if as_of < PLAN_START:
        return "New plan starts Mon 10/5"
    status = go_no_go_status(as_of)
    stats = since_plan_stats(as_of, view)
    if stats is None:
        line = f"Since the new plan (Mon 10/5): unavailable. {status}"
    else:
        line = (
            f"Since the new plan (Mon 10/5): the bot was {pct_phrase(stats.bot_pct)} "
            f"and the S&P was {pct_phrase(stats.spy_pct)}, {pts_phrase(stats.pts)} "
            f"({dollar_phrase(stats.dollars)}). {status}"
        )
    return line + "\n" + "\n".join(trade_lines(view.trades))


def standing_line(as_of: date, view: MarketView) -> str:
    equity = equity_on(as_of, as_of, view)
    if equity is None:
        book = "equity unavailable, distance to the $75,000 line unavailable"
    else:
        gap = equity - D(TRIPWIRE)
        side = "above" if gap >= 0 else "below"
        book = (
            f"equity {level_dollars(equity)}, {level_dollars(abs(gap))} {side} "
            f"the $75,000 line"
        )
    if view.positions is None:
        positions = "open positions unavailable"
    else:
        count = len(view.positions)
        noun = "position" if count == 1 else "positions"
        missing = any(p.get("unrealized_pl") is None for p in view.positions)
        if missing:
            positions = f"{count} open {noun}, unrealized P/L unavailable"
        else:
            total = sum((D(p.get("unrealized_pl") or 0) for p in view.positions), Decimal(0))
            positions = f"{count} open {noun}, unrealized P/L {money_cents(total)}"
    return f"Today's standing: {book}, {positions}."


def is_alert_text(text: str) -> bool:
    """True for anomaly, CRITICAL, naked-position, and ghost findings only."""
    upper = (text or "").upper()
    if "NO ANOMAL" in upper:
        return False
    return any(key in upper for key in ("CRITICAL", "NAKED", "GHOST", "ANOMALY"))


def alerts_line(alerts: list[str] | None) -> str:
    kept = []
    for raw in alerts or []:
        text = " ".join(str(raw).split())
        if text and is_alert_text(text):
            kept.append(text)
    if not kept:
        return ""
    return "Alerts: " + "; ".join(kept)


def legacy_realized(trades: list[ClosedTrade] | None) -> Decimal | None:
    """Realized P&L of non-News / non-Breakout trips, or None if we cannot say."""
    if trades is None:
        return None
    if any(t.strategies is None for t in trades):
        return None
    total = Decimal(0)
    for trade in trades:
        names = trade.strategies or ()
        if any(name in PLAN_STRATEGIES for name in names):
            continue
        total += D(trade.pnl)
    return total


def old_losses_block(as_of: date, view: MarketView) -> str:
    lines = ["OLD LOSSES — already cut, not part of current score"]
    equity = equity_on(as_of, as_of, view)
    if equity is None:
        lines.append("All-time drawdown from the $100,000 start: not available.")
    else:
        drop = _q(D(STARTING_EQUITY) - equity, "0.01")
        if drop > 0:
            lines.append(f"All-time drawdown from the $100,000 start: ${drop:,.2f}.")
        else:
            lines.append("All-time drawdown from the $100,000 start: $0.00.")
    legacy = legacy_realized(view.trades)
    if legacy is None:
        lines.append("Older strategies (not News or Breakout): not available.")
    else:
        lines.append(f"Older strategies (not News or Breakout): {money_cents(legacy)}.")
    return "\n".join(lines)


def read_next_changes(path: Path, as_of: date) -> list[str]:
    """First two '- ' lines, and only when the file was saved on as_of (ET)."""
    try:
        if not path.is_file():
            return []
        modified = datetime.fromtimestamp(path.stat().st_mtime, tz=ET).date()
        if modified != as_of:
            return []
        bullets: list[str] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.startswith("- "):
                continue
            text = line[2:].strip()
            if not text:
                continue
            bullets.append(text)
            if len(bullets) == 2:
                break
        return bullets
    except OSError:
        return []


def next_changes_block(cadence: str, as_of: date, path: Path) -> str:
    if cadence not in {"weekly", "monthly"}:
        return ""
    bullets = read_next_changes(path, as_of)
    if not bullets:
        return ""
    return "Next changes:\n" + "\n".join(f"- {item}" for item in bullets)


def render_body(cadence: str, as_of: date, view: MarketView, path: Path) -> str:
    stats = stats_for(cadence, as_of, view)
    blocks = [
        opening_sentence(cadence, stats),
        period_block(cadence, as_of, view),
        section_three(as_of, view),
    ]
    standing = standing_line(as_of, view)
    alert = alerts_line(view.alerts)
    blocks.append(standing if not alert else standing + "\n" + alert)
    blocks.append(old_losses_block(as_of, view))
    nxt = next_changes_block(cadence, as_of, path)
    if nxt:
        blocks.append(nxt)
    blocks.append(FOOTER)
    return "\n\n".join(blocks)


def render_html(body: str) -> str:
    safe = html.escape(body)
    return (
        "<!DOCTYPE html><html><body>"
        "<pre style=\"font-family:Georgia,serif;font-size:16px;white-space:pre-wrap;"
        "line-height:1.45\">"
        f"{safe}</pre></body></html>"
    )


def build_email(
    as_of: date,
    cadence: str,
    view: MarketView | None = None,
    *,
    next_changes_path: Path | None = None,
) -> Email:
    view = view or MarketView()
    path = NEXT_CHANGES_PATH if next_changes_path is None else next_changes_path
    body = render_body(cadence, as_of, view, path)
    return Email(subject=subject_line(cadence, as_of, view), body=body, html=render_html(body))


# ── Broker reads (GET only) ──────────────────────────────────────────────────

def _headers() -> dict | None:
    key = os.getenv("ALPACA_API_KEY", "").strip()
    secret = os.getenv("ALPACA_API_SECRET", "").strip()
    if not key or not secret:
        return None
    return {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}


def session_date_for_portfolio_bar(ts: datetime) -> date:
    """1D portfolio bars are stamped the next UTC day. Shift back one UTC date."""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc).date() - timedelta(days=1)


def session_date_for_spy_bar(ts: datetime) -> date:
    """SPY daily bars use the session date. Do not apply the portfolio shift."""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(ET).date()


def _parse_stamp(raw) -> datetime | None:
    if isinstance(raw, (int, float)):
        seconds = float(raw)
        if seconds > 10**12:  # milliseconds
            seconds /= 1000.0
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def equity_from_history_payload(payload: dict) -> dict[date, float]:
    if not isinstance(payload, dict):
        return {}
    stamps = payload.get("timestamp") or []
    equities = payload.get("equity") or []
    out: dict[date, float] = {}
    for raw_ts, raw_eq in zip(stamps, equities):
        if raw_eq in (None, ""):
            continue
        try:
            value = float(raw_eq)
        except (TypeError, ValueError):
            continue
        if value <= 0:
            continue
        ts = _parse_stamp(raw_ts)
        if ts is None:
            continue
        out[session_date_for_portfolio_bar(ts)] = value
    return out


def spy_from_bars(bars: list) -> dict[date, float]:
    out: dict[date, float] = {}
    for bar in bars or []:
        if not isinstance(bar, dict):
            continue
        raw_close = bar.get("c", bar.get("close"))
        try:
            close = float(raw_close)
        except (TypeError, ValueError):
            continue
        if close <= 0:
            continue
        ts = _parse_stamp(bar.get("t", bar.get("timestamp")))
        if ts is None:
            continue
        out[session_date_for_spy_bar(ts)] = close
    return out


def strategy_labels(agents: list[str]) -> tuple[str, ...]:
    out: list[str] = []
    for name in agents:
        label = _AGENT_TO_STRATEGY.get(str(name).strip())
        if label and label not in out:
            out.append(label)
    return tuple(out)


def closed_trades_from_books(trips, ledger_trades) -> list[ClosedTrade]:
    """Dollars from the round trip. The name comes from the matched ledger row."""
    from broker_fills import assign_round_trips
    from trade_ledger import broker_time_to_et, expand_agent_names

    assignment = assign_round_trips(ledger_trades, trips)
    named: dict[int, list[str]] = {}
    for trade in ledger_trades:
        if getattr(trade, "is_open", False):
            continue
        agents: list[str] = []
        for raw in (
            getattr(trade, "primary_agent", ""),
            getattr(trade, "contributors", ""),
        ):
            agents.extend(expand_agent_names(str(raw or "")))
        matched = assignment.get(str(getattr(trade, "trade_id", "")), [])
        for trip in matched:
            named[id(trip)] = agents
    rows: list[ClosedTrade] = []
    for trip in trips:
        opened = date.min
        try:
            opened = date.fromisoformat(broker_time_to_et(trip.entry_time)[:10])
        except Exception:
            opened = date.min
        agents = named.get(id(trip))
        strategies: tuple[str, ...] | None
        if not agents:
            # Unmatched, or matched with no leaf name: not reliably attributable.
            strategies = None
        else:
            strategies = strategy_labels(agents)
        rows.append(ClosedTrade(
            opened=opened,
            pnl=float(trip.realized_pnl),
            strategies=strategies,
        ))
    return rows


def _get_json(url: str, headers: dict, params: dict | None = None):
    import requests
    response = requests.get(url, headers=headers, params=params, timeout=15)
    response.raise_for_status()
    return response.json()


def _load_account(headers: dict) -> tuple[float | None, float | None]:
    try:
        payload = _get_json(f"{PAPER_API}/v2/account", headers)
        equity = float(payload.get("equity"))
        last = float(payload.get("last_equity"))
        return equity, last
    except Exception:
        return None, None


def _load_history(headers: dict) -> dict[date, float]:
    try:
        payload = _get_json(
            f"{PAPER_API}/v2/account/portfolio/history",
            headers,
            {"start": "2025-01-01", "timeframe": "1D"},
        )
        return equity_from_history_payload(payload)
    except Exception:
        return {}


def _load_positions(headers: dict) -> list[dict] | None:
    try:
        payload = _get_json(f"{PAPER_API}/v2/positions", headers)
        if not isinstance(payload, list):
            return None
        rows = []
        for row in payload:
            if not isinstance(row, dict):
                continue
            raw = row.get("unrealized_pl")
            try:
                pl = None if raw in (None, "") else float(raw)
            except (TypeError, ValueError):
                pl = None
            rows.append({"unrealized_pl": pl})
        return rows
    except Exception:
        return None


def _spy_from_yfinance(as_of: date) -> dict[date, float]:
    try:
        import yfinance as yf
        frame = yf.Ticker("SPY").history(
            start="2025-01-01",
            end=(as_of + timedelta(days=2)).isoformat(),
            interval="1d",
            auto_adjust=True,
        )
    except Exception:
        return {}
    out: dict[date, float] = {}
    if frame is None or getattr(frame, "empty", True):
        return {}
    for ts, row in frame.iterrows():
        try:
            close = float(row["Close"])
        except (TypeError, ValueError, KeyError):
            continue
        if close <= 0:
            continue
        if getattr(ts, "tzinfo", None) is not None and hasattr(ts, "tz_convert"):
            session = ts.tz_convert(ET).date()
        elif getattr(ts, "tzinfo", None) is not None:
            session = ts.astimezone(ET).date()
        else:
            session = ts.date()
        out[session] = close
    return out


def _load_spy(headers: dict | None, as_of: date) -> dict[date, float]:
    parsed: dict[date, float] = {}
    if headers:
        try:
            payload = _get_json(
                f"{DATA_API}/v2/stocks/SPY/bars",
                headers,
                {
                    "timeframe": "1Day",
                    "start": "2025-01-01",
                    "end": (as_of + timedelta(days=2)).isoformat(),
                    "adjustment": "raw",
                    "feed": "iex",
                    "limit": 1000,
                },
            )
            bars = payload.get("bars") if isinstance(payload, dict) else None
            parsed = spy_from_bars(bars or [])
        except Exception:
            parsed = {}
    # Alpaca's daily bar can lag a few minutes after the close. Fill gaps
    # from yfinance; a bar Alpaca already returned is kept.
    if as_of not in parsed or previous_trading_day(as_of) not in parsed:
        for day, close in _spy_from_yfinance(as_of).items():
            parsed.setdefault(day, close)
    return parsed


def _load_trades() -> list[ClosedTrade] | None:
    if not _headers():
        return None
    try:
        from broker_fills import fetch_round_trips, network_scoring_enabled
        if not network_scoring_enabled():
            return None
        trips = fetch_round_trips()
    except Exception:
        return None
    try:
        import trade_ledger as ledger
        book = list(ledger.all_trades())
    except Exception:
        book = None
    if book is None:
        rows = []
        for trip in trips:
            rows.append(ClosedTrade(opened=date.min, pnl=float(trip.realized_pnl), strategies=None))
        return rows
    try:
        return closed_trades_from_books(trips, book)
    except Exception:
        return None


def _fmt_invariant(item: dict) -> str:
    sev = str(item.get("severity") or "").strip()
    rule = str(item.get("rule") or "").strip()
    detail = " ".join(str(item.get("detail") or "").split())
    return f"{sev} {rule}: {detail}".strip()


def _invariant_alerts() -> list[str]:
    try:
        from invariants import report as invariant_report
        items = invariant_report() or []
    except Exception:
        return []
    lines = []
    for item in items:
        if not isinstance(item, dict):
            continue
        text = _fmt_invariant(item)
        if is_alert_text(text):
            lines.append(text)
    return lines


def _scheduler_alerts() -> list[str]:
    try:
        from daily_reporter import diagnose, read_scheduler_today
        sched = read_scheduler_today()
        stub = {
            "sched": sched,
            "approved_count": 0,
            "rejected_count": 0,
            "raw_signals_total": 0,
            "rejection_reasons": {},
            "approved_trades": [],
            "agent_last_seen": {},
        }
        return [line for line in diagnose(stub) if is_alert_text(line)]
    except Exception:
        return []


def collect_alerts() -> list[str]:
    """Existing anomaly / CRITICAL / naked / ghost findings. Never raises."""
    lines: list[str] = []
    if _headers():
        lines.extend(_invariant_alerts())
    lines.extend(_scheduler_alerts())
    seen = set()
    out = []
    for line in lines:
        key = " ".join(line.split())
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return out


def load_view(as_of: date, *, live: bool) -> MarketView:
    """Read the broker. Any failure leaves that field empty. Never raises."""
    view = MarketView(live_equity=live)
    headers = _headers()
    if headers and live:
        view.account_equity, view.last_equity = _load_account(headers)
        view.positions = _load_positions(headers)
    if headers:
        view.equity_by_day = _load_history(headers)
    view.spy_by_day = _load_spy(headers, as_of)
    try:
        view.trades = _load_trades()
    except Exception:
        view.trades = None
    if live:
        try:
            view.alerts = collect_alerts()
        except Exception:
            view.alerts = []
    return view


def deliver(email: Email) -> bool:
    """Send plain text and simple HTML. No broker calls."""
    address = os.getenv("GMAIL_ADDRESS", "").strip()
    password = os.getenv("GMAIL_APP_PASSWORD", "").strip()
    recipient = os.getenv("REPORT_TO_EMAIL", "").strip() or address
    if not address or not password or not recipient:
        print("ERROR: GMAIL_ADDRESS or GMAIL_APP_PASSWORD not set")
        return False
    message = MIMEMultipart("alternative")
    message["Subject"] = email.subject
    message["From"] = address
    message["To"] = recipient
    message.attach(MIMEText(email.body, "plain", "utf-8"))
    message.attach(MIMEText(email.html, "html", "utf-8"))
    try:
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as server:
            server.login(address, password)
            server.sendmail(address, recipient, message.as_string())
        print(f"After-close email sent to {recipient}")
        print(f"Subject: {email.subject}")
        return True
    except Exception as exc:
        print(f"Failed to send email: {exc}")
        return False


def _parse_date(text: str) -> date:
    raw = text.strip()
    if "/" in raw:
        month, day, year = raw.split("/")
        return date(int(year), int(month), int(day))
    return date.fromisoformat(raw)


def resolve_as_of(argv: list[str], today: date) -> date:
    for i, arg in enumerate(argv):
        if arg == "--preview" and i + 1 < len(argv) and not argv[i + 1].startswith("-"):
            return _parse_date(argv[i + 1])
        if arg.startswith("--preview="):
            return _parse_date(arg.split("=", 1)[1])
    return today


def main(argv: list[str] | None = None, *, today: date | None = None) -> int:
    """--preview prints the note. --send-now emails it. Preview never sends."""
    import sys
    argv = list(sys.argv[1:] if argv is None else argv)
    today = today or datetime.now(ET).date()
    preview = any(a == "--preview" or a.startswith("--preview=") for a in argv)
    send = "--send-now" in argv and not preview
    try:
        as_of = resolve_as_of(argv, today)
    except ValueError as exc:
        print(f"Could not read the date: {exc}")
        return 1
    cadence = select_cadence(as_of)
    if cadence is None:
        print(f"No email on {_mdy(as_of)}: the market is closed.")
        return 0
    try:
        view = load_view(as_of, live=(as_of == today))
        email = build_email(as_of, cadence, view)
    except Exception as exc:
        print(f"Could not build the email: {exc}")
        return 1
    if not send:
        print(f"Subject: {email.subject}")
        print()
        print(email.body)
        return 0
    return 0 if deliver(email) else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
