"""Chart layers: annotation layer field + DB migration, per-symbol chart configuration, state saves."""

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from autobench import db
from autobench.app import create_app
from autobench.schemas import AnnotationIn, AnnotationPatch, Point
from autobench.services import annotations, workspace


@pytest.fixture
def client():
    with TestClient(create_app()) as c:
        yield c


def test_migration_adds_layer_column_to_existing_database(tmp_path):
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    # the annotations table as created by the first release (no `layer` column)
    con.execute("""CREATE TABLE annotations (id VARCHAR PRIMARY KEY, symbol VARCHAR, interval VARCHAR, type VARCHAR,
                   points JSON, text TEXT, style JSON, author VARCHAR, tag VARCHAR, visible BOOLEAN,
                   created_at DATETIME, updated_at DATETIME)""")
    con.execute("""INSERT INTO annotations VALUES ('a1', 'SPY', NULL, 'horizontal_line', '[{"time": null, "price": 500}]',
                   NULL, '{}', 'user', NULL, 1, '2025-01-01 00:00:00', '2025-01-01 00:00:00')""")
    con.commit()
    con.close()
    db.init_db(f"sqlite:///{path}")
    cols = [r[1] for r in sqlite3.connect(path).execute("PRAGMA table_info(annotations)")]
    assert "layer" in cols
    old = annotations.list_("SPY")
    assert old[0]["id"] == "a1" and old[0]["layer"] is None  # existing drawings survive in the default layer
    db.init_db(f"sqlite:///{path}")  # idempotent


def test_layer_defaults_patch_and_delete_by_layer():
    user = annotations.create("AAPL", [AnnotationIn(type="horizontal_line", points=[Point(price=100)])], author="user")[0]
    agent = annotations.create("AAPL", [AnnotationIn(type="horizontal_line", points=[Point(price=110)])], author="agent")[0]
    mine = annotations.create("AAPL", [AnnotationIn(type="horizontal_line", points=[Point(price=120)], layer="ideas")],
                              author="user")[0]
    assert (user["layer"], agent["layer"], mine["layer"]) == (None, "agent", "ideas")
    moved = annotations.update(user["id"], AnnotationPatch(layer="ideas"))
    assert moved["layer"] == "ideas"
    back = annotations.update(user["id"], AnnotationPatch(layer=""))
    assert back["layer"] is None
    assert annotations.delete_(symbol="AAPL", layer="ideas")["deleted"] == 1
    assert annotations.delete_(symbol="AAPL", layer="main")["deleted"] == 1  # the default layer (NULL)
    assert [a["layer"] for a in annotations.list_("AAPL")] == ["agent"]


CFG = {"layers": [
    {"id": "main", "name": "Main", "visible": True, "items": [
        {"id": "price", "kind": "price", "chartType": "candles", "visible": True},
        {"id": "volume", "kind": "volume", "visible": True},
        {"id": "i1", "kind": "indicator", "name": "sma", "params": {"length": 20}, "visible": True},
    ]},
    {"id": "l2", "name": "Spread", "visible": False, "items": [
        {"id": "c1", "kind": "compare", "symbol": "SPY", "display": "pane", "visible": True},
    ]},
], "activeLayer": "main"}


def test_chart_config_endpoints(client):
    assert client.get("/api/chart-config/aapl").json() == {"layers": None}
    saved = client.put("/api/chart-config/aapl", json=CFG).json()
    assert saved["layers"][0]["items"][2]["params"] == {"length": 20} and saved["updated_at"]
    got = client.get("/api/chart-config/AAPL").json()
    assert got["activeLayer"] == "main" and len(got["layers"]) == 2
    assert client.put("/api/chart-config/AAPL", json={"nope": 1}).status_code == 400
    # the default template for new symbols
    assert client.put("/api/chart-config/_default", json=CFG).status_code == 200
    assert client.get("/api/chart-config/_default").json()["layers"][0]["name"] == "Main"
    assert client.get("/api/chart-config").json() == ["AAPL", "_default"]
    # sendBeacon posts raw JSON text
    r = client.post("/api/chart-config/MSFT", content=json.dumps(CFG), headers={"Content-Type": "text/plain"})
    assert r.status_code == 200 and client.get("/api/chart-config/MSFT").json()["layers"][1]["name"] == "Spread"
    r = client.post("/api/ui-state", content=json.dumps({"v": 3, "page": "chart"}), headers={"Content-Type": "text/plain"})
    assert r.status_code == 200 and client.get("/api/ui-state").json()["v"] == 3
    assert client.delete("/api/chart-config/MSFT").status_code == 200
    assert client.delete("/api/chart-config/MSFT").status_code == 404
    assert client.delete("/api/annotations", params={"symbol": "AAPL", "layer": "l2"}).json()["deleted"] == 0


def test_agent_sees_the_users_layers():
    workspace.save_chart_config("SPY", CFG)
    summary = workspace.layers_summary("SPY")
    assert [l["name"] for l in summary] == ["Main", "Spread"]
    assert summary[0]["items"][2]["label"] == "sma(20)"
    assert summary[1]["items"][0] == {"kind": "compare", "label": "SPY (pane)", "visible": True}
    from autobench.mcp_server import get_chart_view

    view = json.loads(get_chart_view())
    assert view["symbol"] == "SPY" and view["layers"][1]["visible"] is False
