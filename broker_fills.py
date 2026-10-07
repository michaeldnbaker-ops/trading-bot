"""
broker_fills.py — L-2026-10-01b
────────────────────────────────
Rebuild per-trade realized P&L from Alpaca FILL activities.

The ledger is attribution (which agents signed the ticket). The broker
fill is the dollar. A round trip is a FIFO match of buys and sells on one
symbol, from flat back toward flat:

  LONG   (buy then sell)   (exit - entry) * qty * multiplier
  SHORT  (sell then buy)   (entry - exit) * qty * multiplier
  option multiplier = 100  (premium is per share; one contract is 100)

Co-signed tickets split that P&L, and the one round-trip paper friction
cost, 1/N across leaf agents. N is the number of unique leaves on the
row (MetaAgent(A, B) → A and B). The trade count is not split: each
leaf participated in the round trip.

When no closed fill matches a ledger row, scoring uses ledger
realized_pnl and logs the trade id. That is the only fallback.

A ledger row that carries entry_order_id and exit_order_id is priced
from those two orders' average fills. FIFO is only the fallback for
rows that do not have both ids, and it does not reuse fills already
claimed by an order-id match. Full history is required: a short window
drops July fills, and FIFO on that history pairs a later sell with an
older lot the broker still holds.

This module does not submit orders and does not write the ledger.
`fetch_round_trips` is read-only (activities, closed orders, positions).
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

log = logging.getLogger("BrokerFills")

OPTION_MULTIPLIER = 100.0
# Opening fill more than this far from the ledger open is a different trade.
MATCH_WINDOW = timedelta(days=10)


@dataclass
class RoundTrip:
    symbol: str
    side: str                 # LONG | SHORT (the opening side)
    qty: float
    entry_price: float
    exit_price: float
    entry_time: str
    exit_time: str
    realized_pnl: float
    order_ids: tuple[str, ...] = ()


@dataclass
class ScoreSlice:
    agent: str
    pnl: float
    cost: float
    trade_pnl: float
    source: str               # "broker" | "ledger"


# Learning Loop 2026-10-05. SPY 11 shares, trade 88374c40e400, was marked
# closed/target at $753.30 while the broker still held that lot. The
# ledger booked a false +$258.57 ((753.30 - ~729.79) * 11). The row stays
# in the ledger. Scoring ignores this id even after the live lot is gone,
# and ignores any later closed row whose symbol, side, and qty (and entry,
# when both sides have one) still match an open ledger lot or an open
# broker position. Reconcile flags the live disagreement; it does not
# delete history.
PHANTOM_EXCLUDED_TRADE_IDS = frozenset({"88374c40e400"})
PHANTOM_QTY_REL = 0.02
PHANTOM_ENTRY_REL = 0.002
PHANTOM_ENTRY_ABS = 0.05


def _num(value) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _qty_close(a, b) -> bool:
    left, right = abs(float(a or 0)), abs(float(b or 0))
    if left <= 0 or right <= 0:
        return False
    return abs(left - right) <= max(0.01, PHANTOM_QTY_REL * max(left, right))


def _entry_close(a: Optional[float], b: Optional[float]) -> bool:
    """True when entries agree, or when either side has no entry to compare."""
    if a is None or b is None or a <= 0 or b <= 0:
        return True
    return abs(a - b) <= max(PHANTOM_ENTRY_ABS, PHANTOM_ENTRY_REL * max(abs(a), abs(b)))


def _broker_lot(position) -> Optional[tuple]:
    if isinstance(position, dict):
        sym = position.get("symbol")
        qty = position.get("qty")
        entry = position.get("avg_entry_price", position.get("entry"))
    else:
        sym = getattr(position, "symbol", None)
        qty = getattr(position, "qty", None)
        entry = getattr(position, "avg_entry_price", None)
    size = _num(qty)
    if not sym or size is None or size == 0:
        return None
    side = "LONG" if size > 0 else "SHORT"
    ent = _num(entry)
    if ent is not None and ent <= 0:
        ent = None
    return str(sym), side, abs(size), ent


def same_open_lot(trade, symbol, side, qty, entry) -> bool:
    """Closed row vs one live lot: symbol, side, qty, and entry when known."""
    if norm_symbol(getattr(trade, "symbol", "")) != norm_symbol(symbol):
        return False
    if position_side(getattr(trade, "side", "")) != position_side(side):
        return False
    if not _qty_close(getattr(trade, "shares", 0), qty):
        return False
    return _entry_close(_num(getattr(trade, "entry_price", None)), _num(entry))


def _live_lot_matches(trade, trades, broker_positions=None) -> list[str]:
    kinds: list[str] = []
    for other in trades or []:
        if other is trade or not getattr(other, "is_open", False):
            continue
        if same_open_lot(
            trade,
            getattr(other, "symbol", ""),
            getattr(other, "side", ""),
            getattr(other, "shares", 0),
            getattr(other, "entry_price", None),
        ):
            kinds.append("open ledger lot")
            break
    for position in broker_positions or []:
        lot = _broker_lot(position)
        if lot is None:
            continue
        sym, side, qty, entry = lot
        if same_open_lot(trade, sym, side, qty, entry):
            kinds.append("open broker position")
            break
    return kinds


def closed_lot_still_open_reason(trade, trades, broker_positions=None) -> Optional[str]:
    """Durable rule: this closed row still describes a lot that is open.

    None when the row is open, or when no live lot matches. Used by
    reconcile to FLAG the disagreement. Does not by itself name the
    2026-10-05 id — that id is a scoring exclusion even after the lot
    is actually closed.
    """
    if getattr(trade, "is_open", False):
        return None
    kinds = _live_lot_matches(trade, trades, broker_positions)
    if not kinds:
        return None
    tid = str(getattr(trade, "trade_id", "") or "")
    where = " and ".join(kinds)
    return (
        f"phantom closed lot {tid} {getattr(trade, 'symbol', '')} "
        f"x{getattr(trade, 'shares', 0)}: closed row still matches {where}"
    )


def scoring_skip_reason(trade, trades, broker_positions=None) -> Optional[str]:
    """Why rotation/scorecard must ignore this closed row's realized P&L.

    The ledger row is not deleted. ``PHANTOM_EXCLUDED_TRADE_IDS`` covers
    the known false SPY close. The durable rule covers the same shape
    on any later row while the live lot is still open.
    """
    if getattr(trade, "is_open", False):
        return None
    tid = str(getattr(trade, "trade_id", "") or "")
    if tid in PHANTOM_EXCLUDED_TRADE_IDS:
        return (
            f"excluded phantom closed lot {tid} "
            "(Learning Loop 2026-10-05; row kept in the ledger)"
        )
    return closed_lot_still_open_reason(trade, trades, broker_positions)


def split_lot_flags(trades, broker_positions=None) -> list[dict]:
    """One CRITICAL flag per closed row that still matches a live lot.

    ``message`` is the reconcile/alert line. ``detail`` is the invariant
    text (severity is added by the caller). History is not modified.
    """
    flags = []
    seen: set[str] = set()
    for trade in trades or []:
        reason = closed_lot_still_open_reason(trade, trades, broker_positions)
        if not reason:
            continue
        tid = str(getattr(trade, "trade_id", "") or "")
        if tid in seen:
            continue
        seen.add(tid)
        kinds = _live_lot_matches(trade, trades, broker_positions)
        where = " and ".join(kinds) if kinds else "a live lot"
        try:
            qty = float(getattr(trade, "shares", 0) or 0)
        except (TypeError, ValueError):
            qty = 0.0
        pnl = round(float(getattr(trade, "realized_pnl", 0) or 0), 2)
        symbol = str(getattr(trade, "symbol", "") or "")
        detail = (
            f"{symbol} {tid} x{qty:g} booked ${pnl:+.2f} while the same lot "
            f"is still open ({where})"
        )
        flags.append({
            "trade_id": tid,
            "symbol": symbol,
            "qty": qty,
            "realized_pnl": pnl,
            "detail": detail,
            "message": f"CRITICAL: {detail}",
        })
    return flags


@dataclass
class CompareRow:
    trade_id: str
    symbol: str
    side: str
    opened_at: str
    agents: list[str]
    ledger_pnl: float
    broker_pnl: Optional[float]
    qty_ledger: float
    qty_broker: Optional[float]
    entry_ledger: float
    entry_broker: Optional[float]
    exit_ledger: Optional[float]
    exit_broker: Optional[float]
    note: str
    source: str
    slices: list[ScoreSlice] = field(default_factory=list)
    score: bool = True


def network_scoring_enabled() -> bool:
    """Whether evaluate()/snapshot() may call Alpaca.

    auto (default): on in production, off under unittest so the suite
    stays on the ledger fallback unless a test injects round trips.
    BROKER_FILL_SCORING=on/off overrides.
    """
    flag = os.getenv("BROKER_FILL_SCORING", "auto").strip().lower()
    if flag in {"0", "off", "false", "no"}:
        return False
    if flag in {"1", "on", "true", "yes"}:
        return True
    return "unittest" not in sys.modules


def is_option_symbol(symbol: str) -> bool:
    try:
        from invariants import is_option_symbol as _is
        return bool(_is(symbol))
    except Exception:
        return len(str(symbol or "").replace("/", "")) > 12


def contract_multiplier(symbol: str) -> float:
    return OPTION_MULTIPLIER if is_option_symbol(symbol) else 1.0


def norm_symbol(symbol: str) -> str:
    return str(symbol or "").replace("/", "").upper()


def position_side(raw: str) -> str:
    s = str(raw or "").strip().upper()
    if s in {"SHORT", "SELL"}:
        return "SHORT"
    return "LONG"


def _order_side(raw) -> str:
    """Normalize a fill/order side to buy or sell."""
    s = str(raw or "").lower().split(".")[-1]
    if s in {"buy", "long"}:
        return "buy"
    if s in {"sell", "short"}:
        return "sell"
    return ""


def realized_pnl(side: str, entry: float, exit_: float, qty: float, symbol: str) -> float:
    """Round-trip dollars. Shorts profit when price falls. Options ×100."""
    q = abs(float(qty or 0))
    mult = contract_multiplier(symbol)
    if position_side(side) == "SHORT":
        gross = (float(entry) - float(exit_)) * q * mult
    else:
        gross = (float(exit_) - float(entry)) * q * mult
    return round(gross, 2)


def paper_friction(notional: float) -> float:
    """Same 10 bps / $2 floor as trade_ledger.round_trip_cost, on real notional."""
    from trade_ledger import ROUND_TRIP_COST_BPS, ROUND_TRIP_COST_FLOOR_USD
    bps_cost = abs(float(notional or 0)) * float(ROUND_TRIP_COST_BPS) / 10_000.0
    return round(max(float(ROUND_TRIP_COST_FLOOR_USD), bps_cost), 2)


def split_amount(total: float, n: int) -> list[float]:
    """Split dollars 1/N. The last slice absorbs the rounding remainder."""
    if n <= 0:
        return []
    total = round(float(total), 2)
    if n == 1:
        return [total]
    base = round(total / n, 2)
    parts = [base] * (n - 1)
    parts.append(round(total - base * (n - 1), 2))
    return parts


def _get(obj, *names, default=None):
    if isinstance(obj, dict):
        for name in names:
            if name in obj and obj[name] not in (None, ""):
                return obj[name]
        return default
    for name in names:
        value = getattr(obj, name, None)
        if value not in (None, ""):
            return value
    return default


def _as_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_time(raw) -> Optional[datetime]:
    """UTC datetime. Ledger wall-clock strings are America/New_York."""
    if raw is None:
        return None
    if isinstance(raw, datetime):
        dt = raw
    else:
        text = str(raw).strip()
        if not text:
            return None
        if "T" not in text and "+" not in text and not text.endswith("Z"):
            try:
                from trade_ledger import ET
                dt = datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=ET)
            except ValueError:
                return None
        else:
            try:
                dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def normalize_fill(raw) -> Optional[dict]:
    """One execution print. Closed orders collapse to their average fill."""
    symbol = str(_get(raw, "symbol", default="") or "")
    if not symbol:
        return None
    side = _order_side(_get(raw, "side", default=""))
    if not side:
        return None
    qty = _as_float(_get(raw, "qty", "filled_qty", "cum_qty", default=0))
    price = _as_float(_get(raw, "price", "filled_avg_price", default=0))
    if qty <= 0 or price <= 0:
        return None
    when = _get(raw, "transaction_time", "filled_at", "submitted_at", "timestamp", default="")
    if not str(when or ""):
        return None
    order_id = str(_get(raw, "order_id", "id", default="") or "")
    return {
        "symbol": symbol,
        "side": side,
        "qty": qty,
        "price": price,
        "time": str(when),
        "order_id": order_id,
    }


def _normalized_fills(fills: Iterable) -> list[dict]:
    rows = []
    for raw in fills:
        if isinstance(raw, dict) and raw.get("time") and raw.get("side") in {"buy", "sell"}:
            norm = raw
        else:
            norm = normalize_fill(raw)
        if norm:
            rows.append(norm)
    rows.sort(key=lambda f: parse_time(f["time"]) or datetime.min.replace(tzinfo=timezone.utc))
    return rows


def _order_vwap(parts: list[dict]) -> tuple[float, float, str, str]:
    """qty, average price, earliest time, latest time."""
    qty = sum(float(p["qty"]) for p in parts)
    avg = sum(float(p["price"]) * float(p["qty"]) for p in parts) / qty
    earliest = min(parts, key=lambda p: parse_time(p["time"]) or datetime.max.replace(tzinfo=timezone.utc))
    latest = max(parts, key=lambda p: parse_time(p["time"]) or datetime.min.replace(tzinfo=timezone.utc))
    return qty, avg, str(earliest["time"]), str(latest["time"])


def _trips_from_order_ids(rows: list[dict], trades: Iterable) -> tuple[list[RoundTrip], set[int]]:
    """Round trips whose entry and exit orders are on the ledger row.

    P&L is (exit average − entry average) × filled qty, short sign and
    the option multiplier included. Fills used here are not offered to FIFO,
    so a later sell is not paired with an older lot the broker still holds.
    """
    by_order: dict[str, list[int]] = {}
    for i, row in enumerate(rows):
        oid = str(row.get("order_id") or "")
        if oid:
            by_order.setdefault(oid, []).append(i)
    consumed: set[int] = set()
    trips: list[RoundTrip] = []
    used_pairs: set[tuple[str, str]] = set()
    closed = [t for t in trades or [] if not getattr(t, "is_open", False)]
    closed.sort(key=lambda t: getattr(t, "opened_at_et", "") or "")
    for trade in closed:
        eid = str(getattr(trade, "entry_order_id", "") or "")
        xid = str(getattr(trade, "exit_order_id", "") or "")
        if not eid or not xid or eid == xid or (eid, xid) in used_pairs:
            continue
        e_idx = [i for i in by_order.get(eid, []) if i not in consumed]
        x_idx = [i for i in by_order.get(xid, []) if i not in consumed]
        if not e_idx or not x_idx:
            continue
        e_fills = [rows[i] for i in e_idx]
        x_fills = [rows[i] for i in x_idx]
        if norm_symbol(e_fills[0]["symbol"]) != norm_symbol(x_fills[0]["symbol"]):
            continue
        e_qty, e_avg, e_time, _e_last = _order_vwap(e_fills)
        x_qty, x_avg, _x_first, x_time = _order_vwap(x_fills)
        qty = min(e_qty, x_qty)
        if qty <= 0:
            continue
        symbol = str(e_fills[0]["symbol"])
        side = position_side(getattr(trade, "side", ""))
        trips.append(RoundTrip(
            symbol=symbol,
            side=side,
            qty=round(qty, 8),
            entry_price=round(e_avg, 4),
            exit_price=round(x_avg, 4),
            entry_time=e_time,
            exit_time=x_time,
            realized_pnl=realized_pnl(side, e_avg, x_avg, qty, symbol),
            order_ids=(eid, xid),
        ))
        consumed.update(e_idx)
        consumed.update(x_idx)
        used_pairs.add((eid, xid))
    return trips, consumed


def build_round_trips(fills: Iterable, trades: Iterable | None = None) -> list[RoundTrip]:
    """Round trips. Order ids first when `trades` carries them; FIFO after.

    Partial fills on one FIFO entry become separate slices. An order-id
    match is one trip at the two orders' average fills. An opening lot
    that never closes is not a trip (still open at the broker).
    """
    rows = _normalized_fills(fills)
    consumed: set[int] = set()
    oid_trips: list[RoundTrip] = []
    if trades:
        oid_trips, consumed = _trips_from_order_ids(rows, trades)
    remaining = [row for i, row in enumerate(rows) if i not in consumed]

    by_symbol: dict[str, list[dict]] = {}
    for row in remaining:
        by_symbol.setdefault(row["symbol"], []).append(row)

    trips: list[RoundTrip] = list(oid_trips)
    for symbol, prints in by_symbol.items():
        trips.extend(_fifo_symbol(symbol, prints))
    return trips


def _fifo_symbol(symbol: str, fills: list[dict]) -> list[RoundTrip]:
    lots: list[dict] = []
    trips: list[RoundTrip] = []
    for fill in fills:
        qty = float(fill["qty"])
        price = float(fill["price"])
        side = fill["side"]
        if not lots or lots[0]["side"] == side:
            lots.append({
                "qty": qty, "price": price, "time": fill["time"],
                "side": side, "order_id": fill.get("order_id") or "",
            })
            continue
        while qty > 1e-9 and lots and lots[0]["side"] != side:
            lot = lots[0]
            take = min(lot["qty"], qty)
            opening = "LONG" if lot["side"] == "buy" else "SHORT"
            pnl = realized_pnl(opening, lot["price"], price, take, symbol)
            ids = tuple(i for i in (lot.get("order_id"), fill.get("order_id")) if i)
            trips.append(RoundTrip(
                symbol=symbol,
                side=opening,
                qty=round(take, 8),
                entry_price=float(lot["price"]),
                exit_price=price,
                entry_time=str(lot["time"]),
                exit_time=str(fill["time"]),
                realized_pnl=pnl,
                order_ids=ids,
            ))
            lot["qty"] -= take
            qty -= take
            if lot["qty"] <= 1e-9:
                lots.pop(0)
        if qty > 1e-9:
            lots.append({
                "qty": qty, "price": price, "time": fill["time"],
                "side": side, "order_id": fill.get("order_id") or "",
            })
    return trips


def _leaf_names(trade) -> list[str]:
    names = list(getattr(trade, "leaf_agents", None) or [])
    if names:
        return names
    from trade_ledger import leaf_agent_names
    return leaf_agent_names(
        getattr(trade, "primary_agent", ""),
        getattr(trade, "contributors", ""),
    )


def _trip_notional(trip: RoundTrip) -> float:
    return abs(float(trip.qty) * float(trip.entry_price) * contract_multiplier(trip.symbol))


def slices_for_trade(trade, trips: Optional[list[RoundTrip]], *, warn_unmatched: bool) -> list[ScoreSlice]:
    """1/N slices. `trips is None` means the broker book was not loaded.

    An empty list means we looked and nothing matched — log and use the ledger.
    """
    names = _leaf_names(trade)
    if not names:
        return []
    from trade_ledger import round_trip_cost
    ledger_pnl = round(float(getattr(trade, "realized_pnl", 0) or 0), 2)
    if trips:
        pnl = round(sum(t.realized_pnl for t in trips), 2)
        cost = paper_friction(sum(_trip_notional(t) for t in trips))
        source = "broker"
    else:
        pnl = ledger_pnl
        cost = round_trip_cost(trade)
        source = "ledger"
        if warn_unmatched:
            log.warning(
                "broker fill unmatched trade_id=%s symbol=%s side=%s "
                "ledger_realized=%s — scoring ledger",
                getattr(trade, "trade_id", ""),
                getattr(trade, "symbol", ""),
                getattr(trade, "side", ""),
                f"{ledger_pnl:.2f}",
            )
    n = len(names)
    pnls = split_amount(pnl, n)
    costs = split_amount(cost, n)
    return [
        ScoreSlice(agent=name, pnl=p, cost=c, trade_pnl=pnl, source=source)
        for name, p, c in zip(names, pnls, costs)
    ]


_MATCH_COUNTS = {"order_id": 0, "fifo": 0, "unmatched": 0}


def match_method_counts() -> dict[str, int]:
    """How the last assign_round_trips call priced closed rows."""
    return dict(_MATCH_COUNTS)


def assign_round_trips(trades: Iterable, trips: list[RoundTrip]) -> dict[str, list[RoundTrip]]:
    """Order-id match first, then greedy closest-open-time FIFO.

    Each trip is used at most once. Qty stops the FIFO match once the
    ledger size is covered, so a later round trip on the same symbol
    stays available for the next row. Price is not a reject: signal
    price vs fill average is one of the gaps we measure.

    A row with both order ids claims the trip built from those orders
    even when FIFO would have paired the sell with an older open lot.
    """
    global _MATCH_COUNTS
    pools: dict[tuple[str, str], list[int]] = {}
    by_pair: dict[tuple[str, str], list[int]] = {}
    for i, trip in enumerate(trips):
        pools.setdefault((norm_symbol(trip.symbol), trip.side), []).append(i)
        ids = tuple(trip.order_ids)
        if len(ids) >= 2 and ids[0] and ids[1]:
            by_pair.setdefault((str(ids[0]), str(ids[1])), []).append(i)

    used: set[int] = set()
    out: dict[str, list[RoundTrip]] = {}
    closed = [t for t in trades if not getattr(t, "is_open", False)]
    closed.sort(key=lambda t: getattr(t, "opened_at_et", "") or "")
    window = MATCH_WINDOW.total_seconds()
    order_id_n = 0
    fifo_n = 0
    pending = []
    for trade in closed:
        eid = str(getattr(trade, "entry_order_id", "") or "")
        xid = str(getattr(trade, "exit_order_id", "") or "")
        tid = str(getattr(trade, "trade_id", ""))
        if eid and xid:
            hits = [i for i in by_pair.get((eid, xid), []) if i not in used]
            if hits:
                out[tid] = [trips[i] for i in hits]
                used.update(hits)
                order_id_n += 1
                continue
        pending.append(trade)
    for trade in pending:
        key = (norm_symbol(getattr(trade, "symbol", "")), position_side(getattr(trade, "side", "")))
        opened = parse_time(getattr(trade, "opened_at_et", ""))
        candidates = [i for i in pools.get(key, []) if i not in used]

        def _dist(i: int) -> float:
            when = parse_time(trips[i].entry_time)
            if opened is None or when is None:
                return 1e18
            return abs((when - opened).total_seconds())

        candidates.sort(key=_dist)
        need = abs(float(getattr(trade, "shares", 0) or 0))
        chosen: list[RoundTrip] = []
        got = 0.0
        for i in candidates:
            if _dist(i) > window:
                continue
            chosen.append(trips[i])
            used.add(i)
            got += abs(trips[i].qty)
            if need > 0 and got + 1e-6 >= need * 0.98:
                break
            if need <= 0:
                break
        out[str(getattr(trade, "trade_id", ""))] = chosen
        if chosen:
            fifo_n += 1
    unmatched = len(closed) - order_id_n - fifo_n
    _MATCH_COUNTS = {"order_id": order_id_n, "fifo": fifo_n, "unmatched": unmatched}
    return out


def prepare_scoring_book(round_trips: Optional[list[RoundTrip]]):
    """(trips, available). None loads the network when scoring is enabled.

    A caller-supplied list is 'available' even when empty: empty means we
    looked and every closed trade is an unmatched fallback.
    """
    if round_trips is not None:
        return list(round_trips), True
    if not network_scoring_enabled():
        return [], False
    try:
        return fetch_round_trips(), True
    except Exception as e:
        log.warning("broker fill load failed (%s) — scoring from ledger", e)
        return [], False


def compare_ledger(trades: Iterable, trips: list[RoundTrip], *, broker_open: Optional[set[str]] = None,
                   broker_positions=None) -> list[CompareRow]:
    """Per closed ledger row: broker round-trip P&L vs ledger realized."""
    trade_list = list(trades)
    assignment = assign_round_trips(trade_list, trips)
    held = {norm_symbol(s) for s in (broker_open or set())}
    rows: list[CompareRow] = []
    for trade in trade_list:
        if getattr(trade, "is_open", False):
            continue
        matched = assignment.get(str(getattr(trade, "trade_id", "")), [])
        ledger_pnl = round(float(getattr(trade, "realized_pnl", 0) or 0), 2)
        if matched:
            broker_pnl = round(sum(t.realized_pnl for t in matched), 2)
            qty_b = round(sum(t.qty for t in matched), 4)
            entry_b = _vwap(matched, "entry_price")
            exit_b = _vwap(matched, "exit_price")
            source = "broker"
            note = _mismatch_note(trade, matched, ledger_pnl, broker_pnl)
        else:
            broker_pnl = None
            qty_b = entry_b = exit_b = None
            source = "ledger"
            note = "NO BROKER FILL — ledger fallback"
            if norm_symbol(getattr(trade, "symbol", "")) in held:
                note = "LEDGER EXITED EARLY — broker still holds; " + note
        skip = scoring_skip_reason(trade, trade_list, broker_positions)
        score = True
        if skip:
            note = "PHANTOM CLOSED LOT — excluded from scoring; " + note
            score = False
            slices: list[ScoreSlice] = []
        else:
            slices = slices_for_trade(
                trade, matched, warn_unmatched=False,
            )
        rows.append(CompareRow(
            trade_id=str(getattr(trade, "trade_id", "")),
            symbol=str(getattr(trade, "symbol", "")),
            side=position_side(getattr(trade, "side", "")),
            opened_at=str(getattr(trade, "opened_at_et", "") or ""),
            agents=_leaf_names(trade),
            ledger_pnl=ledger_pnl,
            broker_pnl=broker_pnl,
            qty_ledger=float(getattr(trade, "shares", 0) or 0),
            qty_broker=qty_b,
            entry_ledger=float(getattr(trade, "entry_price", 0) or 0),
            entry_broker=entry_b,
            exit_ledger=(
                None if getattr(trade, "exit_price", None) in (None, "")
                else float(trade.exit_price)
            ),
            exit_broker=exit_b,
            note=note,
            source=source,
            slices=slices,
            score=score,
        ))
    return rows


def _vwap(trips: list[RoundTrip], field_name: str) -> float:
    qty = sum(t.qty for t in trips)
    if qty <= 0:
        return 0.0
    return round(sum(getattr(t, field_name) * t.qty for t in trips) / qty, 4)


def _mismatch_note(trade, trips: list[RoundTrip], ledger_pnl: float, broker_pnl: float) -> str:
    if abs(ledger_pnl - broker_pnl) < 1.0:
        return "ok"
    notes: list[str] = []
    if ledger_pnl * broker_pnl < 0 and abs(ledger_pnl) >= 1 and abs(broker_pnl) >= 1:
        notes.append("SIGN")
    if is_option_symbol(getattr(trade, "symbol", "")) and abs(ledger_pnl * OPTION_MULTIPLIER - broker_pnl) <= max(1.0, abs(broker_pnl) * 0.02):
        notes.append("OPTION x100 missing on ledger (premium treated as a share)")
    qty_b = sum(t.qty for t in trips)
    qty_l = abs(float(getattr(trade, "shares", 0) or 0))
    if qty_l > 0 and abs(qty_l - qty_b) / qty_l > 0.05:
        notes.append(f"QTY ledger {qty_l:g} vs fill {qty_b:g}")
    entry_b = _vwap(trips, "entry_price")
    entry_l = float(getattr(trade, "entry_price", 0) or 0)
    if entry_l > 0 and abs(entry_l - entry_b) > max(0.05, entry_l * 0.002):
        notes.append(f"ENTRY ledger {entry_l:.4f} vs fill {entry_b:.4f}")
    exit_l = getattr(trade, "exit_price", None)
    exit_b = _vwap(trips, "exit_price")
    if exit_l not in (None, "") and abs(float(exit_l) - exit_b) > max(0.05, abs(float(exit_l)) * 0.002):
        notes.append(f"EXIT ledger {float(exit_l):.4f} vs fill {exit_b:.4f}")
    if not notes:
        notes.append("P&L differs")
    return "; ".join(notes)


def agent_pnl_deltas(rows: list[CompareRow]) -> dict[str, float]:
    """Broker slice minus the 1/N ledger slice, per leaf.

    Positive: the ledger booked the agent too low (P&L understated).
    Negative: the ledger booked the agent too high.
    Unmatched rows are omitted — there is no broker number to diff.
    """
    totals: dict[str, float] = {}
    for row in rows:
        if not getattr(row, "score", True) or row.broker_pnl is None or not row.agents:
            continue
        ledger_parts = split_amount(row.ledger_pnl, len(row.agents))
        broker_parts = split_amount(row.broker_pnl, len(row.agents))
        for name, led, bro in zip(row.agents, ledger_parts, broker_parts):
            totals[name] = round(totals.get(name, 0.0) + (bro - led), 2)
    return totals


def realized_on_date(trips: Iterable[RoundTrip], day: str) -> float:
    """Full round-trip P&L of trips whose exit falls on `day` in ET.

    Same definition as the ledger's realized-today (lifetime dollars of
    what closed today), not today's mark-to-market slice.
    """
    from trade_ledger import broker_time_to_et
    total = 0.0
    for trip in trips:
        exit_et = broker_time_to_et(trip.exit_time)[:10]
        if exit_et == day:
            total += trip.realized_pnl
    return round(total, 2)


def assert_paper_client(client) -> None:
    """Refuse a TradingClient that was constructed for live trading."""
    paper = getattr(client, "paper", None)
    if paper is None:
        paper = getattr(client, "_paper", None)
    if paper is False:
        raise RuntimeError("refusing non-paper TradingClient")


class _ReadOnly:
    """Block order-mutating calls. The diagnostic only reads."""

    _BLOCKED = ("submit", "cancel", "close", "replace", "delete", "post", "patch")

    def __init__(self, client):
        self._client = client

    def __getattr__(self, name):
        lower = name.lower()
        if lower.startswith(self._BLOCKED):
            raise RuntimeError(f"read-only diagnostic refused {name}")
        return getattr(self._client, name)


def activity_after_date(trades: Iterable | None = None) -> Optional[str]:
    """YYYY-MM-DD, 30 days before the ledger's earliest opened_at.

    That is the first date a row was opened, not merely the oldest position
    still open. A window that starts at the oldest live lot drops the July
    closes the scorer still has to price. The 30 days cover an entry fill
    that printed before the log line.
    """
    if trades is None:
        try:
            import trade_ledger as tl
            trades = tl.all_trades()
        except Exception:
            return None
    opened: list[str] = []
    for trade in trades or []:
        raw = str(getattr(trade, "opened_at_et", "") or "")[:10]
        if len(raw) == 10 and raw[4] == "-" and raw[7] == "-":
            opened.append(raw)
    if not opened:
        return None
    earliest = datetime.strptime(min(opened), "%Y-%m-%d").date() - timedelta(days=30)
    return earliest.isoformat()


def _activity_pages(client, after: Optional[str] = None):
    """Yield FILL activities, oldest first, until a short page.

    alpaca-py 0.43.4 TradingClient has no get_account_activities. The
    REST helper is client.get('/account/activities/FILL', params).
    """
    getter = getattr(client, "get", None)
    if not callable(getter):
        raise RuntimeError("TradingClient has no REST get()")
    token = None
    seen: set[str] = set()
    for _page in range(500):
        params = {"direction": "asc", "page_size": 100}
        if after:
            params["after"] = after
        if token:
            params["page_token"] = token
        batch = getter("/account/activities/FILL", params)
        if isinstance(batch, dict):
            batch = batch.get("activities") or batch.get("data") or []
        batch = list(batch or [])
        if not batch:
            return
        yield from batch
        if len(batch) < 100:
            return
        nxt = str(_get(batch[-1], "id", default="") or "")
        if not nxt or nxt == token or nxt in seen:
            return
        seen.add(nxt)
        token = nxt
    log.warning("fill activity pagination stopped at 500 pages")


def _fills_from_closed_orders(client) -> list[dict]:
    getter = getattr(client, "get_orders", None)
    if getter is None:
        return []
    try:
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest
        orders = list(getter(GetOrdersRequest(status=QueryOrderStatus.CLOSED, limit=500)) or [])
    except Exception:
        try:
            orders = list(getter() or [])
        except Exception:
            return []
    fills = []
    for order in orders:
        norm = normalize_fill(order)
        if norm:
            fills.append(norm)
    return fills


def fetch_fills(client, after: Optional[str] = None) -> tuple[list[dict], str]:
    """(fills, source). Activities first; closed orders only if none came back.

    `source` is 'activities' or 'closed_orders'. Read-only. `after` defaults
    to 30 days before the ledger's earliest open date. A fallback logs a
    WARNING and does not include credentials.
    """
    assert_paper_client(client)
    if after is None:
        after = activity_after_date()
    reason = ""
    raw: list = []
    try:
        raw = list(_activity_pages(client, after=after))
    except Exception as exc:
        reason = redact(str(exc))
    fills = [f for f in (normalize_fill(a) for a in raw) if f]
    if fills:
        fills.sort(key=lambda f: parse_time(f["time"]) or datetime.min.replace(tzinfo=timezone.utc))
        return fills, "activities"
    if reason:
        log.warning(
            "fill activities unavailable (%s) — falling back to closed orders",
            reason,
        )
    else:
        log.warning("fill activities empty — falling back to closed orders")
    fills = _fills_from_closed_orders(client)
    fills.sort(key=lambda f: parse_time(f["time"]) or datetime.min.replace(tzinfo=timezone.utc))
    return fills, "closed_orders"


def fetch_open_positions(client=None) -> list:
    """Live paper positions. Read-only. Empty when the broker cannot be read."""
    if client is None:
        client = make_paper_client()
    assert_paper_client(client)
    getter = getattr(client, "get_all_positions", None)
    if getter is None:
        return []
    try:
        return list(getter() or [])
    except Exception as e:
        log.warning("position read failed (%s)", e)
        return []


def fetch_open_symbols(client) -> set[str]:
    assert_paper_client(client)
    getter = getattr(client, "get_all_positions", None)
    if getter is None:
        return set()
    try:
        positions = list(getter() or [])
    except Exception as e:
        log.warning("position read failed (%s)", e)
        return set()
    return {str(_get(p, "symbol", default="")) for p in positions if _get(p, "symbol")}


def make_paper_client():
    """TradingClient(paper=True). Keys from the environment, never logged."""
    key = os.getenv("ALPACA_API_KEY", "")
    secret = os.getenv("ALPACA_API_SECRET", "")
    if not key or not secret:
        raise RuntimeError("ALPACA_API_KEY and ALPACA_API_SECRET must be set")
    from alpaca.trading.client import TradingClient
    client = TradingClient(api_key=key, secret_key=secret, paper=True)
    assert_paper_client(client)
    return _ReadOnly(client)


def fetch_round_trips() -> list[RoundTrip]:
    """Live paper book → round trips. Read-only.

    Ledger order ids are applied before FIFO so a sell is not tied to an
    older lot the broker still holds.
    """
    client = make_paper_client()
    fills, _source = fetch_fills(client)
    trades: list = []
    try:
        import trade_ledger as tl
        trades = tl.all_trades()
    except Exception as exc:
        log.warning("ledger unavailable for order-id match (%s)", type(exc).__name__)
    return build_round_trips(fills, trades=trades)


def redact(message: str) -> str:
    text = str(message)
    for name in ("ALPACA_API_KEY", "ALPACA_API_SECRET"):
        secret = os.getenv(name) or ""
        if secret:
            text = text.replace(secret, "***")
    return text
