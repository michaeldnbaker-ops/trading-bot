"""
risk_caps.py
────────────
Shared per-trade hard caps. Paper sizing, the order path, options premium,
and the research harnesses all call these so a knob changed in one place
cannot silently disagree with the others.

RISK_PER_TRADE (default $320) is dollar risk to the stop, not notional.
MAX_NOTIONAL_USD (default $1,500) is the absolute share-notional cap.
One share that would break either cap is rejected. Callers must not
replace a 0 result with 1.
"""

from __future__ import annotations

import os
from datetime import date, datetime
from typing import Optional


def risk_per_trade_usd() -> float:
    """Dollar risk to the stop. Env override, default $320."""
    try:
        return float(os.getenv("RISK_PER_TRADE", "320"))
    except (TypeError, ValueError):
        return 320.0


def max_notional_usd() -> float:
    try:
        return float(os.getenv("MAX_NOTIONAL_USD", "1500"))
    except (TypeError, ValueError):
        return 1500.0


def dynamic_risk_shares(
    entry: float,
    stop: float,
    *,
    notional_cap: float,
    risk_cap: float,
) -> int:
    """Whole shares inside both caps. Floors. Never rounds up to 1.

    ``shares * |entry - stop| <= risk_cap`` and ``shares * entry <= notional_cap``.
    Returns 0 when a single share would exceed either limit — that is the
    MSTR-style bypass where ``max(1, int(notional / price))`` bought one
    share priced above the notional cap.
    """
    try:
        entry_f = float(entry)
        stop_f = float(stop)
        notional = float(notional_cap)
        risk = float(risk_cap)
    except (TypeError, ValueError):
        return 0
    if entry_f <= 0 or notional <= 0 or risk <= 0:
        return 0
    stop_dist = abs(entry_f - stop_f)
    if stop_dist <= 0:
        return 0
    by_risk = int(risk / stop_dist)
    by_notional = int(notional / entry_f)
    shares = min(by_risk, by_notional)
    if shares < 1:
        return 0
    # int() already floors; this only catches a float that landed 1 over.
    if shares * stop_dist > risk + 1e-6:
        shares -= 1
    if shares * entry_f > notional + 1e-6:
        shares -= 1
    return shares if shares >= 1 else 0


def option_contracts(ask: float, risk_cap: float) -> int:
    """Contracts whose premium (the max loss) fits in the dollar risk cap.

    One contract that costs more than the cap is 0, not 1.
    """
    try:
        ask_f = float(ask)
        risk = float(risk_cap)
    except (TypeError, ValueError):
        return 0
    per = ask_f * 100.0
    if ask_f <= 0 or risk <= 0 or per <= 0:
        return 0
    qty = int(risk / per)
    if qty < 1:
        return 0
    if qty * per > risk + 1e-6:
        qty -= 1
    return qty if qty >= 1 else 0


def _sym(symbol) -> str:
    return str(symbol or "").replace("/", "").upper()


def _field(obj, name, default=None):
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _option_on_underlying(opt_symbol: str, underlying: str) -> bool:
    """True for an OCC symbol on this equity (AMD250117C00... vs AMD).

    The character after the ticker must be a digit so 'A' does not match
    every AAPL contract.
    """
    opt = _sym(opt_symbol)
    und = _sym(underlying)
    if len(und) < 1 or len(opt) <= len(und):
        return False
    if not opt.startswith(und):
        return False
    return opt[len(und)].isdigit()


def entry_blocked(symbol: str, positions, orders) -> Optional[str]:
    """Why a new entry must not be sent, or None if the symbol is flat.

    Blocks an existing share position, an option on the same underlying,
    and any open order on the symbol. Does not inspect order side: a
    resting order is enough to refuse another entry.
    """
    want = _sym(symbol)
    if not want:
        return "missing symbol"
    for p in positions or []:
        sym = _sym(_field(p, "symbol", ""))
        try:
            qty = float(_field(p, "qty", 0) or 0)
        except (TypeError, ValueError):
            qty = 0.0
        if qty == 0 or not sym:
            continue
        if sym == want or _option_on_underlying(sym, want):
            return f"broker already holds {sym}"
    for o in orders or []:
        sym = _sym(_field(o, "symbol", ""))
        if sym == want or _option_on_underlying(sym, want):
            return f"open order already working on {sym}"
    return None


def _as_date(ts) -> Optional[date]:
    if ts is None:
        return None
    if isinstance(ts, datetime):
        return ts.date()
    if isinstance(ts, date):
        return ts
    if hasattr(ts, "date") and callable(ts.date):
        try:
            d = ts.date()
            if isinstance(d, date):
                return d
        except Exception:
            return None
    text = str(ts)[:10]
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        return None


def completed_bar_value(last_value: float, prev_value: Optional[float],
                        last_ts, now: datetime) -> float:
    """ATR (or any daily series) that does not include today's unfinished bar.

    During the cash session the last daily bar contains today's high and
    low, which are not known at the open and still moving. Use the prior
    completed bar until 16:00 America/New_York. After the close, today's
    bar is a real session and is kept. A missing or NaN prior value falls
    back to the last value.
    """
    last = float(last_value)
    if prev_value is None:
        return last
    try:
        prev = float(prev_value)
    except (TypeError, ValueError):
        return last
    if prev != prev:  # NaN
        return last
    bar_day = _as_date(last_ts)
    if bar_day is None or now is None:
        return last
    # Today's daily bar is still forming until the 16:00 ET cash close.
    if bar_day == now.date() and (now.hour, now.minute) < (16, 0):
        return prev
    return last
