"""
market_scheduler.py
───────────────────
Main entry point for the cloud-hosted trading loop.

• Runs ONLY during US market hours: Mon–Fri 09:30–16:00 ET
• Skips NYSE holidays automatically (uses pandas_market_calendars)
• Every TICK_SECONDS, runs the agent ensemble and logs results
• Twice daily (10:00 AM and 3:30 PM ET), runs the evaluation + rotation cycle
• On SIGTERM/SIGINT, shuts down cleanly

Deploy on Google Cloud VM (see CLOUD_SETUP.md):
  python market_scheduler.py

Or run as a systemd service so it auto-starts on VM reboot:
  see cloud_setup_guide.md for the unit file
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# ── Optional: pandas_market_calendars for holiday-aware scheduling ──────────
try:
    import pandas_market_calendars as mcal
    _NYSE = mcal.get_calendar("NYSE")
    _CALENDAR_AVAILABLE = True
except ImportError:
    _CALENDAR_AVAILABLE = False

from agent_evaluator import AgentEvaluator
from agent_rotator   import AgentRotator

# ── Config ───────────────────────────────────────────────────────────────────
ET              = ZoneInfo("America/New_York")
MARKET_OPEN     = (9, 30)    # hour, minute ET
MARKET_CLOSE    = (16, 0)    # hour, minute ET
TICK_SECONDS    = 60         # how often the main loop fires
EVAL_TIMES_ET   = [(10, 0), (15, 30)]   # twice-daily evaluation windows
LEARN_TIME_ET   = (15, 45)              # weekly learning run: Fridays at 3:45 PM ET
SUMMARY_TIME_ET = (15, 55)             # daily Slack summary: 3:55 PM ET

SLACK_WEBHOOK   = os.getenv("SLACK_WEBHOOK_URL", "")
SLACK_CHANNEL   = "#trading-alerts"

# ── Logging setup ────────────────────────────────────────────────────────────
LOG_FILE = os.path.join(os.path.dirname(__file__), "logs", "scheduler.log")
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("scheduler")

# ── Graceful shutdown ────────────────────────────────────────────────────────
_running = True

def _handle_shutdown(signum, frame):
    global _running
    log.info("Shutdown signal received — finishing current tick then stopping.")
    _running = False

signal.signal(signal.SIGTERM, _handle_shutdown)
signal.signal(signal.SIGINT,  _handle_shutdown)


# ── Market hours helpers ──────────────────────────────────────────────────────

def is_market_open(now: datetime) -> bool:
    """True if 'now' (timezone-aware) is within NYSE trading hours."""
    if now.weekday() >= 5:   # Saturday=5, Sunday=6
        return False

    # Holiday check (requires pandas_market_calendars)
    if _CALENDAR_AVAILABLE:
        date_str = now.strftime("%Y-%m-%d")
        schedule = _NYSE.schedule(start_date=date_str, end_date=date_str)
        if schedule.empty:
            return False   # holiday

    market_open  = now.replace(hour=MARKET_OPEN[0],  minute=MARKET_OPEN[1],  second=0, microsecond=0)
    market_close = now.replace(hour=MARKET_CLOSE[0], minute=MARKET_CLOSE[1], second=0, microsecond=0)

    return market_open <= now < market_close


def is_eval_time(now: datetime) -> bool:
    """True if 'now' matches one of the twice-daily evaluation windows (within 1 minute)."""
    for hour, minute in EVAL_TIMES_ET:
        window_start = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        window_end   = now.replace(hour=hour, minute=minute + 1, second=0, microsecond=0)
        if window_start <= now < window_end:
            return True
    return False


# ── Agent ensemble (Phase C — wired) ─────────────────────────────────────────

def run_agent_tick():
    """Execute one full ensemble cycle: signals → risk bridge → paper/live orders."""
    now = datetime.now(ET)
    log.debug(f"Agent tick at {now.strftime('%H:%M:%S ET')}")
    # Protection is independent of signal generation. If the ensemble
    # throws, trailing stops still have to be on.
    try:
        from order_executor import ensure_protective_exits
        ensure_protective_exits()
    except Exception as pe:
        log.error(f"Exit backstop failed: {pe}")
    try:
        # Also refresh open positions in the ledger every 5 minutes
        if now.minute % 5 == 0:
            try:
                import trade_ledger as _ledger
                result = _ledger.refresh_open_positions()
                log.debug(f"Ledger refresh: {result}")
            except Exception as le:
                log.debug(f"Ledger refresh failed: {le}")
        else:
            try:
                import trade_ledger as _ledger
                _ledger.close_ghosts()
            except Exception:
                pass

        from ensemble import run_ensemble
        approved = run_ensemble()
        if approved:
            log.info(f"Tick produced {len(approved)} approved signal(s)")
    except Exception as e:
        log.error(f"Ensemble tick failed: {e}", exc_info=True)


def post_daily_slack_summary():
    """Post end-of-day summary to Slack #trading-alerts at 3:55 PM ET."""
    if not SLACK_WEBHOOK:
        log.debug("SLACK_WEBHOOK_URL not set — skipping daily summary")
        return
    try:
        import json, urllib.request, urllib.error

        today_str = datetime.now(ET).strftime("%Y-%m-%d")

        # ── Pull real trade data from ledger ──────────────────────────────
        realized = unrealized = 0.0
        total_trades = wins = losses = open_count = 0
        best_trade = worst_trade = None
        try:
            import trade_ledger as _ledger
            all_t = _ledger.all_trades()
            today_t = [t for t in all_t if t.opened_at_et.startswith(today_str)]
            closed_t = [t for t in today_t if not t.is_open]
            open_t   = [t for t in today_t if t.is_open]
            realized   = sum(t.realized_pnl or 0 for t in closed_t)
            unrealized = sum(t.unrealized_pnl or 0 for t in open_t)
            total_trades = len(today_t)
            wins   = sum(1 for t in closed_t if (t.realized_pnl or 0) > 0)
            losses = sum(1 for t in closed_t if (t.realized_pnl or 0) <= 0)
            open_count = len(open_t)

            if closed_t:
                best_trade  = max(closed_t, key=lambda t: t.realized_pnl or 0)
                worst_trade = min(closed_t, key=lambda t: t.realized_pnl or 0)
        except Exception:
            pass

        # ── Error count from log ──────────────────────────────────────────
        errors_today = 0
        log_path = os.path.join(os.path.dirname(__file__), "logs", "scheduler.log")
        try:
            with open(log_path) as f:
                for line in f:
                    if today_str in line and "[ERROR]" in line:
                        errors_today += 1
        except Exception:
            pass

        try:
            from alpaca_stream import is_streaming
            stream_status = "Alpaca stream ✅" if is_streaming() else "yfinance fallback"
        except Exception:
            stream_status = "unknown"

        pnl_total = realized + unrealized
        pnl_sign  = "+" if pnl_total >= 0 else ""
        pnl_emoji = "📈" if pnl_total >= 0 else "📉"
        status_icon = "✅" if errors_today < 10 else "⚠️"

        lines = [
            f"{status_icon} *BluSterling Daily Summary — PAPER — {datetime.now(ET).strftime('%a %b %d, %Y')}*",
            f"",
            f"_Paper trading only. These are not live fills._",
            f"",
            f"{pnl_emoji} *Total P&L: {pnl_sign}${pnl_total:,.2f}*  _(realized: {'+' if realized>=0 else ''}${realized:,.2f} | open: {'+' if unrealized>=0 else ''}${unrealized:,.2f})_",
            f"• Trades today: *{total_trades}*  ({wins}W / {losses}L closed, {open_count} still open)",
        ]

        if best_trade and (best_trade.realized_pnl or 0) > 0:
            lines.append(f"🏆 Best: *{best_trade.symbol}* {best_trade.side}  +${best_trade.realized_pnl:,.2f}")
        if worst_trade and (worst_trade.realized_pnl or 0) < 0:
            lines.append(f"📉 Worst: *{worst_trade.symbol}* {worst_trade.side}  ${worst_trade.realized_pnl:,.2f}")

        lines += [
            f"",
            f"• Data: {stream_status}  |  Errors: {errors_today}",
            f"_Next run: Mon–Fri 9:30 AM ET_",
        ]

        text = "\n".join(lines)

        payload = json.dumps({"text": text}).encode()
        req = urllib.request.Request(
            SLACK_WEBHOOK,
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=10)
        log.info("Daily Slack summary posted.")
    except Exception as e:
        log.warning(f"Slack summary failed: {e}")


def send_daily_email():
    """Send end-of-day recap email via Gmail SMTP."""
    try:
        import smtplib, json
        from email.mime.multipart import MIMEMultipart
        from email.mime.text import MIMEText
        gmail   = os.getenv("GMAIL_ADDRESS", "")
        pw      = os.getenv("GMAIL_APP_PASSWORD", "")
        to      = os.getenv("REPORT_TO_EMAIL", gmail)
        if not gmail or not pw:
            log.debug("Gmail creds not set — skipping email")
            return
        import trade_ledger as _ledger
        trades   = _ledger.all_trades()
        today    = datetime.now(ET).strftime("%Y-%m-%d")
        today_t  = [t for t in trades if t.opened_at_et.startswith(today)]
        open_t   = [t for t in today_t if t.is_open]
        closed_t = [t for t in today_t if not t.is_open]
        realized = sum(t.realized_pnl or 0 for t in closed_t)
        unreal   = sum(t.unrealized_pnl or 0 for t in open_t)
        rows = "".join(
            f"<tr><td>{t.symbol}</td><td>{t.side}</td>"
            f"<td>${t.entry_price:.2f}</td><td>{t.status}</td>"
            f"<td style='color:{'green' if (t.realized_pnl or t.unrealized_pnl or 0)>=0 else 'red'}'>"
            f"${(t.realized_pnl or t.unrealized_pnl or 0):+.2f}</td></tr>"
            for t in today_t
        )
        html = f"""<html><body>
        <h2>BluSterling Trading Bot — {datetime.now(ET).strftime('%b %d, %Y')}</h2>
        <p><b>Realized P&L:</b> <span style="color:{'green' if realized>=0 else 'red'}">${realized:+.2f}</span> &nbsp;
           <b>Unrealized:</b> ${unreal:+.2f} &nbsp;
           <b>Trades today:</b> {len(today_t)}</p>
        <table border="1" cellpadding="4" style="border-collapse:collapse">
        <tr><th>Symbol</th><th>Side</th><th>Entry</th><th>Status</th><th>P&L</th></tr>
        {rows if rows else '<tr><td colspan=5>No trades today</td></tr>'}
        </table>
        <p style="color:gray;font-size:12px">BluSterling & Associates LLC — paper trading</p>
        </body></html>"""
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"Trading Bot — {datetime.now(ET).strftime('%b %d')} | P&L ${realized:+.2f}"
        msg["From"]    = gmail
        msg["To"]      = to
        msg.attach(MIMEText(html, "html"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(gmail, pw)
            s.sendmail(gmail, to, msg.as_string())
        log.info(f"Daily recap email sent to {to}")
    except Exception as e:
        log.warning(f"Email send failed: {e}")


def seconds_until_next_minute(now: datetime) -> float:
    """Seconds until the next minute boundary (wall clock)."""
    nxt = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
    return max(0.0, (nxt - now).total_seconds())


def tick_spilled_into_new_minute(last_tick_minute: int, now: datetime) -> bool:
    """True when work that started on last_tick_minute has crossed into another.

    A cycle a few seconds over 60s used to sleep another full TICK_SECONDS
    and skip the minute it had already entered. The loop should run that
    minute immediately instead of sleeping past it.
    """
    if last_tick_minute < 0:
        return False
    return (now.hour * 60 + now.minute) != last_tick_minute


def _order_side_name(order) -> str:
    return str(getattr(order, "side", "") or "").lower().split(".")[-1]


def _load_open_orders(client) -> list:
    try:
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest
        req = GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=500)
    except ImportError:
        req = None
    try:
        if req is None:
            return list(client.get_orders())
        return list(client.get_orders(req))
    except Exception as e:
        log.warning(f"Orphan reconcile: open orders unreadable ({e})")
        return []


def _exit_side_token(signed: float):
    try:
        from alpaca.trading.enums import OrderSide
        return OrderSide.SELL if signed > 0 else OrderSide.BUY
    except ImportError:
        return "sell" if signed > 0 else "buy"


def _closing_orders(orders, signed_qty: float) -> list:
    out = []
    for o in orders:
        side = _order_side_name(o)
        if signed_qty > 0 and side == "sell":
            out.append(o)
        elif signed_qty < 0 and side == "buy":
            out.append(o)
    return out


def reconcile_orphan_positions(client, ledger_open: set[str]) -> list[dict]:
    """Close losing broker positions the ledger already exited.

    Winning orphans are kept. Their open exit orders are not cancelled
    before that decision — cancelling first is what left kept names with
    no stop while the log said the trailing stop was still active.

    A kept orphan that is not fully covered gets a protective exit
    re-placed: an equity trailing stop sized to the broker qty, or an
    options stop. If the broker rejects the options stop, the log says
    so. It does not claim a trailing stop is active.
    """
    from invariants import is_option_symbol, position_is_protected

    actions: list[dict] = []
    try:
        positions = list(client.get_all_positions())
    except Exception as e:
        log.warning(f"Orphan reconcile failed: {e}")
        return [{"action": "error", "message": str(e)}]

    open_orders = _load_open_orders(client)
    by_sym: dict[str, list] = {}
    for o in open_orders:
        by_sym.setdefault(str(getattr(o, "symbol", "")), []).append(o)

    for p in positions:
        sym = str(p.symbol)
        key = sym.replace("/", "")
        if key in ledger_open or sym in ledger_open:
            continue
        try:
            signed = float(p.qty)
            upl = float(p.unrealized_pl)
        except (TypeError, ValueError):
            continue
        sym_orders = by_sym.get(sym, [])
        if upl > 0:
            action = _keep_winning_orphan(
                client, p, sym, signed, upl, sym_orders, position_is_protected,
                is_option_symbol,
            )
        else:
            action = _close_losing_orphan(client, sym, sym_orders)
        actions.append(action)
        (log.warning if action.get("level") == "warning" else log.info)(action["message"])
    return actions


def _keep_winning_orphan(client, position, sym, signed, upl, sym_orders,
                         position_is_protected, is_option_symbol) -> dict:
    closing = _closing_orders(sym_orders, signed)
    if position_is_protected(signed, closing):
        trails = [
            o for o in closing
            if "trail" in str(
                getattr(o, "order_type", "") or getattr(o, "type", "")
            ).lower()
        ]
        kind = "trailing stop" if trails else "protective exit"
        msg = (f"Reconcile: KEEPING winning orphan {sym} "
               f"(+${upl:.0f}) — {kind} still active")
        return {"symbol": sym, "action": "kept", "protected": True,
                "replaced": False, "message": msg}

    # An undersized exit holds shares and blocks a full-size replacement.
    # Cancel only those closing orders, and only because they do not cover.
    # Equity replacement restores the old exit if the full-size trail is rejected.
    if is_option_symbol(sym):
        _cancel_closing(client, sym, closing)
        return _reprotect_option(client, position, sym, upl)
    return _reprotect_equity(client, position, sym, signed, upl, closing)


def _cancel_closing(client, sym, closing) -> float:
    old_qty = 0.0
    for o in closing:
        try:
            q = getattr(o, "qty", None)
            if q is not None:
                old_qty += abs(float(q))
        except (TypeError, ValueError):
            pass
        try:
            client.cancel_order_by_id(o.id)
        except Exception as e:
            log.warning(f"Reconcile: {sym} cancel undersized exit failed: {e}")
    return old_qty


def _reprotect_equity(client, position, sym, signed, upl, closing=None) -> dict:
    from order_executor import (
        DEFAULT_TRAIL_PCT, _order_qty, _submit_trail_with_retry, _trail_for_profit,
    )
    qty = _order_qty(signed)
    try:
        live = client.get_open_position(sym)
        qty = _order_qty(live.qty) or qty
        signed = float(live.qty)
    except Exception:
        pass
    plpc = 0.0
    try:
        plpc = abs(float(getattr(position, "unrealized_plpc", 0) or 0)) * 100
    except (TypeError, ValueError):
        plpc = 0.0
    trail_pct = _trail_for_profit(plpc) if plpc >= 4 else DEFAULT_TRAIL_PCT
    old_qty = _cancel_closing(client, sym, closing or [])
    side = _exit_side_token(signed)
    order, err = _submit_trail_with_retry(client, sym, qty, side, trail_pct)
    if order is None and old_qty > 0:
        restored, restore_err = _submit_trail_with_retry(
            client, sym, _order_qty(old_qty), side, trail_pct)
        if restored is not None:
            msg = (f"Reconcile: KEEPING winning orphan {sym} (+${upl:.0f}) — "
                   f"full-size trail rejected ({err}); restored {old_qty:g}-share "
                   f"exit, position still under-covered")
            return {"symbol": sym, "action": "kept", "protected": False,
                    "replaced": False, "level": "warning", "message": msg}
        err = restore_err or err
    if order is None:
        msg = (f"Reconcile: KEEPING winning orphan {sym} (+${upl:.0f}) — "
               f"NO protective exit; trail rejected ({err})")
        return {"symbol": sym, "action": "kept", "protected": False,
                "replaced": False, "level": "warning", "message": msg}
    msg = (f"Reconcile: KEEPING winning orphan {sym} (+${upl:.0f}) — "
           f"re-placed trailing stop {trail_pct}% on {qty}")
    return {"symbol": sym, "action": "kept", "protected": True,
            "replaced": True, "message": msg}


def _reprotect_option(client, position, sym, upl) -> dict:
    from options_executor import submit_option_protective_stop
    result = submit_option_protective_stop(client, position)
    if result.get("placed"):
        msg = (f"Reconcile: KEEPING winning option orphan {sym} (+${upl:.0f}) — "
               f"re-placed options stop ${result.get('stop_price')} "
               f"on {result.get('qty')} contract(s)")
        return {"symbol": sym, "action": "kept", "protected": True,
                "replaced": True, "message": msg}
    msg = (f"Reconcile: KEEPING winning option orphan {sym} (+${upl:.0f}) — "
           f"NO protective exit; broker refused an options stop "
           f"({result.get('error')}). manage_options_exits is the only backstop")
    return {"symbol": sym, "action": "kept", "protected": False,
            "replaced": False, "level": "warning", "message": msg}


def _close_losing_orphan(client, sym, sym_orders) -> dict:
    for o in sym_orders:
        try:
            client.cancel_order_by_id(o.id)
        except Exception:
            pass
    client.close_position(sym)
    msg = (f"Reconcile: closed orphan broker position {sym} "
           f"(ledger already exited it)")
    return {"symbol": sym, "action": "closed", "message": msg}


def sync_alpaca_positions():
    """
    Pull closed orders from Alpaca and update trade_ledger with realized P&L.
    Runs at market close so the strategy learner has real data each Friday.
    """
    try:
        from alpaca.trading.client import TradingClient
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest
        import trade_ledger as _ledger

        key    = os.getenv("ALPACA_API_KEY", "")
        secret = os.getenv("ALPACA_API_SECRET", "")
        if not key or not secret:
            return
        client = TradingClient(api_key=key, secret_key=secret, paper=True)
        req    = GetOrdersRequest(status=QueryOrderStatus.CLOSED, limit=50)
        orders = client.get_orders(req)
        trades = _ledger.load_ledger()
        updated = 0
        for order in orders:
            sym  = str(order.symbol)
            side = "LONG" if str(order.side) == "buy" else "SHORT"
            for tid, t in trades.items():
                if t.symbol == sym and t.side == side and t.is_open:
                    filled = float(order.filled_avg_price or 0)
                    if filled > 0:
                        pnl = (filled - t.entry_price) * t.shares
                        if side == "SHORT":
                            pnl = -pnl
                        t.status          = "target" if pnl >= 0 else "stop"
                        t.exit_price      = filled
                        t.exit_at_et      = datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S")
                        t.realized_pnl    = round(pnl, 2)
                        t.unrealized_pnl  = 0.0
                        updated += 1
                        break
        if updated:
            _ledger.save_ledger(trades)
            log.info(f"Alpaca sync: updated {updated} closed position(s) in ledger")

        # ── Auto-reconcile drift: close broker positions the ledger has
        # already booked as exited. The ledger's simulated exits and the
        # broker's trailing-stop exits are different engines; when the
        # simulation closes first, the real position lingers and consumes
        # buying power invisibly (this froze the account on 2026-07-08).
        #
        # Only liquidate LOSERS. A profitable orphan means the ledger's
        # price simulation closed early while the broker's exit is still
        # riding a winner — killing those forfeits the runners the
        # asymmetry design exists to capture (2026-07-27: GPC +$1,060,
        # NFLX +$877). Exit orders are cancelled only on the liquidate
        # path. A kept orphan keeps, or is given, a real protective exit.
        try:
            ledger_open = {t.symbol.replace("/", "") for t in _ledger.open_positions()}
            reconcile_orphan_positions(client, ledger_open)
        except Exception as e:
            log.warning(f"Orphan reconcile failed: {e}")
    except Exception as e:
        log.warning(f"Alpaca position sync failed: {e}")


def run_learning_cycle():
    """Run the strategy learner — Fridays at 3:45 PM ET."""
    log.info("=" * 60)
    log.info("Running weekly strategy learning cycle...")
    try:
        from strategy_learner import StrategyLearner
        learner = StrategyLearner()
        result  = learner.learn()
        log.info(
            f"Learning complete: win_rate={result.get('overall_win_rate', 'N/A')} "
            f"pnl=${result.get('overall_pnl', 0):+.2f}"
        )
    except Exception as e:
        log.error(f"Strategy learning error: {e}", exc_info=True)
    log.info("=" * 60)


# ── Evaluation cycle ──────────────────────────────────────────────────────────

_eval_done_this_window: set[str] = set()

def run_eval_cycle():
    """Run evaluation + rotation. Called twice per market day."""
    log.info("═" * 60)
    log.info("Starting evaluation and rotation cycle...")
    try:
        evaluator = AgentEvaluator()
        report    = evaluator.evaluate()
        evaluator.save_report(report)
        log.info("\n" + report.summary_text())
        # Same eval windows as today (10:00 and 15:30 ET). Records the
        # size-tilt qualifier list once per date; does not move the clock.
        try:
            from size_tilt import ensure_today
            ensure_today(report)
        except Exception as e:
            log.warning(f"size tilt qualifier record failed: {e}")

        if report.flagged_agents:
            log.info(f"Flagged agents detected: {report.flagged_agents} — running rotation...")
            rotator = AgentRotator()
            result  = rotator.run_rotation()
            log.info(f"Rotation actions: {result['actions']}")
        else:
            log.info("All agents within performance threshold — no rotation needed.")
    except Exception as e:
        log.error(f"Evaluation cycle error: {e}", exc_info=True)
    log.info("═" * 60)


# ── Main loop ─────────────────────────────────────────────────────────────────

def main():
    log.info("╔══════════════════════════════════════════════════╗")
    log.info("║     Trading Bot Market Scheduler — Starting      ║")
    log.info("╚══════════════════════════════════════════════════╝")
    log.info(f"Market hours: {MARKET_OPEN[0]:02d}:{MARKET_OPEN[1]:02d} – "
             f"{MARKET_CLOSE[0]:02d}:{MARKET_CLOSE[1]:02d} ET  |  Tick: {TICK_SECONDS}s")

    global _running
    last_tick_minute = -1

    while _running:
        now = datetime.now(ET)

        if is_market_open(now):
            # ── Agent tick (once per minute) ──────────────────────────────
            current_minute = now.hour * 60 + now.minute
            if current_minute != last_tick_minute:
                last_tick_minute = current_minute
                run_agent_tick()

            # ── Twice-daily evaluation ────────────────────────────────────
            window_key = f"{now.date()}_{now.hour}_{now.minute}"
            if is_eval_time(now) and window_key not in _eval_done_this_window:
                _eval_done_this_window.add(window_key)
                run_eval_cycle()

            # ── Weekly learning run (Fridays only at 3:45 PM ET) ─────────
            learn_key = f"learn_{now.date()}"
            if (now.weekday() == 4  # Friday
                    and now.hour == LEARN_TIME_ET[0]
                    and now.minute == LEARN_TIME_ET[1]
                    and learn_key not in _eval_done_this_window):
                _eval_done_this_window.add(learn_key)
                run_learning_cycle()

            # ── Daily Slack summary at 3:55 PM ET ────────────────────────
            # Email is deliberately NOT sent here — daily_reporter.py's cron
            # job at 4:35 PM sends the one comprehensive daily email. Sending
            # both meant two emails a day covering overlapping info, which
            # was the actual cause of notification overload (not a missing
            # setting). One Slack ping + one email per day, that's it.
            summary_key = f"summary_{now.date()}"
            if (now.hour == SUMMARY_TIME_ET[0]
                    and now.minute == SUMMARY_TIME_ET[1]
                    and summary_key not in _eval_done_this_window):
                _eval_done_this_window.add(summary_key)
                post_daily_slack_summary()
                sync_alpaca_positions()

        else:
            # Outside market hours — sleep longer to conserve resources
            now_str = now.strftime("%a %Y-%m-%d %H:%M ET")
            if now.second < TICK_SECONDS:  # log once per tick period
                log.debug(f"Market closed ({now_str}) — waiting...")
            time.sleep(TICK_SECONDS * 5)
            continue

        # Align to the next minute. If this pass already spilled into a
        # new minute, run that tick now instead of sleeping a full
        # TICK_SECONDS and skipping it.
        now_after = datetime.now(ET)
        if tick_spilled_into_new_minute(last_tick_minute, now_after):
            continue
        delay = seconds_until_next_minute(now_after)
        time.sleep(delay if delay > 0 else 0.05)

    log.info("Scheduler stopped cleanly.")


if __name__ == "__main__":
    main()
