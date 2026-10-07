"""
trade_ledger.py — v1.1 (2026-04-24)
───────────────────────────────────
Single source of truth for paper-trade P&L.

Bridges the gap between what the bot ACTUALLY logs (PAPER TRADE lines in
scheduler.log) and what the reporter needs to read (a structured ledger).

Architecture:
  scheduler.log  ──[parse_log()]──▶  paper_trades.csv (the ledger)
       │                                      │
       │                                      ├──▶ daily reporter (today's slice)
       │                                      ├──▶ portfolio view (cumulative)
       │                                      └──▶ agent evaluator (per-agent)
       │
       └─ refresh_open_positions() updates current price + checks target/stop hits

CSV schema (one row per trade):
  trade_id, opened_at_et, symbol, side, primary_agent, contributors,
  entry_price, target_price, stop_price, risk_dollar, shares,
  status, exit_price, exit_at_et, exit_reason,
  realized_pnl, unrealized_pnl, current_price, last_updated_et,
  entry_order_id, exit_order_id

Status values:
  open      — position still active, target & stop not yet hit
  target    — target price reached → realized win
  stop      — stop price reached → realized loss
  expired   — held > MAX_HOLD_DAYS without hit → closed at current price

Idempotency: trade_id = sha1(opened_at_et + symbol + side + entry_price)[:12]
Re-running parse_log() never duplicates a row.

CHANGE LOG (L-2026-10-01b):
  Equity target/stop simulation and MAX_HOLD_DAYS do not close option
  symbols. An option row stays open while the broker holds the contract.
  It is booked closed only when the broker is flat (protective stop
  filled, or a premium-rule close already happened there). Underlying
  bars must not mark the option exited.
"""

from __future__ import annotations

import csv
import hashlib
import logging
import os
import re
from collections import defaultdict
from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional
from zoneinfo import ZoneInfo

# ── Paths ────────────────────────────────────────────────────────────────────
BASE_DIR  = Path(__file__).resolve().parent
LOGS_DIR  = BASE_DIR / "logs"
DATA_DIR  = BASE_DIR / "data"
LEDGER    = DATA_DIR / "paper_trades.csv"
SCHEDLOG  = LOGS_DIR / "scheduler.log"

DATA_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)

ET = ZoneInfo("America/New_York")
log = logging.getLogger("TradeLedger")

# Load .env explicitly. This module is imported by cron jobs and run
# standalone, where nothing else has loaded credentials — sync_from_broker
# silently returned HTTP 401 in exactly that case, so the heal never ran
# outside the main bot process.
try:
    from dotenv import load_dotenv as _ld
    _ld(BASE_DIR / ".env")
except Exception:
    pass

# ── Tunables ─────────────────────────────────────────────────────────────────
DEFAULT_RISK_PER_TRADE = float(os.getenv("RISK_PER_TRADE", "320"))  # $ per trade
MAX_HOLD_DAYS          = int(os.getenv("MAX_HOLD_DAYS", "5"))       # auto-expire after N

# Paper friction model for Learning Loop FLAG / PROMOTE / scorecard.
# Alpaca paper is $0 commission. Kill and promote still haircut P&L so
# "edge" is not a frictionless fill. 5 bps per side, $1/side floor.
ROUND_TRIP_COST_BPS = 10.0
ROUND_TRIP_COST_FLOOR_USD = 2.0

LONG_SIDES  = {"LONG", "BUY", "CALL"}
SHORT_SIDES = {"SHORT", "SELL", "PUT"}

CSV_FIELDS = [
    "trade_id", "opened_at_et", "symbol", "side",
    "primary_agent", "contributors",
    "entry_price", "target_price", "stop_price",
    "risk_dollar", "shares",
    "status", "exit_price", "exit_at_et", "exit_reason",
    "realized_pnl", "unrealized_pnl", "current_price", "last_updated_et",
    # Added after the original columns. load_ledger tolerates a file
    # that does not have them yet.
    "entry_order_id", "exit_order_id",
]


# ── Regex — bot's PAPER TRADE log line format ───────────────────────────────
# Example:
#   2026-04-24 10:06:09,237 [INFO] PAPER TRADE: META SHORT entry=$663.05 \
#       target=$616.64 stop=$679.63 agent=MetaAgent(ShortMomentumAgent)
#
# Real production logs prepend an emoji (📋, 📓, etc.) between [INFO] and
# PAPER TRADE, so we tolerate any optional non-alphanumeric prefix token.
PAPER_TRADE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})[,\.]\d+\s+"
    r"\[INFO\]\s+(?:[^\w\s]+\s+)?PAPER\s+TRADE:\s+"
    r"(?P<symbol>[A-Z][A-Z0-9\.\-]*)\s+"
    r"(?P<side>[A-Z]+)\s+"
    r"entry=\$?(?P<entry>[\d\.]+)\s+"
    r"target=\$?(?P<target>[\d\.]+)\s+"
    r"stop=\$?(?P<stop>[\d\.]+)\s+"
    r"agent=(?P<agent>.+?)\s*$"
)


@dataclass
class Trade:
    trade_id:       str
    opened_at_et:   str
    symbol:         str
    side:           str               # LONG | SHORT (normalized)
    primary_agent:  str
    contributors:   str               # comma-joined sub-agents
    entry_price:    float
    target_price:   float
    stop_price:     float
    risk_dollar:    float
    shares:         float
    status:         str = "open"      # open | target | stop | expired
    exit_price:     Optional[float] = None
    exit_at_et:     str = ""
    exit_reason:    str = ""
    realized_pnl:   float = 0.0
    unrealized_pnl: float = 0.0
    current_price:  Optional[float] = None
    last_updated_et: str = ""
    entry_order_id: str = ""
    exit_order_id:  str = ""

    @property
    def is_open(self) -> bool:
        return self.status == "open"

    @property
    def all_agents(self) -> list[str]:
        agents = [self.primary_agent]
        if self.contributors:
            agents += [a.strip() for a in self.contributors.split(",") if a.strip()]
        return agents

    @property
    def leaf_agents(self) -> list[str]:
        """Unique real agents after MetaAgent(...) unwrap. Empty for wrappers."""
        return leaf_agent_names(self.primary_agent, self.contributors)

    @property
    def primary_leaves(self) -> list[str]:
        return expand_agent_names(self.primary_agent)

    @property
    def contributor_leaves(self) -> list[str]:
        prim = set(self.primary_leaves)
        return [n for n in expand_agent_names(self.contributors) if n not in prim]

    @property
    def opened_date_et(self) -> str:
        return self.opened_at_et[:10]


# ── Parsing helpers ──────────────────────────────────────────────────────────

def _normalize_side(raw_side: str) -> str:
    s = raw_side.upper().strip()
    if s in SHORT_SIDES: return "SHORT"
    if s in LONG_SIDES:  return "LONG"
    return s  # leave unknowns alone for visibility


def expand_agent_names(raw: str) -> list[str]:
    """Split ``MetaAgent(SubA, SubB)`` into real leaf agent names.

    Display/attribution helper only — does not change how trades are stored
    or how MetaAgent computes live weights. Bare ``MetaAgent`` / ``BrokerSync``
    yield []. Nested wrapper labels are never returned.
    """
    raw = (raw or "").strip()
    if not raw:
        return []
    m = re.match(r"^([A-Za-z_]+)\s*\(([^)]*)\)\s*$", raw)
    if m:
        wrapper, inner = m.group(1).strip(), m.group(2).strip()
        parts = [p.strip() for p in inner.split(",") if p.strip()]
        if wrapper == "MetaAgent":
            return [p for p in parts if p not in {"MetaAgent", "BrokerSync"}]
        out: list[str] = []
        if wrapper not in {"MetaAgent", "BrokerSync"}:
            out.append(wrapper)
        for p in parts:
            if p not in {"MetaAgent", "BrokerSync"} and p not in out:
                out.append(p)
        return out
    if raw in {"MetaAgent", "BrokerSync"}:
        return []
    if "," in raw:
        out: list[str] = []
        seen: set[str] = set()
        for part in raw.split(","):
            for name in expand_agent_names(part.strip()):
                if name not in seen:
                    seen.add(name)
                    out.append(name)
        return out
    return [raw]


def is_wrapper_agent_name(name: str) -> bool:
    """True for MetaAgent, BrokerSync, or MetaAgent(...) compound labels."""
    n = (name or "").strip()
    if not n or n in {"MetaAgent", "BrokerSync"}:
        return True
    return n.startswith("MetaAgent(")


def leaf_agent_names(*raw_parts: str) -> list[str]:
    """Unique leaf names from one or more stored agent fields, order preserved."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in raw_parts:
        for name in expand_agent_names(raw):
            if name not in seen:
                seen.add(name)
                out.append(name)
    return out


def round_trip_cost(trade: Trade) -> float:
    """Conservative paper round-trip friction for one ledger row."""
    shares = float(getattr(trade, "shares", 0) or 0)
    entry = float(getattr(trade, "entry_price", 0) or 0)
    notional = abs(shares * entry)
    if notional <= 0:
        notional = abs(float(getattr(trade, "risk_dollar", 0) or 0))
    bps_cost = notional * ROUND_TRIP_COST_BPS / 10_000.0
    return round(max(ROUND_TRIP_COST_FLOOR_USD, bps_cost), 2)


def expectancy_after_costs(
    gross_pnls: Iterable[float],
    costs: Optional[Iterable[float]] = None,
) -> Optional[float]:
    """Mean P&L after paper friction. None when there are no trades."""
    pnls = [float(p) for p in gross_pnls]
    if not pnls:
        return None
    if costs is None:
        cost_list = [ROUND_TRIP_COST_FLOOR_USD] * len(pnls)
    else:
        cost_list = [float(c) for c in costs]
        if len(cost_list) != len(pnls):
            raise ValueError("costs must align 1:1 with gross_pnls")
    net = sum(p - c for p, c in zip(pnls, cost_list))
    return round(net / len(pnls), 4)


def _parse_agent_field(agent_raw: str) -> tuple[str, str]:
    """Split MetaAgent(SubA, SubB) → ('MetaAgent', 'SubA, SubB').
    Plain 'TechnicalAgent' → ('TechnicalAgent', '')."""
    agent_raw = agent_raw.strip()
    m = re.match(r"^([A-Za-z_]+)\s*\(([^)]*)\)\s*$", agent_raw)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return agent_raw, ""


def _trade_id(opened_at_et: str, symbol: str, side: str, entry: float) -> str:
    seed = f"{opened_at_et}|{symbol}|{side}|{entry:.4f}"
    return hashlib.sha1(seed.encode()).hexdigest()[:12]


def _shares_for_risk(risk_dollar: float, entry: float, stop: float | None = None) -> float:
    """Shares implied by a dollar risk budget and the stop distance.

    Dividing risk by entry treated RISK_PER_TRADE as notional. On a 4%
    stop that understated size (and therefore reconstructed P&L) by ~25x.
    When the stop is missing, fall back to the old notional reading rather
    than inventing a stop.
    """
    if risk_dollar <= 0 or entry <= 0:
        return 0.0
    if stop is not None:
        dist = abs(entry - float(stop))
        if dist > 0:
            return round(risk_dollar / dist, 4)
    return round(risk_dollar / entry, 4)


def parse_paper_trade_line(line: str) -> Optional[Trade]:
    """Parse one scheduler.log line; return Trade or None if not a paper trade line."""
    m = PAPER_TRADE_RE.match(line.rstrip())
    if not m:
        return None
    ts        = m.group("ts")
    symbol    = m.group("symbol")
    side      = _normalize_side(m.group("side"))
    entry     = float(m.group("entry"))
    target    = float(m.group("target"))
    stop      = float(m.group("stop"))
    primary, contribs = _parse_agent_field(m.group("agent"))
    risk      = DEFAULT_RISK_PER_TRADE
    shares    = _shares_for_risk(risk, entry, stop)
    return Trade(
        trade_id      = _trade_id(ts, symbol, side, entry),
        opened_at_et  = ts,
        symbol        = symbol,
        side          = side,
        primary_agent = primary,
        contributors  = contribs,
        entry_price   = entry,
        target_price  = target,
        stop_price    = stop,
        risk_dollar   = risk,
        shares        = shares,
    )


# ── Ledger I/O ───────────────────────────────────────────────────────────────

def load_ledger() -> dict[str, Trade]:
    """Read the CSV ledger into {trade_id: Trade}."""
    if not LEDGER.exists():
        return {}
    out: dict[str, Trade] = {}
    with LEDGER.open(newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                out[row["trade_id"]] = Trade(
                    trade_id        = row["trade_id"],
                    opened_at_et    = row["opened_at_et"],
                    symbol          = row["symbol"],
                    side            = row["side"],
                    primary_agent   = row["primary_agent"],
                    contributors    = row.get("contributors", ""),
                    entry_price     = float(row["entry_price"]),
                    target_price    = float(row["target_price"]),
                    stop_price      = float(row["stop_price"]),
                    risk_dollar     = float(row["risk_dollar"]),
                    shares          = float(row["shares"]),
                    status          = row.get("status", "open"),
                    exit_price      = float(row["exit_price"]) if row.get("exit_price") else None,
                    exit_at_et      = row.get("exit_at_et", ""),
                    exit_reason     = row.get("exit_reason", ""),
                    realized_pnl    = float(row.get("realized_pnl") or 0),
                    unrealized_pnl  = float(row.get("unrealized_pnl") or 0),
                    current_price   = float(row["current_price"]) if row.get("current_price") else None,
                    last_updated_et = row.get("last_updated_et", ""),
                    entry_order_id  = row.get("entry_order_id") or "",
                    exit_order_id   = row.get("exit_order_id") or "",
                )
            except (KeyError, ValueError) as e:
                # tolerate corrupt rows; skip
                continue
    return out


def save_ledger(trades: dict[str, Trade]) -> None:
    """Atomic write of the entire ledger."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = LEDGER.with_suffix(".csv.tmp")
    with tmp.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        # Sort by opened time so the file is human-readable
        for t in sorted(trades.values(), key=lambda x: x.opened_at_et):
            row = asdict(t)
            # Normalize None → "" for CSV
            for k, v in row.items():
                if v is None:
                    row[k] = ""
            writer.writerow(row)
    tmp.replace(LEDGER)


# ── Backfill from scheduler.log ──────────────────────────────────────────────

def parse_log(log_path: Path = SCHEDLOG) -> tuple[int, int]:
    """Scan scheduler.log; merge any new PAPER TRADE entries into the ledger.
    Returns (newly_added, total_in_ledger). Idempotent."""
    if not log_path.exists():
        return (0, 0)
    existing = load_ledger()
    added = 0
    try:
        with log_path.open(errors="ignore") as f:
            for line in f:
                t = parse_paper_trade_line(line)
                if t is None:
                    continue
                if t.trade_id not in existing:
                    existing[t.trade_id] = t
                    added += 1
    except OSError:
        return (0, len(existing))
    if added:
        save_ledger(existing)
    return (added, len(existing))


# ── Open-position resolver (target / stop / expiry checks) ───────────────────

def _import_yf():
    try:
        import yfinance as yf  # type: ignore
        return yf
    except ImportError:
        return None


# Alpaca order format (slash) → yfinance fetch format (dash). Without this
# mapping, yf.Ticker("BTC/USD") 404s silently and crypto positions never get
# priced — unrealized P&L stays 0.0 forever and the evaluator judges
# CryptoAgent on fake zeros.
_YF_SYMBOL_MAP = {"BTC/USD": "BTC-USD", "ETH/USD": "ETH-USD", "SOL/USD": "SOL-USD"}


def _fetch_price_path(symbol: str, since_iso_et: str, yf) -> Optional[object]:
    """Return a DataFrame of price bars from `since` through now, or None."""
    try:
        opened_dt = datetime.fromisoformat(since_iso_et)
        if opened_dt.tzinfo is None:
            opened_dt = opened_dt.replace(tzinfo=ET)
    except Exception:
        return None
    days_held = max(1, (datetime.now(ET) - opened_dt).days + 1)
    period    = f"{min(days_held + 2, 60)}d"
    interval  = "5m" if days_held <= 5 else "1d"
    try:
        df = yf.Ticker(_YF_SYMBOL_MAP.get(symbol, symbol)).history(period=period, interval=interval)
        if df is None or df.empty:
            return None
        # Localize index to ET
        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC").tz_convert(ET)
        else:
            df.index = df.index.tz_convert(ET)
        # Trim to bars at/after entry
        df = df[df.index >= opened_dt]
        return df
    except Exception:
        return None


def equity_simulation_applies(symbol: str) -> bool:
    """Equity target, stop, ATR path, and MAX_HOLD_DAYS.

    Option contracts market-close only on -50% premium, CLOSE_DTE, or a
    broker protective-stop fill. +100% / +150% ratchet that stop; they
    do not book the option exited from an equity bar. A yfinance path of
    the underlying must not book the option exited. L-2026-10-01b.
    """
    from invariants import is_option_symbol
    return not is_option_symbol(symbol)


def _resolve_option_ledger_row(trade: Trade, broker_open, broker_fills,
                               now_iso: str, activities=None) -> str:
    """Keep or close one option ledger row. Returns 'open' or 'closed'.

    Equity target/stop/hold-time are not consulted. The row stays open
    while the broker holds the contract, including the day after entry
    on a small premium loss. A flat broker book means the protective
    stop filled or a premium rule already closed it.
    """
    trade.last_updated_et = now_iso
    if broker_open is None:
        return "open"
    key = trade.symbol.replace("/", "")
    if key in broker_open or trade.symbol in broker_open:
        return "open"
    if _recently_opened(trade):
        return "open"
    hit = closing_fill(trade, activities) if activities else None
    fill = (broker_fills or {}).get(key) or (broker_fills or {}).get(trade.symbol)
    if hit is not None:
        px, when, oid = hit
        trade.status = "stop" if _pnl_for(trade, px) < 0 else "target"
        trade.exit_price = px
        trade.exit_at_et = broker_time_to_et(when)
        trade.exit_reason = "broker avg fill"
        if oid:
            trade.exit_order_id = oid
    elif fill is not None and not isinstance(fill, list):
        px, when = fill[0], fill[1]
        trade.status = "stop"
        trade.exit_price = px
        trade.exit_at_et = broker_time_to_et(when)
        trade.exit_reason = "broker fill (protective stop)"
        if len(fill) > 2 and fill[2]:
            trade.exit_order_id = str(fill[2])
    else:
        trade.status = "stop"
        trade.exit_price = trade.current_price or trade.entry_price
        trade.exit_at_et = now_iso
        trade.exit_reason = "broker flat (option close)"
    trade.realized_pnl = _pnl_for(trade, float(trade.exit_price or trade.entry_price))
    trade.unrealized_pnl = 0.0
    return "closed"


def _check_hits(trade: Trade, df) -> tuple[Optional[str], Optional[float], Optional[str]]:
    """Walk price bars in time order. First bar that touches target/stop wins.
    Returns (status, exit_price, exit_at_et) or (None, None, None) if still open.

    Option symbols always stay open here. Their premium rules live in
    options_executor; this walker is the equity stop/target engine.
    """
    if not equity_simulation_applies(trade.symbol):
        return (None, None, None)
    if df is None or df.empty:
        return (None, None, None)
    is_long = trade.side == "LONG"
    for ts, row in df.iterrows():
        hi = float(row.get("High", row.get("Close", 0)))
        lo = float(row.get("Low",  row.get("Close", 0)))
        if is_long:
            if hi >= trade.target_price:
                return ("target", trade.target_price, ts.strftime("%Y-%m-%d %H:%M:%S"))
            if lo <= trade.stop_price:
                return ("stop", trade.stop_price, ts.strftime("%Y-%m-%d %H:%M:%S"))
        else:  # SHORT
            if lo <= trade.target_price:
                return ("target", trade.target_price, ts.strftime("%Y-%m-%d %H:%M:%S"))
            if hi >= trade.stop_price:
                return ("stop", trade.stop_price, ts.strftime("%Y-%m-%d %H:%M:%S"))
    return (None, None, None)


def broker_time_to_et(when: str) -> str:
    """Alpaca transaction_time is UTC. exit_at_et is an ET wall clock.

    Slicing the ISO string left ghost closes stamped in UTC (18:05 stored
    as if it were 18:05 ET). Parse and convert. A value that is already
    an ET wall clock without a timezone is returned unchanged.
    """
    if not when:
        return ""
    raw = str(when).strip()
    if "T" not in raw and "+" not in raw and not raw.endswith("Z"):
        return raw[:19]
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(ET).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return raw[:19].replace("T", " ")


def _contract_multiplier(symbol: str) -> float:
    """One option contract is 100 shares of premium. Equities stay 1."""
    try:
        from invariants import is_option_symbol
        if is_option_symbol(symbol):
            return 100.0
    except Exception:
        if len(str(symbol or "").replace("/", "")) > 12:
            return 100.0
    return 1.0


def _pnl_for(trade: Trade, exit_price: float) -> float:
    sign = 1 if trade.side == "LONG" else -1
    mult = _contract_multiplier(getattr(trade, "symbol", ""))
    return round(trade.shares * (exit_price - trade.entry_price) * sign * mult, 2)


def _order_attr(order, *names):
    if isinstance(order, dict):
        for name in names:
            if name in order and order[name] not in (None, ""):
                return order[name]
        return None
    for name in names:
        value = getattr(order, name, None)
        if value not in (None, ""):
            return value
    return None


def _activity_row(raw) -> Optional[dict]:
    """One FILL print as a plain dict. Quotes and empty prints are dropped."""
    if not isinstance(raw, dict):
        raw = {
            "symbol": _order_attr(raw, "symbol"),
            "side": _order_attr(raw, "side"),
            "qty": _order_attr(raw, "qty", "filled_qty"),
            "price": _order_attr(raw, "price", "filled_avg_price"),
            "transaction_time": _order_attr(raw, "transaction_time", "filled_at"),
            "order_id": _order_attr(raw, "order_id", "id"),
        }
    symbol = str(raw.get("symbol") or "")
    if not symbol:
        return None
    side = str(raw.get("side") or "").lower().split(".")[-1]
    if side in {"buy", "long"}:
        side = "buy"
    elif side in {"sell", "short"}:
        side = "sell"
    else:
        return None
    try:
        qty = float(raw.get("qty") or raw.get("filled_qty") or 0)
        price = float(raw.get("price") or raw.get("filled_avg_price") or 0)
    except (TypeError, ValueError):
        return None
    if qty <= 0 or price <= 0:
        return None
    when = raw.get("transaction_time") or raw.get("filled_at") or ""
    if isinstance(when, datetime):
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        when = when.isoformat()
    when = str(when or "")
    if not when:
        return None
    return {
        "symbol": symbol,
        "side": side,
        "qty": qty,
        "price": price,
        "time": when,
        "order_id": str(raw.get("order_id") or ""),
    }


def closing_fill(trade: Trade, activities) -> Optional[tuple[float, str, str]]:
    """(avg price, fill time, order id) of the order that closed `trade`.

    The price is the size-weighted average of that order's prints. It is
    not the stop or target trigger, and it is not a later quote or a
    same-side fill. Prefers `exit_order_id` when that order actually
    printed; otherwise the closing-side order after the open whose size
    is closest to the position.
    """
    close_side = "sell" if trade.side == "LONG" else "buy"
    key = str(trade.symbol or "").replace("/", "")
    groups: dict[str, list[dict]] = {}
    for raw in activities or []:
        row = raw if isinstance(raw, dict) and raw.get("time") and row_is_norm(raw) else _activity_row(raw)
        if not row:
            continue
        if str(row["symbol"]).replace("/", "") != key:
            continue
        if row["side"] != close_side:
            continue
        if not _fill_after_open(trade, row["time"]):
            continue
        oid = row.get("order_id") or ""
        if oid and oid == str(getattr(trade, "entry_order_id", "") or ""):
            continue
        groups.setdefault(oid or f"print-{row['time']}", []).append(row)
    if not groups:
        return None
    preferred = str(getattr(trade, "exit_order_id", "") or "")
    if preferred and preferred in groups:
        chosen = preferred
    else:
        target = abs(float(getattr(trade, "shares", 0) or 0))

        def _rank(group_id: str):
            parts = groups[group_id]
            qty = sum(p["qty"] for p in parts)
            miss = abs(qty - target) if target else 0.0
            last = max(p["time"] for p in parts)
            return (miss, last)

        chosen = min(groups, key=_rank)
    parts = groups[chosen]
    qty = sum(p["qty"] for p in parts)
    avg = sum(p["price"] * p["qty"] for p in parts) / qty
    when = max(parts, key=lambda p: p["time"])["time"]
    oid = "" if str(chosen).startswith("print-") else str(chosen)
    return round(avg, 4), when, oid


def row_is_norm(raw: dict) -> bool:
    return raw.get("side") in {"buy", "sell"} and "time" in raw and "price" in raw


def _fill_after_open(trade: Trade, when: str) -> bool:
    """True when the print is at or after the ledger open (5 min of slack)."""
    opened_raw = str(getattr(trade, "opened_at_et", "") or "")[:19]
    if not opened_raw:
        return True
    try:
        opened = datetime.fromisoformat(opened_raw).replace(tzinfo=ET)
    except ValueError:
        return True
    et = broker_time_to_et(when)
    try:
        stamped = datetime.fromisoformat(et[:19]).replace(tzinfo=ET)
    except ValueError:
        return True
    return stamped >= opened - timedelta(minutes=5)


def book_broker_close(trade: Trade, order) -> bool:
    """Book one closing order's average fill. False when `order` is not the exit.

    A buy closes a short and a sell closes a long. The entry order is
    ignored, even when it is the newest closed order on the symbol.
    Price is filled_avg_price and the clock is filled_at. A missing fill
    time does not fall through to wall-clock now, a trigger, or a quote.
    """
    if not getattr(trade, "is_open", True) or trade.status != "open":
        return False
    side = str(_order_attr(order, "side") or "").lower().split(".")[-1]
    if side in {"buy", "long"}:
        closes = "SHORT"
    elif side in {"sell", "short"}:
        closes = "LONG"
    else:
        return False
    if trade.side != closes:
        return False
    sym = str(_order_attr(order, "symbol") or "")
    if sym.replace("/", "") != str(trade.symbol or "").replace("/", ""):
        return False
    oid = str(_order_attr(order, "id", "order_id") or "")
    if oid and oid == str(getattr(trade, "entry_order_id", "") or ""):
        return False
    try:
        avg = float(_order_attr(order, "filled_avg_price", "price") or 0)
    except (TypeError, ValueError):
        return False
    if avg <= 0:
        return False
    when = _order_attr(order, "filled_at", "transaction_time")
    if isinstance(when, datetime):
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        when = when.isoformat()
    when = str(when or "")
    if not when:
        return False
    trade.exit_price = avg
    trade.exit_at_et = broker_time_to_et(when)
    trade.exit_order_id = oid or str(getattr(trade, "exit_order_id", "") or "")
    trade.exit_reason = "broker avg fill"
    trade.realized_pnl = _pnl_for(trade, avg)
    trade.unrealized_pnl = 0.0
    trade.status = "target" if trade.realized_pnl >= 0 else "stop"
    return True


def _book_activity_exit(trade: Trade, activities, *, status: str, reason: str,
                        fallback_price, fallback_when) -> None:
    """Close on the broker average when we have one, else the caller's price."""
    hit = closing_fill(trade, activities) if activities else None
    if hit is not None:
        price, when, oid = hit
        if oid:
            trade.exit_order_id = oid
        trade.exit_price = price
        trade.exit_at_et = broker_time_to_et(when)
        trade.exit_reason = "broker avg fill"
        trade.realized_pnl = _pnl_for(trade, price)
        trade.status = "target" if trade.realized_pnl >= 0 else "stop"
    else:
        trade.status = status
        trade.exit_price = fallback_price
        trade.exit_at_et = broker_time_to_et(str(fallback_when or ""))
        trade.exit_reason = reason
        trade.realized_pnl = _pnl_for(trade, float(trade.exit_price or trade.entry_price))
    trade.unrealized_pnl = 0.0


def sync_from_broker() -> dict:
    """Re-open ledger rows for positions the broker actually holds.

    The broker-override in refresh_open_positions stops NEW orphans, but
    rows already wrongly closed stay closed — the ledger keeps under-
    reporting exposure and the reconcile warning never clears. This heals
    the existing divergence: any equity position the broker holds that the
    ledger shows closed is re-opened at its real broker cost basis, so
    both systems describe the same book.

    Deliberately one-directional: it re-opens what the broker holds and
    never closes anything. Closing stays with the broker's own fills.
    """
    out = {"reopened": 0, "created": 0, "checked": 0}
    try:
        import os as _os, requests as _rq
        h = {"APCA-API-KEY-ID": _os.getenv("ALPACA_API_KEY", ""),
             "APCA-API-SECRET-KEY": _os.getenv("ALPACA_API_SECRET", "")}
        r = _rq.get("https://paper-api.alpaca.markets/v2/positions", headers=h, timeout=15)
        if r.status_code != 200:
            return {**out, "error": f"HTTP {r.status_code}"}
        positions = r.json()
    except Exception as e:
        return {**out, "error": str(e)}

    trades = load_ledger()
    live_open = {t.symbol.replace("/", "") for t in trades.values() if t.is_open}
    now_iso = datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S")

    for p in positions:
        sym = str(p.get("symbol", ""))
        if len(sym) > 12:            # options tracked separately
            continue
        out["checked"] += 1
        if sym in live_open:
            continue

        qty   = float(p.get("qty") or 0)
        basis = float(p.get("avg_entry_price") or 0)
        unrl  = float(p.get("unrealized_pl") or 0)
        cur   = float(p.get("current_price") or 0)
        side  = "LONG" if qty > 0 else "SHORT"
        if basis <= 0 or qty == 0:
            continue

        # Prefer resurrecting the most recent closed row for this symbol
        prior = sorted(
            [t for t in trades.values()
             if t.symbol.replace("/", "") == sym and t.side == side and not t.is_open],
            key=lambda t: t.exit_at_et or t.opened_at_et, reverse=True)
        if prior:
            t = prior[0]
            t.status = "open"
            t.exit_price = None
            t.exit_at_et = ""
            t.exit_reason = ""
            t.realized_pnl = 0.0
            t.unrealized_pnl = unrl
            t.current_price = cur
            t.shares = abs(qty)
            t.entry_price = basis
            t.last_updated_et = now_iso
            out["reopened"] += 1
        else:
            tid = _trade_id(now_iso, sym, side, basis)
            trades[tid] = Trade(
                trade_id=tid, opened_at_et=now_iso, symbol=sym, side=side,
                primary_agent="BrokerSync", contributors="",
                entry_price=basis,
                target_price=basis * (1.08 if side == "LONG" else 0.92),
                stop_price=basis * (0.96 if side == "LONG" else 1.04),
                risk_dollar=abs(basis - basis * (0.96 if side == "LONG" else 1.04)) * abs(qty),
                shares=abs(qty), status="open",
                unrealized_pnl=unrl, current_price=cur, last_updated_et=now_iso)
            out["created"] += 1

    if out["reopened"] or out["created"]:
        save_ledger(trades)
        log.info(f"broker sync: re-opened {out['reopened']}, adopted {out['created']} "
                 f"position(s) the ledger had lost track of")
    return out


def _recently_opened(trade: Trade, seconds: int = 120, now: datetime | None = None) -> bool:
    """True if the row is so new the broker fill may not have landed yet."""
    try:
        opened = datetime.fromisoformat(trade.opened_at_et[:19]).replace(tzinfo=ET)
        clock = now or datetime.now(ET)
        if clock.tzinfo is None:
            clock = clock.replace(tzinfo=ET)
        return (clock - opened).total_seconds() < seconds
    except Exception:
        return False


def _recent_fill_activities(days: int = 30) -> list[dict]:
    """Recent paper FILL rows. Empty when the broker cannot be read.

    Several pages, newest first, so a close can average the exit order
    instead of keeping whichever print arrived last. Headers stay off
    the log.
    """
    import requests as _rq
    headers = {
        "APCA-API-KEY-ID": os.getenv("ALPACA_API_KEY", ""),
        "APCA-API-SECRET-KEY": os.getenv("ALPACA_API_SECRET", ""),
    }
    after = (datetime.now(ET) - timedelta(days=days)).strftime("%Y-%m-%d")
    rows: list[dict] = []
    token = None
    try:
        for _page in range(10):
            params = {"after": after, "page_size": 100, "direction": "desc"}
            if token:
                params["page_token"] = token
            response = _rq.get(
                "https://paper-api.alpaca.markets/v2/account/activities/FILL",
                headers=headers, params=params, timeout=15,
            )
            if response.status_code != 200:
                break
            batch = response.json()
            if not isinstance(batch, list) or not batch:
                break
            rows.extend(a for a in batch if isinstance(a, dict))
            if len(batch) < 100:
                break
            nxt = str(batch[-1].get("id") or "")
            if not nxt or nxt == token:
                break
            token = nxt
    except Exception as exc:
        log.debug("fill history unavailable (%s)", type(exc).__name__)
    return rows


def _fetch_ghost_book() -> dict:
    """Positions, pending symbols, and the latest fill per symbol.

    Returns {broker_syms, pending, fills} or {error}. Callers that already
    have this book (tests) skip the network.
    """
    import os as _os, requests as _rq
    h = {"APCA-API-KEY-ID": _os.getenv("ALPACA_API_KEY", ""),
         "APCA-API-SECRET-KEY": _os.getenv("ALPACA_API_SECRET", "")}
    r = _rq.get("https://paper-api.alpaca.markets/v2/positions",
                headers=h, timeout=15)
    if r.status_code != 200:
        return {"error": f"HTTP {r.status_code}"}
    broker_syms = {p["symbol"] for p in r.json()}
    pending = set()
    try:
        ords = _rq.get("https://paper-api.alpaca.markets/v2/orders",
                       headers=h, params={"status": "open", "limit": 200},
                       timeout=15)
        if ords.status_code == 200:
            pending = {o.get("symbol") for o in ords.json() if o.get("symbol")}
    except Exception:
        pass
    activity_rows = _recent_fill_activities()
    fills: dict[str, tuple[float, str]] = {}
    for a in activity_rows:
        sym = a.get("symbol")
        when = a.get("transaction_time")
        if not sym or not when:
            continue
        try:
            price = float(a["price"])
        except (KeyError, TypeError, ValueError):
            continue
        prev = fills.get(sym)
        if prev is None or str(when) > prev[1]:
            fills[sym] = (price, str(when))
    return {
        "broker_syms": broker_syms,
        "pending": pending,
        "fills": fills,
        "activity_rows": activity_rows,
    }


def close_ghosts(book: dict | None = None, now: datetime | None = None) -> dict:
    """Close ledger rows the broker does not hold.

    sync_from_broker is one-directional: it re-opens orphans. Ghosts —
    ledger open, broker empty — inflate perceived exposure, trip gates,
    and produced the SUNB warning. Close them against the actual fill
    when we have one. Skip rows opened in the last two minutes (fill
    race) and crypto (own scheduler / different symbol format).

    `book` and `now` are for tests: pass the broker snapshot and a fixed
    clock so this does not touch the network or the wall clock.
    """
    out = {"closed": 0, "skipped_recent": 0, "checked": 0}
    try:
        from invariants import is_crypto_symbol, is_option_symbol, ghost_symbols
    except Exception as e:
        return {**out, "error": str(e)}
    if book is None:
        try:
            book = _fetch_ghost_book()
        except Exception as e:
            return {**out, "error": str(e)}
    if book.get("error") and "broker_syms" not in book:
        return {**out, "error": book["error"]}
    broker_syms = book.get("broker_syms") or set()
    pending = book.get("pending") or set()
    fills = book.get("fills") or {}
    activity_rows = book.get("activity_rows") or []

    trades = load_ledger()
    open_syms = [t.symbol for t in trades.values() if t.is_open]
    ghosts = ghost_symbols(open_syms, broker_syms)
    clock = now or datetime.now(ET)
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=ET)
    now_iso = clock.strftime("%Y-%m-%d %H:%M:%S")
    for t in list(trades.values()):
        if not t.is_open:
            continue
        key = t.symbol.replace("/", "")
        if key not in ghosts:
            continue
        if is_crypto_symbol(t.symbol) or is_option_symbol(t.symbol):
            continue
        out["checked"] += 1
        if _recently_opened(t, now=clock) or t.symbol in pending or key in pending:
            out["skipped_recent"] += 1
            continue
        fill = fills.get(key) or fills.get(t.symbol)
        hit = closing_fill(t, activity_rows) if activity_rows else None
        if hit is not None:
            px, when, oid = hit
            t.status = "target" if _pnl_for(t, px) >= 0 else "stop"
            t.exit_price = px
            t.exit_at_et = broker_time_to_et(when)
            t.exit_reason = "broker avg fill"
            if oid:
                t.exit_order_id = oid
        elif fill is not None:
            px, when = fill[0], fill[1]
            t.status = "stop"
            t.exit_price = px
            t.exit_at_et = broker_time_to_et(when)
            t.exit_reason = "broker fill (ghost close)"
        else:
            px = t.current_price or t.entry_price
            t.status = "expired"
            t.exit_price = px
            t.exit_at_et = now_iso
            t.exit_reason = "ghost — broker no longer holds"
        t.realized_pnl = _pnl_for(t, float(t.exit_price or t.entry_price))
        t.unrealized_pnl = 0.0
        t.last_updated_et = now_iso
        out["closed"] += 1
    if out["closed"]:
        save_ledger(trades)
        log.info(f"ghost close: closed {out['closed']} ledger row(s) the "
                 f"broker does not hold")
    return out


def refresh_open_positions(max_symbols: int = 60) -> dict:
    """For every open trade, fetch price path, mark hits, update unrealized P&L.
    Returns summary dict for logging."""
    yf = _import_yf()
    if yf is None:
        return {"error": "yfinance not installed", "updated": 0}

    sync_from_broker()          # heal divergence before evaluating exits
    close_ghosts()              # close ledger rows the broker does not hold
    trades = load_ledger()
    open_trades = [t for t in trades.values() if t.is_open]
    if not open_trades:
        save_ledger(trades)  # touch file even if nothing open
        return {"checked": 0, "closed_target": 0, "closed_stop": 0, "expired": 0, "still_open": 0}

    # ── BROKER IS TRUTH FOR POSITION STATE ────────────────────────────
    # Root cause of the drift that has plagued this account: this
    # function SIMULATED exits from yfinance bars while the broker held
    # the real GTC trailing stops. Two engines deciding the same thing —
    # the ledger would "close" a trade the broker still held, creating an
    # orphan that consumed buying power invisibly. It froze the account
    # twice ($0 buying power, 597 failed orders) and hid $19,908 of
    # losses from the circuit breaker. Clearing orphans by hand never
    # worked because the simulation immediately recreated them.
    #
    # Fix: ask the broker what it actually holds. A position the broker
    # still has stays OPEN in the ledger no matter what the price path
    # suggests. Only positions the broker has genuinely exited get
    # closed here, and simulation is reserved for symbols the broker
    # cannot answer for (crypto handled by its own scheduler, or an API
    # outage — in which case we degrade to the old behaviour rather than
    # freeze).
    broker_open: set | None = None
    try:
        import os as _os, requests as _rq
        _h = {"APCA-API-KEY-ID": _os.getenv("ALPACA_API_KEY", ""),
              "APCA-API-SECRET-KEY": _os.getenv("ALPACA_API_SECRET", "")}
        _r = _rq.get("https://paper-api.alpaca.markets/v2/positions",
                     headers=_h, timeout=15)
        if _r.status_code == 200:
            broker_open = {p["symbol"] for p in _r.json()}
    except Exception as _be:
        log.warning(f"broker position check failed ({_be}) — "
                    f"falling back to price simulation this run")
        broker_open = None

    # Recent closing fills, so an exit can be attributed to what actually
    # happened rather than to whichever branch noticed it first.
    #
    # RNG (2026-08-10) is the case this fixes: a 3% trailing stop captured a
    # +29% run and sold 369 shares at $62.55. Because the broker no longer
    # held it, the expiry branch below then fired and stamped the row
    # "expired after 16d". The dollars were right, but daily_postmortem.py
    # and the strategy learner key on exit_reason — so the best trade of the
    # week was teaching the system that holding to expiry wins, when the
    # truth was that a tight trail on a runner wins. Mislabelled causes are
    # worse than missing ones: the loop learns confidently in the wrong
    # direction.
    activity_rows: list[dict] = []
    broker_fills: dict[str, tuple[float, str]] = {}
    try:
        activity_rows = _recent_fill_activities()
        for _a in activity_rows:
            _sym = _a.get("symbol")
            _when = _a.get("transaction_time")
            if not _sym or not _when:
                continue
            try:
                _px = float(_a["price"])
            except (KeyError, TypeError, ValueError):
                continue
            # Latest print is only the fallback. The close below prefers
            # the exit order's size-weighted average.
            _prev = broker_fills.get(_sym)
            if _prev is None or str(_when) > _prev[1]:
                broker_fills[_sym] = (_px, str(_when))
    except Exception as _fe:
        log.debug("fill history unavailable (%s) — exit attribution degrades",
                  type(_fe).__name__)

    # Group by symbol so we minimize yfinance calls
    by_symbol: dict[str, list[Trade]] = defaultdict(list)
    for t in open_trades:
        by_symbol[t.symbol].append(t)

    closed_target = closed_stop = expired = still_open = 0
    now_et = datetime.now(ET)
    now_iso = now_et.strftime("%Y-%m-%d %H:%M:%S")

    # Cap the work per run to avoid timeouts on huge backfills
    symbols_to_process = list(by_symbol.keys())[:max_symbols]

    for symbol in symbols_to_process:
        # Fetch once per symbol — earliest open trade dictates start
        earliest = min(by_symbol[symbol], key=lambda t: t.opened_at_et)
        df = _fetch_price_path(symbol, earliest.opened_at_et, yf)
        last_price = None
        if df is not None and not df.empty:
            try:
                last_price = float(df["Close"].iloc[-1])
            except Exception:
                last_price = None

        for t in by_symbol[symbol]:
            if not equity_simulation_applies(t.symbol):
                outcome = _resolve_option_ledger_row(
                    t, broker_open, broker_fills, now_iso,
                    activities=activity_rows)
                if outcome == "closed":
                    expired += 1
                else:
                    still_open += 1
                trades[t.trade_id] = t
                continue
            # Filter df to bars at/after this trade's open
            t_df = None
            if df is not None and not df.empty:
                try:
                    opened_dt = datetime.fromisoformat(t.opened_at_et).replace(tzinfo=ET)
                    t_df = df[df.index >= opened_dt]
                except Exception:
                    t_df = df
            status, exit_price, exit_at = _check_hits(t, t_df)

            # Broker override: if the broker still holds this symbol, the
            # trade is OPEN regardless of what the simulated price path
            # says. This is the line that stops orphans being created.
            if broker_open is not None and status:
                if t.symbol.replace("/", "") in broker_open:
                    log.debug(f"{t.symbol}: sim says {status} but broker still "
                              f"holds it — keeping open (trail active)")
                    status = None

            # Ghost close inside the refresh loop: if the broker is reachable
            # and does not hold this equity, do not wait for MAX_HOLD_DAYS.
            # That wait is what left SUNB (and any fast round-trip) as a
            # permanent "open" row until expiry, inflating exposure.
            if (broker_open is not None
                    and t.symbol.replace("/", "") not in broker_open
                    and "/" not in t.symbol
                    and len(t.symbol) <= 12
                    and not _recently_opened(t)):
                _fill = broker_fills.get(t.symbol.replace("/", ""))
                _hit = closing_fill(t, activity_rows)
                if _hit is not None:
                    _px, _when, _oid = _hit
                    t.status       = "target" if _pnl_for(t, _px) >= 0 else "stop"
                    t.exit_price   = _px
                    t.exit_at_et   = broker_time_to_et(_when)
                    t.exit_reason  = "broker avg fill"
                    if _oid:
                        t.exit_order_id = _oid
                elif _fill is not None:
                    _px, _when = _fill
                    t.status       = "stop"
                    t.exit_price   = _px
                    t.exit_at_et   = broker_time_to_et(_when)
                    t.exit_reason  = "broker fill (ghost close)"
                elif last_price is not None:
                    t.status       = "expired"
                    t.exit_price   = last_price
                    t.exit_at_et   = now_iso
                    t.exit_reason  = "ghost — broker no longer holds"
                else:
                    t.last_updated_et = now_iso
                    trades[t.trade_id] = t
                    still_open += 1
                    continue
                t.realized_pnl   = _pnl_for(t, t.exit_price)
                t.unrealized_pnl = 0.0
                expired += 1
                t.last_updated_et = now_iso
                trades[t.trade_id] = t
                continue

            if status:
                # Broker-held names never reach here (the override above
                # clears status). When the broker is unreachable this is
                # the simulated target/stop. When the exit order printed,
                # that average is the close — not the trigger that the
                # bar touched, and not a later quote.
                _hit = closing_fill(t, activity_rows)
                if _hit is not None:
                    _px, _when, _oid = _hit
                    t.exit_price   = _px
                    t.exit_at_et   = broker_time_to_et(_when)
                    t.exit_reason  = "broker avg fill"
                    if _oid:
                        t.exit_order_id = _oid
                    t.realized_pnl = _pnl_for(t, _px)
                    t.status = "target" if t.realized_pnl >= 0 else "stop"
                else:
                    t.status        = status
                    t.exit_price    = exit_price
                    t.exit_at_et    = exit_at
                    t.exit_reason   = "target hit" if status == "target" else "stop hit"
                    t.realized_pnl  = _pnl_for(t, exit_price)
                t.unrealized_pnl = 0.0
                if t.status == "target": closed_target += 1
                else:                    closed_stop   += 1
            else:
                # Still open — check expiry
                try:
                    opened_dt = datetime.fromisoformat(t.opened_at_et).replace(tzinfo=ET)
                    age_days  = (now_et - opened_dt).days
                except Exception:
                    age_days = 0
                # NEVER expire a position the broker still holds. This is
                # the same double-count the stop/target override fixed, in
                # the branch it did not cover: setting status=None routed
                # broker-held trades straight INTO this expiry path, which
                # booked their UNREALIZED gains as REALIZED while the
                # position stayed open — on 2026-08-04 that reported
                # +$13,178 "realized" against a real broker day of +$2,175,
                # an $11,003 phantom profit (RNG +$4,245, GOOGL +$2,280,
                # SAP +$1,620 were all still open).
                #
                # It also contradicted the strongest finding in the data:
                # the 5d+ holding bucket is the only profitable one, and a
                # 5-day forced expiry closed exactly those winners on paper
                # while their trailing stops kept running (RNG 11d, PLTR 36d).
                _broker_holds = (broker_open is not None
                                 and t.symbol.replace("/", "") in broker_open)
                if _broker_holds and age_days >= MAX_HOLD_DAYS:
                    log.debug(f"{t.symbol}: {age_days}d old but broker still holds it "
                              f"— not expiring (trail active)")
                if (not _broker_holds) and age_days >= MAX_HOLD_DAYS and last_price is not None:
                    # The broker no longer holds it. If a real fill exists,
                    # that fill IS the exit — price it and name it correctly.
                    # Only a position that left with no fill behind it was
                    # genuinely closed by hold-time expiry.
                    _fill = broker_fills.get(t.symbol.replace("/", ""))
                    _hit = closing_fill(t, activity_rows)
                    if _hit is not None:
                        _px, _when, _oid = _hit
                        t.exit_price   = _px
                        t.exit_at_et   = broker_time_to_et(_when)
                        t.exit_reason  = "broker avg fill"
                        if _oid:
                            t.exit_order_id = _oid
                        t.realized_pnl = _pnl_for(t, _px)
                        t.status = "target" if t.realized_pnl >= 0 else "stop"
                    elif _fill is not None:
                        _px, _when = _fill
                        t.status       = "stop"
                        t.exit_price   = _px
                        t.exit_at_et   = broker_time_to_et(_when)
                        t.exit_reason  = f"trail stop hit after {age_days}d"
                    else:
                        t.status       = "expired"
                        t.exit_price   = last_price
                        t.exit_at_et   = now_iso
                        t.exit_reason  = f"expired after {age_days}d"
                    t.realized_pnl   = _pnl_for(t, t.exit_price)
                    t.unrealized_pnl = 0.0
                    expired += 1
                else:
                    if last_price is not None:
                        t.current_price  = round(last_price, 2)
                        t.unrealized_pnl = _pnl_for(t, last_price)
                    still_open += 1

            t.last_updated_et = now_iso
            trades[t.trade_id] = t

    save_ledger(trades)
    return {
        "checked":        len(open_trades),
        "closed_target":  closed_target,
        "closed_stop":    closed_stop,
        "expired":        expired,
        "still_open":     still_open,
        "symbols_seen":   len(symbols_to_process),
    }


# ── Query API (used by the reporter) ─────────────────────────────────────────

def all_trades() -> list[Trade]:
    return sorted(load_ledger().values(), key=lambda t: t.opened_at_et)


def trades_on_date(date_str: str) -> list[Trade]:
    """date_str: 'YYYY-MM-DD' in ET"""
    return [t for t in all_trades() if t.opened_date_et == date_str]


def open_positions() -> list[Trade]:
    return [t for t in all_trades() if t.is_open]


def has_open_position(symbol: str, side: str) -> bool:
    """True if there's already an open trade for this symbol+side.
    Used to stop the ensemble from re-entering the same position every tick."""
    side = side.upper()
    return any(t.symbol == symbol and t.side == side for t in open_positions())


def epoch_start() -> str:
    """Line-in-the-sand date for agent EVALUATION (not reporting).

    Trades before this date were distorted by the duplicate-entry bug
    (one bad signal re-entered 10-18x per tick, fixed 2026-07-01) and a
    frozen risk circuit breaker — so per-agent P&L from that era measures
    the bugs, not the agents. Evaluator and MetaAgent weighting only count
    trades from this date; reports still show full history for pre/post
    comparison. Override with LEDGER_EPOCH_START in .env if ever needed.
    """
    import os
    return os.getenv("LEDGER_EPOCH_START", "2026-07-02")


def epoch_trades() -> list[Trade]:
    """All trades opened on/after the evaluation epoch."""
    cutoff = epoch_start()
    return [t for t in all_trades() if t.opened_at_et[:10] >= cutoff]


def closed_trades() -> list[Trade]:
    return [t for t in all_trades() if not t.is_open]


def record_trade(
    symbol: str, side: str, entry_price: float,
    target_price: float, stop_price: float, risk_dollar: float,
    shares: float, primary_agent: str, contributors: str = "",
    order_id: str = "",
    exit_order_id: str = "",
) -> str:
    """
    Write a new open trade directly to the ledger CSV.
    Called by order_executor after a successful Alpaca order submission.
    Returns the trade_id.
    """
    now_et = datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S")
    raw = f"{now_et}{symbol}{side.upper()}{entry_price}"
    trade_id = hashlib.sha1(raw.encode()).hexdigest()[:12]

    trades = load_ledger()
    if trade_id in trades:
        return trade_id  # idempotent — already recorded

    side_norm = "LONG" if side.upper() in LONG_SIDES else "SHORT"
    t = Trade(
        trade_id        = trade_id,
        opened_at_et    = now_et,
        symbol          = symbol,
        side            = side_norm,
        primary_agent   = primary_agent,
        contributors    = contributors,
        entry_price     = entry_price,
        target_price    = target_price,
        stop_price      = stop_price,
        risk_dollar     = risk_dollar,
        shares          = shares,
        status          = "open",
        entry_order_id  = str(order_id or ""),
        exit_order_id   = str(exit_order_id or ""),
    )
    trades[trade_id] = t
    save_ledger(trades)
    return trade_id


def daily_pnl_series() -> list[dict]:
    """Group ALL trades by opened-date. Return [{date, realized, unrealized, count}]."""
    by_date: dict[str, dict] = defaultdict(lambda: {
        "realized": 0.0, "unrealized": 0.0, "count": 0, "wins": 0, "losses": 0,
    })
    for t in all_trades():
        d = by_date[t.opened_date_et]
        d["count"] += 1
        d["realized"]   += t.realized_pnl
        d["unrealized"] += t.unrealized_pnl
        pnl = t.realized_pnl if not t.is_open else t.unrealized_pnl
        if pnl >= 0: d["wins"]   += 1
        else:        d["losses"] += 1
    out = []
    for date in sorted(by_date.keys()):
        d = by_date[date]
        d["date"]     = date
        d["total"]    = round(d["realized"] + d["unrealized"], 2)
        d["realized"] = round(d["realized"], 2)
        d["unrealized"] = round(d["unrealized"], 2)
        out.append(d)
    return out


def cumulative_pnl() -> dict:
    """Lifetime aggregates: total P&L, trade count, win rate, best/worst day."""
    series = daily_pnl_series()
    trades = all_trades()
    if not trades:
        return {
            "total_pnl": 0.0, "realized_pnl": 0.0, "unrealized_pnl": 0.0,
            "trade_count": 0, "open_count": 0, "closed_count": 0,
            "wins": 0, "losses": 0, "win_rate": 0.0,
            "best_day": None, "worst_day": None,
            "first_trade_date": None, "last_trade_date": None,
            "trading_days": 0,
        }
    realized   = sum(t.realized_pnl for t in trades)
    unrealized = sum(t.unrealized_pnl for t in trades)
    closed     = [t for t in trades if not t.is_open]
    wins       = sum(1 for t in closed if t.realized_pnl >= 0)
    losses     = len(closed) - wins
    best  = max(series, key=lambda d: d["total"]) if series else None
    worst = min(series, key=lambda d: d["total"]) if series else None
    return {
        "total_pnl":       round(realized + unrealized, 2),
        "realized_pnl":    round(realized, 2),
        "unrealized_pnl":  round(unrealized, 2),
        "trade_count":     len(trades),
        "open_count":      sum(1 for t in trades if t.is_open),
        "closed_count":    len(closed),
        "wins":            wins,
        "losses":          losses,
        "win_rate":        round(wins / len(closed) * 100, 1) if closed else 0.0,
        "best_day":        best,
        "worst_day":       worst,
        "first_trade_date": trades[0].opened_date_et,
        "last_trade_date":  trades[-1].opened_date_et,
        "trading_days":    len(series),
    }


def per_agent_attribution() -> list[dict]:
    """Leaf-agent aggregates after MetaAgent unwrap.

    Wrapper labels (``MetaAgent``, ``MetaAgent(...)``, ``BrokerSync``) are
    never rows. A trade stored as ``MetaAgent(NewsAgent, OptionsFlowAgent)``
    credits NewsAgent and OptionsFlowAgent with the real P&L — the roster
    used to show $0 on those leaves while the wrapper held the only entry.
    """
    by_agent: dict[str, dict] = defaultdict(lambda: {
        "agent": "", "trades_total": 0, "trades_open": 0, "trades_closed": 0,
        "wins": 0, "losses": 0,
        "realized_pnl": 0.0, "unrealized_pnl": 0.0,
        "cost_total": 0.0,
        "as_primary": 0, "as_contributor": 0,
        "best_trade": None, "worst_trade": None,
        "_gross_pnls": [], "_costs": [],
    })
    for t in all_trades():
        primary = t.primary_leaves
        contrib = t.contributor_leaves
        leaves = t.leaf_agents
        if not leaves:
            continue
        cost = round_trip_cost(t)
        pnl = t.realized_pnl if not t.is_open else t.unrealized_pnl
        for agent in leaves:
            d = by_agent[agent]
            d["agent"] = agent
            d["trades_total"] += 1
            if agent in primary:
                d["as_primary"] += 1
            else:
                d["as_contributor"] += 1
            d["cost_total"] += cost
            d["_gross_pnls"].append(pnl)
            d["_costs"].append(cost)
            if t.is_open:
                d["trades_open"]    += 1
                d["unrealized_pnl"] += t.unrealized_pnl
            else:
                d["trades_closed"]  += 1
                d["realized_pnl"]   += t.realized_pnl
                if t.realized_pnl >= 0: d["wins"]   += 1
                else:                   d["losses"] += 1
            if d["best_trade"] is None or pnl > d["best_trade"]["pnl"]:
                d["best_trade"] = {
                    "symbol": t.symbol, "side": t.side, "pnl": round(pnl, 2),
                    "date": t.opened_date_et,
                }
            if d["worst_trade"] is None or pnl < d["worst_trade"]["pnl"]:
                d["worst_trade"] = {
                    "symbol": t.symbol, "side": t.side, "pnl": round(pnl, 2),
                    "date": t.opened_date_et,
                }
    out = []
    for d in by_agent.values():
        total_pnl = d["realized_pnl"] + d["unrealized_pnl"]
        d["total_pnl"]    = round(total_pnl, 2)
        d["realized_pnl"] = round(d["realized_pnl"], 2)
        d["unrealized_pnl"] = round(d["unrealized_pnl"], 2)
        d["cost_total"] = round(d["cost_total"], 2)
        d["pnl_after_costs"] = round(total_pnl - d["cost_total"], 2)
        d["expectancy_after_costs"] = expectancy_after_costs(
            d.pop("_gross_pnls"), d.pop("_costs")
        )
        if d["trades_closed"] > 0:
            d["win_rate"]   = round(d["wins"] / d["trades_closed"] * 100, 1)
            d["avg_pnl"]    = round(d["realized_pnl"] / d["trades_closed"], 2)
        else:
            d["win_rate"]   = 0.0
            d["avg_pnl"]    = 0.0
        out.append(d)
    # Rank by total_pnl descending (best first)
    return sorted(out, key=lambda x: -x["total_pnl"])


# ── CLI / cron entrypoint ───────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    print(f"[trade_ledger] base_dir = {BASE_DIR}")
    print(f"[trade_ledger] scheduler log = {SCHEDLOG} (exists: {SCHEDLOG.exists()})")
    print(f"[trade_ledger] ledger file = {LEDGER}")

    added, total = parse_log()
    print(f"[trade_ledger] parse_log: +{added} new trades  ({total} total in ledger)")

    if "--refresh-prices" in sys.argv or "--refresh" in sys.argv:
        print("[trade_ledger] refreshing open positions...")
        result = refresh_open_positions()
        print(f"[trade_ledger] {result}")

    if "--summary" in sys.argv:
        cum = cumulative_pnl()
        print("\n=== CUMULATIVE ===")
        for k, v in cum.items():
            print(f"  {k:20} {v}")
        print("\n=== PER-AGENT (top 10 by P&L) ===")
        for row in per_agent_attribution()[:10]:
            print(f"  {row['agent']:25} trades={row['trades_total']:4} "
                  f"pnl=${row['total_pnl']:>+10.2f}  win%={row['win_rate']:>5.1f}")
