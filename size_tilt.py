"""
size_tilt.py
────────────
Evidence-gated per-agent size tilt. OFF unless SIZE_TILT_ENABLED is true.

A qualifying leaf agent's per-trade notional cap is 1.5× MAX_NOTIONAL_USD
($1,500 → $2,250 at the default). That order also skips the MAX_POSITION_PCT
2% cap, but only up to the tilted absolute. Everyone else keeps the existing
min(size, 2% of equity, $1,500) clamp.

Qualification is recomputed once per ET date from AgentEvaluator after-cost
expectancy. Both bars are required, and exactly 0 does not pass:

  • 20-day expectancy per trade > 0 over >= 10 trades
  • all-time expectancy per trade > 0 over >= 30 trades

Benched agents (logs/agent_summary.json active:false) and pinned agents
(pinned_reason present) never qualify. CryptoAgent never qualifies, and an
order on any crypto symbol is never tilted.

The day's list is logged at INFO and written to logs/size_tilt_qualifiers.json
(date, flag, qualifiers, per-agent reason). An empty qualifier list is valid.
Reasons are required only for the evaluator's own non-wrapper roster.
MetaAgent(...) wrappers (trade_ledger.is_wrapper_agent_name) and names that
appear only in agent_summary.json do not block the save. An evaluator row
with 0 trades is recorded as "no trades" and does not qualify. An empty
reason map, or a roster name missing from reasons, is not saved: that call
returns no qualifiers and the next call computes again.

With SIZE_TILT_ENABLED off, order entry does not compute or write that file.
The daily eval (10:00 ET, 9:00 CT) does.

.env keys consumed:
  SIZE_TILT_ENABLED   default false   ("1" / "true" / "yes" / "on" enable it)
  MAX_NOTIONAL_USD    default 1500    (tilted cap is 1.5× this value)
  MAX_POSITION_PCT    default 2.0     (waived for a tilted order, up to 1.5×)
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

log = logging.getLogger("SizeTilt")

SIZE_TILT_MULTIPLIER = 1.5
MIN_TRADES_20D = 10
MIN_TRADES_ALL = 30
CRYPTO_AGENT = "CryptoAgent"

# Process cache of today's payload. Reset in tests.
_cache: Optional[dict] = None


def enabled() -> bool:
    """Live read. Default false. Never cached, so a restart picks up .env."""
    raw = os.getenv("SIZE_TILT_ENABLED", "false").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def base_notional_cap() -> float:
    return float(os.getenv("MAX_NOTIONAL_USD", "1500"))


def tilted_absolute_cap(base: Optional[float] = None) -> float:
    """1.5× the absolute notional cap. $2,250 when MAX_NOTIONAL_USD is $1,500."""
    b = base_notional_cap() if base is None else float(base)
    return b * SIZE_TILT_MULTIPLIER


def qualifiers_path() -> Path:
    import trade_ledger as _ledger
    return _ledger.LOGS_DIR / "size_tilt_qualifiers.json"


def today_iso(now: Optional[datetime] = None) -> str:
    if now is None:
        import trade_ledger as _ledger
        now = datetime.now(_ledger.ET)
    if now.tzinfo is not None:
        import trade_ledger as _ledger
        now = now.astimezone(_ledger.ET)
    return now.strftime("%Y-%m-%d")


def reset_for_tests() -> None:
    global _cache
    _cache = None


def load_summary() -> dict:
    """agent_summary.json rows. Missing file → nobody is benched or pinned."""
    import trade_ledger as _ledger
    path = _ledger.LOGS_DIR / "agent_summary.json"
    if not path.exists():
        return {}
    try:
        with open(path) as f:
            data = json.load(f) or {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def order_leaves(agent: str, contributing: str = "") -> list[str]:
    """Leaf names on an order. MetaAgent(A, B) unwraps; bare MetaAgent is []."""
    from trade_ledger import expand_agent_names
    out: list[str] = []
    seen: set[str] = set()
    for raw in (agent or "", contributing or ""):
        for name in expand_agent_names(raw):
            if name not in seen:
                seen.add(name)
                out.append(name)
    return out


def is_crypto_order(symbol: Optional[str], agent: str = "", contributing: str = "",
                    instrument: str = "") -> bool:
    """CryptoAgent, a crypto instrument, or any crypto symbol. Never tilted."""
    if str(instrument or "").strip().lower() == "crypto":
        return True
    for name in order_leaves(agent, contributing):
        if name == CRYPTO_AGENT:
            return True
    if (agent or "").strip() == CRYPTO_AGENT:
        return True
    sym = str(symbol or "").strip()
    if not sym:
        return False
    try:
        from session_gates import is_crypto_symbol
        if is_crypto_symbol(sym):
            return True
    except Exception:
        pass
    return False


def _window_blocker(label: str, trades: int, need: int, exp) -> Optional[str]:
    """None when this window clears the bar. Expectancy must be strictly > 0."""
    n = int(trades or 0)
    if n < need:
        return f"{label} trades {n} < {need}"
    try:
        value = float(exp)
    except (TypeError, ValueError):
        value = None
    # Strictly greater than zero. Exactly 0, negative, and missing all fail.
    if value is None or not (value > 0):
        shown = "n/a" if value is None else f"{value:+.2f}"
        return f"{label} after-cost expectancy {shown} over {n} trades is not > 0"
    return None


def decide(stats, summary_row: Optional[dict] = None) -> tuple[bool, str]:
    """(qualifies, reason) from one AgentEvaluator row plus its summary entry.

    After-cost expectancy only (expectancy_after_costs_20d and
    expectancy_after_costs). Gross P&L is not a qualifier. Both windows are
    required. Bench and pin block even when the numbers would pass.
    """
    row = summary_row or {}
    name = str(getattr(stats, "name", "") or "")
    blockers: list[str] = []

    if name == CRYPTO_AGENT:
        blockers.append("crypto excluded")

    benched = False
    if "active" in row and not bool(row.get("active")):
        benched = True
    elif getattr(stats, "active", True) is False:
        benched = True
    if benched:
        blockers.append("benched (active:false)")

    pinned = str(row.get("pinned_reason") or "").strip()
    if pinned:
        blockers.append(f"pinned (pinned_reason={pinned})")

    exp20 = getattr(stats, "expectancy_after_costs_20d", None)
    exp_all = getattr(stats, "expectancy_after_costs", None)
    n20 = int(getattr(stats, "trades_20d", 0) or 0)
    nall = int(getattr(stats, "trades_total", 0) or 0)

    block20 = _window_blocker("20d", n20, MIN_TRADES_20D, exp20)
    block_all = _window_blocker("all-time", nall, MIN_TRADES_ALL, exp_all)
    if block20:
        blockers.append(block20)
    if block_all:
        blockers.append(block_all)

    if blockers:
        return False, "; ".join(blockers)

    return True, (
        f"qualifies: 20d after-cost expectancy {float(exp20):+.2f} over {n20} trades; "
        f"all-time after-cost expectancy {float(exp_all):+.2f} over {nall} trades"
    )


def _payload_from_report(report, summary: dict, day: str) -> dict:
    """Reasons for the evaluator roster. Wrappers are omitted.

    A roster agent with 0 trades gets the reason "no trades" and does not
    qualify. Summary-only names are not added here.
    """
    from trade_ledger import is_wrapper_agent_name
    reasons: dict[str, str] = {}
    qualifiers: list[str] = []
    for stats in sorted(getattr(report, "agents", []) or [], key=lambda s: s.name):
        name = str(getattr(stats, "name", "") or "")
        if not name or is_wrapper_agent_name(name):
            continue
        if int(getattr(stats, "trades_total", 0) or 0) == 0:
            reasons[name] = "no trades"
            continue
        row = summary.get(name) if isinstance(summary, dict) else None
        if not isinstance(row, dict):
            row = {}
        ok, reason = decide(stats, row)
        reasons[name] = reason
        if ok:
            qualifiers.append(name)
    qualifiers.sort()
    return {
        "date": day,
        "SIZE_TILT_ENABLED": enabled(),
        "qualifiers": qualifiers,
        "reasons": reasons,
    }


def _write(payload: dict) -> None:
    path = qualifiers_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.replace(path)


def _remember(payload: dict) -> dict:
    global _cache
    _cache = payload
    return payload


def _roster_names(report) -> set[str]:
    """Individual agents the evaluator scored. Wrappers are not required."""
    from trade_ledger import is_wrapper_agent_name
    names: set[str] = set()
    for stats in getattr(report, "agents", []) or []:
        name = str(getattr(stats, "name", "") or "").strip()
        if not name or is_wrapper_agent_name(name):
            continue
        names.add(name)
    return names


def _coverage_problem(reasons: dict, roster: set[str]) -> Optional[str]:
    """None when this result may be cached.

    Empty reasons, or a non-wrapper roster name with no reason, may not.
    Summary-only names are not in ``roster`` and do not block.
    """
    if not isinstance(reasons, dict) or not reasons:
        return "per-agent reasons are empty"
    missing = sorted(roster - set(reasons))
    if missing:
        return "per-agent reasons missing evaluator roster: " + ", ".join(missing)
    return None


def _log_payload(payload: dict) -> None:
    names = ", ".join(payload.get("qualifiers") or []) or "(none)"
    log.info(
        "SIZE TILT daily date=%s SIZE_TILT_ENABLED=%s qualifiers=%s",
        payload.get("date"),
        payload.get("SIZE_TILT_ENABLED"),
        names,
    )


def _refused(day: str, problem: str) -> dict:
    """This call qualifies nobody and must not lock the day."""
    log.warning(
        "SIZE TILT daily date=%s not saved (%s); no qualifiers, will recompute",
        day, problem,
    )
    return {
        "date": day,
        "SIZE_TILT_ENABLED": enabled(),
        "qualifiers": [],
        "reasons": {},
    }


def ensure_today(report=None, summary: Optional[dict] = None) -> dict:
    """Return today's qualifier payload, computing it at most once per ET date.

    Pass the evaluator report when the caller just built one (the 10:00 ET /
    9:00 CT eval, and the 15:30 ET repeat). Otherwise this reads the ledger
    via AgentEvaluator. A same-day file with a non-empty reason map is reused
    so a restart does not re-log. The first complete compute locks the day.

    The required set is the evaluator roster minus wrapper names. Agents that
    exist only in agent_summary.json are ignored. Empty reasons, or a roster
    name missing from reasons, are not cached and not written. That call
    returns no qualifiers so the next call recomputes. Order entry must not
    call this when SIZE_TILT_ENABLED is off.
    """
    day = today_iso()
    if _cache is not None and _cache.get("date") == day:
        return _cache

    if summary is None:
        summary = load_summary()

    if report is None:
        path = qualifiers_path()
        if path.exists():
            try:
                data = json.loads(path.read_text())
                if isinstance(data, dict) and data.get("date") == day:
                    # Day lock: a non-empty same-day map is enough. Do not
                    # re-check agent_summary.json — wrapper and summary-only
                    # rows are not part of the required set.
                    reasons = data.get("reasons") or {}
                    if isinstance(reasons, dict) and reasons:
                        return _remember(data)
                    log.warning(
                        "size tilt qualifier file ignored "
                        "(per-agent reasons are empty); recomputing"
                    )
            except Exception as e:
                log.warning(f"size tilt qualifier file unreadable ({e}); recomputing")

    if report is None:
        from agent_evaluator import AgentEvaluator
        report = AgentEvaluator().evaluate()

    payload = _payload_from_report(report, summary or {}, day)
    problem = _coverage_problem(
        payload.get("reasons") or {},
        _roster_names(report),
    )
    if problem is not None:
        return _refused(day, problem)
    try:
        _write(payload)
    except Exception as e:
        return _refused(day, f"qualifier file not written ({e})")
    _log_payload(payload)
    return _remember(payload)


def qualifying_agents() -> set[str]:
    """Today's evidence qualifiers. Empty when the daily record cannot be built."""
    try:
        payload = ensure_today()
    except Exception as e:
        log.warning(f"size tilt qualifiers unavailable ({e})")
        return set()
    return set(payload.get("qualifiers") or [])


def order_is_tilted(agent: str, symbol: Optional[str] = None,
                    contributing: str = "", instrument: str = "") -> bool:
    """True only when the flag is on, the order is not crypto, and every leaf qualifies.

    A merged MetaAgent(A, B) ticket tilts only when both leaves qualified.
    One non-qualifier on the ticket keeps the $1,500 path.
    """
    if not enabled():
        return False
    if is_crypto_order(symbol, agent, contributing, instrument):
        return False
    leaves = order_leaves(agent, contributing)
    if not leaves:
        return False
    quals = qualifying_agents()
    return all(name in quals for name in leaves)


def clamp_notional(
    pos_usd: float,
    equity: float,
    *,
    tilted: bool,
    max_notional: float,
    max_position_pct: float,
) -> tuple[float, float, float]:
    """(clamped_usd, pct_cap, abs_cap).

    Untilted: min(size, equity × pct, absolute). That is the existing path.
    Tilted: absolute becomes 1.5× and the percent cap cannot bind below it,
    so the order may exceed 2% but not the tilted absolute.
    """
    pct_cap = float(equity) * (float(max_position_pct) / 100.0)
    abs_cap = float(max_notional)
    if tilted:
        abs_cap = tilted_absolute_cap(max_notional)
        pct_cap = max(pct_cap, abs_cap)
    clamped = min(float(pos_usd), pct_cap, abs_cap)
    return clamped, pct_cap, abs_cap
