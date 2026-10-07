"""
Attach entry_order_id and exit_order_id from logs/scheduler.log.

Dry-run by default. --apply copies the ledger to a timestamped backup,
then writes. Parses ORDER SUBMITTED lines for the entry id, and trail /
stop lines that log an exit order id. Does not print secrets.

    python scripts/backfill_order_ids.py
    python scripts/backfill_order_ids.py --apply
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

import trade_ledger as tl

# Timestamp, then the bot's ORDER SUBMITTED line. The id is whatever the
# log wrote after order_id= (Alpaca uses UUIDs; tests use shorter tokens).
_SUBMITTED = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})[,\.]\d+.*"
    r"ORDER SUBMITTED:\s+(?P<symbol>\S+)\s+(?P<side>LONG|SHORT|BUY|SELL)\b"
    r".*?order_id=(?P<oid>\S+)"
)
_TRAIL = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})[,\.]\d+.*"
    r"TRAIL SET:\s+(?P<symbol>\S+)\b.*?\(order\s+(?P<oid>[^)\s]+)\)"
)
# Symbol sits on the trail / options-stop phrase. A scan for any
# [A-Z]+ token also matches the "[INFO]" log level.
_EXIT_KEY = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})[,\.]\d+.*"
    r"(?:TRAIL SET:\s+|options stop\s+)(?P<symbol>\S+)\b.*?"
    r"exit_order_id=(?P<oid>\S+)"
)
_OPTIONS = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})[,\.]\d+.*"
    r"OPTIONS:\s+(?P<symbol>[A-Z0-9]+)\b"
)
_MATCH_SECONDS = 30 * 60


def _clean_id(raw: str) -> str:
    return str(raw or "").strip().rstrip(".,;")


def _side(raw: str) -> str:
    s = str(raw or "").upper()
    if s in {"SHORT", "SELL"}:
        return "SHORT"
    return "LONG"


def parse_order_events(text: str) -> list[dict]:
    """Entry and exit order ids in log order. No secrets are read from the line."""
    events: list[dict] = []
    for line in text.splitlines():
        submitted = _SUBMITTED.search(line)
        if submitted:
            events.append({
                "kind": "entry",
                "ts": submitted.group("ts"),
                "symbol": submitted.group("symbol").replace("/", ""),
                "side": _side(submitted.group("side")),
                "order_id": _clean_id(submitted.group("oid")),
            })
            continue
        if "OPTIONS:" in line and "order_id=" in line:
            opt = _OPTIONS.search(line)
            oid = re.search(r"order_id=(\S+)", line)
            if opt and oid:
                events.append({
                    "kind": "entry",
                    "ts": opt.group("ts"),
                    "symbol": opt.group("symbol"),
                    "side": "LONG",
                    "order_id": _clean_id(oid.group(1)),
                })
                continue
        # Trail lines carry both "(order id)" and exit_order_id=. Prefer
        # the phrase that names the symbol, and keep a single event.
        trail = _TRAIL.search(line)
        exit_key = _EXIT_KEY.search(line)
        chosen = trail or exit_key
        if chosen and "ORDER SUBMITTED" not in line:
            events.append({
                "kind": "exit",
                "ts": chosen.group("ts"),
                "symbol": chosen.group("symbol").replace("/", ""),
                "side": "",
                "order_id": _clean_id(chosen.group("oid")),
            })
    return [e for e in events if e["order_id"]]


def _opened_delta(trade, ts: str) -> float | None:
    try:
        opened = datetime.strptime(str(trade.opened_at_et)[:19], "%Y-%m-%d %H:%M:%S")
        stamp = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return abs((opened - stamp).total_seconds())


def _best_trade(trades: dict, event: dict, used: set[str]):
    best = None
    best_delta = None
    for trade in trades.values():
        if trade.trade_id in used:
            continue
        if str(trade.symbol).replace("/", "") != event["symbol"]:
            continue
        if event["side"] and trade.side != event["side"]:
            continue
        delta = _opened_delta(trade, event["ts"])
        if delta is None or delta > _MATCH_SECONDS:
            continue
        if best_delta is None or delta < best_delta:
            best = trade
            best_delta = delta
    return best


def plan_updates(trades: dict, events: list[dict]) -> list[dict]:
    """Updates that would fill a blank order id. Does not mutate `trades`."""
    updates: list[dict] = []
    used_entries: set[str] = set()
    used_exits: set[str] = set()
    claimed_ids: set[str] = set()
    for event in events:
        oid = event["order_id"]
        if oid in claimed_ids:
            continue
        if event["kind"] == "entry":
            trade = _best_trade(trades, event, used_entries)
            if trade is None or trade.entry_order_id:
                continue
            used_entries.add(trade.trade_id)
            claimed_ids.add(oid)
            updates.append({
                "trade_id": trade.trade_id,
                "field": "entry_order_id",
                "order_id": oid,
                "symbol": trade.symbol,
            })
        else:
            trade = _best_trade(trades, event, used_exits)
            if trade is None or trade.exit_order_id:
                continue
            if oid == (trade.entry_order_id or ""):
                continue
            used_exits.add(trade.trade_id)
            claimed_ids.add(oid)
            updates.append({
                "trade_id": trade.trade_id,
                "field": "exit_order_id",
                "order_id": oid,
                "symbol": trade.symbol,
            })
    return updates


def apply_updates(trades: dict, updates: list[dict]) -> int:
    by_id = {t.trade_id: t for t in trades.values()}
    n = 0
    for update in updates:
        trade = by_id.get(update["trade_id"])
        if trade is None:
            continue
        setattr(trade, update["field"], update["order_id"])
        n += 1
    return n


def backup_ledger(path: Path) -> Path:
    stamp = datetime.now(tl.ET).strftime("%Y%m%dT%H%M%S")
    dest = path.with_name(f"{path.name}.bak-{stamp}")
    shutil.copy2(path, dest)
    return dest


def format_plan(updates: list[dict]) -> str:
    if not updates:
        return "backfill: no blank order ids to fill"
    lines = [f"backfill: {len(updates)} order id(s) to write"]
    for update in updates:
        lines.append(
            f"  {update['symbol']} {update['trade_id']} "
            f"{update['field']}={update['order_id']}"
        )
    return "\n".join(lines)


def run(argv: list[str] | None = None, *, ledger_path: Path | None = None,
        log_path: Path | None = None) -> int:
    parser = argparse.ArgumentParser(description="Backfill ledger order ids from scheduler.log")
    parser.add_argument("--apply", action="store_true",
                        help="write the ledger after a timestamped backup")
    args = parser.parse_args(argv)
    if ledger_path is not None:
        tl.LEDGER = Path(ledger_path)
    path = Path(log_path) if log_path is not None else tl.SCHEDLOG
    if not tl.LEDGER.exists():
        print(f"backfill: no ledger at {tl.LEDGER}")
        return 0
    text = path.read_text(encoding="utf-8", errors="ignore") if path.exists() else ""
    trades = tl.load_ledger()
    updates = plan_updates(trades, parse_order_events(text))
    print(format_plan(updates))
    if not args.apply:
        print("dry-run: ledger not written (pass --apply to write)")
        return 0
    if not updates:
        return 0
    backup = backup_ledger(tl.LEDGER)
    apply_updates(trades, updates)
    tl.save_ledger(trades)
    print(f"applied {len(updates)} id(s); backup {backup.name}")
    return 0


def main(argv: list[str] | None = None) -> int:
    return run(argv)


if __name__ == "__main__":
    sys.exit(main())
