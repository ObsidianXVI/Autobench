"""Price and indicator alerts. The scheduler calls `check()` periodically; triggered alerts are
published on the event bus (the UI shows them live as toasts) and every firing is also kept in the
inbox (`alert_events`), so alerts that fired while no browser tab was open are still seen."""

from __future__ import annotations

import logging
import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import select

from ..db import AlertEventRow, AlertRow, session_scope, utcnow
from .events import bus
from .market import get_bars, get_quotes, normalize_symbol

log = logging.getLogger(__name__)

KINDS = {
    "above": "price is at or above the value",
    "below": "price is at or below the value",
    "crosses": "price crosses the value in either direction",
    "change_above": "today's change is at or above the value (%)",
    "change_below": "today's change is at or below the value (%)",
    "expression": "an indicator condition becomes true on the latest bar, e.g. crosses_above(close, sma(50))",
}
EXPRESSION_INTERVAL_MIN = timedelta(minutes=5)  # indicator alerts are evaluated at most this often
INBOX_KEEP = 500  # newest inbox entries kept


def to_dict(r: AlertRow) -> dict:
    return {
        "id": r.id, "symbol": r.symbol, "kind": r.kind, "value": r.value, "expression": r.expression,
        "interval": r.interval, "message": r.message, "status": r.status, "repeat": r.repeat,
        "annotation_id": r.annotation_id, "author": r.author, "last_price": r.last_price,
        "last_checked": r.last_checked.isoformat() + "Z" if r.last_checked else None,
        "triggered_at": r.triggered_at.isoformat() + "Z" if r.triggered_at else None,
        "trigger_price": r.trigger_price, "trigger_count": r.trigger_count,
        "created_at": r.created_at.isoformat() + "Z", "description": describe(r),
    }


def describe(r: AlertRow) -> str:
    v = r.value
    match r.kind:
        case "above":
            return f"{r.symbol} ≥ {v:g}"
        case "below":
            return f"{r.symbol} ≤ {v:g}"
        case "crosses":
            return f"{r.symbol} crosses {v:g}"
        case "change_above":
            return f"{r.symbol} up {v:g}% today"
        case "change_below":
            return f"{r.symbol} down {abs(v or 0):g}% today"
        case _:
            return f"{r.symbol}: {r.expression} ({r.interval})"


def create(symbol: str, kind: str, value: float | None = None, expression: str | None = None, interval: str = "1d",
           message: str | None = None, repeat: bool = False, annotation_id: str | None = None,
           author: str = "user") -> dict:
    if kind not in KINDS:
        raise ValueError(f"kind must be one of: {', '.join(KINDS)}")
    sym = normalize_symbol(symbol)
    if kind == "expression":
        if not expression or not expression.strip():
            raise ValueError("expression alerts need an expression, e.g. rsi(14) < 30")
        _evaluate_expression(sym, expression, interval)  # validate now, not at trigger time
    elif value is None:
        raise ValueError(f"'{kind}' alerts need a value")
    with session_scope() as s:
        r = AlertRow(id=uuid.uuid4().hex[:12], symbol=sym, kind=kind, value=value, expression=expression,
                     interval=interval, message=message, repeat=repeat, annotation_id=annotation_id, author=author)
        s.add(r)
        s.flush()
        out = to_dict(r)
    bus.publish("alert.created", out)
    return out


def list_(symbol: str | None = None, status: str | None = None) -> list[dict]:
    with session_scope() as s:
        q = select(AlertRow).order_by(AlertRow.created_at.desc())
        if symbol:
            q = q.where(AlertRow.symbol == normalize_symbol(symbol))
        if status:
            q = q.where(AlertRow.status == status)
        return [to_dict(r) for r in s.scalars(q)]


def update(alert_id: str, patch: dict[str, Any]) -> dict:
    allowed = {"value", "message", "status", "repeat", "expression", "interval"}
    with session_scope() as s:
        r = s.get(AlertRow, alert_id)
        if r is None:
            raise KeyError(f"Alert {alert_id} not found")
        for k, v in patch.items():
            if k not in allowed:
                raise ValueError(f"Cannot change '{k}'")
            if k == "status" and v not in ("active", "paused", "triggered"):
                raise ValueError("status must be active, paused or triggered")
            setattr(r, k, v)
        if patch.get("status") == "active":
            r.last_price = None  # re-arm: crossing detection starts fresh
        out = to_dict(r)
    bus.publish("alert.updated", out)
    return out


def delete(alert_id: str) -> dict:
    with session_scope() as s:
        r = s.get(AlertRow, alert_id)
        if r is None:
            raise KeyError(f"Alert {alert_id} not found")
        s.delete(r)
    bus.publish("alert.deleted", {"id": alert_id})
    return {"deleted": alert_id}


def delete_for_annotation(annotation_id: str) -> None:
    with session_scope() as s:
        for r in s.scalars(select(AlertRow).where(AlertRow.annotation_id == annotation_id)):
            s.delete(r)
            bus.publish("alert.deleted", {"id": r.id})


def _evaluate_expression(symbol: str, expression: str, interval: str) -> bool:
    from .expressions import evaluate_bool, max_lookback_hint

    bars = max(250, max_lookback_hint(expression) + 50)
    lookback = f"{int(bars * 1.6) + 10}d" if interval == "1d" else None
    df = get_bars(symbol, interval, lookback=lookback).df
    if df.empty:
        raise ValueError(f"No data for {symbol}")
    return bool(evaluate_bool(df, expression).iloc[-1])


def _fires(r: AlertRow, price: float, change_pct: float | None, now) -> bool:
    """Price alerts fire on the transition into the condition (so a repeating alert fires once
    per crossing, not on every check); daily-change alerts at most once per day."""
    prev = r.last_price
    v = r.value
    match r.kind:
        case "above":
            return price >= v and (prev is None or prev < v)
        case "below":
            return price <= v and (prev is None or prev > v)
        case "crosses":
            return prev is not None and ((prev < v <= price) or (prev > v >= price))
        case "change_above" | "change_below":
            met = change_pct is not None and (change_pct >= v if r.kind == "change_above" else change_pct <= v)
            return met and not (r.triggered_at and r.triggered_at.date() == now.date())
    return False


def check() -> list[dict]:
    """Evaluate active alerts against current quotes. Returns the alerts that fired."""
    with session_scope() as s:
        active = [to_dict(r) for r in s.scalars(select(AlertRow).where(AlertRow.status == "active"))]
    if not active:
        return []
    quotes = get_quotes(sorted({a["symbol"] for a in active if a["kind"] != "expression"}))
    fired = []
    now = utcnow()
    for a in active:
        with session_scope() as s:
            r = s.get(AlertRow, a["id"])
            if r is None or r.status != "active":
                continue
            hit = False
            try:
                if r.kind == "expression":
                    if r.last_checked and now - r.last_checked < EXPRESSION_INTERVAL_MIN:
                        continue
                    hit = _evaluate_expression(r.symbol, r.expression or "", r.interval)
                    # fire on the transition false -> true (last_price stores the previous state 0/1)
                    was = r.last_price
                    r.last_price = 1.0 if hit else 0.0
                    hit = hit and was != 1.0
                    price = (quotes.get(r.symbol) or {}).get("price")
                else:
                    q = quotes.get(r.symbol)
                    if not q:
                        continue
                    price = float(q["price"])
                    hit = _fires(r, price, q.get("change_pct"), now)
                    r.last_price = price
            except Exception as e:
                log.warning("alert %s check failed: %s", r.id, e)
                continue
            r.last_checked = now
            if hit:
                r.trigger_count += 1
                r.triggered_at = now
                r.trigger_price = price
                if not r.repeat:
                    r.status = "triggered"
                ev = AlertEventRow(id=uuid.uuid4().hex[:12], alert_id=r.id, symbol=r.symbol, description=describe(r),
                                   message=r.message, price=price, author=r.author, at=now)
                s.add(ev)
                out = {**to_dict(r), "event_id": ev.id}
                fired.append(out)
    if fired:
        _prune_inbox()
    for out in fired:
        bus.publish("alert.triggered", out)
    return fired


# ---------------------------------------------------------------- inbox


def _event_dict(e: AlertEventRow) -> dict:
    return {"id": e.id, "alert_id": e.alert_id, "symbol": e.symbol, "description": e.description,
            "message": e.message, "price": e.price, "author": e.author, "read": e.read,
            "at": e.at.isoformat() + "Z"}


def inbox(limit: int = 100, unread_only: bool = False) -> dict:
    """Recent alert firings, newest first, with the unread count."""
    from sqlalchemy import func

    with session_scope() as s:
        q = select(AlertEventRow).order_by(AlertEventRow.at.desc(), AlertEventRow.id).limit(max(1, min(limit, INBOX_KEEP)))
        if unread_only:
            q = q.where(AlertEventRow.read.is_(False))
        items = [_event_dict(e) for e in s.scalars(q)]
        unread = s.scalar(select(func.count()).select_from(AlertEventRow).where(AlertEventRow.read.is_(False))) or 0
    return {"items": items, "unread": unread}


def mark_read(ids: list[str] | None = None, read: bool = True) -> dict:
    """Mark the given inbox entries (all when ids is None) read or unread."""
    from sqlalchemy import update as sql_update

    with session_scope() as s:
        q = sql_update(AlertEventRow).values(read=read)
        if ids is not None:
            q = q.where(AlertEventRow.id.in_(ids))
        s.execute(q)
    out = inbox(limit=1)
    bus.publish("inbox.updated", {"unread": out["unread"]})
    return {"unread": out["unread"]}


def clear_inbox(ids: list[str] | None = None) -> dict:
    """Delete inbox entries (all when ids is None). The alerts themselves are not touched."""
    from sqlalchemy import delete as sql_delete

    with session_scope() as s:
        q = sql_delete(AlertEventRow)
        if ids is not None:
            q = q.where(AlertEventRow.id.in_(ids))
        s.execute(q)
    out = inbox(limit=1)
    bus.publish("inbox.updated", {"unread": out["unread"]})
    return {"unread": out["unread"]}


def _prune_inbox() -> None:
    from sqlalchemy import delete as sql_delete

    with session_scope() as s:
        keep = select(AlertEventRow.id).order_by(AlertEventRow.at.desc()).limit(INBOX_KEEP)
        s.execute(sql_delete(AlertEventRow).where(AlertEventRow.id.not_in(keep)))
