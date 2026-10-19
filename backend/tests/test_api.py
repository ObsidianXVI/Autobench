"""REST API + WebSocket integration (FastAPI TestClient, fake data)."""

import pytest
from fastapi.testclient import TestClient

from autobench.app import create_app


@pytest.fixture
def client():
    with TestClient(create_app()) as c:
        yield c


def test_health_and_market_endpoints(client):
    assert client.get("/api/health").json()["bar_provider"] == "fake"
    bars = client.get("/api/bars/aapl", params={"interval": "1d", "lookback": "3mo"}).json()
    assert bars["symbol"] == "AAPL" and len(bars["bars"]) > 50
    assert set(bars["bars"][0]) == {"time", "open", "high", "low", "close", "volume"}
    assert client.get("/api/quote/AAPL").json()["price"] > 0
    assert client.get("/api/symbols/search", params={"q": "MS"}).json()[0]["symbol"] == "MSFT"
    assert len(client.get("/api/indicators").json()) >= 40


def test_indicator_series_align_with_bars(client):
    body = {"interval": "1d", "lookback": "6mo", "indicators": [{"name": "sma", "params": {"length": 50}},
                                                                  {"name": "macd"}]}
    r = client.post("/api/indicators/AAPL", json=body).json()
    bars = client.get("/api/bars/AAPL", params={"interval": "1d", "lookback": "6mo"}).json()["bars"]
    assert r["time"] == [b["time"] for b in bars]
    sma = r["results"][0]
    assert sma["overlay"] is True and len(sma["values"]["sma"]) == len(bars)
    assert sma["values"]["sma"][0] is not None, "warm-up history must be fetched so the series is complete"
    assert set(r["results"][1]["values"]) == {"macd", "signal", "histogram"}


def test_error_mapping(client):
    assert client.get("/api/bars/AAPL", params={"interval": "7m"}).status_code == 400
    assert client.get("/api/analyses/doesnotexist").status_code == 404
    assert client.post("/api/annotations/AAPL", json={"type": "trendline", "points": [{"price": 1}]}).status_code == 422


def test_annotation_crud_and_ws_events(client):
    with client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "hello"
        a = client.post("/api/annotations/AAPL", json={
            "type": "rectangle", "points": [{"time": "2025-01-02", "price": 100}, {"time": "2025-02-03", "price": 110}],
            "style": {"fill_color": "rgba(34,197,94,0.15)"}, "tag": "zone"}).json()
        assert a["author"] == "user" and a["points"][0]["time"] == 1735776000
        ev = ws.receive_json()
        assert ev["type"] == "annotation.created" and ev["data"]["id"] == a["id"]
        u = client.patch(f"/api/annotations/{a['id']}", json={"text": "demand zone", "style": {"color": "#22c55e"}}).json()
        assert u["text"] == "demand zone" and u["style"]["fill_color"] and u["style"]["color"] == "#22c55e"
        assert ws.receive_json()["type"] == "annotation.updated"
        listed = client.get("/api/annotations", params={"symbol": "aapl"}).json()
        assert [x["id"] for x in listed] == [a["id"]]
        assert client.delete(f"/api/annotations/{a['id']}").json()["deleted"] == 1
        assert ws.receive_json()["type"] == "annotation.deleted"
        assert client.delete(f"/api/annotations/{a['id']}").status_code == 404


def test_annotation_interval_filter(client):
    client.post("/api/annotations/AAPL", json={"type": "horizontal_line", "points": [{"price": 100}], "interval": "1h"})
    client.post("/api/annotations/AAPL", json={"type": "horizontal_line", "points": [{"price": 101}]})
    assert len(client.get("/api/annotations", params={"symbol": "AAPL", "interval": "1d"}).json()) == 1
    assert len(client.get("/api/annotations", params={"symbol": "AAPL", "interval": "1h"}).json()) == 2


def test_view_watchlist_notes(client):
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        v = client.put("/api/view", json={"symbol": "nvda", "interval": "1wk"}).json()
        assert v["symbol"] == "NVDA" and v["updated_by"] == "user"
        assert ws.receive_json()["type"] == "view.changed"
    assert client.get("/api/view").json()["interval"] == "1wk"
    w = client.put("/api/watchlists/tech", json={"add": ["aapl", "msft", "aapl"]}).json()
    assert w["symbols"] == ["AAPL", "MSFT"]
    w = client.put("/api/watchlists/tech", json={"remove": ["AAPL"]}).json()
    assert w["symbols"] == ["MSFT"]
    names = [x["name"] for x in client.get("/api/watchlists").json()]
    assert "tech" in names
    n = client.post("/api/notes", json={"title": "Thesis", "content": "AI capex", "symbol": "nvda", "tags": ["thesis"]}).json()
    assert client.get("/api/notes", params={"symbol": "NVDA"}).json()[0]["id"] == n["id"]
    assert client.get("/api/notes", params={"q": "capex"}).json()[0]["title"] == "Thesis"
    client.put(f"/api/notes/{n['id']}", json={"title": "Thesis v2", "content": "x", "symbol": "NVDA"})
    assert client.get("/api/notes").json()[0]["title"] == "Thesis v2"


def test_backtest_and_study_endpoints(client):
    r = client.post("/api/backtest", json={"symbol": "AAPL", "entry": "crosses_above(sma(20), sma(50))",
                                           "exit": "crosses_below(sma(20), sma(50))"})
    assert r.status_code == 200 and r.json()["performance"]["trades"] > 0
    bad = client.post("/api/backtest", json={"symbol": "AAPL", "entry": "import os", "exit": "close > 0"})
    assert bad.status_code == 400
    s = client.post("/api/signal-study", json={"symbol": "AAPL", "condition": "rsi(14) < 35"})
    assert s.status_code == 200 and "forward_returns" in s.json()


def test_event_relay_local_only(client):
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        assert client.post("/api/events/relay", json={"type": "annotation.created", "data": {"id": "x"}}).json()["ok"]
        assert ws.receive_json()["data"]["id"] == "x"
    assert client.post("/api/events/relay", json={"data": 1}).status_code == 400


def test_mcp_endpoint_mounted(client):
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}},
        headers={"Accept": "application/json, text/event-stream", "Host": "127.0.0.1:8000"})
    assert r.status_code == 200
    assert "autobench" in r.text


def test_workbench_endpoints(client):
    from autobench.services.universe import universe

    universe.ensure(wait=True)
    q = client.get("/api/quotes", params={"symbols": "AAPL,MSFT,^VIX"}).json()
    assert {"AAPL", "MSFT"} <= set(q) and q["AAPL"]["price"] > 0
    meta = client.get("/api/screener/meta").json()
    assert {f["key"] for f in meta["fields"]} >= {"pe", "rsi14", "market_cap"} and "valuation" in meta["views"]
    r = client.post("/api/screener", json={"expression": "pe > 0", "sort": "pe", "view": "valuation", "limit": 5}).json()
    pes = [x["pe"] for x in r["rows"]]
    assert pes == sorted(pes) and r["columns"][0] == "symbol"
    bad = client.post("/api/screener", json={"expression": "import os"})
    assert bad.status_code == 400
    assert client.put("/api/screens/mine", json={"signal": "top_gainers"}).json()["name"] == "mine"
    assert client.get("/api/screens").json()[0]["signal"] == "top_gainers"
    assert client.delete("/api/screens/mine").status_code == 200
    assert client.get("/api/snapshot/AAPL").json()["symbol"] == "AAPL"
    assert client.get("/api/snapshot/ZZZZ").json()["in_universe"] is False
    hm = client.get("/api/heatmap", params={"universe": "all", "period": "1m"}).json()
    assert hm["metric"] == "perf_1m" and hm["sectors"]
    assert client.get("/api/breadth").json()["count"] == 10
    assert set(client.get("/api/universe/status").json()) == {"listings", "fundamentals", "bars", "quotes"}
    assert client.post("/api/universe/refresh", params={"parts": "nope"}).status_code == 400
    assert client.get("/api/macro").json()["curve"]
    assert client.get("/api/macro/series/UNRATE").json()["points"]
    assert client.get("/api/calendar/earnings", params={"days": 3}).json()["count"] >= 1
    assert client.get("/api/calendar/economic").json()["count"] >= 1


def test_alert_and_layout_endpoints(client):
    a = client.post("/api/alerts", json={"symbol": "aapl", "kind": "below", "value": 1}).json()
    assert a["symbol"] == "AAPL" and a["status"] == "active"
    assert client.post("/api/alerts", json={"symbol": "AAPL", "kind": "below"}).status_code == 400
    assert client.patch(f"/api/alerts/{a['id']}", json={"status": "paused"}).json()["status"] == "paused"
    assert client.patch(f"/api/alerts/{a['id']}", json={"symbol": "X"}).status_code == 400
    assert client.get("/api/alerts", params={"status": "paused"}).json()[0]["id"] == a["id"]
    assert client.delete(f"/api/alerts/{a['id']}").status_code == 200
    assert client.delete(f"/api/alerts/{a['id']}").status_code == 404
    lay = {"grid": "2x2", "charts": [{"symbol": "SPY", "interval": "1d"}]}
    assert client.put("/api/layouts/Quad", json={"data": lay, "summary": "4 charts"}).json()["name"] == "Quad"
    assert client.get("/api/layouts").json()[0]["summary"] == "4 charts"
    assert client.get("/api/layouts/Quad").json()["data"] == lay
    assert client.delete("/api/layouts/Quad").status_code == 200
    assert client.get("/api/layouts/Quad").status_code == 404
    assert client.put("/api/ui-state", json={"page": "heatmap"}).json() == {"ok": True}
    assert client.get("/api/ui-state").json() == {"page": "heatmap"}
