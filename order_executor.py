"""
order_executor.py
-----------------
Submits actual orders to Alpaca paper trading and records them in trade_ledger.

Supports:
  - Equity LONG:  bracket order (entry + stop-loss + take-profit in one call)
  - Equity SHORT: sell-to-open bracket order
  - Crypto LONG:  market order (Alpaca crypto doesn't support bracket orders)
  - Leveraged ETFs: treated as regular equities

Position sizing is driven by the approved_signal dict from AgentRiskBridge.

.env keys consumed:
  ALPACA_API_KEY       — paper trading API key (PA3EZ46Z9UUC)
  ALPACA_API_SECRET    — paper trading secret
  PAPER_TRADING        — must be "true" (live mode not wired yet)
  RISK_PER_TRADE       — dollar risk TO STOP per trade (default $320; not a notional cap)
  MAX_POSITION_PCT     — max % of live equity notional per trade (default 2.0)
  MAX_NOTIONAL_USD     — absolute notional hard-cap per trade (default 1500)
  SIZE_TILT_ENABLED    — evidence-gated 1.5× notional for qualifying agents
                         (default false). See size_tilt.py. Off leaves this
                         clamp exactly as it is ($1,500 and the 2% cap).
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Optional

from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger("OrderExecutor")

# ── Alpaca imports (alpaca-py) ────────────────────────────────────────────────
try:
    from alpaca.trading.client import TradingClient
    from alpaca.trading.requests import (
        MarketOrderRequest,
        LimitOrderRequest,
        GetOrdersRequest,
    )
    from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass, QueryOrderStatus
    _ALPACA_OK = True
except ImportError:
    _ALPACA_OK = False
    log.warning("alpaca-py not installed — orders will be logged only, not submitted")

# ── Config ────────────────────────────────────────────────────────────────────
# PAPER ONLY. Live trading is not wired; env flags cannot enable it.
# TradingClient is always constructed with paper=True regardless of env.
from invariants import paper_only_violation, naked_equity_symbols, is_crypto_symbol, is_option_symbol

_paper_violation = paper_only_violation()
if _paper_violation:
    log.critical(_paper_violation)
PAPER_TRADING     = True
ALPACA_API_KEY    = os.getenv("ALPACA_API_KEY", "")
ALPACA_API_SECRET = os.getenv("ALPACA_API_SECRET", "")
RISK_PER_TRADE    = float(os.getenv("RISK_PER_TRADE", "320"))  # $ risk to stop, NOT notional
MAX_POSITION_PCT  = float(os.getenv("MAX_POSITION_PCT", "2.0"))   # % of live equity notional
MAX_NOTIONAL_USD  = float(os.getenv("MAX_NOTIONAL_USD", "1500"))  # absolute notional hard-cap
DEFAULT_TRAIL_PCT = 4.0   # matches the ATR stop cap used at entry
PROTECT_RETRY_SEC = 120   # don't hammer Alpaca on a symbol that just rejected

# Symbols Alpaca handles as crypto (use notional sizing, no bracket)
CRYPTO_SYMBOLS = {"BTC/USD", "ETH/USD", "SOL/USD", "AVAX/USD", "DOGE/USD", "LTC/USD"}

# Fallback equity when the broker is unreachable ($100k * 2% = $2k default)
PORTFOLIO_VALUE   = float(os.getenv("ACCOUNT_BALANCE", "100000"))

# Per-symbol last-failed timestamp for the tick-level exit backstop.
_protect_failed_at: dict[str, float] = {}


class OrderExecutor:
    """
    Submits orders to Alpaca and records them in trade_ledger.
    Safe to call with PAPER_TRADING=true — all orders go to paper endpoint.
    """

    # After Alpaca rejects an order (insufficient buying power, bracket
    # conflicts, etc.), don't retry that symbol/side for this long. Failed
    # orders never reach the ledger, so the dedup gate can't see them —
    # without this, one rejected signal got re-approved and re-submitted
    # every 60s tick all day (1,830 doomed submissions on 2026-07-08).
    FAILURE_COOLDOWN_SEC = 3600

    def __init__(self):
        self._failed_at: dict[tuple[str, str], float] = {}
        if not _ALPACA_OK:
            self._client = None
            return
        if not ALPACA_API_KEY or not ALPACA_API_SECRET:
            log.error("ALPACA_API_KEY / ALPACA_API_SECRET not set — cannot submit orders")
            self._client = None
            return
        # Always the paper endpoint. PAPER_TRADING=false / TRADING_MODE=live
        # used to flip this to live — that path is hard-disabled.
        self._client = TradingClient(
            api_key=ALPACA_API_KEY,
            secret_key=ALPACA_API_SECRET,
            paper=True,
        )
        log.info("OrderExecutor ready — paper=True (live trading is not enabled)")

    def _portfolio_equity(self) -> float:
        """Live paper equity for notional clamps; falls back to ACCOUNT_BALANCE."""
        try:
            if self._client is not None:
                acct = self._client.get_account()
                eq = float(getattr(acct, "equity", 0) or 0)
                if 1_000 <= eq <= 100_000_000:
                    return eq
        except Exception as e:
            log.warning(f"live equity unavailable for notional clamp ({e})")
        return PORTFOLIO_VALUE

    # ── Public entry point ─────────────────────────────────────────────────────
    def execute(self, approved_signal: dict) -> dict:
        """
        Execute a paper trade for an approved signal.

        approved_signal keys (from AgentRiskBridge):
          symbol, direction, confidence, entry_price,
          stop_loss_price, target_price, agent, position_size_usd

        Returns a result dict with status, order_id (if submitted), and trade_id.
        """
        symbol    = approved_signal.get("symbol", "")
        direction = approved_signal.get("direction", "long").lower()
        entry     = float(approved_signal.get("entry_price", 0))
        stop      = float(approved_signal.get("stop_loss_price", 0))
        target    = float(approved_signal.get("target_price", 0))
        agent     = approved_signal.get("agent", "Unknown")
        # Prefer the actual sizing AgentRiskBridge computed (risk-based,
        # accounts for stop distance) over a flat default. Previously this
        # always fell back to a fixed 2% notional regardless of what the
        # risk bridge decided, silently ignoring its sizing math.
        sizing    = approved_signal.get("position_sizing") or {}
        pos_usd   = float(sizing.get("total_cost") or approved_signal.get(
                          "position_size_usd", PORTFOLIO_VALUE * MAX_POSITION_PCT / 100))

        # Hard notional clamp. AgentRiskBridge sizes to RISK_PER_TRADE_PCT /
        # stop distance (ATR ≤4% → ~$8k notional from a $320 risk budget).
        # MAX_POSITION_PCT and MAX_NOTIONAL_USD are the notional caps and must
        # bind here — the bridge total_cost path previously bypassed them.
        #
        # SIZE_TILT_ENABLED (default false): a leaf that cleared the
        # after-cost expectancy bars may use 1.5× MAX_NOTIONAL_USD and may
        # exceed the 2% cap, but only up to that tilted absolute ($2,250
        # at the default). Crypto, benched, and pinned agents never take it.
        # The flag-off path is the min() below with tilted=False.
        equity = self._portfolio_equity()
        raw_pos = pos_usd
        tilted = False
        try:
            import size_tilt
            size_tilt.ensure_today()
            tilted = size_tilt.order_is_tilted(
                agent, symbol, approved_signal.get("contributing_agents", ""),
            )
            pos_usd, _tilt_pct, abs_cap = size_tilt.clamp_notional(
                pos_usd, equity, tilted=tilted,
                max_notional=MAX_NOTIONAL_USD,
                max_position_pct=MAX_POSITION_PCT,
            )
        except Exception as e:
            log.warning(f"size tilt clamp failed ({e}); using the $1,500 path")
            tilted = False
            pct_cap = equity * (MAX_POSITION_PCT / 100.0)
            abs_cap = MAX_NOTIONAL_USD
            pos_usd = min(pos_usd, pct_cap, MAX_NOTIONAL_USD)
        if pos_usd < raw_pos - 1e-6:
            pct_shown = equity * (MAX_POSITION_PCT / 100.0)
            extra = " size_tilt" if tilted else ""
            log.info(
                f"NOTIONAL CLAMP: {symbol} ${raw_pos:.0f} → ${pos_usd:.0f} "
                f"(pct_cap=${pct_shown:.0f} @ {MAX_POSITION_PCT}% of "
                f"${equity:,.0f}; abs_cap=${abs_cap:.0f}{extra})"
            )

        if paper_only_violation():
            return self._reject(paper_only_violation())
        if entry <= 0:
            return self._reject("entry_price is 0 or missing")
        if stop <= 0 or target <= 0:
            return self._reject("stop_loss_price or target_price missing")

        from session_gates import assert_paper_only, equity_entries_allowed, is_crypto_symbol as session_is_crypto
        assert_paper_only("OrderExecutor.execute")
        allowed, reason = equity_entries_allowed(symbol=symbol)
        if not allowed:
            log.info(f"⏭  BLOCKED ENTRY: {symbol} {direction.upper()} — {reason}")
            return {"status": "blocked", "symbol": symbol, "direction": direction, "reason": reason}

        if self._client is None:
            return self._log_only(approved_signal)

        import time
        cooldown_key = (symbol, direction)
        failed_at = self._failed_at.get(cooldown_key)
        if failed_at and (time.time() - failed_at) < self.FAILURE_COOLDOWN_SEC:
            remaining = int((self.FAILURE_COOLDOWN_SEC - (time.time() - failed_at)) / 60)
            log.info(f"⏳ COOLDOWN: {symbol} {direction.upper()} — last submission "
                     f"failed, retrying in ~{remaining}m")
            return {"status": "cooldown", "symbol": symbol, "direction": direction}

        is_crypto = session_is_crypto(symbol) or symbol in CRYPTO_SYMBOLS

        try:
            if is_crypto:
                result = self._submit_crypto(symbol, direction, pos_usd)
            else:
                result = self._submit_equity_bracket(
                    symbol, direction, entry, stop, target, pos_usd
                )

            if not _should_record_fill(result):
                if result.get("status") == "unfilled":
                    log.warning(
                        f"ORDER UNFILLED: {symbol} {direction.upper()} — "
                        f"working order cancelled, not ledgered "
                        f"| order_id={result.get('order_id')}"
                    )
                else:
                    log.info(
                        f"ORDER NOT RECORDED: {symbol} {direction.upper()} "
                        f"status={result.get('status')} qty={result.get('qty')}"
                    )
                return result

            log.info(
                f"✅ ORDER SUBMITTED: {symbol} {direction.upper()} "
                f"${pos_usd:.0f} | qty={result.get('qty')} "
                f"fill={result.get('fill_price')} "
                f"| order_id={result.get('order_id')} "
                f"| agent={agent}"
            )
            self._record_ledger(approved_signal, result)
            return result

        except Exception as e:
            log.error(f"Order submission failed for {symbol}: {e}", exc_info=True)
            self._failed_at[cooldown_key] = time.time()
            return self._log_only(approved_signal)

    # ── Equity entry + trailing-stop exit ─────────────────────────────────────
    def _submit_equity_bracket(
        self, symbol: str, direction: str,
        entry: float, stop: float, target: float, pos_usd: float
    ) -> dict:
        """Market entry + GTC TRAILING stop. No fixed take-profit.

        The asymmetry mandate (2026-07-15): a fixed take-profit sold every
        winner at +3-4% and forfeited the runners — a stock that goes on
        to +10% paid the same as one that stalled at the target. The
        trailing stop cuts losers at roughly the ATR stop distance, but
        ratchets up behind the high-water mark on winners and only fires
        on a real reversal. Losses stay capped; wins are uncapped. That
        asymmetry is the entire engine of a compounding account.
        """
        qty       = max(1, int(pos_usd / entry))
        # Trail distance = the ATR stop distance as a percent, clamped 2-6%
        trail_pct = round(min(max(abs(entry - stop) / entry * 100, 2.0), 6.0), 2)

        from alpaca.trading.requests import MarketOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce
        side      = OrderSide.BUY  if direction == "long" else OrderSide.SELL
        exit_side = OrderSide.SELL if direction == "long" else OrderSide.BUY

        entry_order = self._client.submit_order(MarketOrderRequest(
            symbol=symbol, qty=qty, side=side, time_in_force=TimeInForce.DAY,
        ))

        # Wait for the fill so the trailing stop isn't rejected for missing qty.
        #
        # A DAY market order that is still working after this wait must not
        # be left live. On 2026-09-24 P filled 12 @ $122.21 after the wait;
        # the caller had already treated the attempt as unfilled (ledger
        # shares=0) and the backstop trail covered only the 1 share that
        # had printed during the wait. Cancel the remainder. If the cancel
        # races a fill, ledger and protect whatever the broker actually holds.
        import time as _t
        o = entry_order
        st = ""
        for _ in range(15):
            o = self._client.get_order_by_id(entry_order.id)
            st = _norm_status(o)
            if st in ("filled", "canceled", "expired", "rejected"):
                break
            _t.sleep(1)

        filled_qty = _filled_qty_of(o)
        fill_price = _fill_price_of(o)
        # The broker's position is the authority — it also absorbs any
        # pre-existing holding this order added to.
        protect_qty = _broker_position_qty(self._client, symbol) or filled_qty

        if protect_qty < 1 and st not in ("canceled", "expired", "rejected", "filled"):
            self._cancel_quiet(entry_order.id)
            _t.sleep(0.4)
            try:
                o = self._client.get_order_by_id(entry_order.id)
            except Exception:
                pass
            filled_qty = _filled_qty_of(o)
            fill_price = _fill_price_of(o) or fill_price
            protect_qty = _broker_position_qty(self._client, symbol) or filled_qty
            st = _norm_status(o)

        if protect_qty < 1:
            log.error(f"{symbol}: entry did not fill (status={st}) — "
                      f"cancelled working order; nothing to ledger")
            return {
                "status": "unfilled",
                "symbol": symbol,
                "order_id": str(entry_order.id),
                "qty": 0,
                "cancelled": True,
            }

        # Partial fill with the DAY order still working: cancel the rest so
        # later shares cannot arrive outside the protective stop.
        if st not in ("filled", "canceled", "expired", "rejected"):
            self._cancel_quiet(entry_order.id)
            _t.sleep(0.4)
            try:
                o = self._client.get_order_by_id(entry_order.id)
            except Exception:
                pass
            filled_qty = max(filled_qty, _filled_qty_of(o))
            fill_price = _fill_price_of(o) or fill_price
            protect_qty = _broker_position_qty(self._client, symbol) or filled_qty
            st = _norm_status(o)

        if protect_qty < 1:
            return {
                "status": "unfilled",
                "symbol": symbol,
                "order_id": str(entry_order.id),
                "qty": 0,
                "cancelled": True,
            }

        if fill_price <= 0:
            fill_price = entry

        if protect_qty != qty:
            log.warning(f"{symbol}: requested {qty} but hold {protect_qty} — "
                        f"sizing the trailing stop to the actual position")

        trail_order, trail_err = _submit_trail_with_retry(
            self._client, symbol, protect_qty, exit_side, trail_pct
        )
        if trail_order is None:
            # Entry filled. Ledger must still record it. The tick-level
            # backstop (ensure_protective_exits) retries every minute —
            # returning "submitted" here is what lets it see the position.
            log.critical(f"{symbol}: UNPROTECTED after fill — trail failed "
                         f"({trail_err}); tick backstop will retry")
        else:
            log.info(f"🪤 TRAIL SET: {symbol} exit trails {trail_pct}% behind "
                     f"high-water mark (order {trail_order.id}) — upside uncapped")

        return {
            "status":       "submitted",
            "order_id":     str(entry_order.id),
            "symbol":       symbol,
            "direction":    direction,
            "qty":          protect_qty,
            "entry":        entry,
            "fill_price":   fill_price,
            "stop":         stop,
            "target":       target,       # bookkeeping marker only — real exit is the trail
            "trail_percent": trail_pct,
            "protected":    trail_order is not None,
        }

    # ── Crypto market order ───────────────────────────────────────────────────
    def _submit_crypto(self, symbol: str, direction: str, notional: float) -> dict:
        side = OrderSide.BUY if direction == "long" else OrderSide.SELL
        req = MarketOrderRequest(
            symbol=symbol,
            notional=round(notional, 2),
            side=side,
            time_in_force=TimeInForce.GTC,
        )
        order = self._client.submit_order(req)
        return {
            "status":    "submitted",
            "order_id":  str(order.id),
            "symbol":    symbol,
            "direction": direction,
            "notional":  notional,
        }

    # ── Fallback: log only (no Alpaca connection) ─────────────────────────────
    def _log_only(self, approved_signal: dict) -> dict:
        symbol    = approved_signal.get("symbol", "")
        direction = approved_signal.get("direction", "long")
        entry     = approved_signal.get("entry_price", 0)
        target    = approved_signal.get("target_price", entry)
        stop      = approved_signal.get("stop_loss_price", entry)
        agent     = approved_signal.get("agent", "Unknown")
        log.info(
            f"📋 PAPER TRADE (log-only): {symbol} {direction.upper()} "
            f"entry=${entry} target=${target} stop=${stop} agent={agent}"
        )
        return {"status": "logged", "symbol": symbol, "direction": direction}

    def _reject(self, reason: str) -> dict:
        log.warning(f"OrderExecutor rejected: {reason}")
        return {"status": "rejected", "reason": reason}

    def _cancel_quiet(self, order_id) -> None:
        try:
            self._client.cancel_order_by_id(order_id)
        except Exception as e:
            log.warning(f"cancel {order_id} failed: {e}")

    # ── Write to trade_ledger ─────────────────────────────────────────────────
    def _record_ledger(self, signal: dict, order_result: dict) -> None:
        try:
            import trade_ledger as _ledger
            signal_entry = float(signal.get("entry_price", 0) or 0)
            # Broker fill, not the signal price. A signal at $100 that
            # prints at $122.21 must not be ledgered as a $0 or $100 basis.
            fill = order_result.get("fill_price")
            entry = float(fill) if fill else signal_entry
            stop   = float(signal.get("stop_loss_price", 0) or 0)
            target = float(signal.get("target_price", 0) or 0)
            # Crypto orders are notional (no qty) — derive fractional shares
            # from notional/entry so the ledger can compute real P&L. With
            # shares recorded as 0, unrealized P&L multiplied by zero and
            # crypto positions were invisible to every downstream evaluator.
            qty = order_result.get("qty")
            if not qty and entry > 0:
                qty = round(float(order_result.get("notional", 0) or 0) / entry, 8)
            qty = float(qty or 0)
            if qty <= 0 or entry <= 0:
                log.warning(
                    f"not ledgering {signal.get('symbol')}: "
                    f"qty={qty} entry={entry} (no fill)"
                )
                return
            risk = abs(entry - stop) * qty
            _ledger.record_trade(
                symbol        = signal["symbol"],
                side          = signal.get("direction", "long"),
                entry_price   = entry,
                target_price  = target,
                stop_price    = stop,
                risk_dollar   = risk,
                shares        = qty,
                primary_agent = signal.get("agent", "MetaAgent"),
                contributors  = signal.get("contributing_agents", ""),
                order_id      = order_result.get("order_id", ""),
            )
        except Exception as e:
            log.warning(f"Could not record to trade_ledger: {e}")


def _should_record_fill(result: dict) -> bool:
    """Ledger and ORDER SUBMITTED only after a real submission with size.

    Unfilled equity orders are cancelled, not recorded. A submitted equity
    fill must carry qty. Crypto notionals have no qty key and still record.
    """
    if result.get("status") != "submitted":
        return False
    if "qty" in result and not result.get("qty"):
        return False
    return True


def _norm_status(order) -> str:
    return str(getattr(order, "status", "") or "").lower().split(".")[-1]


def _filled_qty_of(order) -> int:
    try:
        return int(float(getattr(order, "filled_qty", 0) or 0))
    except (TypeError, ValueError):
        return 0


def _fill_price_of(order) -> float:
    try:
        return float(getattr(order, "filled_avg_price", 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def _broker_position_qty(client, symbol: str) -> int:
    try:
        p = client.get_open_position(symbol)
        return abs(int(float(p.qty)))
    except Exception:
        return 0


def _order_qty(qty) -> int | float:
    """Whole shares as int; otherwise the float Alpaca will accept."""
    q = abs(float(qty or 0))
    if q <= 0:
        return 0
    if abs(q - round(q)) < 1e-8:
        return int(round(q))
    return q


def _order_abs_qty_obj(order):
    q = getattr(order, "qty", None)
    if q is None and isinstance(order, dict):
        q = order.get("qty")
    if q is None:
        return None
    try:
        return abs(float(q))
    except (TypeError, ValueError):
        return None


def _closing_orders_for(orders, symbol: str, signed_qty: float) -> list:
    """Closing-side open orders for one symbol (sell vs long, buy vs short)."""
    out = []
    for o in orders:
        if isinstance(o, dict):
            sym = str(o.get("symbol") or "")
            side = str(o.get("side") or "")
        else:
            sym = str(getattr(o, "symbol", "") or "")
            side = str(getattr(o, "side", "") or "")
        if sym != symbol:
            continue
        side = side.lower().split(".")[-1]
        if signed_qty > 0 and side == "sell":
            out.append(o)
        elif signed_qty < 0 and side == "buy":
            out.append(o)
    return out


def _submit_trail_with_retry(client, symbol: str, qty, exit_side, trail_pct: float,
                             attempts: int = 3):
    """Submit a GTC trailing stop, retrying after a qty refresh.

    Returns (order, error). Never raises — the caller decides whether the
    entry is still worth recording.
    """
    from alpaca.trading.requests import TrailingStopOrderRequest
    from alpaca.trading.enums import TimeInForce
    last_err = None
    protect_qty = _order_qty(qty)
    if protect_qty == 0:
        return None, "qty=0"
    import time as _t
    for attempt in range(attempts):
        try:
            order = client.submit_order(TrailingStopOrderRequest(
                symbol=symbol, qty=protect_qty, side=exit_side,
                trail_percent=trail_pct, time_in_force=TimeInForce.GTC,
            ))
            return order, None
        except Exception as e:
            last_err = e
            log.warning(f"{symbol}: trail attempt {attempt + 1}/{attempts} failed: {e}")
            try:
                _p = client.get_open_position(symbol)
                protect_qty = _order_qty(_p.qty)
            except Exception:
                pass
            if protect_qty == 0:
                return None, last_err
            _t.sleep(0.8)
    return None, last_err


def ensure_protective_exits(client=None) -> dict:
    """Tick-level backstop: every equity position must have a closing exit.

    The Aug 14 fill-qty fix stops NEW naked positions at submit time.
    This catches everything that still slips through: trail-widen cancel
    gaps, late fills after the 15s wait, rejected trails, and leftovers
    from before those commits deployed.

    A closing order that covers fewer shares than the broker position is
    naked, not protected. Those undersized exits are cancelled and
    replaced with a trail sized to the broker's actual qty. If the
    replacement fails, the previous (smaller) exit is restored so the
    position is not left with nothing.
    """
    out = {"checked": 0, "protected": [], "failed": [], "already_ok": 0, "skipped": 0}
    if paper_only_violation():
        return {**out, "error": paper_only_violation()}
    ex = get_executor() if client is None else None
    client = client or (ex._client if ex is not None else None)
    if client is None:
        return {**out, "error": "no broker client"}

    try:
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus, OrderSide
        positions = list(client.get_all_positions())
        orders = list(client.get_orders(GetOrdersRequest(
            status=QueryOrderStatus.OPEN, limit=200)))
    except Exception as e:
        log.error(f"exit backstop: cannot read broker ({e})")
        return {**out, "error": str(e)}

    out["checked"] = len(positions)
    naked = naked_equity_symbols(positions, orders)
    if not naked:
        out["already_ok"] = len([
            p for p in positions
            if not is_crypto_symbol(str(p.symbol)) and not is_option_symbol(str(p.symbol))
        ])
        return out

    import time as _t
    now = _t.time()
    pos_by_sym = {str(p.symbol): p for p in positions}
    for sym in naked:
        last = _protect_failed_at.get(sym)
        if last and (now - last) < PROTECT_RETRY_SEC:
            out["skipped"] += 1
            continue
        p = pos_by_sym.get(sym)
        if p is None:
            continue
        signed = float(p.qty)
        # Size from the live broker position, not from an earlier partial.
        try:
            live = client.get_open_position(sym)
            signed = float(live.qty)
        except Exception:
            pass
        qty = _order_qty(signed)
        if qty == 0:
            continue
        side = OrderSide.SELL if signed > 0 else OrderSide.BUY
        trail_pct = DEFAULT_TRAIL_PCT
        try:
            plpc = abs(float(getattr(p, "unrealized_plpc", 0) or 0)) * 100
            if plpc >= 4:
                trail_pct = _trail_for_profit(plpc)
        except Exception:
            pass
        existing = _closing_orders_for(orders, sym, signed)
        old_qty = 0.0
        if existing:
            for o in existing:
                q = _order_abs_qty_obj(o)
                if q:
                    old_qty += q
                try:
                    client.cancel_order_by_id(o.id)
                except Exception as ce:
                    log.warning(f"{sym}: could not cancel undersized exit: {ce}")
            _t.sleep(0.3)
        placed_qty = qty
        order, err = _submit_trail_with_retry(client, sym, qty, side, trail_pct)
        if order is None and existing and old_qty > 0:
            placed_qty = _order_qty(old_qty)
            log.error(f"{sym}: full-size trail failed ({err}) — "
                      f"restoring {placed_qty}-share exit")
            order, err = _submit_trail_with_retry(
                client, sym, placed_qty, side, trail_pct)
        if order is not None:
            qty = placed_qty
        if order is None:
            _protect_failed_at[sym] = now
            out["failed"].append(sym)
            log.critical(f"🛡️ BACKSTOP FAILED: {sym} still UNPROTECTED ({err})")
        else:
            _protect_failed_at.pop(sym, None)
            out["protected"].append(sym)
            log.warning(f"🛡️ BACKSTOP: {sym} trailing stop {trail_pct}% on {qty} shares "
                        f"(was naked)")
    out["still_naked"] = [s for s in naked if s not in out["protected"]]
    if out["protected"] or out["failed"]:
        log.warning(f"exit backstop: protected {out['protected'] or 'none'}; "
                    f"still naked {out['failed'] or 'none'}")
    return out


def _trail_for_profit(pct_gain: float) -> float:
    """Progressive trail: the bigger the gain, the tighter the protection.

    A flat 8% trail is right for a small winner that needs room to breathe
    and badly wrong for a large one. On 2026-08-07 the book held $10,653
    unrealized with ~$4,865 of it exposed — RNG was +$4,780 with $1,827
    at risk, and JBS/SPY would have exited BELOW their current price.
    Giving back half of every winner defeats the asymmetry the trailing
    stop exists to create.

    Ratchet: room while the trade is proving itself, protection once it
    has proved itself.
    """
    if pct_gain >= 15:
        return 3.0
    if pct_gain >= 8:
        return 4.0
    if pct_gain >= 4:
        return 5.5
    return 8.0


def widen_trails_on_survivors(min_days: float = 2.0,
                             widen_to_pct: float = 8.0) -> None:
    """Give positions that survive 2 days a wider leash.

    Strongest evidence in the dataset (2026-07-31): trades held 5+ days
    won 59% at +$163 avg — the only profitable bucket — while the 1-2 day
    bucket won 23% at -$184. The difference is trades cut before they
    resolved. A position that has already survived two days has earned
    room; widening its trail is what lets it reach the 5d+ bucket where
    the money is. Winners only — a loser gets no extra rope.
    """
    ex = get_executor()
    if ex._client is None:
        return
    try:
        from alpaca.trading.requests import GetOrdersRequest, TrailingStopOrderRequest
        from alpaca.trading.enums import QueryOrderStatus, OrderSide, TimeInForce
        from datetime import datetime, timezone
        import trade_ledger as _tl

        held_days = {}
        for t in _tl.open_positions():
            try:
                d = (datetime.now(_tl.ET)
                     - datetime.fromisoformat(t.opened_at_et[:19]).replace(tzinfo=_tl.ET)).days
                held_days[t.symbol.replace("/", "")] = d
            except Exception:
                continue

        for p in ex._client.get_all_positions():
            sym = str(p.symbol)
            if len(sym) > 12:                      # options handled elsewhere
                continue
            if held_days.get(sym, 0) < min_days:
                continue
            if float(p.unrealized_pl) <= 0:        # losers get no extra rope
                continue
            orders = ex._client.get_orders(GetOrdersRequest(
                status=QueryOrderStatus.OPEN, symbols=[sym]))
            trails = [o for o in orders
                      if str(getattr(o, "order_type", "")).lower().endswith("trailing_stop")]
            if not trails:
                continue
            # Target trail is driven by how much profit there is to
            # protect, not by a fixed widen-to value.
            try:
                gain_pct = float(p.unrealized_plpc) * 100
            except Exception:
                gain_pct = 0.0
            target_pct = _trail_for_profit(gain_pct)
            cur = float(getattr(trails[0], "trail_percent", 0) or 0)
            if abs(cur - target_pct) < 0.5:
                continue

            # RATCHET — only ever tighten. This function's docstring has
            # claimed "ratchet" since it was written, but the code recomputed
            # the target from the live gain and moved in BOTH directions, so
            # a position parked on a tier boundary flipped every tick:
            #
            #   15:28  TRAIL GOOGL 8.0% -> 5.5%  (+4.0%)
            #   15:29  TRAIL GOOGL 5.5% -> 8.0%  (+4.0%)
            #   15:30  TRAIL GOOGL 8.0% -> 5.5%  (+4.0%)
            #        ...every minute for a full session, 2026-08-13
            #
            # _trail_for_profit switches tiers at exactly 4%, and GOOGL sat
            # at +4.0%, so pennies of drift toggled it. Each toggle is a
            # cancel-then-submit, and that gap is where a position ends up
            # with no exit order — the "N positions have NO exit order"
            # CRITICAL has now fired four times (Aug 4: 14 positions, Aug 12:
            # 6, Aug 13: 3). Every occurrence traces back to churn here.
            #
            # Loosening protection on a winner is also just wrong on its own
            # terms: giving back room the trade already earned is the exact
            # behaviour the progressive trail exists to prevent. Tighten as
            # the gain grows; never hand it back.
            if target_pct >= cur > 0:
                continue
            widen_to_pct = target_pct
            # Cancel-then-submit is NOT atomic: if the submit fails, the
            # position is left with NO exit order at all. The invariant
            # check found 14 positions naked this way on 2026-08-04 —
            # unbounded downside on the entire equity book. Verify the
            # replacement exists, and restore the original protection if
            # it does not.
            import time as _t
            side = OrderSide.SELL if int(p.qty) > 0 else OrderSide.BUY
            for o in trails:
                ex._client.cancel_order_by_id(o.id)
            _t.sleep(0.5)
            try:
                ex._client.submit_order(TrailingStopOrderRequest(
                    symbol=sym, qty=abs(int(p.qty)), side=side,
                    trail_percent=widen_to_pct, time_in_force=TimeInForce.GTC))
            except Exception as _se:
                log.error(f"trail widen FAILED for {sym} ({_se}) — restoring "
                          f"original {cur:.1f}% protection")
                try:
                    ex._client.submit_order(TrailingStopOrderRequest(
                        symbol=sym, qty=abs(int(p.qty)), side=side,
                        trail_percent=max(cur, 2.0), time_in_force=TimeInForce.GTC))
                except Exception as _re:
                    log.critical(f"{sym} IS UNPROTECTED — both widen and restore "
                                 f"failed ({_re}); invariant check will flag it")
                continue
            log.info(f"🪢 TRAIL {sym} {cur:.1f}% -> {widen_to_pct:.1f}% "
                     f"(held {held_days.get(sym)}d, {gain_pct:+.1f}%, "
                     f"${float(p.unrealized_pl):+,.0f}) — protection scaled to gain")
        # Cancel-then-submit can leave a gap if this process dies between
        # the two calls. Re-arm anything that ended the pass naked.
        ensure_protective_exits(ex._client)
    except Exception as e:
        log.warning(f"trail widening failed: {e}")
        try:
            ensure_protective_exits()
        except Exception:
            pass


# ── Module-level singleton ────────────────────────────────────────────────────
_executor: Optional[OrderExecutor] = None

def get_executor() -> OrderExecutor:
    global _executor
    if _executor is None:
        _executor = OrderExecutor()
    return _executor


def execute_signal(approved_signal: dict) -> dict:
    """Convenience wrapper — called by ensemble.py."""
    return get_executor().execute(approved_signal)
