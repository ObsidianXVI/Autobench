"""Edits of a symbol's chart layers on behalf of the agent.

The web UI owns the format (web/lib/src/core/layers.dart) and stores it per symbol through
workspace.save_chart_config; this module changes that JSON the same way the UI would (add an indicator
to a layer, restyle it, add another symbol's series, tie a layer to an interval, ...), saves it and
publishes `chart_config.updated` so open tabs reload the symbol. The UI repairs any remaining
invariant on load (SymbolConfig.normalize), but the edits here keep them: at most one price and one
volume per layer, at least one price chart, unique item ids.
"""

from __future__ import annotations

import copy
import uuid
from typing import Any

from ..timeutil import get_interval
from . import workspace
from .events import bus
from .indicators import get_spec, resolve_params
from .market import normalize_symbol

MAIN, AGENT = "main", "agent"
CHART_TYPES = ("candles", "hollow", "bars", "line", "area", "heikin")
SCALES = ("linear", "log", "percent")
LINE_STYLES = ("solid", "dashed", "dotted")
PLOTS = ("line", "histogram", "dots")
DISPLAYS = ("line", "area", "candles")


def _new_id(prefix: str) -> str:
    return prefix + uuid.uuid4().hex[:8]


def builtin() -> dict:
    """Same as SymbolConfig.builtin() in the UI."""
    return {"v": 1, "activeLayer": MAIN, "scale": "linear", "layers": [{
        "id": MAIN, "name": "Main", "visible": True, "locked": False, "items": [
            {"id": "price", "kind": "price", "visible": True, "pane": "main", "chartType": "candles"},
            {"id": "volume", "kind": "volume", "visible": True, "pane": "auto"},
            {"id": "sma50", "kind": "indicator", "visible": True, "pane": "auto", "name": "sma", "params": {"length": 50}},
            {"id": "sma200", "kind": "indicator", "visible": True, "pane": "auto", "name": "sma", "params": {"length": 200}},
            {"id": "rsi", "kind": "indicator", "visible": True, "pane": "auto", "name": "rsi", "params": {}},
        ]}]}


def load(symbol: str) -> dict:
    """The symbol's layers: saved, else the user's default template, else the built-in one."""
    cfg = workspace.get_chart_config(symbol) or workspace.get_chart_config(workspace.DEFAULT_CHART_KEY) or builtin()
    cfg = copy.deepcopy(cfg)
    cfg.pop("updated_at", None)
    if not isinstance(cfg.get("layers"), list) or not cfg["layers"]:
        cfg = builtin()
    return cfg


def save(symbol: str, cfg: dict, what: str, author: str = "agent") -> dict:
    sym = normalize_symbol(symbol)
    workspace.save_chart_config(sym, cfg)
    bus.publish("chart_config.updated", {"symbol": sym, "author": author, "what": what})
    return detail(sym, cfg)


def detail(symbol: str, cfg: dict | None = None) -> dict:
    """Everything on the symbol's chart with the ids needed to change it."""
    cfg = cfg or load(symbol)
    layers = []
    for layer in cfg["layers"]:
        items = []
        for it in layer.get("items", []):
            d = {"id": it["id"], "kind": it.get("kind"), "visible": it.get("visible", True), "pane": it.get("pane", "auto")}
            if it.get("kind") == "price":
                d["chart_type"] = it.get("chartType", "candles")
            elif it.get("kind") == "indicator":
                d.update(name=it.get("name"), params=it.get("params") or {})
                if it.get("styles"):
                    d["styles"] = it["styles"]
                if it.get("levels") is not None:
                    d["levels"] = it["levels"]
            elif it.get("kind") == "compare":
                d.update(symbol=it.get("symbol"), display=it.get("display", "line"))
            if it.get("color"):
                d["color"] = it["color"]
            items.append(d)
        entry = {"id": layer["id"], "name": layer.get("name"), "visible": layer.get("visible", True),
                 "locked": layer.get("locked", False), "interval": layer.get("interval"),
                 "chart": any(i.get("kind") == "price" for i in layer.get("items", [])), "items": items}
        layers.append(entry)
    return {"symbol": normalize_symbol(symbol), "scale": cfg.get("scale", "linear"),
            "active_layer": cfg.get("activeLayer", MAIN), "layers": layers,
            "note": "Layers are listed top first. At an interval the chart shows one chart layer (tied to that "
                    "interval, else the untied one, else the bottom one); layers without a price chart add their "
                    "series on every interval, or only on theirs."}


# ----------------------------------------------------------------------------------- lookups


def _layer(cfg: dict, layer_id: str) -> dict:
    for layer in cfg["layers"]:
        if layer["id"] == layer_id:
            return layer
    for layer in cfg["layers"]:  # the agent may pass the visible name
        if (layer.get("name") or "").lower() == layer_id.lower():
            return layer
    raise KeyError(f"No layer '{layer_id}' (layers: {', '.join(l['id'] for l in cfg['layers'])})")


def _target_layer(cfg: dict, layer_id: str | None, interval: str | None = None) -> dict:
    """Where new series go: the given layer, else the chart layer for [interval] when several exist,
    else the Agent layer (created on demand, like set_chart_view does)."""
    if layer_id:
        if layer_id == AGENT and not any(l["id"] == AGENT for l in cfg["layers"]):
            return _agent_layer(cfg)
        return _layer(cfg, layer_id)
    if interval:
        charts = [l for l in cfg["layers"] if any(i.get("kind") == "price" for i in l.get("items", []))]
        if len(charts) > 1:
            return chart_layer_for(cfg, interval)
    return _agent_layer(cfg)


def _agent_layer(cfg: dict) -> dict:
    for layer in cfg["layers"]:
        if layer["id"] == AGENT:
            layer["visible"] = True
            return layer
    layer = {"id": AGENT, "name": "Agent", "visible": True, "locked": False, "items": []}
    cfg["layers"].insert(0, layer)
    return layer


def chart_layer_for(cfg: dict, interval: str) -> dict:
    """Same rule as SymbolConfig.chartLayerFor in the UI."""
    charts = [l for l in cfg["layers"] if any(i.get("kind") == "price" for i in l.get("items", []))]
    for layer in charts:
        if layer.get("interval") == interval:
            return layer
    for layer in charts:
        if not layer.get("interval"):
            return layer
    return charts[-1] if charts else cfg["layers"][-1]


def _find_item(cfg: dict, item_id: str) -> tuple[dict, dict]:
    for layer in cfg["layers"]:
        for it in layer.get("items", []):
            if it["id"] == item_id:
                return layer, it
    raise KeyError(f"No chart item '{item_id}' (see get_chart_layers for ids)")


def _all_ids(cfg: dict) -> set[str]:
    return {it["id"] for l in cfg["layers"] for it in l.get("items", [])}


def _check_pane(cfg: dict, pane: str | None) -> str | None:
    if pane is None:
        return None
    if pane in ("auto", "main", "new"):
        return pane
    if pane.startswith("with:") and pane[5:] in _all_ids(cfg):
        return pane
    raise ValueError("pane must be auto, main, new or with:<item id>")


def _check_styles(styles: dict | None) -> dict | None:
    if styles is None:
        return None
    out = {}
    for output, st in styles.items():
        if not isinstance(st, dict):
            raise ValueError("styles look like {'sma': {'color': '#f59e0b', 'width': 2, 'line': 'dashed'}}")
        clean = {}
        for k, v in st.items():
            if k == "color" and isinstance(v, str) and v.startswith("#") and len(v) in (4, 7, 9):
                clean[k] = v
            elif k == "width" and isinstance(v, (int, float)) and 0.5 <= v <= 8:
                clean[k] = float(v)
            elif k == "line" and v in LINE_STYLES:
                clean[k] = v
            elif k == "plot" and v in PLOTS:
                clean[k] = v
            elif k == "visible" and isinstance(v, bool):
                if not v:
                    clean[k] = False
            elif v is not None:
                raise ValueError(f"Bad style {k}={v!r}: color is #rrggbb, width 0.5-8, line {'/'.join(LINE_STYLES)}, "
                                 f"plot {'/'.join(PLOTS)}, visible true/false")
        out[output] = clean
    return out


def _indicator_params(name: str, params: dict | None) -> tuple[str, dict]:
    spec = get_spec(name)
    full = resolve_params(spec, params or {})
    return spec.name, {k: v for k, v in full.items() if v != spec.params[k]}


# ----------------------------------------------------------------------------------- edits


def add_series(symbol: str, kind: str, name: str | None = None, params: dict | None = None,
               compare_symbol: str | None = None, layer: str | None = None, pane: str | None = None,
               display: str | None = None, styles: dict | None = None, levels: list[float] | None = None,
               color: str | None = None, interval: str | None = None, author: str = "agent") -> dict:
    cfg = load(symbol)
    target = _target_layer(cfg, layer, interval)
    item: dict[str, Any] = {"visible": True, "pane": _check_pane(cfg, pane) or "auto"}
    match kind:
        case "indicator":
            if not name:
                raise ValueError("indicator name required (see list_indicators)")
            iname, p = _indicator_params(name, params)
            item.update(id=_new_id("a"), kind="indicator", name=iname, params=p)
            if styles:
                item["styles"] = _check_styles(styles)
            if levels is not None:
                item["levels"] = [float(x) for x in levels]
            what = f"Added {iname} to {target.get('name')}"
        case "compare":
            if not compare_symbol:
                raise ValueError("compare_symbol required")
            if display is not None and display not in DISPLAYS:
                raise ValueError(f"display must be one of {', '.join(DISPLAYS)}")
            item.update(id=_new_id("c"), kind="compare", symbol=normalize_symbol(compare_symbol), display=display or "line")
            what = f"Added {item['symbol']} to {target.get('name')}"
        case "volume":
            if any(i.get("kind") == "volume" for i in target["items"]):
                raise ValueError(f"Layer '{target.get('name')}' already shows volume")
            item.update(id="volume" if "volume" not in _all_ids(cfg) else _new_id("v"), kind="volume")
            what = f"Added volume to {target.get('name')}"
        case _:
            raise ValueError("kind must be indicator, compare or volume")
    if color:
        item["color"] = color
    target["items"].append(item)
    out = save(symbol, cfg, what)
    return {"added": item["id"], "layer": target["id"], "chart": out}


def update_item(symbol: str, item_id: str, params: dict | None = None, styles: dict | None = None,
                levels: list[float] | None = None, reset_levels: bool = False, visible: bool | None = None,
                pane: str | None = None, chart_type: str | None = None, display: str | None = None,
                color: str | None = None, move_to_layer: str | None = None, author: str = "agent") -> dict:
    cfg = load(symbol)
    layer, it = _find_item(cfg, item_id)
    kind = it.get("kind")
    if params is not None:
        if kind != "indicator":
            raise ValueError("params only apply to indicators")
        _, it["params"] = _indicator_params(it["name"], {**(it.get("params") or {}), **params})
    if styles is not None:
        if kind != "indicator":
            raise ValueError("styles only apply to indicators (use color for other series)")
        merged = {**(it.get("styles") or {})}
        for output, st in (_check_styles(styles) or {}).items():
            merged[output] = {**merged.get(output, {}), **st}
            if styles[output].get("visible") is True:
                merged[output].pop("visible", None)
        it["styles"] = {k: v for k, v in merged.items() if v}
        it.pop("color", None)
    if reset_levels:
        it.pop("levels", None)
    elif levels is not None:
        it["levels"] = [float(x) for x in levels]
    if visible is not None:
        it["visible"] = visible
    if pane is not None:
        if kind == "price":
            raise ValueError("The price chart always stays in the main pane")
        it["pane"] = _check_pane(cfg, pane)
    if chart_type is not None:
        if kind != "price":
            raise ValueError("chart_type applies to a price item")
        if chart_type not in CHART_TYPES:
            raise ValueError(f"chart_type must be one of {', '.join(CHART_TYPES)}")
        it["chartType"] = chart_type
    if display is not None:
        if kind != "compare" or display not in DISPLAYS:
            raise ValueError(f"display ({', '.join(DISPLAYS)}) applies to another symbol's series")
        it["display"] = display
    if color is not None:  # "" = back to the automatic color
        if color:
            it["color"] = color
        else:
            it.pop("color", None)
    if move_to_layer:
        dest = _layer(cfg, move_to_layer)
        if dest is not layer:
            if kind in ("price", "volume") and any(i.get("kind") == kind for i in dest["items"]):
                raise ValueError(f"Layer '{dest.get('name')}' already has a {kind} item")
            layer["items"].remove(it)
            dest["items"].append(it)
    return save(symbol, cfg, f"Changed {it.get('name') or it.get('symbol') or kind}")


def remove_items(symbol: str, item_ids: list[str], author: str = "agent") -> dict:
    cfg = load(symbol)
    for iid in item_ids:
        layer, it = _find_item(cfg, iid)
        if it.get("kind") == "price":
            charts = [l for l in cfg["layers"] if any(i.get("kind") == "price" for i in l["items"])]
            if len(charts) < 2:
                raise ValueError("The only price chart cannot be removed (hide it with visible=false instead)")
        layer["items"].remove(it)
    return save(symbol, cfg, f"Removed {len(item_ids)} item(s)")


def manage_layer(symbol: str, action: str, layer_id: str | None = None, name: str | None = None,
                 visible: bool | None = None, locked: bool | None = None, interval: str | None = None,
                 untie_interval: bool = False, with_price_chart: bool = False, position: str | None = None,
                 activate: bool = False, author: str = "agent") -> dict:
    cfg = load(symbol)
    iv = get_interval(interval).name if interval else None
    if action == "create":
        layer = {"id": _new_id("l"), "name": (name or "Layer").strip()[:40] or "Layer", "visible": True, "locked": False,
                 "items": []}
        if iv:
            layer["interval"] = iv
        if with_price_chart:
            layer["items"] += [{"id": _new_id("p"), "kind": "price", "visible": True, "pane": "main", "chartType": "candles"},
                               {"id": _new_id("v"), "kind": "volume", "visible": True, "pane": "auto"}]
        cfg["layers"].insert(0 if position != "bottom" else len(cfg["layers"]), layer)
        if activate:
            cfg["activeLayer"] = layer["id"]
        out = save(symbol, cfg, f"Created layer {layer['name']}")
        return {"created": layer["id"], "chart": out}
    if not layer_id:
        raise ValueError("layer_id required")
    layer = _layer(cfg, layer_id)
    if action == "delete":
        if len(cfg["layers"]) == 1:
            raise ValueError("A chart needs at least one layer")
        cfg["layers"].remove(layer)
        if not any(i.get("kind") == "price" for l in cfg["layers"] for i in l["items"]):
            price = next((i for i in layer["items"] if i.get("kind") == "price"), None)
            if price:  # keep the only price chart, as the UI does
                cfg["layers"][-1]["items"].insert(0, price)
        if cfg.get("activeLayer") == layer["id"]:
            cfg["activeLayer"] = cfg["layers"][0]["id"]
        return save(symbol, cfg, f"Deleted layer {layer.get('name')}")
    if action != "update":
        raise ValueError("action must be create, update or delete")
    if name is not None:
        layer["name"] = name.strip()[:40] or layer["name"]
    if visible is not None:
        layer["visible"] = visible
    if locked is not None:
        layer["locked"] = locked
    if untie_interval:
        layer.pop("interval", None)
    elif iv:
        layer["interval"] = iv
    if with_price_chart and not any(i.get("kind") == "price" for i in layer["items"]):
        layer["items"][:0] = [{"id": _new_id("p"), "kind": "price", "visible": True, "pane": "main", "chartType": "candles"},
                              {"id": _new_id("v"), "kind": "volume", "visible": True, "pane": "auto"}]
    if position in ("top", "bottom"):
        cfg["layers"].remove(layer)
        cfg["layers"].insert(0 if position == "top" else len(cfg["layers"]), layer)
    if activate:
        cfg["activeLayer"] = layer["id"]
    return save(symbol, cfg, f"Changed layer {layer.get('name')}")


def set_options(symbol: str, interval: str | None = None, chart_type: str | None = None, scale: str | None = None,
                author: str = "agent") -> dict | None:
    """Chart type of the chart layer shown at [interval] and the price scale; None when nothing to change."""
    if chart_type is None and scale is None:
        return None
    cfg = load(symbol)
    if scale is not None:
        if scale not in SCALES:
            raise ValueError(f"scale must be one of {', '.join(SCALES)}")
        cfg["scale"] = scale
    if chart_type is not None:
        if chart_type not in CHART_TYPES:
            raise ValueError(f"chart_type must be one of {', '.join(CHART_TYPES)}")
        layer = chart_layer_for(cfg, interval or "1d")
        price = next((i for i in layer["items"] if i.get("kind") == "price"), None)
        if price is not None:
            price["chartType"] = chart_type
    return save(symbol, cfg, "Changed chart style")
