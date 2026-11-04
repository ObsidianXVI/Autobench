"""The agent drives the web app through MCP: chart layers, layouts, watchlists, alerts, notes, screens,
order-change proposals (approved by the user), and the trading journal."""

import json

import pytest
from fastapi.testclient import TestClient
from mcp import Client

from autobench.app import create_app
from autobench.mcp_server import mcp
from autobench.services import workspace
from autobench.services.events import bus

pytestmark = pytest.mark.anyio
H = {"X-Autobench": "1"}


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def client():
    with TestClient(create_app()) as c:
        c.post("/api/trading/sim/price", json={"symbol": "AAPL", "price": 100}, headers=H)
        yield c


async def call(c, name, args=None):
    r = await c.call_tool(name, args or {})
    text = r.content[0].text
    return r.is_error, (text if r.is_error else json.loads(text))


def last_event(kind):
    return next(e["data"] for e in reversed(bus.history) if e["type"] == kind)


async def test_chart_layers_through_mcp(client):
    async with Client(mcp) as c:
        await call(c, "set_chart_view", {"symbol": "NVDA", "interval": "1d"})
        err, d = await call(c, "get_chart_layers")
        assert not err and d["symbol"] == "NVDA" and d["layers"][0]["id"] == "main"  # built-in until the user edits
        err, r = await call(c, "edit_chart_add", {"kind": "indicator", "name": "ema", "params": {"length": 21},
                                                  "styles": {"ema": {"color": "#22c55e", "width": 2, "line": "dashed"}}})
        assert not err and r["layer"] == "agent"
        ema = r["added"]
        cfg = workspace.get_chart_config("NVDA")
        agent = next(l for l in cfg["layers"] if l["id"] == "agent")
        assert agent["items"][0] == {"visible": True, "pane": "auto", "id": ema, "kind": "indicator", "name": "ema",
                                     "params": {"length": 21}, "styles": {"ema": {"color": "#22c55e", "width": 2.0, "line": "dashed"}}}
        err, msg = await call(c, "edit_chart_add", {"kind": "indicator", "name": "nope"})
        assert err
        err, _ = await call(c, "edit_chart_item", {"item_id": ema, "params": {"length": 9}, "styles": {"ema": {"width": 3}},
                                                   "move_to_layer": "Main"})
        assert not err
        main = workspace.get_chart_config("NVDA")["layers"][1]
        it = next(i for i in main["items"] if i["id"] == ema)
        assert it["params"] == {"length": 9} and it["styles"]["ema"] == {"color": "#22c55e", "width": 3.0, "line": "dashed"}
        # a 30m entry layer with its own price chart, and another symbol in a new pane
        err, r = await call(c, "edit_chart_layer", {"action": "create", "name": "Entry", "interval": "30m", "with_price_chart": True})
        assert not err
        err, r2 = await call(c, "edit_chart_add", {"kind": "compare", "compare_symbol": "amd", "layer": "Entry", "pane": "new",
                                                   "display": "candles"})
        assert not err and r2["layer"] == r["created"]
        d = r2["chart"]
        entry = next(l for l in d["layers"] if l["name"] == "Entry")
        assert entry["interval"] == "30m" and entry["chart"] and [i["kind"] for i in entry["items"]] == ["price", "volume", "compare"]
        err, _ = await call(c, "set_chart_view", {"chart_type": "heikin", "scale": "log"})
        cfg = workspace.get_chart_config("NVDA")
        assert cfg["scale"] == "log"
        main_price = next(i for i in cfg["layers"][-1]["items"] if i["kind"] == "price")
        assert main_price["chartType"] == "heikin"  # the untied Main layer is the 1d chart
        err, _ = await call(c, "edit_chart_remove", {"item_ids": [ema]})
        assert not err and ema not in json.dumps(workspace.get_chart_config("NVDA"))
        err, msg = await call(c, "edit_chart_remove", {"item_ids": ["price"]})
        assert not err  # the Entry layer still has a price chart
        err, msg = await call(c, "edit_chart_layer", {"action": "delete", "layer_id": "Entry"})
        assert not err
        cfg = workspace.get_chart_config("NVDA")
        assert sum(i["kind"] == "price" for l in cfg["layers"] for i in l["items"]) == 1  # the last price chart is kept


async def test_layouts_watchlists_alerts_notes_screens(client):
    async with Client(mcp) as c:
        err, r = await call(c, "set_chart_layout", {"grid": "2v", "charts": [{"symbol": "aapl"}, {"symbol": "msft", "interval": "1h"}]})
        assert not err and r["applied"]["charts"][1] == {"symbol": "MSFT", "interval": "1h"}
        assert last_event("ui.layout")["data"]["grid"] == "2v"
        workspace.set_ui_state({"layout": r["applied"]})
        err, _ = await call(c, "manage_layouts", {"action": "save_current", "name": "Pair"})
        err, ls = await call(c, "manage_layouts", {"action": "list"})
        assert [x["name"] for x in ls] == ["Pair"]
        err, r = await call(c, "set_chart_layout", {"load_layout": "Pair"})
        assert r["applied"]["grid"] == "2v"
        # watchlists with sections and rename
        await call(c, "update_watchlist", {"name": "tech", "add": ["aapl"]})
        await call(c, "update_watchlist", {"name": "tech", "add": ["nvda", "amd"], "section": "semis"})
        err, wl = await call(c, "update_watchlist", {"name": "tech", "add": ["intc"], "section": "SEMIS", "rename_to": "Tech"})
        assert wl == {**wl, "name": "Tech", "symbols": ["AAPL", "###SEMIS", "NVDA", "AMD", "INTC"]}
        err, _ = await call(c, "delete_watchlist", {"name": "Tech"})
        assert not err and [w["name"] for w in workspace.list_watchlists()] == ["default"]
        # alerts
        err, a = await call(c, "create_alert", {"symbol": "AAPL", "kind": "above", "value": 500})
        err, a = await call(c, "update_alert", {"alert_id": a["id"], "value": 1, "status": "paused"})
        assert (a["value"], a["status"]) == (1, "paused")
        err, box = await call(c, "get_alert_inbox")
        assert box == {"items": [], "unread": 0}
        # notes and screens
        err, n = await call(c, "save_note", {"title": "t", "content": "c", "symbol": "AAPL"})
        err, _ = await call(c, "delete_note", {"note_id": n["id"]})
        assert not err
        await call(c, "save_screen", {"name": "Cheap", "filters": []})
        err, ss = await call(c, "list_screens")
        assert "Cheap" in json.dumps(ss)
        err, _ = await call(c, "delete_screen", {"name": "Cheap"})
        assert not err
        err, _ = await call(c, "show_in_ui", {"page": "trading", "tab": "proposals"})
        assert last_event("ui.navigate")["tab"] == "proposals"


async def test_order_change_proposal_needs_the_users_approval(client):
    t = client.post("/api/trading/orders", json={"symbol": "AAPL", "side": "buy", "qty": 10, "order_type": "limit",
                                                 "limit_price": 101, "stop_loss": 95}, headers=H).json()
    assert t["status"] == "filled"
    sl = next(o for o in client.get("/api/trading/orders").json() if o["role"] == "stop_loss")
    async with Client(mcp) as c:
        err, msg = await call(c, "propose_order_change", {"order_id": sl["id"], "stop_price": 105, "rationale": "Trail it up"})
        assert err and "through the market" in msg
        err, msg = await call(c, "propose_order_change", {"order_id": sl["id"], "limit_price": 99, "rationale": "Trail it up"})
        assert err and "without a limit" in msg
        err, p = await call(c, "propose_order_change", {"order_id": sl["id"], "stop_price": 92, "rationale": "Give it room to breathe"})
        assert not err and p["status"] == "pending" and p["spec"]["changes"] == {"stop_price": 92.0}
        assert "further away" in p["check"]["warnings"][0]
        err, p = await call(c, "propose_order_change", {"order_id": sl["id"], "stop_price": 99, "rationale": "Breakeven after the move"})
        # still unchanged until the user approves; the agent has no tool to approve
        assert next(o for o in client.get("/api/trading/orders").json() if o["role"] == "stop_loss")["stop_price"] == 95
        a = client.post(f"/api/trading/tickets/{p['id']}/approve", headers=H).json()
        assert a["status"] == "applied"
        assert next(o for o in client.get("/api/trading/orders").json() if o["role"] == "stop_loss")["stop_price"] == 99
        # an order change does not count as an order sent today
        assert client.get("/api/trading/tickets", params={"status": "pending"}).json()[0]["spec"]["changes"]["stop_price"] == 92


async def test_journal(client):
    client.post("/api/trading/orders", json={"symbol": "AAPL", "side": "buy", "qty": 10, "order_type": "limit",
                                             "limit_price": 101, "stop_loss": 95, "take_profit": 120}, headers=H)
    client.post("/api/trading/sim/price", json={"symbol": "AAPL", "price": 110}, headers=H)
    client.post("/api/trading/positions/AAPL/close", json={}, headers=H)
    j = client.get("/api/journal/trades").json()
    (t,) = j["trades"]
    assert t["status"] == "closed" and t["planned_stop"] == 95 and t["planned_target"] == 120 and t["net_pnl"] > 0
    a = t["analysis"]
    alts = {x["label"]: x for x in a["alternatives"]}
    assert alts["At the planned stop"]["net_pnl"] < 0 < alts["At the planned target"]["vs_actual"]
    assert "in_trade" in a  # fake bars exist for the trade window
    client.post("/api/notes", json={"title": "Why I sold", "content": "Into resistance", "symbol": "AAPL", "tags": [f"trade:{t['id']}"]})
    t2 = client.get(f"/api/journal/trades/{t['id']}").json()
    assert t2["notes"][0]["title"] == "Why I sold" and len(t2["path"]) > 5
    client.post("/api/annotations/AAPL", json={"type": "horizontal_line", "points": [{"price": 100}]})
    d = client.get("/api/journal/drawings").json()
    assert d["count"] == 1 and d["symbols"][0]["symbol"] == "AAPL"
    p = client.get("/api/journal/performance").json()
    assert p["period"]["closed_trades"] == 1 and p["by_symbol"][0]["key"] == "AAPL" and p["curve"][0]["pnl"] == t["net_pnl"]
    n = client.get("/api/journal/notes").json()
    assert n["symbols"][0]["items"][0]["trade_id"] == t["id"]
    async with Client(mcp) as c:
        err, perf = await call(c, "get_journal", {"section": "performance"})
        assert not err and perf["stats"]["closed_trades"] == 1
        err, tr = await call(c, "get_journal", {"section": "trade", "trade_id": t["id"]})
        assert not err and tr["id"] == t["id"]


def test_post_exit_analysis_of_an_older_trade():
    import time

    from autobench.services.journal import analyze_trade, price_path
    from autobench.services.market import get_bars

    now = int(time.time())
    df = get_bars("AAPL", "1d", lookback="1y").df
    opened, closed = int(df.index[-60]), int(df.index[-50])
    entry, exit_ = float(df["c"].iloc[-60]), float(df["c"].iloc[-50])
    t = {"symbol": "AAPL", "side": "long", "status": "closed", "opened_at": opened, "closed_at": closed, "entry_avg": entry,
         "exit_avg": exit_, "exit_qty": 10, "max_qty": 10, "commission": 2.0, "net_pnl": round((exit_ - entry) * 10 - 2, 2),
         "planned_stop": entry * 0.95, "planned_target": None, "fills": []}
    a = analyze_trade(t, now)
    after = a["after_exit"]
    assert after["sessions"] == 20 and set(after["marks"]) == {"1d", "5d", "20d"}
    p5 = float(df["c"].iloc[-45])
    assert after["marks"]["5d"]["price"] == round(p5, 2) and after["marks"]["5d"]["vs_exit"] == round((p5 - exit_) * 10, 2)
    assert after["best_price"] >= after["marks"]["5d"]["price"] >= after["worst_price"]
    it = a["in_trade"]
    assert it["interval"] == "1d" and it["best_price"] >= max(entry, exit_) and it["mae"] <= 0 <= it["mfe"]
    labels = [x["label"] for x in a["alternatives"]]
    assert labels[:1] == ["At the planned stop"] and "Held 20 more sessions" in labels
    path = price_path("AAPL", opened, closed, now)
    assert len(path) == 10 + 11 + 20
