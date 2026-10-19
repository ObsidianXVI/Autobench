"""Built-in paper-trading simulator: a cash account filled at the latest quote. Used for tests, demos
and when no IBKR Gateway is available. State persists in the app_state table.

Matching (on every placement and every tick): market orders fill at the current price; limits fill
when the price is at or through the limit (at the better of the two); stops trigger when the price
reaches the stop and then fill like a market (stop-limit: like a limit at the limit price). Children
(stop-loss / take-profit) wait until their parent fills and cancel each other when one fills."""

from __future__ import annotations

import time
from datetime import UTC, datetime

from ... import db
from .base import DONE_STATUSES, OPEN_STATUSES, Broker, BrokerError, make_ref, parse_ref

STATE_KEY = "sim:book"
ACCOUNT_ID = "SIM-PAPER"


def _commission(qty: float) -> float:
    return round(max(1.0, 0.005 * qty), 2)  # IBKR fixed-rate style


def _today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


class SimBroker(Broker):
    kind = "sim"

    def __init__(self, starting_cash: float = 100_000.0):
        super().__init__()
        self.starting_cash = starting_cash
        self._connected = False
        self._prices: dict[str, float] = {}  # manual overrides (tests, what-ifs)
        self._quote_cache: dict[str, tuple[float, float]] = {}

    # --- persistence ---------------------------------------------------------------------------
    def _load(self) -> dict:
        book = db.state_get(STATE_KEY)
        if not book:
            book = {"cash": self.starting_cash, "start_cash": self.starting_cash, "positions": {}, "orders": [],
                    "fills": [], "next_id": 1, "day": _today(), "day_start_equity": self.starting_cash,
                    "realized": 0.0}
        return book

    def _save(self, book: dict) -> None:
        book["orders"] = book["orders"][-500:]
        book["fills"] = book["fills"][-2000:]
        db.state_set(STATE_KEY, book)

    def reset(self, cash: float | None = None) -> None:
        with self._lock:
            c = float(cash or self.starting_cash)
            db.state_set(STATE_KEY, {"cash": c, "start_cash": c, "positions": {}, "orders": [], "fills": [],
                                     "next_id": 1, "day": _today(), "day_start_equity": c, "realized": 0.0})
            self._prices.clear()
        self.emit("account", None)

    # --- prices ---------------------------------------------------------------------------------
    def set_price(self, symbol: str, price: float | None) -> None:
        with self._lock:
            if price is None:
                self._prices.pop(symbol.upper(), None)
            else:
                self._prices[symbol.upper()] = float(price)
        self.tick()

    def price(self, symbol: str) -> float | None:
        sym = symbol.upper()
        if sym in self._prices:
            return self._prices[sym]
        hit = self._quote_cache.get(sym)
        if hit and time.time() - hit[1] < 5:
            return hit[0]
        try:
            from .. import market

            p = float(market.get_quote(sym)["price"])
        except Exception:
            return hit[0] if hit else None
        self._quote_cache[sym] = (p, time.time())
        return p

    # --- lifecycle ------------------------------------------------------------------------------
    def connect(self) -> None:
        self._connected = True
        self.emit("status", self.status())

    def disconnect(self) -> None:
        self._connected = False
        self.emit("status", self.status())

    def status(self) -> dict:
        return {"kind": self.kind, "connected": self._connected, "account": ACCOUNT_ID, "paper": True,
                "message": "Simulated paper account (fills at the latest quote)" if self._connected else "Disconnected"}

    # --- reads ----------------------------------------------------------------------------------
    def _roll_day(self, book: dict) -> None:
        if book.get("day") != _today():
            book["day"] = _today()
            book["day_start_equity"] = self._equity(book)

    def _equity(self, book: dict) -> float:
        total = book["cash"]
        for sym, p in book["positions"].items():
            px = self.price(sym) or p["avg_cost"]
            total += p["qty"] * px
        return total

    def account(self) -> dict:
        with self._lock:
            book = self._load()
            self._roll_day(book)
            positions = self.positions(book)
            mv = sum(p["market_value"] for p in positions)
            gross = sum(abs(p["market_value"]) for p in positions)
            unreal = sum(p["unrealized_pnl"] for p in positions)
            net = book["cash"] + mv
            reserved = sum((o["limit_price"] or o["stop_price"] or self.price(o["symbol"]) or 0) * (o["qty"] - o["filled_qty"])
                           for o in book["orders"]
                           if o["side"] == "buy" and o["status"] in ("working", "partially_filled") and o["role"] != "take_profit")
            avail = book["cash"] - reserved
            return {"account": ACCOUNT_ID, "currency": "USD", "net_liquidation": round(net, 2), "cash": round(book["cash"], 2),
                    "buying_power": round(max(0.0, avail), 2), "available_funds": round(avail, 2),
                    "gross_position_value": round(gross, 2), "unrealized_pnl": round(unreal, 2),
                    "realized_pnl": round(book.get("realized", 0.0), 2),
                    "daily_pnl": round(net - book["day_start_equity"], 2), "usd_to_base": 1.0}

    def positions(self, book: dict | None = None) -> list[dict]:
        book = book or self._load()
        out = []
        for sym, p in sorted(book["positions"].items()):
            if not p["qty"]:
                continue
            px = self.price(sym) or p["avg_cost"]
            out.append({"symbol": sym, "qty": p["qty"], "avg_cost": round(p["avg_cost"], 4), "market_price": px,
                        "market_value": round(p["qty"] * px, 2),
                        "unrealized_pnl": round((px - p["avg_cost"]) * p["qty"], 2),
                        "realized_pnl": round(p.get("realized", 0.0), 2)})
        return out

    def orders(self, include_done: bool = False) -> list[dict]:
        book = self._load()
        return [dict(o) for o in book["orders"] if include_done or o["status"] in OPEN_STATUSES]

    def fills(self) -> list[dict]:
        return [dict(f) for f in self._load()["fills"]]

    # --- writes ---------------------------------------------------------------------------------
    def _new_order(self, book: dict, symbol: str, ticket_id: str, role: str, leg: dict, parent_id: str | None) -> dict:
        oid = f"S{book['next_id']}"
        book["next_id"] += 1
        now = int(time.time())
        return {"id": oid, "ref": make_ref(ticket_id, role), "ticket_id": ticket_id, "role": role, "parent_id": parent_id,
                "symbol": symbol, "side": leg["side"], "qty": float(leg["qty"]), "type": leg["type"],
                "limit_price": leg.get("limit_price"), "stop_price": leg.get("stop_price"), "tif": leg.get("tif", "day"),
                "outside_rth": bool(leg.get("outside_rth")), "status": "pending" if parent_id else "working",
                "filled_qty": 0.0, "avg_fill_price": None, "message": None, "created_at": now, "updated_at": now,
                "source": "autobench", "triggered": False}

    def place(self, symbol, ticket_id, entry, take_profit=None, stop_loss=None, entry_role="entry"):
        if not self._connected:
            raise BrokerError("Simulator is not connected")
        with self._lock:
            book = self._load()
            parent = self._new_order(book, symbol, ticket_id, entry_role, entry, None)
            created = [parent]
            for role, leg in (("take_profit", take_profit), ("stop_loss", stop_loss)):
                if leg:
                    created.append(self._new_order(book, symbol, ticket_id, role, leg, parent["id"]))
            book["orders"].extend(created)
            self._save(book)
        for o in created:
            self.emit("order", dict(o))
        self.tick()
        return [o for o in self.orders(include_done=True) if o["id"] in {c["id"] for c in created}]

    def modify(self, order_id, qty=None, limit_price=None, stop_price=None):
        with self._lock:
            book = self._load()
            o = next((x for x in book["orders"] if x["id"] == order_id), None)
            if o is None:
                raise KeyError(f"Order {order_id} not found")
            if o["status"] in DONE_STATUSES:
                raise BrokerError(f"Order {order_id} is {o['status']}")
            if qty is not None:
                o["qty"] = float(qty)
            if limit_price is not None:
                o["limit_price"] = float(limit_price)
            if stop_price is not None:
                o["stop_price"] = float(stop_price)
            o["updated_at"] = int(time.time())
            self._save(book)
        self.emit("order", dict(o))
        self.tick()
        return next(x for x in self.orders(include_done=True) if x["id"] == order_id)

    def cancel(self, order_id):
        cancelled = []
        with self._lock:
            book = self._load()
            o = next((x for x in book["orders"] if x["id"] == order_id), None)
            if o is None:
                raise KeyError(f"Order {order_id} not found")
            if o["status"] in DONE_STATUSES:
                return
            for x in book["orders"]:  # cancelling a parent cancels its children
                if (x["id"] == order_id or x["parent_id"] == order_id) and x["status"] not in DONE_STATUSES:
                    x["status"] = "cancelled"
                    x["updated_at"] = int(time.time())
                    cancelled.append(dict(x))
            self._save(book)
        for x in cancelled:
            self.emit("order", x)

    # --- matching -------------------------------------------------------------------------------
    def tick(self) -> None:
        if not self._connected:
            return
        events: list[tuple[str, dict]] = []
        with self._lock:
            book = self._load()
            self._roll_day(book)
            changed = True
            while changed:  # a parent fill can activate children that are immediately marketable
                changed = False
                for o in book["orders"]:
                    if o["status"] != "working":
                        continue
                    px = self.price(o["symbol"])
                    if px is None:
                        continue
                    fill_px = self._match(o, px)
                    if fill_px is None:
                        continue
                    self._fill(book, o, fill_px, events)
                    changed = True
            if events:
                self._save(book)
        for kind, data in events:
            self.emit(kind, data)
        if events:
            self.emit("account", None)

    @staticmethod
    def _match(o: dict, px: float) -> float | None:
        buy = o["side"] == "buy"
        t = o["type"]
        if t in ("stop", "stop_limit") and not o.get("triggered"):
            stop = o["stop_price"]
            if (buy and px >= stop) or (not buy and px <= stop):
                o["triggered"] = True
            else:
                return None
        if t in ("market", "stop"):
            return px
        lim = o["limit_price"]
        if buy and px <= lim:
            return px if t == "limit" else min(px, lim)
        if not buy and px >= lim:
            return px if t == "limit" else max(px, lim)
        return None

    def _fill(self, book: dict, o: dict, px: float, events: list) -> None:
        qty = o["qty"] - o["filled_qty"]
        now = int(time.time())
        o.update(status="filled", filled_qty=o["qty"], avg_fill_price=round(px, 4), updated_at=now)
        sign = 1 if o["side"] == "buy" else -1
        pos = book["positions"].setdefault(o["symbol"], {"qty": 0.0, "avg_cost": 0.0, "realized": 0.0})
        realized = 0.0
        q0 = pos["qty"]
        if q0 == 0 or (q0 > 0) == (sign > 0):  # opening / adding
            new_q = q0 + sign * qty
            pos["avg_cost"] = (abs(q0) * pos["avg_cost"] + qty * px) / abs(new_q)
            pos["qty"] = new_q
        else:  # reducing, possibly flipping
            closing = min(qty, abs(q0))
            realized = (px - pos["avg_cost"]) * closing * (1 if q0 > 0 else -1)
            rest = qty - closing
            pos["qty"] = q0 + sign * closing
            if rest > 0:
                pos["qty"] = sign * rest
                pos["avg_cost"] = px
            elif pos["qty"] == 0:
                pos["avg_cost"] = 0.0
        comm = _commission(qty)
        pos["realized"] = pos.get("realized", 0.0) + realized
        book["realized"] = book.get("realized", 0.0) + realized - comm
        book["cash"] -= sign * qty * px + comm
        if pos["qty"] == 0:
            book["positions"].pop(o["symbol"], None)
        fill = {"exec_id": f"SX{book['next_id']}", "order_id": o["id"], "ref": o["ref"], "symbol": o["symbol"],
                "side": o["side"], "qty": qty, "price": round(px, 4), "time": now, "commission": comm,
                "realized_pnl": round(realized, 2) if realized else None, "account": ACCOUNT_ID}
        book["next_id"] += 1
        book["fills"].append(fill)
        events.append(("order", dict(o)))
        events.append(("fill", dict(fill)))
        # bracket bookkeeping
        if o["parent_id"] is None:
            for c in book["orders"]:
                if c["parent_id"] == o["id"] and c["status"] == "pending":
                    c["status"] = "working"
                    c["updated_at"] = now
                    events.append(("order", dict(c)))
        else:
            for c in book["orders"]:  # one-cancels-other between siblings
                if c["parent_id"] == o["parent_id"] and c["id"] != o["id"] and c["status"] in OPEN_STATUSES:
                    c["status"] = "cancelled"
                    c["updated_at"] = now
                    events.append(("order", dict(c)))


__all__ = ["SimBroker", "parse_ref"]
