"""
report_data.py
───────────────
ONE validated snapshot. Every report renders from this and nothing else.

Why this exists: daily_reporter.py grew to 1,806 lines reading 9 data
sources, 3 of them dead. Each section chose its own source, so three
separate bugs shipped in one week — all silent, all flattering:

  2026-07-30  ledger-sourced P&L claimed "+$2,376, beating SPY" during a
              week broker equity fell $19,908                 ($22k error)
  2026-07-31  "No approved trades today" while AXTI and CVX filled
              (section still read the dead trade_log.jsonl)
  2026-07-31  "+$4,209, equity $88,927" on a day the account was at
              $86,176 and down $2,750 — Alpaca's daily history bar only
              posts AFTER settlement, so eq[-1] is always yesterday
                                                            ($6,960 error)

The rule enforced here:
    BROKER is truth for money.  LEDGER is truth for attribution.
Nothing downstream gets to decide.

CHANGE LOG (L-2026-10-01b):
    The RECONCILE line used to subtract ledger lifetime realized (and
    option intraday marks) from broker day equity change. Those are
    different numbers: day change is today's mark-to-market, ledger
    realized is the full life of trades that closed today. The line now
    compares broker realized today (round trips exited today) with
    ledger realized today, and prints unrealized change on its own.

And critically: this SELF-VALIDATES. If ledger P&L and broker equity
disagree beyond tolerance, the snapshot carries a loud discrepancy that
the report prints at the top. A wrong number that announces itself is
recoverable; a wrong number that looks right is what cost this account
three days of bad decisions.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

BASE = Path(__file__).resolve().parent
load_dotenv(BASE / ".env")
ET = ZoneInfo("America/New_York")
log = logging.getLogger("ReportData")

PAPER_API = "https://paper-api.alpaca.markets"
START_EQUITY = 100_000.0
RECONCILE_TOLERANCE = 250.0     # $ gap that triggers a visible warning


def unexplained_day_gap(broker_realized, ledger_realized, unrealized_change=0.0) -> float:
    """Booking gap: broker realized today vs ledger realized today.

    Both sides are the full round-trip P&L of positions that closed
    today (broker from fills, ledger from ledger rows). `unrealized_change`
    is today's mark on positions still open. It is accepted so callers
    can pass it and is deliberately not part of the gap — mixing it in
    was the old formula (day equity change minus lifetime realized minus
    option marks), which flagged a definitional difference as drift.

    Sep 30: no closes, both realized sides 0, gap 0; the -$374 day is
    unrealized. Oct 1: the +$914 ledger number is lifetime realized of
    the closes, not today's -$358 mark.
    """
    del unrealized_change  # reported beside the gap, never inside it
    return abs(float(broker_realized) - float(ledger_realized))


def reconcile_line(broker_day_pnl, broker_realized, ledger_realized,
                   unrealized_change, *, tolerance: float = RECONCILE_TOLERANCE) -> str:
    """One scorecard line. Realized vs realized, unrealized beside it."""
    gap = unexplained_day_gap(broker_realized, ledger_realized, unrealized_change)
    if gap > tolerance:
        head = (
            f"RECONCILE: broker realized today ${float(broker_realized):+,.0f} vs "
            f"ledger realized today ${float(ledger_realized):+,.0f} — "
            f"${gap:,.0f} unexplained booking gap."
        )
    else:
        head = (
            f"RECONCILE: broker realized today ${float(broker_realized):+,.0f} vs "
            f"ledger realized today ${float(ledger_realized):+,.0f} "
            f"(gap ${gap:,.0f})."
        )
    return (
        f"{head} Unrealized change today ${float(unrealized_change):+,.0f} "
        f"is separate. Broker day mark-to-market ${float(broker_day_pnl):+,.0f} "
        f"is not lifetime realized."
    )


def _broker_realized_today(day: str):
    """Full round-trip P&L of broker fills that exited on `day` (ET).

    None when the paper book cannot be read. Never raises.
    """
    try:
        from broker_fills import fetch_round_trips, network_scoring_enabled, realized_on_date
        if not network_scoring_enabled():
            return None
        return realized_on_date(fetch_round_trips(), day)
    except Exception as e:
        log.warning("broker realized today unavailable (%s)", e)
        return None


def _hdr() -> dict:
    return {"APCA-API-KEY-ID": os.getenv("ALPACA_API_KEY", ""),
            "APCA-API-SECRET-KEY": os.getenv("ALPACA_API_SECRET", "")}


def snapshot() -> dict:
    """The single validated view. Never raises; degrades with warnings."""
    d: dict = {"warnings": [], "generated_at": datetime.now(ET).isoformat(timespec="seconds"),
               "today": datetime.now(ET).strftime("%Y-%m-%d")}

    # ── MONEY: broker only ────────────────────────────────────────────
    try:
        a = requests.get(f"{PAPER_API}/v2/account", headers=_hdr(), timeout=15).json()
        d["equity"] = float(a.get("equity") or 0)
        d["prev_close"] = float(a.get("last_equity") or 0)
        d["buying_power"] = float(a.get("buying_power") or 0)
        d["options_bp"] = float(a.get("options_buying_power") or 0)
        d["day_pnl"] = d["equity"] - d["prev_close"]
        d["total_pnl"] = d["equity"] - START_EQUITY
        d["total_pct"] = (d["equity"] / START_EQUITY - 1) * 100
    except Exception as e:
        d["warnings"].append(f"CRITICAL: broker account unreachable ({e}) — "
                             f"all money figures below are unavailable, not zero")
        for k in ("equity", "prev_close", "buying_power", "options_bp",
                  "day_pnl", "total_pnl", "total_pct"):
            d[k] = None
        return d

    # Multi-day windows. eq[-1] is YESTERDAY (settled bars only) — the
    # live equity above is today, so windows compare live vs eq[-N].
    d["windows"] = []
    try:
        h = requests.get(f"{PAPER_API}/v2/account/portfolio/history",
                         params={"period": "3M", "timeframe": "1D"},
                         headers=_hdr(), timeout=15).json()
        eq = [e for e in (h.get("equity") or []) if e]
        import yfinance as yf
        spy = yf.Ticker("SPY").history(period="3mo", interval="1d")

        def spy_pct(bars: int) -> float:
            bars = min(bars, len(spy) - 1)
            if bars <= 0:
                return 0.0
            return (float(spy["Close"].iloc[-1]) / float(spy["Close"].iloc[-1 - bars]) - 1) * 100

        specs = [("1-day", d["prev_close"], 1), ("5-day", eq[-5] if len(eq) >= 5 else None, 5),
                 ("20-day", eq[-20] if len(eq) >= 20 else None, 20),
                 ("Since start", START_EQUITY, len(spy) - 1)]
        for label, prev, bars in specs:
            if not prev:
                continue
            bot = (d["equity"] / prev - 1) * 100
            sp = spy_pct(bars)
            d["windows"].append({"label": label, "chg": d["equity"] - prev,
                                 "bot_pct": bot, "spy_pct": sp, "edge": bot - sp})
    except Exception as e:
        d["warnings"].append(f"benchmark windows unavailable ({e})")

    # ── POSITIONS: broker only ────────────────────────────────────────
    try:
        ps = requests.get(f"{PAPER_API}/v2/positions", headers=_hdr(), timeout=15).json()
        def _entry(raw):
            try:
                if raw in (None, ""):
                    return None
                px = float(raw)
            except (TypeError, ValueError):
                return None
            return px if px > 0 else None

        d["positions"] = [{
            "symbol": p["symbol"], "qty": float(p["qty"]),
            "is_option": len(p["symbol"]) > 12,
            "side": "LONG" if float(p["qty"]) > 0 else "SHORT",
            "mv": float(p["market_value"]), "unrl": float(p["unrealized_pl"]),
            "unrl_intraday": float(p.get("unrealized_intraday_pl") or 0),
            "avg_entry_price": _entry(p.get("avg_entry_price")),
        } for p in ps]
        d["unrealized"] = sum(p["unrl"] for p in d["positions"])
        d["gross_exposure"] = sum(abs(p["mv"]) for p in d["positions"])
        d["leverage"] = (d["gross_exposure"] / d["equity"]) if d["equity"] else 0
    except Exception as e:
        d["warnings"].append(f"positions unavailable ({e})")
        d["positions"], d["unrealized"] = [], None

    # ── ATTRIBUTION: ledger only (never money-of-record) ──────────────
    try:
        import trade_ledger as _tl
        from broker_fills import scoring_skip_reason
        today = d["today"]
        book = list(_tl.all_trades())
        opened = [t for t in book if (t.opened_at_et or "")[:10] == today]
        broker_lots = d.get("positions") or []
        closed = []
        for t in book:
            if t.is_open or (t.exit_at_et or "")[:10] != today:
                continue
            reason = scoring_skip_reason(t, book, broker_lots)
            if reason:
                d["warnings"].append(
                    "CRITICAL: phantom closed lot excluded from realized "
                    f"today — {reason}"
                )
                continue
            closed.append(t)
        d["opened_today"] = [{
            "time": (t.opened_at_et or "")[11:19], "symbol": t.symbol,
            "side": t.side,
            "agent": str(t.primary_agent).replace("MetaAgent(", "").rstrip(")"),
            "notional": float(t.entry_price or 0) * float(t.shares or 0),
        } for t in opened]
        d["closed_today"] = [{
            "symbol": t.symbol, "side": t.side, "pnl": t.realized_pnl or 0.0,
            "agent": str(t.primary_agent).replace("MetaAgent(", "").rstrip(")"),
        } for t in closed]
        d["ledger_realized_today"] = sum(c["pnl"] for c in d["closed_today"])
    except Exception as e:
        d["warnings"].append(f"ledger attribution unavailable ({e})")
        d["opened_today"], d["closed_today"] = [], []
        d["ledger_realized_today"] = None

    # ── SELF-VALIDATION — the point of this module ────────────────────
    # Like with like: broker realized today (round trips that exited
    # today) vs ledger realized today. Unrealized change is printed
    # beside that, not subtracted. Day equity change stays on the money
    # line; it is mark-to-market and is not the booking check.
    if d.get("ledger_realized_today") is not None and d.get("day_pnl") is not None:
        unrealized_change = sum(
            float(p.get("unrl_intraday") or 0) for p in d.get("positions", [])
        )
        d["unrealized_change"] = round(unrealized_change, 2)
        options_day = sum(
            float(p.get("unrl_intraday") or 0)
            for p in d.get("positions", [])
            if p.get("is_option")
        )
        d["options_day_pnl"] = round(options_day, 2)
        broker_realized = _broker_realized_today(d["today"])
        d["broker_realized_today"] = broker_realized
        if broker_realized is None:
            d["reconcile_gap"] = None
            d["warnings"].append(
                "RECONCILE: broker realized today unavailable — not comparing "
                "day mark-to-market to lifetime ledger realized. "
                f"Ledger realized today ${d['ledger_realized_today']:+,.0f}. "
                f"Unrealized change today ${unrealized_change:+,.0f}. "
                f"Broker day mark-to-market ${d['day_pnl']:+,.0f}."
            )
        else:
            gap = unexplained_day_gap(
                broker_realized, d["ledger_realized_today"], unrealized_change)
            d["reconcile_gap"] = gap
            d["warnings"].append(reconcile_line(
                d["day_pnl"], broker_realized, d["ledger_realized_today"],
                unrealized_change,
            ))

    # Positions the ledger does not know about — the drift that has
    # repeatedly consumed buying power invisibly.
    try:
        import trade_ledger as _tl
        from invariants import ghost_symbols, naked_equity_symbols
        led = {t.symbol.replace("/", "") for t in _tl.open_positions()}
        orphans = [p["symbol"] for p in d.get("positions", [])
                   if not p["is_option"] and p["symbol"] not in led]
        d["orphans"] = orphans
        if orphans:
            d["warnings"].append(
                f"DRIFT: broker holds {len(orphans)} position(s) the ledger has "
                f"closed: {', '.join(orphans[:8])}")
        ghosts = ghost_symbols(led, [p["symbol"] for p in d.get("positions", [])])
        d["ghosts"] = ghosts
        if ghosts:
            d["warnings"].append(
                f"GHOST: ledger shows {len(ghosts)} open the broker does not hold: "
                f"{', '.join(ghosts[:8])}")
        try:
            od = requests.get(f"{PAPER_API}/v2/orders", headers=_hdr(),
                              params={"status": "open", "limit": 200}, timeout=15).json()
            naked = naked_equity_symbols(
                requests.get(f"{PAPER_API}/v2/positions", headers=_hdr(), timeout=15).json(),
                od)
            d["naked"] = naked
            if naked:
                d["warnings"].append(
                    f"CRITICAL: {len(naked)} position(s) have NO exit order: "
                    f"{', '.join(naked[:8])}")
        except Exception:
            d["naked"] = []
    except Exception:
        d["orphans"] = []
        d["ghosts"] = []
        d["naked"] = []

    # Invariant layer — explicit rules plus statistical anomaly detection.
    # The snapshot's own checks catch what it computes; invariants.py
    # checks the things NO single module owns (orphans, unprotected
    # positions, double-counted P&L, stacking, leverage, novel outliers).
    try:
        from invariants import report as _inv
        for i in _inv():
            d["warnings"].append(f"[{i['severity']}] {i['rule']}: {i['detail']}")
    except Exception as _ie:
        d["warnings"].append(f"invariant check unavailable ({_ie})")

    if d.get("leverage", 0) > 2.0:
        d["warnings"].append(f"LEVERAGE {d['leverage']:.2f}x — gross exposure "
                             f"${d['gross_exposure']:,.0f} on ${d['equity']:,.0f} equity")
    if d.get("buying_power", 1e9) < 20_000:
        d["warnings"].append(f"BUYING POWER ${d['buying_power']:,.0f} — near the "
                             f"reserve floor; new entries will be blocked")
    return d


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    s = snapshot()
    print(f"equity      ${s['equity']:,.2f}   day {s['day_pnl']:+,.2f}   "
          f"total {s['total_pct']:+.2f}%")
    print(f"positions   {len(s['positions'])}  unrealized ${s['unrealized']:+,.2f}  "
          f"leverage {s.get('leverage',0):.2f}x")
    print(f"opened today {len(s['opened_today'])}   closed today {len(s['closed_today'])}")
    for w in s["windows"]:
        print(f"  {w['label']:12} bot {w['bot_pct']:+6.2f}%  spy {w['spy_pct']:+6.2f}%  "
              f"edge {w['edge']:+6.2f}%")
    print("\nWARNINGS:" if s["warnings"] else "\nno warnings")
    for w in s["warnings"]:
        print(f"  ⚠ {w}")
