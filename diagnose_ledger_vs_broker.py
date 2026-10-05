"""
diagnose_ledger_vs_broker.py — L-2026-10-01b
────────────────────────────────────────────
Read-only comparison of Alpaca paper fills to the trade ledger.

Rebuilds per-trade realized P&L from FILL activities (closed orders only
if no activities come back) and prints it next to ledger realized_pnl.
Does not submit, cancel, or close orders. Does not write the ledger.

    cd /home/mddnnbr/tading-bot && python3.11 diagnose_ledger_vs_broker.py

Keys: ALPACA_API_KEY / ALPACA_API_SECRET from the environment (.env on the
VM is fine; do not commit them). TradingClient is constructed paper=True.
"""

from __future__ import annotations

import sys

import broker_fills as bf


def format_report(rows: list[bf.CompareRow], *, day: str | None = None,
                        trips: list[bf.RoundTrip] | None = None,
                        fill_source: str = "",
                        ledger_realized_today: float | None = None) -> str:
    lines = [
        "READ-ONLY paper fill diagnostic (L-2026-10-01b)",
        "No orders submitted. Ledger not written.",
    ]
    if fill_source:
        lines.append(f"fill source: {fill_source}")
    matched = [r for r in rows if r.broker_pnl is not None]
    unmatched = [r for r in rows if r.broker_pnl is None]
    lines.append(
        f"ledger closed {len(rows)}  matched {len(matched)}  "
        f"unmatched (ledger fallback) {len(unmatched)}"
    )
    lines.append("")
    header = (
        f"{'symbol':<22} {'side':<6} {'ledger':>10} {'broker':>10} "
        f"{'delta':>10}  note"
    )
    lines.append(header)
    lines.append("-" * len(header))
    for row in rows:
        broker = "—" if row.broker_pnl is None else f"{row.broker_pnl:+.2f}"
        delta = "—" if row.broker_pnl is None else f"{row.broker_pnl - row.ledger_pnl:+.2f}"
        agents = ",".join(row.agents) or "—"
        lines.append(
            f"{row.symbol:<22} {row.side:<6} {row.ledger_pnl:+10.2f} {broker:>10} "
            f"{delta:>10}  {row.note}  [{agents}]"
        )
    lines.append("")
    deltas = bf.agent_pnl_deltas(rows)
    if deltas:
        lines.append("Per-agent broker minus 1/N ledger (positive = ledger understated):")
        for name, delta in sorted(deltas.items(), key=lambda kv: kv[1]):
            if delta > 1:
                direction = "ledger too low"
            elif delta < -1:
                direction = "ledger too high"
            else:
                direction = "in line"
            lines.append(f"  {name:<24} {delta:+10.2f}  {direction}")
    else:
        lines.append("Per-agent delta: no matched fills.")
    if day and trips is not None:
        broker_today = bf.realized_on_date(trips, day)
        lines.append("")
        lines.append(
            f"broker realized on {day} (full round trips exited that ET date): "
            f"${broker_today:+,.2f}"
        )
        if ledger_realized_today is not None:
            gap = abs(broker_today - float(ledger_realized_today))
            lines.append(
                f"ledger realized on {day}: ${float(ledger_realized_today):+,.2f}  "
                f"booking gap ${gap:,.2f}"
            )
        lines.append(
            "Unrealized intraday marks are not in this gap. "
            "Broker day equity change is mark-to-market, not lifetime realized."
        )
    return "\n".join(lines)


def run(client=None, trades=None, day: str | None = None) -> str:
    """Build the report. Pass client/trades in tests. Default path is the VM."""
    fill_source = ""
    if client is None:
        client = bf.make_paper_client()
    if trades is None:
        import trade_ledger as tl
        # Open rows stay in the book so a closed twin can be recognized.
        trades = list(tl.all_trades())
    fills, fill_source = bf.fetch_fills(client)
    positions = bf.fetch_open_positions(client)
    open_symbols = {
        str(bf._get(p, "symbol", default=""))
        for p in positions
        if bf._get(p, "symbol")
    }
    trips = bf.build_round_trips(fills)
    rows = bf.compare_ledger(
        trades, trips, broker_open=open_symbols, broker_positions=positions,
    )
    ledger_today = None
    if day:
        ledger_today = round(sum(
            float(getattr(t, "realized_pnl", 0) or 0)
            for t in trades
            if not getattr(t, "is_open", False)
            and str(getattr(t, "exit_at_et", "") or "")[:10] == day
            and not bf.scoring_skip_reason(t, trades, positions)
        ), 2)
    return format_report(
        rows, day=day, trips=trips, fill_source=fill_source,
        ledger_realized_today=ledger_today,
    )


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    day = None
    if "--day" in argv:
        i = argv.index("--day")
        day = argv[i + 1]
    try:
        if day is None:
            from datetime import datetime
            from trade_ledger import ET
            day = datetime.now(ET).strftime("%Y-%m-%d")
        text = run(day=day)
    except Exception as e:
        print(f"diagnostic failed: {bf.redact(e)}", file=sys.stderr)
        return 2
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
