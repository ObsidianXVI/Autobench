"""Chart view state, research notes and watchlists."""

from __future__ import annotations

import uuid

from sqlalchemy import select

from .. import db
from ..db import NoteRow, WatchlistRow, session_scope, utcnow
from ..schemas import ChartView, NoteIn
from ..timeutil import get_interval
from .events import bus
from .indicators import get_spec, resolve_params
from .market import normalize_symbol

# --- chart view ----------------------------------------------------------------------------

DEFAULT_VIEW = {"symbol": "SPY", "interval": "1d", "from_time": None, "to_time": None,
                "indicators": [{"name": "sma", "params": {"length": 50}}, {"name": "sma", "params": {"length": 200}},
                               {"name": "rsi", "params": {}}]}


def get_view() -> dict:
    return db.state_get("chart_view", DEFAULT_VIEW) or DEFAULT_VIEW


def set_view(view: ChartView, author: str = "agent") -> dict:
    cur = dict(get_view())
    patch = view.model_dump(exclude_unset=True)
    if patch.get("symbol"):
        patch["symbol"] = normalize_symbol(patch["symbol"])
    if patch.get("interval"):
        patch["interval"] = get_interval(patch["interval"]).name
    if "indicators" in patch and patch["indicators"] is not None:
        clean = []
        for item in patch["indicators"]:
            if not isinstance(item, dict) or "name" not in item:
                raise ValueError("Each indicator must look like {'name': 'sma', 'params': {'length': 50}}")
            spec = get_spec(item["name"])
            params = resolve_params(spec, item.get("params") or {})
            clean.append({"name": spec.name, "params": {k: v for k, v in params.items() if v != spec.params[k]}})
        patch["indicators"] = clean
    if patch.get("symbol") and patch["symbol"] != cur.get("symbol") and "from_time" not in patch:
        patch["from_time"] = patch["to_time"] = None  # new symbol: let the UI pick a default range
    cur.update(patch)
    cur["updated_by"] = author
    cur["updated_at"] = utcnow().isoformat() + "Z"
    db.state_set("chart_view", cur)
    bus.publish("view.changed", cur)
    return cur


# --- notes ---------------------------------------------------------------------------------


def _note(r: NoteRow) -> dict:
    return {"id": r.id, "symbol": r.symbol, "title": r.title, "content": r.content, "tags": r.tags,
            "author": r.author, "created_at": r.created_at.isoformat() + "Z", "updated_at": r.updated_at.isoformat() + "Z"}


def save_note(note: NoteIn, note_id: str | None = None, author: str = "agent") -> dict:
    with session_scope() as s:
        if note_id:
            r = s.get(NoteRow, note_id)
            if r is None:
                raise KeyError(f"Note {note_id} not found")
            r.title, r.content, r.tags = note.title, note.content, note.tags
            r.symbol = normalize_symbol(note.symbol) if note.symbol else None
            r.updated_at = utcnow()
        else:
            r = NoteRow(id=uuid.uuid4().hex[:12], symbol=normalize_symbol(note.symbol) if note.symbol else None,
                        title=note.title, content=note.content, tags=note.tags, author=author,
                        created_at=utcnow(), updated_at=utcnow())
            s.add(r)
        s.flush()
        d = _note(r)
    bus.publish("note.saved", {k: v for k, v in d.items() if k != "content"})
    return d


def list_notes(symbol: str | None = None, tag: str | None = None, query: str | None = None, limit: int = 20,
               include_content: bool = True) -> list[dict]:
    q = select(NoteRow).order_by(NoteRow.updated_at.desc())
    if symbol:
        q = q.where(NoteRow.symbol == normalize_symbol(symbol))
    if query:
        like = f"%{query}%"
        q = q.where(NoteRow.title.ilike(like) | NoteRow.content.ilike(like))
    with session_scope() as s:
        rows = s.execute(q).scalars().all()
    out = [_note(r) for r in rows if not tag or tag in (r.tags or [])][:limit]
    if not include_content:
        for n in out:
            n["preview"] = n.pop("content")[:200]
    return out


def delete_note(note_id: str) -> dict:
    with session_scope() as s:
        r = s.get(NoteRow, note_id)
        if r is None:
            raise KeyError(f"Note {note_id} not found")
        s.delete(r)
    bus.publish("note.deleted", {"id": note_id})
    return {"deleted": note_id}


# --- watchlists ----------------------------------------------------------------------------


def _wl(r: WatchlistRow) -> dict:
    return {"name": r.name, "symbols": r.symbols, "updated_at": r.updated_at.isoformat() + "Z"}


def list_watchlists() -> list[dict]:
    with session_scope() as s:
        rows = s.execute(select(WatchlistRow).order_by(WatchlistRow.name)).scalars().all()
        if not rows:
            r = WatchlistRow(name="default", symbols=[], created_at=utcnow(), updated_at=utcnow())
            s.add(r)
            rows = [r]
        return [_wl(r) for r in rows]


def get_watchlist(name: str) -> dict:
    with session_scope() as s:
        r = s.get(WatchlistRow, name)
        if r is None:
            raise KeyError(f"Watchlist '{name}' not found")
        return _wl(r)


def update_watchlist(name: str, add: list[str] | None = None, remove: list[str] | None = None,
                     replace: list[str] | None = None) -> dict:
    name = name.strip()
    if not name:
        raise ValueError("watchlist name required")
    with session_scope() as s:
        r = s.get(WatchlistRow, name)
        if r is None:
            r = WatchlistRow(name=name, symbols=[], created_at=utcnow(), updated_at=utcnow())
            s.add(r)
        syms = list(r.symbols or [])
        if replace is not None:
            syms = []
            add = replace
        for x in add or []:
            x = normalize_symbol(x)
            if x not in syms:
                syms.append(x)
        rm = {normalize_symbol(x) for x in remove or []}
        r.symbols = [x for x in syms if x not in rm]
        r.updated_at = utcnow()
        s.flush()
        d = _wl(r)
    bus.publish("watchlist.updated", d)
    return d


def delete_watchlist(name: str) -> dict:
    with session_scope() as s:
        r = s.get(WatchlistRow, name)
        if r is None:
            raise KeyError(f"Watchlist '{name}' not found")
        s.delete(r)
    bus.publish("watchlist.deleted", {"name": name})
    return {"deleted": name}


# --- workbench layouts & UI state ----------------------------------------------------------
# Opaque JSON owned by the web UI (chart grid, per-chart symbol/interval/indicators/style,
# panel sizes, active page). Stored server-side so it survives reloads and follows the user
# across browsers; the agent can read and switch layouts too.

_MAX_LAYOUT_BYTES = 256_000


def _check_size(data: dict) -> None:
    import json

    if len(json.dumps(data)) > _MAX_LAYOUT_BYTES:
        raise ValueError("layout is too large")


def list_layouts() -> list[dict]:
    items = (db.state_get("layouts", {"items": {}}) or {"items": {}})["items"]
    return [{"name": k, "updated_at": v.get("updated_at"), "summary": v.get("summary")} for k, v in sorted(items.items())]


def get_layout(name: str) -> dict:
    items = (db.state_get("layouts", {"items": {}}) or {"items": {}})["items"]
    if name not in items:
        raise KeyError(f"No layout named '{name}'")
    return {"name": name, **items[name]}


def save_layout(name: str, data: dict, summary: str | None = None, author: str = "user") -> dict:
    name = name.strip()
    if not name or len(name) > 60:
        raise ValueError("layout name must be 1-60 characters")
    _check_size(data)
    store = db.state_get("layouts", {"items": {}}) or {"items": {}}
    store["items"][name] = {"data": data, "summary": summary, "updated_at": utcnow().isoformat() + "Z", "author": author}
    db.state_set("layouts", store)
    bus.publish("layouts.updated", {"name": name, "author": author})
    return {"name": name, **store["items"][name]}


def delete_layout(name: str) -> dict:
    store = db.state_get("layouts", {"items": {}}) or {"items": {}}
    if name not in store["items"]:
        raise KeyError(f"No layout named '{name}'")
    del store["items"][name]
    db.state_set("layouts", store)
    bus.publish("layouts.updated", {"name": name, "deleted": True})
    return {"deleted": name}


def get_ui_state() -> dict:
    return db.state_get("ui_state", {}) or {}


def set_ui_state(data: dict) -> dict:
    _check_size(data)
    db.state_set("ui_state", data)
    return {"ok": True}


# --- per-symbol chart configuration (layers) ------------------------------------------------
# The UI keeps one configuration per symbol: an ordered list of layers, each holding the price
# chart, volume, indicators and other symbols' series. "_default" is the template for symbols
# that have never been opened.

DEFAULT_CHART_KEY = "_default"


def _chart_key(symbol: str) -> str:
    return "chartcfg:" + (DEFAULT_CHART_KEY if symbol == DEFAULT_CHART_KEY else normalize_symbol(symbol))


def get_chart_config(symbol: str) -> dict | None:
    return db.state_get(_chart_key(symbol), None)


def save_chart_config(symbol: str, data: dict) -> dict:
    if not isinstance(data.get("layers"), list):
        raise ValueError("chart config needs a 'layers' list")
    _check_size(data)
    data = {**data, "updated_at": utcnow().isoformat() + "Z"}
    db.state_set(_chart_key(symbol), data)
    return data


def delete_chart_config(symbol: str) -> dict:
    with session_scope() as s:
        row = s.get(db.StateRow, _chart_key(symbol))
        if row is None:
            raise KeyError(f"No saved chart configuration for {symbol}")
        s.delete(row)
    return {"deleted": symbol}


def list_chart_configs() -> list[str]:
    with session_scope() as s:
        keys = s.scalars(select(db.StateRow.key).where(db.StateRow.key.like("chartcfg:%"))).all()
    return sorted(k.split(":", 1)[1] for k in keys)


def layers_summary(symbol: str) -> list[dict] | None:
    """Compact description of what the user has on a symbol's chart (for the agent)."""
    cfg = get_chart_config(symbol)
    if not cfg:
        return None
    out = []
    for layer in cfg.get("layers", []):
        items = []
        for it in layer.get("items", []):
            kind = it.get("kind")
            if kind == "indicator":
                label = it.get("name", "") + ("(" + ",".join(str(v) for v in (it.get("params") or {}).values()) + ")"
                                             if it.get("params") else "")
            elif kind == "compare":
                label = f"{it.get('symbol')} ({it.get('display', 'overlay')})"
            elif kind == "price":
                label = f"price ({it.get('chartType', 'candles')})"
            else:
                label = kind or "?"
            items.append({"kind": kind, "label": label, "visible": it.get("visible", True)})
        entry = {"id": layer.get("id"), "name": layer.get("name"), "visible": layer.get("visible", True), "items": items}
        if layer.get("interval"):
            entry["interval"] = layer["interval"]  # the layer only shows at this interval
        if any(it.get("kind") == "price" for it in layer.get("items", [])):
            entry["chart"] = True  # holds its own price chart: on screen when the chart is at its interval
        out.append(entry)
    return out
