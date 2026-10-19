"""Trading: risk checks, round trips, the simulator broker end to end through the API, agent tickets via
MCP, the kill switch, live-account gating and the IBKR mapping helpers."""

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from mcp import Client

from autobench.app import create_app
from autobench.mcp_server import mcp
from autobench.schemas import OrderSpec, TradingSettings
from autobench.services.trading import check_order, round_trips, trade_stats, trading

H = {"X-Autobench": "1"}


@pytest.fixture
def client():
    with TestClient(create_app()) as c:
        c.post("/api/trading/sim/price", json={"symbol": "AAPL", "price": 100}, headers=H)
        c.post("/api/trading/sim/price", json={"symbol": "MSFT", "price": 50}, headers=H)
        yield c


def price(c, sym, p):
    r = c.post("/api/trading/sim/price", json={"symbol": sym, "price": p}, headers=H)
    assert r.status_code == 200, r.text


def order(c, **kw):
    body = {"symbol": "AAPL", "side": "buy", "qty": 10, "order_type": "limit", "limit_price": 99, **kw}
    return c.post("/api/trading/orders", json=body, headers=H)


# ---------------------------------------------------------------------------------- pure risk check

ACCOUNT = {"net_liquidation": 100_000, "cash": 100_000, "available_funds": 100_000, "daily_pnl": 0, "usd_to_base": 1.0,
           "currency": "USD"}


def chk(spec: dict, actor="user", positions=(), account=None, settings=None, **kw):
    args = dict(actor=actor, account=account or ACCOUNT, positions=list(positions), open_orders=[],
                settings=settings or TradingSettings(), price=100.0, orders_today=0, halted=False, paper=True,
                live_allowed=False, armed=False, connected=True)
    args.update(kw)
    return check_order(OrderSpec(**{"symbol": "AAPL", "side": "buy", "qty": 10, "limit_price": 99, **spec}), **args)


def test_risk_check_limits():
    ok = chk({"stop_loss": 95, "take_profit": 110})
    assert ok["ok"] and ok["intent"] == "open"
    e = ok["estimate"]
    assert e["value"] == 990 and e["risk_value"] == 40 and e["reward_risk"] == 2.75 and e["position_after"] == 10
    assert "No stop-loss" in chk({})["warnings"][0]
    assert any("exceeds" in x for x in chk({"qty": 200})["errors"])  # $19,800 > $10,000
    assert any("% of equity" in x for x in chk({"qty": 90, "stop_loss": 98},
                                              settings=TradingSettings(limits={"max_order_value": 1e6, "max_position_pct": 5}))["errors"])
    assert any("Short selling" in x for x in chk({"side": "sell", "limit_price": 101})["errors"])
    assert any("Market orders" in x for x in chk({"order_type": "market"})["errors"])
    assert any("need a stop_loss" in x for x in chk({}, actor="agent")["errors"])
    assert any("must be below" in x for x in chk({"stop_loss": 120})["errors"])
    assert any("Risk to the stop" in x for x in chk({"qty": 100, "stop_loss": 80},
                                                     settings=TradingSettings(limits={"max_order_value": 1e6, "max_position_pct": 50}))["errors"])
    assert any("Daily loss" in x for x in chk({}, account={**ACCOUNT, "daily_pnl": -2500})["errors"])
    assert any("Daily order limit" in x for x in chk({}, orders_today=30)["errors"])
    assert any("available funds" in x for x in chk({}, account={**ACCOUNT, "available_funds": 500})["errors"])
    assert any("not connected" in x for x in chk({}, connected=False)["errors"])
    assert any("through the market" in x for x in chk({"limit_price": 110})["warnings"])


def test_risk_check_closing_orders_and_gates():
    long = [{"symbol": "AAPL", "qty": 50}]
    # selling out of a long is a close: market allowed, no size limits, no stop needed (even for the agent)
    c = chk({"side": "sell", "qty": 50, "order_type": "market"}, actor="agent", positions=long)
    assert c["ok"] and c["intent"] == "close"
    assert any("flip" in x for x in chk({"side": "sell", "qty": 60, "limit_price": 101}, positions=long)["errors"])
    # kill switch: the user can still close, nobody can open
    assert chk({"side": "sell", "qty": 50, "order_type": "market"}, positions=long, halted=True)["ok"]
    assert not chk({"stop_loss": 95}, halted=True)["ok"]
    assert not chk({"side": "sell", "qty": 50, "order_type": "market"}, actor="agent", positions=long, halted=True)["ok"]
    # live accounts: blocked unless enabled AND armed
    assert any("LIVE account" in x for x in chk({"stop_loss": 95}, paper=False)["errors"])
    assert any("not armed" in x for x in chk({"stop_loss": 95}, paper=False, live_allowed=True)["errors"])
    assert chk({"stop_loss": 95}, paper=False, live_allowed=True, armed=True)["ok"]
    # symbol lists
    s = TradingSettings(limits={"symbol_allowlist": ["MSFT"]})
    assert any("allowed symbols" in x for x in chk({}, settings=s)["errors"])


# ---------------------------------------------------------------------------------- round trips


def fill(i, side, qty, price, t, sym="AAPL", comm=1.0, ref=None):
    return {"exec_id": f"E{i}", "symbol": sym, "side": side, "qty": qty, "price": price, "time": t, "commission": comm,
            "ref": ref}


def test_round_trips_long_short_partial_and_flip():
    fills = [
        fill(1, "buy", 10, 100, 1, ref="ab:T1:entry"), fill(2, "buy", 10, 110, 2),  # avg 105
        fill(3, "sell", 5, 120, 3), fill(4, "sell", 15, 90, 4),  # +75, -225 → -150
        fill(5, "sell", 10, 50, 5, sym="MSFT"), fill(6, "buy", 10, 40, 6, sym="MSFT"),  # short +100
        fill(7, "buy", 5, 10, 7, sym="X"), fill(8, "sell", 8, 12, 8, sym="X"),  # +10, then a 3-share short opens
    ]
    tr = round_trips(fills)
    by = {(t["symbol"], t["status"]): t for t in tr}
    a = by[("AAPL", "closed")]
    assert a["side"] == "long" and a["entry_avg"] == 105 and a["realized_pnl"] == -150 and a["commission"] == 4
    assert a["net_pnl"] == -154 and a["max_qty"] == 20 and a["ticket_ids"] == ["T1"]
    m = by[("MSFT", "closed")]
    assert m["side"] == "short" and m["realized_pnl"] == 100
    assert by[("X", "closed")]["realized_pnl"] == 10
    x_open = by[("X", "open")]
    assert x_open["side"] == "short" and x_open["qty"] == 3 and x_open["entry_avg"] == 12
    stats = trade_stats(tr)
    assert stats["closed_trades"] == 3 and stats["wins"] == 2 and stats["open_trades"] == 1


# ---------------------------------------------------------------------------------- simulator via the API


def test_api_guard(client):
    body = {"symbol": "AAPL", "side": "buy", "qty": 1, "limit_price": 99}
    assert client.post("/api/trading/orders", json=body).status_code == 403  # no X-Autobench header
    assert client.post("/api/trading/orders", json=body, headers={**H, "host": "evil.example"}).status_code == 403
    assert client.post("/api/trading/preview", json=body).status_code == 200  # read-only


def test_sim_bracket_lifecycle_and_history(client):
    st = client.get("/api/trading/status").json()
    assert st["kind"] == "sim" and st["connected"] and st["paper"] and st["can_trade"]
    t = order(client, stop_loss=95, take_profit=110).json()
    assert t["status"] == "working" and len(t["order_ids"]) == 3 and t["source"] == "user"
    orders = client.get("/api/trading/orders", params={"symbol": "AAPL"}).json()
    roles = {o["role"]: o for o in orders}
    assert roles["entry"]["status"] == "working" and roles["stop_loss"]["status"] == "pending"
    price(client, "AAPL", 98.5)  # entry fills at 98.5 (through the limit)
    ov = client.get("/api/trading/overview").json()
    pos = ov["positions"][0]
    assert pos["symbol"] == "AAPL" and pos["qty"] == 10 and pos["avg_cost"] == 98.5 and pos["stop"] == 95
    assert ov["capital"]["invested"] == 985 and ov["capital"]["unprotected_positions"] == []
    assert client.get("/api/trading/tickets").json()[0]["status"] == "filled"
    price(client, "AAPL", 94)  # stop triggers, target cancelled (one-cancels-other)
    assert client.get("/api/trading/overview").json()["positions"] == []
    done = {o["role"]: o["status"] for o in client.get("/api/trading/orders", params={"include_done": True}).json()}
    assert done == {"entry": "filled", "stop_loss": "filled", "take_profit": "cancelled"}
    hist = client.get("/api/trading/trades").json()
    tr = hist["trades"][0]
    assert tr["status"] == "closed" and tr["entry_avg"] == 98.5 and tr["exit_avg"] == 94 and tr["realized_pnl"] == -45
    assert tr["planned_stop"] == 95 and tr["r_multiple"] == round((-45 - 2) / 35, 2)
    assert hist["stats"]["losses"] == 1
    chart = client.get("/api/trading/chart/AAPL").json()
    assert len(chart["fills"]) == 2 and chart["trades"][0]["id"] == tr["id"]
    assert client.get("/api/trading/equity").json()  # snapshots taken after fills
    assert {"order", "submit", "fill"} <= {a["action"] for a in client.get("/api/trading/audit").json()}


def test_modify_cancel_close_and_orphan_cleanup(client):
    t = order(client, stop_loss=95).json()
    entry = next(i for i in t["order_ids"])
    r = client.patch(f"/api/trading/orders/{entry}", json={"limit_price": 97}, headers=H)
    assert r.json()["limit_price"] == 97
    assert client.delete(f"/api/trading/orders/{entry}", headers=H).json()["status"] == "cancelled"
    assert client.get("/api/trading/orders").json() == []  # the child went with its parent
    order(client, stop_loss=95, take_profit=120, limit_price=101)  # marketable: fills at 100
    assert client.get("/api/trading/overview").json()["positions"][0]["qty"] == 10
    # closing at market leaves the stop and target orphaned: they are cancelled automatically
    c = client.post("/api/trading/positions/AAPL/close", json={}, headers=H).json()
    assert c["status"] == "filled"
    trading._worker.submit(lambda: None).result()  # wait for the reconciliation job
    assert client.get("/api/trading/orders").json() == []
    assert any(a["action"] == "cancel_orphan" for a in client.get("/api/trading/audit").json())
    # a stop modified through the market is refused
    order(client, stop_loss=95, limit_price=101)
    sl = next(o for o in client.get("/api/trading/orders").json() if o["role"] == "stop_loss")
    assert client.patch(f"/api/trading/orders/{sl['id']}", json={"stop_price": 105}, headers=H).status_code == 400


def test_partial_close_shrinks_protection(client):
    order(client, qty=20, stop_loss=95, limit_price=101)
    client.post("/api/trading/positions/AAPL/close", json={"qty": 5}, headers=H)
    trading._worker.submit(lambda: None).result()
    sl = next(o for o in client.get("/api/trading/orders").json() if o["role"] == "stop_loss")
    assert sl["qty"] == 15


def test_rejections_are_explained(client):
    r = order(client, qty=1000)
    assert r.status_code == 400 and "exceeds" in r.json()["detail"]
    r = order(client, side="sell", limit_price=101)
    assert r.status_code == 400 and "Short selling" in r.json()["detail"]


def test_kill_switch(client):
    order(client, stop_loss=95, limit_price=101)  # position with a stop
    order(client, symbol="MSFT", limit_price=45, stop_loss=40)  # working entry
    res = client.post("/api/trading/halt", json={"reason": "test"}, headers=H).json()
    assert res["halted"] and len(res["cancelled_orders"]) == 1  # the MSFT entry; the AAPL stop stays
    left = client.get("/api/trading/orders").json()
    assert [o["role"] for o in left] == ["stop_loss"]
    assert order(client, stop_loss=95).status_code == 400
    assert client.post("/api/trading/positions/AAPL/close", json={}, headers=H).json()["status"] == "filled"
    client.post("/api/trading/resume", headers=H)
    assert order(client, stop_loss=95).status_code == 200
    res = client.post("/api/trading/halt", json={"flatten": True}, headers=H).json()
    assert client.get("/api/trading/orders").json() == [] and client.get("/api/trading/overview").json()["positions"] == []


def test_settings_and_live_arm(client):
    s = client.put("/api/trading/settings", json={"mode": "auto", "limits": {"max_order_value": 500}}, headers=H).json()
    assert s["mode"] == "auto" and s["limits"]["max_order_value"] == 500 and s["limits"]["max_position_pct"] == 20
    assert client.put("/api/trading/settings", json={"mode": "yolo"}, headers=H).status_code in (400, 422)
    r = client.post("/api/trading/arm", json={"confirm": "LIVE"}, headers=H)
    assert r.status_code == 400 and "AUTOBENCH_ALLOW_LIVE_TRADING" in r.json()["detail"]


# ---------------------------------------------------------------------------------- agent via MCP

@pytest.fixture
def anyio_backend():
    return "asyncio"


async def call(c, name, args=None):
    r = await c.call_tool(name, args or {})
    text = r.content[0].text
    return r.is_error, (text if r.is_error else json.loads(text))


@pytest.mark.anyio
async def test_agent_proposals_need_approval(client):
    async with Client(mcp) as c:
        err, pf = await call(c, "get_portfolio")
        assert not err and pf["status"]["kind"] == "sim" and pf["limits"]["mode"] == "confirm"
        err, msg = await call(c, "propose_order", {"symbol": "AAPL", "side": "buy", "qty": 10, "limit_price": 99,
                                                   "rationale": "Pullback to rising 20-day average, volume drying up."})
        assert err and "need a stop_loss" in msg
        err, pv = await call(c, "preview_order", {"symbol": "AAPL", "side": "buy", "qty": 10, "limit_price": 99, "stop_loss": 95})
        assert pv["ok"] and pv["would"] == "await_approval"
        err, t = await call(c, "propose_order", {"symbol": "AAPL", "side": "buy", "qty": 10, "limit_price": 101,
                                                 "stop_loss": 95, "take_profit": 115,
                                                 "rationale": "Pullback to rising 20-day average, volume drying up."})
        assert not err and t["status"] == "pending" and t["source"] == "agent"
        assert client.get("/api/trading/orders").json() == []  # nothing sent yet
        a = client.post(f"/api/trading/tickets/{t['id']}/approve", headers=H).json()
        assert a["status"] == "filled"
        # the agent may not cancel the stop that protects the position
        sl = next(o for o in client.get("/api/trading/orders").json() if o["role"] == "stop_loss")
        err, msg = await call(c, "cancel_order", {"order_id": sl["id"]})
        assert err and "cannot cancel a stop-loss" in msg
        # a second proposal, rejected by the user with a reason the agent can read
        err, t2 = await call(c, "propose_close_position", {"symbol": "AAPL", "rationale": "Momentum faded; take the gain."})
        assert t2["status"] == "pending"
        client.post(f"/api/trading/tickets/{t2['id']}/reject", json={"reason": "Holding into earnings"}, headers=H)
        err, ts = await call(c, "get_order_tickets", {"status": "rejected"})
        assert ts[0]["message"] == "Holding into earnings"
        err, hist = await call(c, "get_trade_history", {})
        assert hist["trades"][0]["status"] == "open"


@pytest.mark.anyio
async def test_agent_auto_mode_on_paper(client):
    client.put("/api/trading/settings", json={"mode": "auto"}, headers=H)
    async with Client(mcp) as c:
        err, t = await call(c, "propose_order", {"symbol": "MSFT", "side": "buy", "qty": 10, "limit_price": 49,
                                                 "stop_loss": 47, "rationale": "Testing auto mode on the simulator."})
        assert not err and t["status"] == "working"
        err, w = await call(c, "cancel_order", {"order_id": t["order_ids"][0], "reason": "changed my mind"})
        assert not err and w["status"] == "cancelled"


def test_stdio_process_cannot_trade():
    trading.enabled_here = False
    try:
        with pytest.raises(ValueError, match="HTTP MCP endpoint"):
            trading.overview()
    finally:
        trading.enabled_here = True


def test_live_account_is_read_only(client, monkeypatch):
    b = trading._broker()
    monkeypatch.setattr(b, "status", lambda: {"kind": "sim", "connected": True, "account": "U123", "paper": False})
    r = order(client, stop_loss=95)
    assert r.status_code == 400 and "LIVE account" in r.json()["detail"]
    assert not client.get("/api/trading/status").json()["can_trade"]


# ---------------------------------------------------------------------------------- IBKR mapping


def test_ibkr_mapping():
    from ib_async import (AccountValue, CommissionReport, Execution, Fill, Order, OrderStatus, Stock, Trade,
                          TradeLogEntry)

    from autobench.services.broker.ibkr import is_paper_account, map_account, map_fill, map_status, map_trade

    assert is_paper_account("DU1234567") and not is_paper_account("U1234567") and is_paper_account(None) is None
    now = datetime(2026, 10, 19, 14, 30, tzinfo=UTC)
    o = Order(orderId=12, action="SELL", totalQuantity=10, orderType="STP", auxPrice=95.0, tif="GTC",
              orderRef="ab:T9:stop_loss", parentId=11)
    tr = Trade(contract=Stock("BRK B", "SMART", "USD"), order=o, orderStatus=OrderStatus(orderId=12, status="PreSubmitted"),
               log=[TradeLogEntry(now, "PreSubmitted", "")])
    m = map_trade(tr, parent_filled=False)
    assert m["symbol"] == "BRK.B" and m["side"] == "sell" and m["type"] == "stop" and m["stop_price"] == 95
    assert m["status"] == "pending" and m["ticket_id"] == "T9" and m["role"] == "stop_loss" and m["parent_id"] == "11"
    assert map_trade(tr, parent_filled=True)["status"] == "working"
    assert map_status("Submitted", 3, 0) == "partially_filled" and map_status("Inactive", 0, 0) == "inactive"
    f = Fill(contract=Stock("AAPL", "SMART", "USD"),
             execution=Execution(execId="0001", time=now, acctNumber="DU1", side="BOT", shares=10, price=101.5,
                                 orderId=11, orderRef="ab:T9:entry"),
             commissionReport=CommissionReport(execId="0001", commission=1.0, realizedPNL=1.7976931348623157e308),
             time=now)
    mf = map_fill(f)
    assert mf["side"] == "buy" and mf["qty"] == 10 and mf["commission"] == 1.0 and mf["realized_pnl"] is None
    assert mf["time"] == int(now.timestamp())
    vals = [AccountValue("DU1", "NetLiquidation", "135000", "SGD", ""), AccountValue("DU1", "TotalCashValue", "50000", "SGD", ""),
            AccountValue("DU1", "ExchangeRate", "1.35", "USD", ""), AccountValue("DU1", "UnrealizedPnL", "12", "BASE", "")]
    a = map_account(vals, "DU1", -20.0)
    assert a["currency"] == "SGD" and a["net_liquidation"] == 135000 and a["usd_to_base"] == 1.35
    assert a["unrealized_pnl"] == 12 and a["daily_pnl"] == -20


def test_user_exit_orders_are_tagged_and_cleaned_up(client):
    order(client, limit_price=101)  # unprotected long 10 @ 100
    stop = order(client, side="sell", order_type="stop", stop_price=95).json()  # standalone protective stop
    tp = order(client, side="sell", limit_price=110).json()  # limit above the market = take-profit
    roles = {o["id"]: o["role"] for o in client.get("/api/trading/orders").json()}
    assert roles[stop["order_ids"][0]] == "stop_loss" and roles[tp["order_ids"][0]] == "take_profit"
    assert client.get("/api/trading/overview").json()["positions"][0]["stop"] == 95
    # a closing order cannot carry its own bracket
    r = order(client, side="sell", limit_price=110, stop_loss=120)
    assert r.status_code == 400 and "cannot carry" in r.json()["detail"]
    price(client, "AAPL", 111)  # target fills: the stop would now open a short, so it is cancelled
    trading._worker.submit(lambda: None).result()
    assert client.get("/api/trading/orders").json() == []
    assert client.get("/api/trading/overview").json()["positions"] == []
