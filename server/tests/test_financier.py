"""Financier mode: serving its web build at /financier/ and the read-only link to its Firestore data."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from autobench.app import create_app
from autobench.config import get_settings
from autobench.services import financier


def _v(x):
    """A Python value as a Firestore REST Value (what the API returns)."""
    if x is None:
        return {"nullValue": None}
    if isinstance(x, bool):
        return {"booleanValue": x}
    if isinstance(x, int):
        return {"integerValue": str(x)}
    if isinstance(x, float):
        return {"doubleValue": x}
    if isinstance(x, dict):
        return {"mapValue": {"fields": {k: _v(v) for k, v in x.items()}}}
    if isinstance(x, list):
        return {"arrayValue": {"values": [_v(v) for v in x]}}
    return {"stringValue": x}


DATA = {
    "physicalAccounts": [{"id": "ibkr", "name": "IBKR"}, {"id": "dbs", "name": "DBS"}],
    "virtualAccounts": [{"id": "lfund", "name": "L-Fund"}, {"id": "swing", "name": "Swing"}, {"id": "ops", "name": "Ops"}],
    "virtualAccountBackings": [{"id": "lfund", "physicalAccountIds": ["ibkr"]},
                               {"id": "swing", "physicalAccountIds": ["ibkr", "dbs"]},
                               {"id": "ops", "physicalAccountIds": ["dbs"]}],
    "accountBalances": [
        {"id": "b1", "physicalAccountId": "ibkr", "amount": 10000.0, "recordedAt": "2026-10-01T00:00:00Z"},
        {"id": "b2", "physicalAccountId": "ibkr", "amount": 12500.5, "recordedAt": "2026-11-01T00:00:00Z"},
        {"id": "b3", "physicalAccountId": "dbs", "amount": 3000, "recordedAt": "2026-11-02T00:00:00Z"},
    ],
    "virtualLedgerEntries": [
        {"id": "l1", "virtualAccountId": "lfund", "amount": 9000.0},
        {"id": "l2", "virtualAccountId": "lfund", "amount": -500.0},
        {"id": "l3", "virtualAccountId": "swing", "amount": 4000},
    ],
    "portfolios": [
        {"id": "p1", "name": "Swing Portfolio", "physicalAccountId": "ibkr", "brokerAccountId": "U123", "baseCurrency": "SGD"},
        {"id": "p2", "name": "Paper", "physicalAccountId": "dbs"},
    ],
    "trades": [
        {"id": "t1", "portfolioId": "p1", "symbol": "nvda", "direction": "long", "quantity": 10, "entryPrice": 100.0,
         "stopLossPrice": 92.0, "takeProfitPrice": 125.0, "openedAt": "2026-10-10T00:00:00Z", "note": "base breakout"},
        {"id": "t2", "portfolioId": "p2", "symbol": "NVDA", "direction": "long", "quantity": 1, "entryPrice": 99.0,
         "openedAt": "2026-10-11T00:00:00Z"},
        {"id": "t3", "portfolioId": "p1", "symbol": "AAPL", "direction": "long", "quantity": 5, "entryPrice": 200.0,
         "openedAt": "2026-01-01T00:00:00Z", "closedAt": "2026-02-01T00:00:00Z", "exitPrice": 210.0},
    ],
    "tradeDeposits": [{"id": "d1", "portfolioId": "p1", "amount": 5000.0}, {"id": "d2", "portfolioId": "p1", "amount": 2500}],
}


def test_decode_round_trip():
    raw = {"a": 1, "b": 2.5, "c": "x", "d": None, "e": True, "f": [1, "y"], "g": {"h": 3}}
    assert {k: financier.decode(_v(v)) for k, v in raw.items()} == raw
    assert financier.decode({"timestampValue": "2026-11-01T00:00:00Z"}) == "2026-11-01T00:00:00Z"


def test_build_summary_links_the_broker_account():
    s = financier.build_summary(DATA, broker_account="U123")
    linked = s["linked_portfolio"]
    assert linked["name"] == "Swing Portfolio" and linked["base_currency"] == "SGD"
    assert linked["physical_account"] == {"id": "ibkr", "name": "IBKR", "balance": 12500.5,
                                          "recorded_at": "2026-11-01T00:00:00Z"}  # the latest observation
    assert {v["name"]: v["balance"] for v in linked["virtual_accounts"]} == {"L-Fund": 8500.0, "Swing": 4000.0}
    assert linked["deposits"] == 7500.0
    assert next(v for v in s["virtual_accounts"] if v["name"] == "Swing")["backed_by"] == ["IBKR", "DBS"]
    assert next(v for v in s["virtual_accounts"] if v["name"] == "Ops")["balance"] is None  # no ledger yet
    # open trades only; one plan per symbol, the linked portfolio's first
    assert {t["symbol"] for t in s["open_trades"]} == {"NVDA"} and len(s["open_trades"]) == 2
    assert s["plans"]["NVDA"]["stop"] == 92.0 and s["plans"]["NVDA"]["target"] == 125.0
    assert s["plans"]["NVDA"]["portfolio"] == "Swing Portfolio"


def test_build_summary_without_a_matching_account():
    s = financier.build_summary(DATA, broker_account="U999")
    assert s["linked_portfolio"] is None and not any(p["linked"] for p in s["portfolios"])
    assert s["plans"]["NVDA"]["stop"] == 92.0  # still the plan that has a stop


def _transport(calls):
    def handler(request: httpx.Request):
        calls.append(request)
        assert request.headers["authorization"] == "Bearer tok"
        assert "/projects/test-project/databases/(default)/documents/" in request.url.path
        coll = request.url.path.rsplit("/", 1)[-1]
        docs = DATA.get(coll, [])
        page = request.url.params.get("pageToken")
        # two pages for trades, to exercise pagination
        if coll == "trades" and page is None:
            chunk, nxt = docs[:1], "p2"
        elif coll == "trades":
            chunk, nxt = docs[1:], None
        else:
            chunk, nxt = docs, None
        body = {"documents": [{"name": f"projects/x/databases/(default)/documents/{coll}/{d['id']}",
                               "fields": {k: _v(v) for k, v in d.items() if k != "id"}} for d in chunk]}
        if nxt:
            body["nextPageToken"] = nxt
        return httpx.Response(200, json=body)

    return handler


def test_summary_reads_firestore_and_caches(monkeypatch):
    from autobench.services import gcp

    monkeypatch.setattr(gcp, "access_token", lambda client: "tok")
    monkeypatch.setattr(financier, "_broker_account", lambda: "U123")
    financier._cache.update(at=0.0, data=None, project=None)
    calls = []
    factory = lambda: httpx.Client(transport=httpx.MockTransport(_transport(calls)))  # noqa: E731
    s = financier.summary(client_factory=factory)
    assert s["linked_portfolio"]["name"] == "Swing Portfolio" and len(s["open_trades"]) == 2
    n = len(calls)
    assert n == len(financier.COLLECTIONS) + 1  # trades took two pages
    financier.summary(client_factory=factory)
    assert len(calls) == n  # cached
    financier.summary(refresh=True, client_factory=factory)
    assert len(calls) == 2 * n
    financier._cache.update(at=0.0, data=None, project=None)


def test_summary_without_google_login_is_a_provider_error(monkeypatch):
    from autobench.providers.base import ProviderError
    from autobench.services import gcp

    def no_login(client):
        raise gcp.GcpError("No Google application-default login")

    monkeypatch.setattr(gcp, "access_token", no_login)
    financier._cache.update(at=0.0, data=None, project=None)
    with pytest.raises(ProviderError, match="No Google login"):
        financier.summary(refresh=True, client_factory=lambda: httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500))))
    with TestClient(create_app()) as c:
        r = c.get("/api/financier/summary?refresh=1")
        assert r.status_code == 200 and r.json()["available"] is False and "Google" in r.json()["error"]


@pytest.fixture
def build(tmp_path, monkeypatch):
    d = tmp_path / "web"
    (d / "assets").mkdir(parents=True)
    (d / "index.html").write_text('<!DOCTYPE html><html><head>\n  <base href="/">\n</head><body>F</body></html>')
    (d / "main.dart.js").write_text("console.log(1)")
    (d / "flutter_bootstrap.js").write_text("// boot")
    (d / "assets" / "a.json").write_text("{}")
    (tmp_path / "secret.txt").write_text("nope")
    monkeypatch.setenv("AUTOBENCH_FINANCIER_DIR", str(d))
    get_settings.cache_clear()
    return d


def test_serves_the_financier_build(build):
    with TestClient(create_app()) as c:
        st = c.get("/api/financier/status").json()
        assert st["ui"]["built"] and st["project"] == "test-project"
        r = c.get("/financier", follow_redirects=False)
        assert r.status_code in (301, 302, 307) and r.headers["location"] == "/financier/"
        r = c.get("/financier/")
        assert r.status_code == 200 and '<base href="/financier/">' in r.text and r.headers["cache-control"] == "no-cache"
        assert '<base href="/financier/">' in c.get("/financier/portfolio").text  # in-app route → the console
        r = c.get("/financier/main.dart.js")
        assert r.text == "console.log(1)" and "max-age" in r.headers["cache-control"]
        assert c.get("/financier/flutter_bootstrap.js").headers["cache-control"] == "no-cache"
        assert c.get("/financier/assets/a.json").json() == {}
        assert c.get("/financier/missing.js").status_code == 404
        assert c.get("/financier/..%2Fsecret.txt").status_code == 404
        assert "nope" not in c.get("/financier/../secret.txt").text


def test_not_built():
    with TestClient(create_app()) as c:
        assert c.get("/api/financier/status").json()["ui"]["built"] is False
        r = c.get("/financier/")
        assert r.status_code == 404 and "not built" in r.text


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_mcp_financier_tool(monkeypatch):
    from mcp import Client

    from autobench.mcp_server import mcp

    monkeypatch.setattr(financier, "summary", lambda refresh=False: financier.build_summary(DATA, "U123"))
    async with Client(mcp) as c:
        r = await c.call_tool("get_financier_summary", {})
        assert not r.is_error
        body = json.loads(r.content[0].text)
        assert body["linked_portfolio"]["name"] == "Swing Portfolio" and body["plans"]["NVDA"]["stop"] == 92.0
        r = await c.call_tool("show_in_ui", {"page": "financier", "tab": "portfolio"})
        assert not r.is_error
        g = await c.call_tool("get_guide", {"topic": "workflow"})
        assert not g.is_error
