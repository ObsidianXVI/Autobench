"""Interactive Brokers via IB Gateway / TWS (socket API) using ib_async.

ib_async is asyncio-based; it runs on a private event loop in its own thread, and every call from
the (threaded) web server or MCP tools is marshalled onto that loop. The connection is kept alive:
when Gateway restarts or logs out, it reconnects every few seconds.

Paper accounts have ids starting with "DU" (or "DF" for advisor paper accounts)."""

from __future__ import annotations

import asyncio
import logging
import math
import threading
import time
from typing import Any, Callable

from .base import Broker, BrokerError, make_ref, parse_ref

log = logging.getLogger(__name__)
# ib_async logs every failed connection attempt at ERROR; connection state is reported (once per
# change) by this module and shown in the UI instead
logging.getLogger("ib_async").setLevel(logging.CRITICAL)

_TYPE_TO_IB = {"market": "MKT", "limit": "LMT", "stop": "STP", "stop_limit": "STP LMT"}
_TYPE_FROM_IB = {v: k for k, v in _TYPE_TO_IB.items()}
_INFO_CODES = set(range(2100, 2170)) | {1102}  # farm/connectivity notices, not errors
_UNSET = 1.0e300  # ib_async uses ~1.8e308 for "unset" doubles


def is_paper_account(account: str | None) -> bool | None:
    if not account:
        return None
    return account.upper().startswith(("DU", "DF"))


def _num(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) or abs(f) >= _UNSET else f


def to_symbol(ib_symbol: str) -> str:
    return ib_symbol.replace(" ", ".")


def to_ib_symbol(symbol: str) -> str:
    return symbol.replace(".", " ").replace("/", " ")


def map_status(status: str, filled: float, parent_id: int, parent_filled: bool | None = None) -> str:
    if status in ("PendingSubmit", "ApiPending"):
        return "pending"
    if status == "PreSubmitted":
        return "pending" if parent_id and parent_filled is False else "working"
    if status in ("Submitted", "PendingCancel", "ApiUpdate"):
        return "partially_filled" if filled > 0 else "working"
    if status == "Filled":
        return "filled"
    if status in ("Cancelled", "ApiCancelled"):
        return "cancelled"
    if status == "ValidationError":
        return "rejected"
    if status == "Inactive":
        return "inactive"
    return "pending"


def order_key(order) -> str:
    """Stable id for an IB order: the API order id, or the permanent id for orders placed elsewhere."""
    return str(order.orderId) if order.orderId and order.orderId > 0 else f"p{order.permId}"


def map_trade(trade, parent_filled: bool | None = None, message: str | None = None) -> dict:
    o, st = trade.order, trade.orderStatus
    ticket_id, role = parse_ref(o.orderRef)
    filled = float(st.filled or 0)
    created = trade.log[0].time.timestamp() if trade.log else time.time()
    updated = trade.log[-1].time.timestamp() if trade.log else created
    if message is None and trade.log:
        last = trade.log[-1]
        message = last.message or None
    return {
        "id": order_key(o), "ref": o.orderRef or None, "ticket_id": ticket_id, "role": role,
        "parent_id": str(o.parentId) if o.parentId else None, "symbol": to_symbol(trade.contract.symbol),
        "side": "buy" if o.action == "BUY" else "sell", "qty": float(o.totalQuantity),
        "type": _TYPE_FROM_IB.get(o.orderType, o.orderType.lower()),
        "limit_price": _num(o.lmtPrice), "stop_price": _num(o.auxPrice) if o.orderType in ("STP", "STP LMT") else None,
        "tif": (o.tif or "DAY").lower(), "outside_rth": bool(o.outsideRth),
        "status": map_status(st.status, filled, o.parentId, parent_filled), "filled_qty": filled,
        "avg_fill_price": _num(st.avgFillPrice) or None, "message": message,
        "created_at": int(created), "updated_at": int(updated),
        "source": "autobench" if ticket_id else "external", "perm_id": o.permId,
    }


def map_fill(fill) -> dict:
    e, c = fill.execution, fill.commissionReport
    return {
        "exec_id": e.execId, "order_id": str(e.orderId) if e.orderId and e.orderId > 0 else f"p{e.permId}",
        "ref": e.orderRef or None, "symbol": to_symbol(fill.contract.symbol),
        "side": "buy" if e.side == "BOT" else "sell", "qty": float(e.shares), "price": float(e.price),
        "time": int(e.time.timestamp()), "commission": _num(c.commission) if c and c.execId else None,
        "realized_pnl": (_num(c.realizedPNL) or None) if c and c.execId else None, "account": e.acctNumber,
    }


def map_account(values: list, account: str, daily_pnl: float | None) -> dict:
    """Account values arrive per tag and currency; report them in the account's base currency."""
    by: dict[tuple[str, str], str] = {(v.tag, v.currency): v.value for v in values if v.account == account}
    base = next((cur for (tag, cur) in by if tag == "NetLiquidation" and cur not in ("", "BASE")), "USD")

    def g(tag: str) -> float | None:
        for cur in (base, "BASE", "USD", ""):
            if (tag, cur) in by:
                return _num(by[(tag, cur)])
        return None

    usd_to_base = 1.0 if base == "USD" else (_num(by.get(("ExchangeRate", "USD"))) or 1.0)
    return {
        "account": account, "currency": base, "net_liquidation": g("NetLiquidation"), "cash": g("TotalCashValue"),
        "buying_power": g("BuyingPower"), "available_funds": g("AvailableFunds"),
        "gross_position_value": g("GrossPositionValue"), "unrealized_pnl": g("UnrealizedPnL"),
        "realized_pnl": g("RealizedPnL"), "daily_pnl": daily_pnl, "usd_to_base": usd_to_base,
    }


def map_position(item) -> dict:
    return {
        "symbol": to_symbol(item.contract.symbol), "qty": float(item.position), "avg_cost": float(item.averageCost),
        "market_price": _num(item.marketPrice), "market_value": _num(item.marketValue),
        "unrealized_pnl": _num(item.unrealizedPNL), "realized_pnl": _num(item.realizedPNL),
    }


class IbkrBroker(Broker):
    kind = "ibkr"

    def __init__(self, host: str = "127.0.0.1", port: int = 4002, client_id: int = 7, account: str | None = None):
        super().__init__()
        self.host, self.port, self.client_id = host, port, client_id
        self._preferred = account or None  # the account to use when the login holds several
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self.ib = None
        self._want = False
        self._account: str | None = None
        self._accounts: list[str] = []
        self._message = "Not connected"
        self._last_attempt = 0.0
        self._failures = 0
        self._contracts: dict[str, Any] = {}
        self._order_messages: dict[str, str] = {}
        self._pnl_singles: dict[int, Any] = {}
        self._ready = threading.Event()

    # --- loop plumbing ----------------------------------------------------------------------------
    def _start_loop(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._ready.clear()

        def run():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            from ib_async import IB

            self.ib = IB()
            self._wire(self.ib)
            loop.create_task(self._keepalive())
            self._ready.set()
            loop.run_forever()

        self._thread = threading.Thread(target=run, name="ibkr", daemon=True)
        self._thread.start()
        self._ready.wait(10)

    def _call(self, fn: Callable[[], Any], timeout: float = 15) -> Any:
        """Run fn (sync, or returning an awaitable) on the IB loop and wait for its result."""
        if self._loop is None:
            raise BrokerError("IBKR is not connected")

        async def runner():
            r = fn()
            if asyncio.iscoroutine(r) or isinstance(r, asyncio.Future):
                r = await r
            return r

        fut = asyncio.run_coroutine_threadsafe(runner(), self._loop)
        try:
            return fut.result(timeout)
        except TimeoutError as e:
            fut.cancel()
            raise BrokerError("IBKR request timed out") from e

    def _require(self) -> None:
        if not (self.ib and self.ib.isConnected()):
            raise BrokerError(f"IBKR is not connected ({self._message}). Start IB Gateway and log in "
                              f"(API port {self.port}).")

    # --- lifecycle --------------------------------------------------------------------------------
    def connect(self) -> None:
        self._want = True
        self._start_loop()
        self._last_attempt = 0
        assert self._loop is not None
        asyncio.run_coroutine_threadsafe(self._try_connect(), self._loop)

    def disconnect(self) -> None:
        self._want = False
        if self.ib and self._loop:
            try:
                self._call(lambda: self.ib.disconnect(), timeout=5)
            except Exception:
                pass
        self._message = "Disconnected"
        self.emit("status", self.status())

    async def _keepalive(self) -> None:
        while True:
            await asyncio.sleep(3)
            wait = 10 if self._failures < 6 else 30  # back off after a minute of failures
            if self._want and not self.ib.isConnected() and time.time() - self._last_attempt > wait:
                await self._try_connect()

    async def _try_connect(self) -> None:
        if self.ib.isConnected():
            return
        self._last_attempt = time.time()
        try:
            await self.ib.connectAsync(self.host, self.port, clientId=self.client_id, timeout=8)
        except Exception as e:  # Gateway not running, wrong port, client id in use, …
            msg = f"Cannot reach IB Gateway/TWS at {self.host}:{self.port} ({type(e).__name__}: {e})".strip()
            self._failures += 1
            if msg != self._message:
                log.warning("IBKR: %s; retrying in the background", msg)
                self._message = msg
                self.emit("status", self.status())
            return
        self._failures = 0
        self._accounts = list(self.ib.managedAccounts())
        choice = self._preferred if self._preferred in self._accounts else (self._accounts[0] if self._accounts else None)
        self._account = None
        self._message = "Connected"
        try:
            await self._use_account(choice)
            await self.ib.reqAllOpenOrdersAsync()  # include orders placed in TWS / other clients
        except Exception as e:
            log.info("IBKR post-connect requests failed: %s", e)
        log.info("IBKR connected: account %s (%s)", self._account, "paper" if is_paper_account(self._account) else "LIVE")
        self.emit("status", self.status())
        self.emit("account", None)

    async def _use_account(self, account: str | None) -> None:
        """Point the portfolio / account-value / P&L subscriptions at one account. IBKR streams portfolio
        updates for a single account at a time; ib_async only subscribes by itself when the login has one."""
        old, self._account = self._account, account
        if not account:
            return
        if old and old != account:
            try:
                self.ib.cancelPnL(old)
            except Exception:
                pass
        for sub in self._pnl_singles.values():
            try:
                self.ib.cancelPnLSingle(sub.account, sub.modelCode, sub.conId)
            except Exception:
                pass
        self._pnl_singles.clear()
        if len(self._accounts) > 1 or (old and old != account):
            try:
                await asyncio.wait_for(self.ib.reqAccountUpdatesAsync(account), 10)
            except asyncio.TimeoutError:  # some multi-account logins never finish the download; values still stream
                log.info("IBKR: account download for %s did not finish; using position + P&L subscriptions", account)
        if not self.ib.pnl(account):
            self.ib.reqPnL(account)
        try:
            await asyncio.wait_for(self.ib.reqPositionsAsync(), 10)
        except asyncio.TimeoutError:
            log.info("IBKR: positions request timed out")
        self._watch_positions()

    def _watch_positions(self) -> None:
        """Per-position P&L subscriptions (value, unrealized) for the selected account. They stand in for the
        portfolio stream, which IBKR leaves empty for some logins that hold several accounts."""
        for p in self.ib.positions(self._account or ""):
            con = p.contract.conId
            if p.contract.secType == "STK" and p.position and con not in self._pnl_singles:
                self._pnl_singles[con] = self.ib.reqPnLSingle(p.account, "", con)

    def select_account(self, account: str) -> None:
        self._require()
        if account not in self._accounts:
            raise ValueError(f"Account {account} is not in this IBKR login (available: {', '.join(self._accounts) or 'none'})")
        self._preferred = account
        if account != self._account:
            self._call(lambda: self._use_account(account), timeout=20)
            log.info("IBKR: using account %s (%s)", account, "paper" if is_paper_account(account) else "LIVE")
        self.emit("status", self.status())
        self.emit("account", None)

    def _mine(self, account: str | None) -> bool:
        """Orders and fills of the selected account (an empty account means the login's only one)."""
        return len(self._accounts) <= 1 or not account or account == self._account

    def status(self) -> dict:
        connected = bool(self.ib and self.ib.isConnected())
        return {"kind": self.kind, "connected": connected, "account": self._account if connected else None,
                "paper": is_paper_account(self._account) if connected else None,
                "accounts": [{"id": a, "paper": is_paper_account(a)} for a in self._accounts] if connected else [],
                "message": self._message if not connected else ("Connected" if self._message == "Connected" else self._message),
                "host": self.host, "port": self.port, "client_id": self.client_id}

    # --- events -----------------------------------------------------------------------------------
    def _wire(self, ib) -> None:
        def on_trade(trade):
            self.emit("order", self._map(trade))

        ib.openOrderEvent += on_trade
        ib.orderStatusEvent += on_trade
        ib.execDetailsEvent += lambda trade, fill: self.emit("fill", map_fill(fill))
        ib.commissionReportEvent += lambda trade, fill, report: self.emit("fill", map_fill(fill))
        ib.pnlEvent += lambda pnl: self.emit("account", None)
        ib.updatePortfolioEvent += lambda item: self.emit("account", None)
        ib.positionEvent += lambda pos: (self._watch_positions(), self.emit("account", None))
        ib.disconnectedEvent += self._on_disconnect
        ib.errorEvent += self._on_error

    def _on_disconnect(self) -> None:
        self._pnl_singles.clear()  # subscriptions end with the connection
        if self._want:
            self._message = "Connection lost; reconnecting…"
        self.emit("status", self.status())

    def _on_error(self, req_id, code, message, contract=None, *_args) -> None:
        if code in _INFO_CODES:
            return
        if code in (1100, 2110):
            self._message = message
            self.emit("status", self.status())
            return
        if req_id and req_id > 0:
            self._order_messages[str(req_id)] = f"{message} (code {code})"
            trade = next((t for t in self.ib.trades() if t.order.orderId == req_id), None)
            if trade is not None:
                self.emit("order", self._map(trade))
        self.emit("error", {"order_id": str(req_id) if req_id and req_id > 0 else None, "code": code, "message": message})

    def _map(self, trade) -> dict:
        parent_filled = None
        if trade.order.parentId:
            parent = next((t for t in self.ib.trades() if t.order.orderId == trade.order.parentId), None)
            parent_filled = parent is not None and parent.orderStatus.status == "Filled"
        key = order_key(trade.order)
        return map_trade(trade, parent_filled, self._order_messages.get(key))

    # --- reads ------------------------------------------------------------------------------------
    def account(self) -> dict:
        self._require()

        def read():
            pnl = self.ib.pnl(self._account) if self._account else []
            daily = _num(pnl[0].dailyPnL) if pnl else None
            return map_account(self.ib.accountValues(self._account or ""), self._account or "", daily)

        return self._call(read)

    def positions(self) -> list[dict]:
        self._require()

        def read():
            # the positions list is complete once loaded; the portfolio stream (prices, P&L) can lag behind it
            # or stay empty on logins with several accounts, so per-position P&L fills the gaps
            items = {p.contract.conId: p for p in self.ib.portfolio(self._account or "")
                     if p.position and p.contract.secType == "STK"}
            held = [p for p in self.ib.positions(self._account or "") if p.position and p.contract.secType == "STK"]
            if not held:
                return [map_position(p) for p in items.values()]
            self._watch_positions()
            out = []
            for p in held:
                item = items.get(p.contract.conId)
                if item is not None:
                    out.append(map_position(item))
                    continue
                pnl = self._pnl_singles.get(p.contract.conId)
                value = _num(pnl.value) if pnl is not None else None
                out.append({"symbol": to_symbol(p.contract.symbol), "qty": float(p.position), "avg_cost": float(p.avgCost),
                            "market_price": value / float(p.position) if value is not None else None,
                            "market_value": value, "unrealized_pnl": _num(pnl.unrealizedPnL) if pnl is not None else None,
                            "realized_pnl": _num(pnl.realizedPnL) if pnl is not None else None})
            return out

        return self._call(read)

    def orders(self, include_done: bool = False) -> list[dict]:
        self._require()

        def read():
            trades = self.ib.trades() if include_done else self.ib.openTrades()
            return [self._map(t) for t in trades if t.contract.secType == "STK" and self._mine(t.order.account)]

        return self._call(read)

    def fills(self) -> list[dict]:
        self._require()
        return self._call(lambda: [map_fill(f) for f in self.ib.fills()
                                   if f.contract.secType == "STK" and self._mine(f.execution.acctNumber)])

    # --- writes -----------------------------------------------------------------------------------
    async def _contract(self, symbol: str):
        c = self._contracts.get(symbol)
        if c is None:
            from ib_async import Stock

            res = await self.ib.qualifyContractsAsync(Stock(to_ib_symbol(symbol), "SMART", "USD"))
            c = res[0] if res else None
            if c is None or not getattr(c, "conId", 0):
                raise BrokerError(f"IBKR does not know the US stock {symbol}")
            self._contracts[symbol] = c
        return c

    @staticmethod
    def _order(leg: dict, ref: str):
        from ib_async import Order

        t = leg["type"]
        o = Order(action="BUY" if leg["side"] == "buy" else "SELL", totalQuantity=float(leg["qty"]),
                  orderType=_TYPE_TO_IB[t], tif=leg.get("tif", "day").upper(), outsideRth=bool(leg.get("outside_rth")),
                  orderRef=ref)
        if t in ("limit", "stop_limit"):
            o.lmtPrice = float(leg["limit_price"])
        if t in ("stop", "stop_limit"):
            o.auxPrice = float(leg["stop_price"])
        return o

    def place(self, symbol, ticket_id, entry, take_profit=None, stop_loss=None, entry_role="entry"):
        self._require()

        async def run():
            contract = await self._contract(symbol)
            children = [(r, leg) for r, leg in (("take_profit", take_profit), ("stop_loss", stop_loss)) if leg]
            parent = self._order(entry, make_ref(ticket_id, entry_role))
            parent.orderId = self.ib.client.getReqId()
            parent.transmit = not children
            if self._account:
                parent.account = self._account
            orders = [parent]
            for i, (role, leg) in enumerate(children):
                o = self._order(leg, make_ref(ticket_id, role))
                o.orderId = self.ib.client.getReqId()
                o.parentId = parent.orderId
                o.ocaGroup, o.ocaType = f"ab-{ticket_id}", 1
                o.transmit = i == len(children) - 1  # the last child transmits the whole bracket
                if self._account:
                    o.account = self._account
                orders.append(o)
            trades = [self.ib.placeOrder(contract, o) for o in orders]
            await asyncio.sleep(0.3)  # let the first status arrive
            return [self._map(t) for t in trades]

        return self._call(run, timeout=20)

    def _find(self, order_id: str):
        for t in self.ib.trades():
            if order_key(t.order) == order_id:
                return t
        raise KeyError(f"Order {order_id} not found")

    def modify(self, order_id, qty=None, limit_price=None, stop_price=None):
        self._require()

        async def run():
            t = self._find(order_id)
            if t.orderStatus.status in ("Filled", "Cancelled", "ApiCancelled", "Inactive"):
                raise BrokerError(f"Order {order_id} is {t.orderStatus.status.lower()}")
            o = t.order
            if qty is not None:
                o.totalQuantity = float(qty)
            if limit_price is not None:
                o.lmtPrice = float(limit_price)
            if stop_price is not None:
                o.auxPrice = float(stop_price)
            o.transmit = True
            self.ib.placeOrder(t.contract, o)
            await asyncio.sleep(0.3)
            return self._map(t)

        return self._call(run)

    def cancel(self, order_id):
        self._require()

        def run():
            t = self._find(order_id)
            if t.orderStatus.status in ("Filled", "Cancelled", "ApiCancelled"):
                return
            self.ib.cancelOrder(t.order)

        self._call(run)
