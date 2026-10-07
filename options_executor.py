"""
options_executor.py
────────────────────
Defined-risk leverage: buy calls/puts instead of shares on high-conviction
signals.

Why this exists (2026-07-30): margin leverage nearly ruined the account —
running 2.43x on equities turned a -2.5% SPY day into -15%, because a
share position's downside is unbounded and gaps blow through stops
(AMKR gapped -14.5% overnight and lost 4x its intended risk). A long
option's maximum loss is the premium paid. Full stop. No margin call, no
gap risk, no overnight tail. That is the only form of leverage that
belongs in this account until expectancy is proven.

Trade-offs this ACCEPTS (they are real, not hidden):
  • A losing option often goes to ZERO — 100% loss is normal, where a
    stopped-out share position loses ~4%. Sizing must reflect that.
  • Theta: the position bleeds value every day even if price is flat.
    Mitigated by 30-45 DTE entries and closing at <=10 DTE.
  • Spreads are wider than stocks — enforced via a max-spread filter.

Risk model:
  • Premium paid per trade IS the max loss, capped at OPTIONS_RISK_PCT of
    equity (default 1% ~= $900). This is the "only risk our principal"
    property the equity book never had.
  • Only signals at or above OPTIONS_MIN_CONFIDENCE route here; everything
    else still trades shares. Options are the conviction expression, not
    the default.

CHANGE LOG (L-2026-10-05, option profit ratchet):
  A long option is market-flattened only on -50% of premium, CLOSE_DTE,
  or a filled broker stop. +100% and +150% move that stop; they do not
  market-sell. At mark or last >= 2× entry the sell stop goes to entry
  (break-even). At >= 2.5× entry it goes to 1.5× entry (lock +50%).
  The stop is only ever raised, and the old order is cancelled first
  so a lower stop is not left resting under the new one.

CHANGE LOG (L-2026-10-01b):
  Equity trailing stops, ATR stops, and hold-time expiry do not apply.
  The exit mark is the quote mid, else the mark implied by dollar P&L
  — not a last print sitting on the bid. The protective stop is
  cancelled only when a premium rule is actually closing the contract,
  so Alpaca does not reject the close as uncovered.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import date, timedelta

from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger("OptionsExecutor")

OPTIONS_ENABLED        = os.getenv("OPTIONS_ENABLED", "true").lower() == "true"
OPTIONS_MIN_CONFIDENCE = float(os.getenv("OPTIONS_MIN_CONFIDENCE", "0.70"))
OPTIONS_RISK_PCT       = float(os.getenv("OPTIONS_RISK_PCT", "1.0"))   # % of equity per trade
MIN_DTE, MAX_DTE       = 25, 50      # entry window: enough time for the thesis
CLOSE_DTE              = 10          # exit before theta accelerates
MAX_SPREAD_PCT         = 15.0        # skip illiquid contracts
STOP_LOSS_MULT         = 0.50        # protective stop, and the market flatten, at -50%
# Profit ratchet on the resting sell stop. These are not market flattens:
# a market sell at the same mark would cancel the stop this ratchet just set.
RATCHET_BREAKEVEN_MARK = 2.0         # +100%: mark or last >= 2× entry
RATCHET_BREAKEVEN_STOP = 1.0         # stop at entry (break-even)
RATCHET_LOCK_MARK      = 2.5         # +150%: mark or last >= 2.5× entry
RATCHET_LOCK_STOP      = 1.5         # stop at 1.5× entry (lock +50%)
PROFIT_TAKE_MULT       = RATCHET_BREAKEVEN_MARK  # name kept; now a stop step
_OCC_EXPIRY            = re.compile(r"(\d{6})[CP]")


def occ_expiry(symbol: str) -> date | None:
    """Expiration date embedded in an OCC symbol, or None."""
    m = _OCC_EXPIRY.search(str(symbol or ""))
    if not m:
        return None
    raw = m.group(1)
    try:
        return date(2000 + int(raw[:2]), int(raw[2:4]), int(raw[4:6]))
    except ValueError:
        return None


def occ_dte(symbol: str, today: date | None = None) -> int | None:
    exp = occ_expiry(symbol)
    if exp is None:
        return None
    return (exp - (today or date.today())).days


def premium_mark(position, quote_mid: float | None = None) -> float | None:
    """Per-share premium for the +100% / -50% rules.

    Quote mid wins. Otherwise the mark implied by dollar P&L, which
    stays in premium units. ``current_price`` is the last print: on a
    wide spread it sits on the bid and looks like a -50% stop while
    the position is only slightly red. That is the check that fired
    on CCL261030C00026000 at 15:50 ET on 2026-09-30.
    """
    if quote_mid is not None:
        try:
            mid = float(quote_mid)
        except (TypeError, ValueError):
            mid = 0.0
        if mid > 0:
            return mid
    try:
        qty = abs(float(getattr(position, "qty", 0) or 0))
        basis = abs(float(getattr(position, "avg_entry_price", 0) or 0))
    except (TypeError, ValueError):
        qty, basis = 0.0, 0.0
    upl_raw = getattr(position, "unrealized_pl", None)
    if qty > 0 and basis > 0 and upl_raw is not None:
        try:
            cost = basis * qty * 100.0
            if cost > 0:
                mark = basis * (1.0 + float(upl_raw) / cost)
                if mark > 0:
                    return mark
        except (TypeError, ValueError):
            pass
    try:
        cur = float(getattr(position, "current_price", 0) or 0)
    except (TypeError, ValueError):
        cur = 0.0
    return cur if cur > 0 else None


def option_exit_reason(symbol: str, entry_premium: float, mark: float | None,
                       today: date | None = None) -> str | None:
    """Why this long option must be market-flattened, or None to keep it.

    Software flattens are only -50% of premium and CLOSE_DTE. +100% and
    +150% ratchet the protective stop (see long_option_stop_target);
    they are not market sells. Equity trailing stops, ATR stops, and
    MAX_HOLD_DAYS are not reasons. A filled broker stop is already flat
    at the broker; this function does not invent that fill.
    """
    try:
        entry = float(entry_premium)
    except (TypeError, ValueError):
        entry = 0.0
    ratio = None
    if entry > 0 and mark is not None:
        try:
            px = float(mark)
        except (TypeError, ValueError):
            px = 0.0
        if px > 0:
            ratio = px / entry
    if ratio is not None and ratio <= STOP_LOSS_MULT:
        return f"stop -{(1 - ratio) * 100:.0f}%"
    dte = occ_dte(symbol, today)
    if dte is not None and dte <= CLOSE_DTE:
        return f"{dte}d to expiry — theta guard"
    return None


def ratchet_observation(position, quote_mid: float | None = None) -> float | None:
    """Premium used for the profit ratchet: the higher of mark and last.

    Either print at a threshold moves the stop. The -50% flatten still
    uses premium_mark alone, so a bid last cannot fake a stop-out.
    """
    mark = premium_mark(position, quote_mid=quote_mid)
    try:
        last = float(getattr(position, "current_price", 0) or 0)
    except (TypeError, ValueError):
        last = 0.0
    vals = []
    if mark is not None and mark > 0:
        vals.append(float(mark))
    if last > 0:
        vals.append(last)
    return max(vals) if vals else None


def long_option_stop_target(entry: float, mark: float | None) -> float:
    """Sell-stop for a long option.

    Below +100% the stop stays at -50% of premium. At +100% it moves to
    entry. At +150% it moves to 1.5× entry. This never returns a stop
    below the -50% floor.
    """
    try:
        basis = abs(float(entry))
    except (TypeError, ValueError):
        basis = 0.0
    floor = round(max(basis * STOP_LOSS_MULT, 0.01), 2) if basis > 0 else 0.01
    if basis <= 0 or mark is None:
        return floor
    try:
        ratio = float(mark) / basis
    except (TypeError, ValueError):
        return floor
    if ratio >= RATCHET_LOCK_MARK:
        return round(max(basis * RATCHET_LOCK_STOP, floor), 2)
    if ratio >= RATCHET_BREAKEVEN_MARK:
        return round(max(basis * RATCHET_BREAKEVEN_STOP, floor), 2)
    return floor


def ratchet_floor(existing: float | None, target: float, *, long: bool = True) -> float:
    """Never loosen a protective stop.

    Long premium: a higher price locks more. A later mark that only
    qualifies for break-even must not walk a +50% lock back down.
    """
    try:
        tgt = float(target)
    except (TypeError, ValueError):
        return 0.01
    if existing is None:
        return round(tgt, 2)
    try:
        cur = float(existing)
    except (TypeError, ValueError):
        return round(tgt, 2)
    if cur <= 0:
        return round(tgt, 2)
    if long:
        return round(max(cur, tgt), 2)
    return round(min(cur, tgt), 2)


def stop_replace_decision(existing: float | None, has_order: bool, target: float,
                          *, long: bool = True) -> str:
    """``place``, ``raise``, or ``keep``.

    A resting order whose price we cannot read is kept. Replacing it
    could stack a second stop or walk protection the wrong way. A cent
    of rounding is the same stop and is not cancelled.
    """
    if not has_order:
        return "place"
    if existing is None:
        return "keep"
    tightened = ratchet_floor(existing, target, long=long)
    if long:
        return "raise" if tightened > float(existing) + 0.009 else "keep"
    return "raise" if tightened < float(existing) - 0.009 else "keep"


def _order_field(order, name, default=None):
    if isinstance(order, dict):
        return order.get(name, default)
    return getattr(order, name, default)


def _order_side_name(order) -> str:
    return str(_order_field(order, "side", "") or "").lower().split(".")[-1]


def _order_stop_price(order) -> float | None:
    raw = _order_field(order, "stop_price", None)
    if raw is None:
        raw = _order_field(order, "stop", None)
    try:
        if raw is None or raw == "":
            return None
        px = float(raw)
    except (TypeError, ValueError):
        return None
    return px if px > 0 else None


def closing_option_orders(orders, signed: float, symbol: str) -> list:
    out = []
    for order in orders or []:
        sym = str(_order_field(order, "symbol", "") or "")
        if sym and sym != symbol:
            continue
        side = _order_side_name(order)
        if signed > 0 and side == "sell":
            out.append(order)
        elif signed < 0 and side == "buy":
            out.append(order)
    return out


def tightest_stop_price(orders, *, long: bool) -> float | None:
    prices = [px for px in (_order_stop_price(o) for o in orders) if px is not None]
    if not prices:
        return None
    return max(prices) if long else min(prices)


def _clients():
    from alpaca.trading.client import TradingClient
    from alpaca.data.historical.option import OptionHistoricalDataClient
    k, s = os.getenv("ALPACA_API_KEY", ""), os.getenv("ALPACA_API_SECRET", "")
    if not k or not s:
        return None, None
    return (TradingClient(api_key=k, secret_key=s, paper=True),
            OptionHistoricalDataClient(api_key=k, secret_key=s))


def _quote(data_client, symbol: str):
    """Return (bid, ask, mid, spread_pct) for an option symbol."""
    from alpaca.data.requests import OptionLatestQuoteRequest
    q = data_client.get_option_latest_quote(
        OptionLatestQuoteRequest(symbol_or_symbols=symbol))[symbol]
    bid, ask = float(q.bid_price or 0), float(q.ask_price or 0)
    if bid <= 0 or ask <= 0:
        return None
    mid = (bid + ask) / 2
    return bid, ask, mid, (ask - bid) / mid * 100


def select_contract(client, data_client, symbol: str, direction: str,
                    underlying_price: float):
    """Pick a liquid, near-the-money contract 25-50 days out.

    Near-the-money balances leverage against probability: deep OTM is a
    lottery ticket, deep ITM costs nearly as much as the stock.
    """
    from alpaca.trading.requests import GetOptionContractsRequest
    from alpaca.trading.enums import ContractType, AssetStatus

    ctype = ContractType.CALL if direction == "long" else ContractType.PUT
    try:
        res = client.get_option_contracts(GetOptionContractsRequest(
            underlying_symbols=[symbol],
            status=AssetStatus.ACTIVE,
            type=ctype,
            expiration_date_gte=date.today() + timedelta(days=MIN_DTE),
            expiration_date_lte=date.today() + timedelta(days=MAX_DTE),
            strike_price_gte=str(round(underlying_price * 0.90, 2)),
            strike_price_lte=str(round(underlying_price * 1.10, 2)),
            limit=100,
        ))
    except Exception as e:
        log.warning(f"options: contract lookup failed for {symbol}: {e}")
        return None

    contracts = list(res.option_contracts or [])
    if not contracts:
        log.info(f"options: no contracts in window for {symbol}")
        return None

    # Nearest expiry in the window, then strike closest to spot
    contracts.sort(key=lambda c: (c.expiration_date,
                                  abs(float(c.strike_price) - underlying_price)))
    for c in contracts[:6]:
        q = _quote(data_client, c.symbol)
        if not q:
            continue
        bid, ask, mid, spread = q
        if spread > MAX_SPREAD_PCT:
            continue
        return {"symbol": c.symbol, "strike": float(c.strike_price),
                "expiry": str(c.expiration_date), "mid": mid, "ask": ask,
                "spread_pct": round(spread, 1)}
    log.info(f"options: no liquid contract for {symbol} (spreads too wide)")
    return None


def _record_option_open(signal: dict, contract: dict, qty: float,
                        premium: float, order_id: str) -> str:
    """Ledger row for attribution. The symbol is the contract, not the underlying.

    The equity entry gate keys off the underlying, so this row does not
    change that gate. Scoring and the email name the round trip from the
    agent and the order ids stored here.
    """
    try:
        import trade_ledger as tl
        stop = round(max(float(premium) * STOP_LOSS_MULT, 0.01), 4)
        return tl.record_trade(
            symbol=contract["symbol"],
            side="long",
            entry_price=float(premium),
            target_price=round(float(premium) * RATCHET_BREAKEVEN_MARK, 4),
            stop_price=stop,
            risk_dollar=round(abs(float(qty) * float(premium) * 100.0), 2),
            shares=float(qty),
            primary_agent=signal.get("agent") or "MetaAgent",
            contributors=str(
                signal.get("contributing_agents") or signal.get("contributors") or ""
            ),
            order_id=str(order_id or ""),
        )
    except Exception as exc:
        log.warning("options: ledger row not written (%s)", type(exc).__name__)
        return ""


def _stamp_option_exit_order(symbol: str, order_id: str) -> None:
    """Remember the resting exit on the open contract row. A later fill wins."""
    if not symbol or not order_id:
        return
    try:
        import trade_ledger as tl
        trades = tl.load_ledger()
        changed = False
        for trade in trades.values():
            if not trade.is_open or trade.symbol != symbol:
                continue
            if trade.exit_order_id == order_id:
                return
            trade.exit_order_id = order_id
            changed = True
            break
        if changed:
            tl.save_ledger(trades)
    except Exception as exc:
        log.warning("options: exit order id not stored (%s)", type(exc).__name__)


def execute_options_trade(signal: dict) -> dict | None:
    """Buy calls/puts for a high-conviction signal. Returns result or None
    (None means the caller should fall back to the equity path)."""
    if not OPTIONS_ENABLED:
        return None
    conf = float(signal.get("raw_confidence") or signal.get("confidence") or 0)
    try:
        from auto_tune import load as _oe_cfg
        _thr = float(_oe_cfg().get("options_min_confidence", OPTIONS_MIN_CONFIDENCE))
    except Exception:
        _thr = OPTIONS_MIN_CONFIDENCE
    if conf < _thr:
        return None
    symbol = signal.get("symbol", "")
    if not symbol or "/" in symbol:          # crypto has no options here
        return None

    client, data_client = _clients()
    if client is None:
        return None

    try:
        # DEDUP — the ledger row below is the contract symbol, so the
        # ensemble's has_open_position() gate on the underlying still
        # cannot see it. On 2026-07-30 that let WOLF puts stack to 4
        # contracts across two strikes (~$2,000 exposure on an $890
        # budget) and SOFI calls to 18. The -$2,150 "single" WOLF loss
        # was really four stacked entries. Check the broker directly
        # for any live option on this underlying before adding another.
        try:
            for _p in client.get_all_positions():
                _s = str(_p.symbol)
                if len(_s) > 12 and _s.startswith(symbol):
                    log.info(f"options: {symbol} already has an open contract "
                             f"({_s}) — skipping to prevent stacking")
                    return {"status": "skipped_duplicate", "symbol": _s}
        except Exception as _de:
            log.warning(f"options dedup check failed, refusing entry: {_de}")
            return {"status": "dedup_unavailable"}

        equity = float(client.get_account().equity)
        opt_bp = float(getattr(client.get_account(), "options_buying_power", 0) or 0)
        entry  = float(signal.get("entry_price") or 0)
        if entry <= 0:
            return None

        contract = select_contract(client, data_client, symbol,
                                   signal.get("direction", "long"), entry)
        if not contract:
            return None

        # Premium IS the max loss. OPTIONS_RISK_PCT (1% of equity) is
        # ~$1,000 on this account, which is past the $320 hard cap.
        # One contract that costs more than the cap is skipped (qty 0),
        # not forced through.
        from risk_caps import option_contracts, risk_per_trade_usd
        risk_budget = min(equity * (OPTIONS_RISK_PCT / 100), risk_per_trade_usd())
        per_contract = contract["ask"] * 100
        qty = option_contracts(contract["ask"], risk_budget)
        if qty < 1:
            log.info(f"options: {symbol} contract ${per_contract:,.0f} exceeds "
                     f"${risk_budget:,.0f} risk budget — skipping")
            return None
        cost = qty * per_contract
        if cost > opt_bp:
            log.info(f"options: {symbol} cost ${cost:,.0f} > options BP ${opt_bp:,.0f}")
            return None

        from alpaca.trading.requests import MarketOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce
        order = client.submit_order(MarketOrderRequest(
            symbol=contract["symbol"], qty=qty, side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
        ))
        fill_px = float(contract["ask"])
        filled_qty = float(qty)
        try:
            live = client.get_order_by_id(order.id)
            px = float(getattr(live, "filled_avg_price", 0) or 0)
            fq = float(getattr(live, "filled_qty", 0) or 0)
            if px > 0:
                fill_px = px
            if fq > 0:
                filled_qty = fq
        except Exception:
            pass
        order_id = str(order.id)
        log.info(
            f"🎯 OPTIONS: {contract['symbol']} x{filled_qty:g} @ ~${fill_px:.2f} "
            f"| cost ${cost:,.0f} = MAX LOSS | strike {contract['strike']} "
            f"exp {contract['expiry']} spread {contract['spread_pct']}% "
            f"| conf {conf:.2f} | order_id={order_id} "
            f"| agent={signal.get('agent','?')}"
        )
        _record_option_open(signal, contract, filled_qty, fill_px, order_id)
        return {"status": "submitted", "instrument": "option",
                "order_id": order_id, "symbol": contract["symbol"],
                "underlying": symbol, "qty": filled_qty, "premium": fill_px,
                "max_loss": round(filled_qty * fill_px * 100.0, 2)}
    except Exception as e:
        log.warning(f"options: execution failed for {symbol}: {e}")
        return None


def submit_option_protective_stop(client, position, stop_price=None, mark=None) -> dict:
    """Place a stop that caps a long option.

    With no ``stop_price``, a long uses the profit ratchet (break-even at
    +100%, lock +50% at +150%) or the -50% floor when the mark is below
    that. Pass ``stop_price`` to replace a lower stop with a known target
    or to restore the previous stop after a rejected raise.

    Alpaca rejects trailing stops on option contracts. A plain stop is the
    protective exit the broker can actually hold. GTC is tried first, then
    DAY (many option routes only accept DAY). Both failures come back as
    placed=False so the caller can say the broker refused, instead of
    claiming a trailing stop is active.
    """
    sym = str(getattr(position, "symbol", "") or "")
    try:
        signed = float(position.qty)
        basis = abs(float(position.avg_entry_price))
    except (TypeError, ValueError, AttributeError) as e:
        return {"placed": False, "error": f"bad option position: {e}",
                "stop_price": None, "qty": 0}
    qty = abs(int(signed))
    if qty < 1 or basis <= 0:
        return {"placed": False, "error": "qty or premium is zero",
                "stop_price": None, "qty": qty}
    # Long premium: sell stop. Short premium is not how this book enters,
    # but a buy stop above the credit is the mirror. The ratchet is long-only.
    if stop_price is not None:
        try:
            stop_px = round(max(float(stop_price), 0.01), 2)
        except (TypeError, ValueError):
            stop_px = round(max(basis * STOP_LOSS_MULT, 0.01), 2)
        side_name = "sell" if signed > 0 else "buy"
    elif signed > 0:
        if mark is None:
            mark = ratchet_observation(position)
        stop_px = long_option_stop_target(basis, mark)
        side_name = "sell"
    else:
        stop_px = round(basis / STOP_LOSS_MULT, 2)
        side_name = "buy"
    try:
        from alpaca.trading.requests import StopOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce
    except ImportError as e:
        return {"placed": False, "error": f"alpaca stop unavailable: {e}",
                "stop_price": stop_px, "qty": qty}
    side = OrderSide.SELL if side_name == "sell" else OrderSide.BUY
    errors = []
    for tif in (TimeInForce.GTC, TimeInForce.DAY):
        try:
            order = client.submit_order(StopOrderRequest(
                symbol=sym, qty=qty, side=side,
                time_in_force=tif, stop_price=stop_px,
            ))
            oid = str(getattr(order, "id", "") or "")
            log.info(
                "options stop %s exit_order_id=%s stop=%s qty=%s",
                sym, oid, stop_px, qty,
            )
            _stamp_option_exit_order(sym, oid)
            return {
                "placed": True,
                "order_id": oid,
                "stop_price": stop_px,
                "qty": qty,
                "tif": str(tif),
                "error": "",
            }
        except Exception as e:
            errors.append(f"{tif}: {e}")
    return {
        "placed": False,
        "stop_price": stop_px,
        "qty": qty,
        "error": "; ".join(errors) or "broker rejected options stop",
    }


def _load_symbol_orders(client, symbol: str) -> list:
    try:
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest
        return list(client.get_orders(GetOrdersRequest(
            status=QueryOrderStatus.OPEN, symbols=[symbol], limit=50)))
    except Exception:
        try:
            return [
                o for o in client.get_orders()
                if str(_order_field(o, "symbol", "") or "") in ("", symbol)
            ]
        except Exception as e:
            log.warning(f"options: could not list orders for {symbol}: {e}")
            return []


def sync_option_protective_stop(client, position, orders=None, mark=None) -> dict:
    """Place or raise the long-option sell stop. Never stack, never lower.

    Default stop is -50% of premium. At +100% (mark or last >= 2× entry)
    the stop moves to entry. At +150% (>= 2.5×) it moves to 1.5× entry.
    An existing lower stop is cancelled and replaced by one order. An
    equal or tighter stop is left alone. If the raise is rejected, the
    previous stop is put back so the contract is not left naked.
    """
    sym = str(getattr(position, "symbol", "") or "")
    try:
        signed = float(position.qty)
        basis = abs(float(position.avg_entry_price))
    except (TypeError, ValueError, AttributeError) as e:
        return {"action": "refused", "placed": False, "error": str(e),
                "stop_price": None, "qty": 0, "previous": None}
    if orders is None:
        orders = _load_symbol_orders(client, sym)
    closing = closing_option_orders(orders, signed, sym)
    long = signed > 0
    if mark is None and long:
        mark = ratchet_observation(position)
    if long:
        target = long_option_stop_target(basis, mark)
    else:
        target = round(basis / STOP_LOSS_MULT, 2) if basis > 0 else None
    existing = tightest_stop_price(closing, long=long)
    decision = stop_replace_decision(
        existing, bool(closing), target or 0.0, long=long,
    )
    qty = abs(int(signed)) if signed else 0
    if decision == "keep" or target is None:
        return {
            "action": "keep", "placed": False, "stop_price": existing,
            "qty": qty, "error": "", "previous": existing,
        }
    if decision == "raise":
        for order in closing:
            oid = str(_order_field(order, "id", "") or "")
            if not oid:
                continue
            try:
                client.cancel_order_by_id(oid)
            except Exception as e:
                log.warning(f"options: cancel {sym} before ratchet failed: {e}")
    result = submit_option_protective_stop(
        client, position, stop_price=target, mark=mark,
    )
    if not result.get("placed") and decision == "raise" and existing:
        restored = submit_option_protective_stop(
            client, position, stop_price=existing, mark=mark,
        )
        return {
            **result,
            "action": "refused",
            "restored": bool(restored.get("placed")),
            "previous": existing,
        }
    result["action"] = "raised" if decision == "raise" and result.get("placed") else (
        "placed" if result.get("placed") else "refused"
    )
    result["previous"] = existing
    return result


def _cancel_symbol_orders(client, sym: str) -> None:
    """Cancel resting orders so a close is not an extra uncovered sell.

    Alpaca rejects close_position with 40310000 ("account not eligible
    to trade uncovered option contracts") when a protective stop already
    sells the full quantity. Cancel that stop first, and only when a
    premium rule is actually exiting.
    """
    orders = []
    try:
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest
        orders = list(client.get_orders(GetOrdersRequest(
            status=QueryOrderStatus.OPEN, symbols=[sym], limit=50)))
    except Exception:
        try:
            orders = list(client.get_orders())
        except Exception as e:
            log.warning(f"options: could not list orders for {sym}: {e}")
            return
    for o in orders:
        if str(getattr(o, "symbol", "")) != sym:
            continue
        try:
            client.cancel_order_by_id(o.id)
        except Exception as e:
            log.warning(f"options: cancel {sym} before exit failed: {e}")


def _flatten_option(client, sym: str) -> None:
    _cancel_symbol_orders(client, sym)
    client.close_position(sym)


def manage_options_exits(today: date | None = None) -> None:
    """Exit rules for open option positions.

    Options can't use Alpaca trailing stops. Market-flatten at -50% and
    at CLOSE_DTE. At +100% / +150% raise the resting sell stop (break-even,
    then lock +50%) instead of selling the bid. Nothing else — not the
    underlying's stop, not a time stop, not a small mark-to-market loss.
    """
    if not OPTIONS_ENABLED:
        return
    client, data_client = _clients()
    if client is None:
        return
    try:
        positions = [p for p in client.get_all_positions()
                     if str(getattr(p, "asset_class", "")).endswith("option")
                     or len(str(p.symbol)) > 12]
    except Exception as e:
        log.warning(f"options: position fetch failed: {e}")
        return

    for p in positions:
        sym = str(p.symbol)
        flattened = False
        try:
            cost_basis = abs(float(p.avg_entry_price))
            if cost_basis <= 0:
                continue
            quote_mid = None
            if data_client is not None:
                try:
                    q = _quote(data_client, sym)
                    if q:
                        quote_mid = q[2]
                except Exception:
                    quote_mid = None
            mark = premium_mark(p, quote_mid=quote_mid)
            reason = option_exit_reason(sym, cost_basis, mark, today=today)
            if not reason:
                observed = ratchet_observation(p, quote_mid=quote_mid)
                result = sync_option_protective_stop(client, p, mark=observed)
                if result.get("action") in {"raised", "placed"} and result.get("placed"):
                    log.info(
                        f"🎯 OPTIONS RATCHET {sym}: stop ${result.get('stop_price')} "
                        f"(entry ${cost_basis:.2f} mark ${observed or 0:.2f})"
                    )
                elif result.get("action") == "refused":
                    log.warning(
                        f"options: {sym} protective stop not updated "
                        f"({result.get('error')})"
                    )
                continue

            flattened = True
            _flatten_option(client, sym)
            shown = mark if mark is not None else 0.0
            log.info(f"🎯 OPTIONS EXIT {sym}: {reason} "
                     f"(entry ${cost_basis:.2f} now ${shown:.2f}, "
                     f"P&L {float(p.unrealized_pl):+,.0f})")
        except Exception as e:
            log.warning(f"options: exit check failed for {sym}: {e}")
            if flattened:
                # The protective stop was cancelled so the close could
                # land. The close did not. Put the stop back.
                try:
                    submit_option_protective_stop(client, p)
                except Exception as re_err:
                    log.warning(f"options: could not restore stop on {sym}: {re_err}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    c, d = _clients()
    if c:
        a = c.get_account()
        print(f"equity ${float(a.equity):,.0f} | options BP "
              f"${float(getattr(a,'options_buying_power',0) or 0):,.0f} | "
              f"level {getattr(a,'options_trading_level','?')}")
        for sym, px, dr in (("AMD", 512.0, "long"), ("SPY", 750.0, "short")):
            ct = select_contract(c, d, sym, dr, px)
            print(f"{sym} {dr}: {ct}")
