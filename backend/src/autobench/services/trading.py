"""Trading: order tickets, server-side risk limits, approvals, the kill switch, fills, round-trip trade
history and the equity curve, on top of a broker (IBKR or the simulator).

Safety model
- Agent orders become *tickets*. In "confirm" mode (default) they wait for the user to approve them in
  the UI; in "auto" mode they are submitted when they pass every limit, on paper accounts only.
- The agent can propose, preview, close positions (as tickets) and cancel its own entry orders. It
  cannot approve, change limits or the mode, arm live trading, or lift a halt (no tools for those).
- Risk limits are checked on submission and again on approval. Orders that reduce a position skip
  the sizing limits so you can always get out.
- A live (non-paper) account is read-only unless AUTOBENCH_ALLOW_LIVE_TRADING=1 *and* the user arms
  live trading in this server session.
- When a fill leaves a position flat (or smaller), leftover protective orders (stop-loss /
  take-profit) for it are cancelled (or shrunk) so they cannot open an unintended position."""

from __future__ import annotations

import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select

from .. import db
from ..config import get_settings
from ..schemas import ClosePosition, OrderModify, OrderSpec, TradingSettings
from ..timeutil import in_regular_session, next_session_close
from .broker import DONE_STATUSES, OPEN_STATUSES, Broker, BrokerError, create_broker, parse_ref
from .events import bus

log = logging.getLogger(__name__)

SETTINGS_KEY = "trading:settings"
CONNECTION_KEY = "trading:connection"
HALT_KEY = "trading:halt"
PROTECTIVE = ("stop_loss", "take_profit", "exit")  # orders that only make sense while a position exists


def _now() -> datetime:
    return db.utcnow()


def _iso(d: datetime | None) -> str | None:
    return d.replace(tzinfo=UTC).isoformat().replace("+00:00", "Z") if d else None


def _r2(v: float | None) -> float | None:
    return None if v is None else round(v, 2)


# ===================================================================================== risk check


def check_order(spec: OrderSpec, *, actor: str, account: dict, positions: list[dict], open_orders: list[dict],
                settings: TradingSettings, price: float | None, orders_today: int, halted: bool,
                paper: bool | None, live_allowed: bool, armed: bool, connected: bool) -> dict:
    """Pure risk check. Returns {ok, errors, warnings, intent, estimate}."""
    lim = settings.limits
    errors: list[str] = []
    warnings: list[str] = []
    sym = spec.symbol
    pos_qty = next((p["qty"] for p in positions if p["symbol"] == sym), 0.0)
    sign = 1 if spec.side == "buy" else -1
    reduces = pos_qty != 0 and (pos_qty > 0) != (sign > 0)
    if reduces and spec.qty > abs(pos_qty):
        errors.append(f"{spec.qty:g} shares would flip the {pos_qty:+g} position; close it first, then open the other side")
    intent = "close" if reduces else "open"
    if intent == "close" and (spec.stop_loss is not None or spec.take_profit is not None):
        errors.append("An order that closes a position cannot carry its own stop-loss / take-profit")

    if not connected:
        errors.append("Broker is not connected")
    if paper is False:
        if not live_allowed:
            errors.append("This is a LIVE account and live trading is disabled (AUTOBENCH_ALLOW_LIVE_TRADING is not set)")
        elif not armed:
            errors.append("Live trading is not armed for this session (arm it in the Trading page)")
    if halted and not (actor == "user" and intent == "close"):
        errors.append("Trading is halted (kill switch); only closing orders by you are allowed")

    ref_price = {"market": price, "limit": spec.limit_price, "stop": spec.stop_price,
                 "stop_limit": spec.limit_price}[spec.order_type]
    if ref_price is None:
        errors.append(f"No current price for {sym}")
        ref_price = 0.0
    if price and spec.order_type == "limit":
        away = (spec.limit_price - price) / price * 100 * sign
        if away > 3:
            warnings.append(f"Limit is {away:.1f}% through the market: it will fill immediately, near {price:.2f}")
        elif away < -15:
            warnings.append(f"Limit is {-away:.1f}% away from the market; it may not fill")
    if price and spec.order_type in ("stop", "stop_limit"):
        if (spec.side == "buy" and spec.stop_price <= price) or (spec.side == "sell" and spec.stop_price >= price):
            warnings.append("Stop is already through the market: it will trigger immediately")

    fx = account.get("usd_to_base") or 1.0
    equity = account.get("net_liquidation") or 0.0
    value_usd = spec.qty * ref_price
    value = value_usd * fx
    projected = pos_qty + sign * spec.qty + sum(
        (1 if o["side"] == "buy" else -1) * (o["qty"] - (o.get("filled_qty") or 0))
        for o in open_orders
        if o["symbol"] == sym and o.get("role") in (None, "entry") and o["status"] in OPEN_STATUSES and o.get("parent_id") is None
    ) * (1 if intent == "open" else 0)
    projected_value = abs(projected) * ref_price * fx
    est: dict = {
        "price": ref_price or None, "market_price": price, "value_usd": _r2(value_usd), "value": _r2(value),
        "currency": account.get("currency") or "USD", "pct_of_equity": _r2(value / equity * 100) if equity else None,
        "position_before": pos_qty, "position_after": pos_qty + sign * spec.qty,
        "position_pct_after": _r2(projected_value / equity * 100) if equity else None,
    }

    if spec.stop_loss is not None:
        if (spec.side == "buy" and spec.stop_loss >= ref_price) or (spec.side == "sell" and spec.stop_loss <= ref_price):
            errors.append(f"stop_loss {spec.stop_loss} must be {'below' if spec.side == 'buy' else 'above'} the entry {ref_price:.2f}")
        else:
            per = abs(ref_price - spec.stop_loss)
            est["risk_per_share"] = round(per, 4)
            est["risk_value"] = _r2(per * spec.qty * fx)
            est["risk_pct_equity"] = _r2(per * spec.qty * fx / equity * 100) if equity else None
    if spec.take_profit is not None:
        if (spec.side == "buy" and spec.take_profit <= ref_price) or (spec.side == "sell" and spec.take_profit >= ref_price):
            errors.append(f"take_profit {spec.take_profit} must be {'above' if spec.side == 'buy' else 'below'} the entry {ref_price:.2f}")
        else:
            est["reward_value"] = _r2(abs(spec.take_profit - ref_price) * spec.qty * fx)
            if est.get("risk_value"):
                est["reward_risk"] = round(est["reward_value"] / est["risk_value"], 2)

    if intent == "open":
        if lim.symbol_allowlist and sym not in [s.upper() for s in lim.symbol_allowlist]:
            errors.append(f"{sym} is not on the allowed symbols list")
        if sym in [s.upper() for s in lim.symbol_blocklist]:
            errors.append(f"{sym} is on the blocked symbols list")
        if spec.side == "sell" and not lim.allow_short:
            errors.append("Short selling is disabled in the risk limits")
        if spec.order_type == "market" and not lim.allow_market_entries:
            errors.append("Market orders may only close positions; use a limit order to open")
        if spec.outside_rth and not lim.allow_outside_rth:
            errors.append("Orders outside regular trading hours are disabled in the risk limits")
        if value_usd > lim.max_order_value:
            errors.append(f"Order value ${value_usd:,.0f} exceeds the ${lim.max_order_value:,.0f} limit")
        if equity and projected_value / equity * 100 > lim.max_position_pct:
            errors.append(f"Position would be {projected_value / equity * 100:.1f}% of equity "
                          f"(limit {lim.max_position_pct:g}%)")
        daily = account.get("daily_pnl")
        if equity and daily is not None and daily < 0 and -daily / equity * 100 >= lim.max_daily_loss_pct:
            errors.append(f"Daily loss limit reached ({daily:,.0f}, limit {lim.max_daily_loss_pct:g}%): no new positions today")
        if orders_today >= lim.max_orders_per_day:
            errors.append(f"Daily order limit reached ({lim.max_orders_per_day})")
        avail = account.get("available_funds")
        if spec.side == "buy" and avail is not None and value > avail + 1e-6:
            errors.append(f"Not enough available funds ({avail:,.0f} {est['currency']}) for {value:,.0f}")
        if spec.stop_loss is None:
            if actor == "agent" and lim.require_stop_for_agent:
                errors.append("Agent orders that open a position need a stop_loss")
            else:
                warnings.append("No stop-loss: the position will be unprotected")
        elif est.get("risk_pct_equity") is not None and est["risk_pct_equity"] > lim.max_risk_pct:
            errors.append(f"Risk to the stop is {est['risk_pct_equity']:.2f}% of equity (limit {lim.max_risk_pct:g}%)")
    return {"ok": not errors, "errors": errors, "warnings": warnings, "intent": intent, "estimate": est}


# ===================================================================================== trade history


def closing_role(spec: OrderSpec, price: float | None) -> str:
    """Role of an order that reduces a position: stops are stop-losses, limits beyond the market are
    take-profits, anything else is a plain exit."""
    if spec.order_type in ("stop", "stop_limit"):
        return "stop_loss"
    if spec.order_type == "limit" and price:
        if (spec.side == "sell" and spec.limit_price > price) or (spec.side == "buy" and spec.limit_price < price):
            return "take_profit"
    return "exit"


def round_trips(fills: list[dict]) -> list[dict]:
    """Group fills into round-trip trades per symbol (FIFO average cost). A trade opens when the position
    leaves zero and closes when it returns to zero; a fill through zero closes one trade and opens the next."""
    out: list[dict] = []
    open_by_sym: dict[str, dict] = {}
    for f in sorted(fills, key=lambda x: (x["time"], x["exec_id"])):
        sym = f["symbol"]
        sign = 1 if f["side"] == "buy" else -1
        qty = float(f["qty"])
        comm = float(f.get("commission") or 0)
        t = open_by_sym.get(sym)
        while qty > 1e-9:
            if t is None:
                t = {"id": f["exec_id"], "symbol": sym, "side": "long" if sign > 0 else "short", "status": "open",
                     "opened_at": f["time"], "closed_at": None, "qty": 0.0, "max_qty": 0.0, "entry_value": 0.0,
                     "entry_qty": 0.0, "exit_value": 0.0, "exit_qty": 0.0, "realized_pnl": 0.0, "commission": 0.0,
                     "fills": [], "ticket_ids": [], "multiplier": float(f.get("multiplier") or 1),
                     "asset_class": f.get("asset_class") or "STK"}
                open_by_sym[sym] = t
            tdir = 1 if t["side"] == "long" else -1
            ticket_id, _ = parse_ref(f.get("ref"))
            if ticket_id and ticket_id not in t["ticket_ids"]:
                t["ticket_ids"].append(ticket_id)
            if sign == tdir:  # adding
                t["entry_value"] += qty * f["price"]
                t["entry_qty"] += qty
                t["qty"] += qty
                t["max_qty"] = max(t["max_qty"], t["qty"])
                t["commission"] += comm
                t["fills"].append({**f, "action": "entry", "qty": qty})
                qty = 0
            else:  # reducing
                avg = t["entry_value"] / t["entry_qty"]
                q = min(qty, t["qty"])
                t["exit_value"] += q * f["price"]
                t["exit_qty"] += q
                t["realized_pnl"] += (f["price"] - avg) * q * tdir * t["multiplier"]
                part = comm * q / float(f["qty"])
                t["commission"] += part
                comm -= part
                t["fills"].append({**f, "action": "exit", "qty": q})
                t["qty"] -= q
                qty -= q
                if t["qty"] <= 1e-9:
                    t["status"], t["closed_at"] = "closed", f["time"]
                    out.append(t)
                    open_by_sym.pop(sym, None)
                    t = None
    out.extend(open_by_sym.values())
    for t in out:
        entry_avg = t["entry_value"] / t["entry_qty"] if t["entry_qty"] else None
        exit_avg = t["exit_value"] / t["exit_qty"] if t["exit_qty"] else None
        t.update(entry_avg=round(entry_avg, 4) if entry_avg else None, exit_avg=round(exit_avg, 4) if exit_avg else None,
                 realized_pnl=round(t["realized_pnl"], 2), commission=round(t["commission"], 2),
                 net_pnl=round(t["realized_pnl"] - t["commission"], 2),
                 return_pct=round(t["realized_pnl"] / (entry_avg * t["exit_qty"] * t["multiplier"]) * 100, 2)
                 if entry_avg and t["exit_qty"] else None,
                 holding_seconds=(t["closed_at"] or int(time.time())) - t["opened_at"])
        for k in ("entry_value", "exit_value"):
            t.pop(k)
    out.sort(key=lambda x: x["opened_at"], reverse=True)
    return out


def trade_stats(trades: list[dict]) -> dict:
    closed = [t for t in trades if t["status"] == "closed"]
    wins = [t["net_pnl"] for t in closed if t["net_pnl"] > 0]
    losses = [t["net_pnl"] for t in closed if t["net_pnl"] <= 0]
    gross_win, gross_loss = sum(wins), -sum(losses)
    rs = [t["r_multiple"] for t in closed if t.get("r_multiple") is not None]
    return {
        "closed_trades": len(closed), "open_trades": len(trades) - len(closed), "wins": len(wins), "losses": len(losses),
        "win_rate": round(len(wins) / len(closed) * 100, 1) if closed else None,
        "net_pnl": round(sum(t["net_pnl"] for t in closed), 2), "commission": round(sum(t["commission"] for t in trades), 2),
        "avg_win": round(gross_win / len(wins), 2) if wins else None,
        "avg_loss": round(-gross_loss / len(losses), 2) if losses else None,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else None,
        "expectancy": round(sum(t["net_pnl"] for t in closed) / len(closed), 2) if closed else None,
        "avg_r": round(sum(rs) / len(rs), 2) if rs else None,
        "best": max((t["net_pnl"] for t in closed), default=None), "worst": min((t["net_pnl"] for t in closed), default=None),
        "avg_hold_hours": round(sum(t["holding_seconds"] for t in closed) / len(closed) / 3600, 1) if closed else None,
    }


# ===================================================================================== service


def _decorate_positions(positions: list[dict], orders: list[dict], fx: float, net: float) -> None:
    """Add weight, unrealized %, the working stop / target and the risk to the stop to each position."""
    for p in positions:
        mv = (p["market_value"] or 0) * fx
        p["weight_pct"] = _r2(mv / net * 100) if net else None
        p["unrealized_pct"] = _r2((p["market_price"] / p["avg_cost"] - 1) * 100 * (1 if p["qty"] > 0 else -1)) \
            if p.get("market_price") and p["avg_cost"] else None
        exit_side = "sell" if p["qty"] > 0 else "buy"
        mine = [o for o in orders if o["symbol"] == p["symbol"] and o["side"] == exit_side and o["status"] in OPEN_STATUSES]
        stops = [o["stop_price"] for o in mine if o.get("role") == "stop_loss" and o.get("stop_price")]
        targets = [o["limit_price"] for o in mine if o.get("role") == "take_profit" and o.get("limit_price")]
        # the nearest protective levels
        p["stop"] = (max(stops) if p["qty"] > 0 else min(stops)) if stops else None
        p["target"] = (min(targets) if p["qty"] > 0 else max(targets)) if targets else None
        p["risk_to_stop"] = _r2(abs(p["market_price"] - p["stop"]) * abs(p["qty"]) * fx) \
            if p["stop"] and p.get("market_price") else None


class TradingService:
    def __init__(self):
        self.broker: Broker | None = None
        self._kind: str | None = None
        self._lock = threading.RLock()
        self._armed = False
        self._last_snapshot = 0.0
        self._last_fill_sync = 0.0
        self._last_account_push = 0.0
        self._account_push_pending = False
        self._worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="trading")
        # False in the stdio MCP process: the broker connection belongs to `autobench serve` only
        self.enabled_here = True

    # --- setup ------------------------------------------------------------------------------------
    def connection(self) -> dict:
        s = get_settings()
        saved = db.state_get(CONNECTION_KEY) or {}
        return {"broker": saved.get("broker") or s.autobench_broker, "host": saved.get("host") or s.ibkr_host,
                "port": saved.get("port") or s.ibkr_port,
                "client_id": saved.get("client_id") if saved.get("client_id") is not None else s.ibkr_client_id,
                "account": saved.get("account") or s.ibkr_account or None}

    def start(self) -> None:
        """Create and connect the configured broker (idempotent)."""
        with self._lock:
            if self.broker is not None or self._kind == "none":
                return
            c = self.connection()
            self._kind = c["broker"]
            self.broker = create_broker(c["broker"], c["host"], int(c["port"]), int(c["client_id"]), c["account"])
            if self.broker is None:
                return
            self.broker.add_listener(self._on_broker_event)
        try:
            self.broker.connect()
        except Exception as e:  # pragma: no cover - connection errors are reported via status
            log.warning("broker connect failed: %s", e)

    def stop(self) -> None:
        with self._lock:
            b, self.broker, self._kind = self.broker, None, None
            self._armed = False
        if b is not None:
            try:
                b.disconnect()
            except Exception:
                pass

    def reconnect(self, broker: str, host: str | None = None, port: int | None = None, client_id: int | None = None,
                  account: str | None = None) -> dict:
        saved = {"broker": broker}
        if account or (db.state_get(CONNECTION_KEY) or {}).get("account"):  # keep the chosen account across reconnects
            saved["account"] = account or db.state_get(CONNECTION_KEY)["account"]
        if host:
            saved["host"] = host
        if port:
            saved["port"] = port
        if client_id is not None:
            saved["client_id"] = client_id
        db.state_set(CONNECTION_KEY, saved)
        self.stop()
        self.start()
        self._audit("user", "connect", details=saved)
        self._publish_status()
        return self.status()

    def select_account(self, account: str) -> dict:
        """User only: switch the account that the Trading page, order ticket and agent use. Disarms live
        trading (arming is per account); proposals made for another account cannot be approved here."""
        b = self._broker()
        prev = b.status().get("account")
        b.select_account(account)
        saved = db.state_get(CONNECTION_KEY) or {"broker": self._kind}
        db.state_set(CONNECTION_KEY, {**saved, "account": account})
        if prev != account:
            self._armed = False
            self._audit("user", "account", details={"from": prev, "to": account})
            self._worker.submit(self._on_connected)  # sync fills and snapshot equity of the new account
        self._publish_status()
        return self.status()

    def _current_account(self) -> str | None:
        return self.broker.status().get("account") if self.broker else None

    def _broker(self) -> Broker:
        if not self.enabled_here:
            raise ValueError("Trading is only available through the HTTP MCP endpoint of a running "
                             "`autobench serve` (http://127.0.0.1:8000/mcp), not the stdio MCP server")
        self.start()
        if self.broker is None:
            raise ValueError("No broker configured (choose IBKR or the simulator in the Trading page)")
        return self.broker

    # --- state ------------------------------------------------------------------------------------
    def settings(self) -> TradingSettings:
        raw = db.state_get(SETTINGS_KEY)
        try:
            return TradingSettings.model_validate(raw) if raw else TradingSettings()
        except Exception:
            return TradingSettings()

    def update_settings(self, data: dict) -> TradingSettings:
        """User only (no MCP tool)."""
        merged = self.settings().model_dump()
        limits = data.pop("limits", None)
        merged.update(data)
        if limits:
            merged["limits"].update(limits)
        new = TradingSettings.model_validate(merged)
        db.state_set(SETTINGS_KEY, new.model_dump())
        self._audit("user", "settings", details=new.model_dump())
        self._publish_status()
        return new

    def halted(self) -> bool:
        return bool((db.state_get(HALT_KEY) or {}).get("halted"))

    def status(self) -> dict:
        b = self.broker
        st = b.status() if b else {"kind": self._kind or self.connection()["broker"], "connected": False, "account": None,
                                   "paper": None, "message": "No broker" if self._kind == "none" else "Not started"}
        s = self.settings()
        return {**st, "mode": s.mode, "halted": self.halted(), "halt": db.state_get(HALT_KEY) or {},
                "armed": self._armed, "live_allowed": get_settings().autobench_allow_live_trading,
                "can_trade": bool(st.get("connected")) and (st.get("paper") is not False or
                                                            (get_settings().autobench_allow_live_trading and self._armed)),
                "connection": self.connection(), "pending_tickets": self._count_tickets("pending")}

    # --- reads ------------------------------------------------------------------------------------
    def account(self) -> dict:
        return self._broker().account()

    def positions(self) -> list[dict]:
        return self._broker().positions()

    def orders(self, include_done: bool = False, symbol: str | None = None) -> list[dict]:
        out = self._broker().orders(include_done)
        if symbol:
            out = [o for o in out if o["symbol"] == symbol.upper()]
        return sorted(out, key=lambda o: o.get("updated_at") or 0, reverse=True)

    def overview(self) -> dict:
        b = self._broker()
        st = self.status()
        if not st["connected"]:
            return {"status": st, "account": None, "capital": None, "positions": [], "orders": []}
        acct, positions, orders = b.account(), b.positions(), b.orders()
        fx = acct.get("usd_to_base") or 1.0
        net = acct.get("net_liquidation") or 0.0
        long_v = sum(p["market_value"] or 0 for p in positions if p["qty"] > 0) * fx
        short_v = -sum(p["market_value"] or 0 for p in positions if p["qty"] < 0) * fx
        committed = sum((o["limit_price"] or o["stop_price"] or 0) * (o["qty"] - (o["filled_qty"] or 0))
                        for o in orders if o["side"] == "buy" and o.get("role") in (None, "entry") and o["status"] in OPEN_STATUSES) * fx
        cash = acct.get("cash") or 0.0
        _decorate_positions(positions, orders, fx, net)
        capital = {
            "currency": acct.get("currency"), "net_liquidation": net, "cash": cash, "invested": _r2(long_v + short_v),
            "long_value": _r2(long_v), "short_value": _r2(short_v), "committed_to_orders": _r2(committed),
            "free_cash": _r2(cash - committed), "invested_pct": _r2((long_v + short_v) / net * 100) if net else None,
            "cash_pct": _r2(cash / net * 100) if net else None,
            "open_risk": _r2(sum(p["risk_to_stop"] or 0 for p in positions)),
            "unprotected_positions": [p["symbol"] for p in positions if not p["stop"]],
        }
        return {"status": st, "account": acct, "capital": capital, "positions": positions, "orders": orders}

    def _context(self, spec_symbol: str) -> dict:
        b = self._broker()
        st = b.status()
        if not st.get("connected"):
            return {"connected": False, "account": {}, "positions": [], "orders": [], "paper": st.get("paper")}
        return {"connected": True, "account": b.account(), "positions": b.positions(), "orders": b.orders(),
                "paper": st.get("paper")}

    def _quote(self, symbol: str) -> float | None:
        b = self.broker
        if b is not None and b.kind == "sim":
            return b.price(symbol)  # type: ignore[attr-defined]
        try:
            from . import market

            return float(market.get_quote(symbol)["price"])
        except Exception:
            return None

    def _orders_today(self) -> int:
        start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
        with db.session_scope() as s:
            rows = s.scalars(select(db.OrderTicketRow).where(db.OrderTicketRow.decided_at >= start)).all()
            return sum(1 for r in rows if r.order_ids)

    def preview(self, spec: OrderSpec, actor: str = "user") -> dict:
        ctx = self._context(spec.symbol)
        price = self._quote(spec.symbol)
        res = check_order(spec, actor=actor, account=ctx["account"], positions=ctx["positions"],
                          open_orders=ctx["orders"], settings=self.settings(), price=price,
                          orders_today=self._orders_today(), halted=self.halted(), paper=ctx["paper"],
                          live_allowed=get_settings().autobench_allow_live_trading, armed=self._armed,
                          connected=ctx["connected"])
        res["mode"] = self.settings().mode
        if actor == "agent" and res["ok"]:
            res["would"] = "submit" if self._auto_ok(ctx["paper"]) else "await_approval"
        return res

    def _auto_ok(self, paper: bool | None) -> bool:
        return self.settings().mode == "auto" and paper is True

    # --- tickets ----------------------------------------------------------------------------------
    def _expiry(self, created: datetime) -> datetime:
        """A proposal lives ticket_ttl_minutes; one made outside regular hours lives at least until the
        next session's close, so it does not expire before the user could act on it."""
        deadline = created + timedelta(minutes=self.settings().ticket_ttl_minutes)
        if not in_regular_session(created):
            deadline = max(deadline, next_session_close(created))
        return deadline

    def _ticket_dict(self, r: db.OrderTicketRow) -> dict:
        return {"id": r.id, "symbol": r.symbol, "spec": r.spec, "source": r.source, "status": r.status,
                "rationale": r.rationale, "analysis_id": r.analysis_id, "check": r.check, "order_ids": r.order_ids,
                "message": r.message, "broker": r.broker, "account": r.account, "created_at": _iso(r.created_at),
                "updated_at": _iso(r.updated_at), "decided_at": _iso(r.decided_at),
                "expires_at": _iso(self._expiry(r.created_at)) if r.status == "pending" else None}

    def tickets(self, status: str | None = None, symbol: str | None = None, limit: int = 100) -> list[dict]:
        with db.session_scope() as s:
            q = select(db.OrderTicketRow).order_by(db.OrderTicketRow.created_at.desc()).limit(limit)
            if status:
                q = q.where(db.OrderTicketRow.status.in_(status.split(",")))
            if symbol:
                q = q.where(db.OrderTicketRow.symbol == symbol.upper())
            return [self._ticket_dict(r) for r in s.scalars(q).all()]

    def ticket(self, ticket_id: str) -> dict:
        with db.session_scope() as s:
            r = s.get(db.OrderTicketRow, ticket_id)
            if r is None:
                raise KeyError(f"Ticket {ticket_id} not found")
            return self._ticket_dict(r)

    def _count_tickets(self, status: str) -> int:
        with db.session_scope() as s:
            return len(s.scalars(select(db.OrderTicketRow.id).where(db.OrderTicketRow.status == status)).all())

    def submit(self, spec: OrderSpec, actor: str = "user", entry_role: str = "entry") -> dict:
        """User: check and send to the broker. Agent: check, then create a pending ticket (or send it,
        in auto mode on a paper account). Raises ValueError with the reasons when the check fails."""
        check = self.preview(spec, actor)
        if not check["ok"]:
            raise ValueError("Order rejected by risk checks: " + "; ".join(check["errors"]))
        st = self._broker().status()
        tid = uuid.uuid4().hex[:10]
        row = db.OrderTicketRow(id=tid, symbol=spec.symbol, spec=spec.model_dump(), source=actor, status="pending",
                                rationale=spec.rationale, analysis_id=spec.analysis_id, check=check,
                                broker=st.get("kind"), account=st.get("account"))
        with db.session_scope() as s:
            s.add(row)
        self._audit(actor, "propose" if actor == "agent" else "order", spec.symbol, tid,
                    {"spec": spec.model_dump(), "estimate": check["estimate"], "warnings": check["warnings"]})
        if actor == "agent" and not self._auto_ok(st.get("paper")):
            t = self.ticket(tid)
            bus.publish("trading.ticket", t)
            return t
        if check["intent"] == "close" and entry_role == "entry":
            entry_role = closing_role(spec, check["estimate"].get("market_price"))
        return self._send(tid, spec, actor, entry_role)

    def _send(self, tid: str, spec: OrderSpec, actor: str, entry_role: str = "entry") -> dict:
        b = self._broker()
        entry = {"side": spec.side, "qty": spec.qty, "type": spec.order_type, "limit_price": spec.limit_price,
                 "stop_price": spec.stop_price, "tif": spec.tif, "outside_rth": spec.outside_rth}
        opp = "sell" if spec.side == "buy" else "buy"
        tp = {"side": opp, "qty": spec.qty, "type": "limit", "limit_price": spec.take_profit, "tif": "gtc",
              "outside_rth": spec.outside_rth} if spec.take_profit else None
        sl = {"side": opp, "qty": spec.qty, "type": "stop", "stop_price": spec.stop_loss, "tif": "gtc",
              "outside_rth": spec.outside_rth} if spec.stop_loss else None
        try:
            orders = b.place(spec.symbol, tid, entry, tp, sl, entry_role=entry_role)
        except (BrokerError, KeyError, ValueError) as e:
            self._update_ticket(tid, status="error", message=str(e), decided=True)
            self._audit(actor, "error", spec.symbol, tid, {"error": str(e)})
            raise ValueError(f"Broker rejected the order: {e}") from e
        self._update_ticket(tid, status="working", order_ids=[o["id"] for o in orders], decided=True,
                            message=None)
        self._audit(actor, "submit" + ("_auto" if actor == "agent" else ""), spec.symbol, tid,
                    {"orders": [o["id"] for o in orders]})
        # the first broker status may already be terminal (e.g. simulator fills market orders at once)
        for o in orders:
            self._ticket_from_order(o)
        t = self.ticket(tid)
        bus.publish("trading.ticket", t)
        return t

    def _update_ticket(self, tid: str, status: str | None = None, message: str | None = "__keep__",
                       order_ids: list | None = None, decided: bool = False) -> dict | None:
        with db.session_scope() as s:
            r = s.get(db.OrderTicketRow, tid)
            if r is None:
                return None
            if status:
                r.status = status
            if message != "__keep__":
                r.message = message
            if order_ids is not None:
                r.order_ids = order_ids
            if decided and r.decided_at is None:
                r.decided_at = _now()
            r.updated_at = _now()
            return self._ticket_dict(r)

    def propose_modify(self, order_id: str, patch: OrderModify, rationale: str, actor: str = "agent") -> dict:
        """A proposed change to a working order (e.g. trail a stop, move a limit). Always waits for the
        user's approval, also in auto mode: the change is applied by the user's approve()."""
        changes = patch.model_dump(exclude_none=True)
        if not changes:
            raise ValueError("Give qty, limit_price or stop_price to change")
        o = self._order(order_id)
        if o.get("status") not in ("working", "pending", "submitted", "partially_filled"):
            raise ValueError(f"Order {order_id} is {o.get('status')}; only working orders can be changed")
        if o.get("source") != "autobench":
            raise ValueError("Only orders placed through Autobench can be changed here")
        if "limit_price" in changes and o.get("type") not in ("limit", "stop_limit"):
            raise ValueError(f"Order {order_id} is a {o.get('type')} order without a limit price")
        if "stop_price" in changes and o.get("type") not in ("stop", "stop_limit"):
            raise ValueError(f"Order {order_id} is a {o.get('type')} order without a stop price")
        warnings = []
        px = self._quote(o["symbol"])
        if o.get("role") == "stop_loss" and "stop_price" in changes and o.get("stop_price") and px:
            long = o["side"] == "sell"
            if (long and changes["stop_price"] >= px) or (not long and changes["stop_price"] <= px):
                raise ValueError(f"A stop at {changes['stop_price']} is through the market ({px:.2f}) and would trigger at once")
            if (long and changes["stop_price"] < o["stop_price"]) or (not long and changes["stop_price"] > o["stop_price"]):
                warnings.append("This moves the stop further away: more risk on the position")
        st = self._broker().status()
        spec = {"action": "modify", "order_id": order_id, "changes": changes, "role": o.get("role"),
                "symbol": o["symbol"], "side": o["side"], "qty": o["qty"], "order_type": o.get("type"),
                "limit_price": o.get("limit_price"), "stop_price": o.get("stop_price"), "tif": o.get("tif"),
                "rationale": rationale}
        tid = uuid.uuid4().hex[:10]
        check = {"ok": True, "intent": "modify", "errors": [], "warnings": warnings,
                 "estimate": {"market_price": px, "price": px}}
        with db.session_scope() as s:
            s.add(db.OrderTicketRow(id=tid, symbol=o["symbol"], spec=spec, source=actor, status="pending",
                                    rationale=rationale, check=check, order_ids=[], broker=st.get("kind"),
                                    account=st.get("account")))
        self._audit(actor, "propose_modify", o["symbol"], tid, {"order_id": order_id, **changes})
        t = self.ticket(tid)
        bus.publish("trading.ticket", t)
        return t

    def _approve_modify(self, t: dict) -> dict:
        spec = t["spec"]
        try:
            self.modify_order(spec["order_id"], OrderModify(**spec["changes"]), "user")
        except (BrokerError, KeyError, ValueError) as e:
            self._update_ticket(t["id"], status="error", message=str(e), decided=True)
            out = self.ticket(t["id"])
            bus.publish("trading.ticket", out)
            raise ValueError(f"Could not change the order: {e}") from e
        self._audit("user", "approve", t["symbol"], t["id"])
        out = self._update_ticket(t["id"], status="applied", message=None, decided=True)
        bus.publish("trading.ticket", out)
        return out

    def approve(self, ticket_id: str) -> dict:
        """User only: re-check a pending agent ticket and send it."""
        t = self.ticket(ticket_id)
        if t["status"] != "pending":
            raise ValueError(f"Ticket is {t['status']}, not pending")
        cur = self._current_account()
        if t.get("account") and cur and t["account"] != cur:
            raise ValueError(f"This proposal was made for account {t['account']}; switch to that account to approve it")
        if (t.get("spec") or {}).get("action") == "modify":
            return self._approve_modify(t)
        spec = OrderSpec.model_validate(t["spec"])
        check = self.preview(spec, "user")
        if not check["ok"]:
            raise ValueError("Cannot approve: " + "; ".join(check["errors"]))
        self._audit("user", "approve", spec.symbol, ticket_id)
        role = closing_role(spec, check["estimate"].get("market_price")) if check["intent"] == "close" else "entry"
        return self._send(ticket_id, spec, "user", role)

    def reject(self, ticket_id: str, reason: str | None = None, actor: str = "user") -> dict:
        t = self.ticket(ticket_id)
        if t["status"] != "pending":
            raise ValueError(f"Ticket is {t['status']}, not pending")
        if actor == "agent" and t["source"] != "agent":
            raise ValueError("The agent can only withdraw its own proposals")
        t = self._update_ticket(ticket_id, status="rejected" if actor == "user" else "cancelled",
                                message=reason or ("Rejected" if actor == "user" else "Withdrawn by the agent"), decided=True)
        self._audit(actor, "reject" if actor == "user" else "withdraw", t["symbol"], ticket_id, {"reason": reason})
        bus.publish("trading.ticket", t)
        return t

    def expire_tickets(self) -> int:
        now = _now()
        expired = []
        with db.session_scope() as s:
            for r in s.scalars(select(db.OrderTicketRow).where(db.OrderTicketRow.status == "pending")).all():
                if self._expiry(r.created_at) > now:
                    continue
                r.status, r.message, r.updated_at = "expired", "Not approved in time", now
                expired.append(self._ticket_dict(r))
        for t in expired:
            bus.publish("trading.ticket", t)
        return len(expired)

    # --- order management -------------------------------------------------------------------------
    def _order(self, order_id: str) -> dict:
        o = next((x for x in self._broker().orders(include_done=True) if x["id"] == order_id), None)
        if o is None:
            raise KeyError(f"Order {order_id} not found")
        return o

    def cancel_order(self, order_id: str, actor: str = "user") -> dict:
        o = self._order(order_id)
        if actor == "agent":
            pos = next((p["qty"] for p in self._broker().positions() if p["symbol"] == o["symbol"]), 0)
            if o.get("role") == "stop_loss" and pos:
                raise ValueError("The agent cannot cancel a stop-loss that protects an open position; "
                                 "propose closing the position instead")
            if o.get("source") != "autobench":
                raise ValueError("The agent can only cancel orders placed through Autobench")
        self._broker().cancel(order_id)
        self._audit(actor, "cancel", o["symbol"], o.get("ticket_id"), {"order_id": order_id, "role": o.get("role")})
        return self._order(order_id)

    def modify_order(self, order_id: str, patch: OrderModify, actor: str = "user") -> dict:
        if actor != "user":
            raise ValueError("Only the user can modify working orders")
        o = self._order(order_id)
        if self.halted() and o.get("role") in (None, "entry"):
            raise ValueError("Trading is halted")
        st = self._broker().status()
        if st.get("paper") is False and not (get_settings().autobench_allow_live_trading and self._armed):
            raise ValueError("Live trading is not enabled/armed")
        if o.get("role") == "stop_loss" and patch.stop_price is not None:
            pos = next((p for p in self._broker().positions() if p["symbol"] == o["symbol"]), None)
            px = self._quote(o["symbol"])
            if pos and px and ((pos["qty"] > 0 and patch.stop_price >= px) or (pos["qty"] < 0 and patch.stop_price <= px)):
                raise ValueError(f"A stop at {patch.stop_price} is through the market ({px:.2f}) and would trigger at once")
        res = self._broker().modify(order_id, qty=patch.qty, limit_price=patch.limit_price, stop_price=patch.stop_price)
        self._audit(actor, "modify", o["symbol"], o.get("ticket_id"), {"order_id": order_id, **patch.model_dump(exclude_none=True)})
        return res

    def close_position(self, symbol: str, req: ClosePosition, actor: str = "user") -> dict:
        symbol = symbol.upper()
        pos = next((p for p in self._broker().positions() if p["symbol"] == symbol), None)
        if pos is None or not pos["qty"]:
            raise ValueError(f"No open {symbol} position")
        qty = req.qty or abs(pos["qty"])
        if qty > abs(pos["qty"]):
            raise ValueError(f"Position is only {abs(pos['qty']):g} shares")
        spec = OrderSpec(symbol=symbol, side="sell" if pos["qty"] > 0 else "buy", qty=qty, order_type=req.order_type,
                         limit_price=req.limit_price, rationale=req.rationale or f"Close {symbol}", tif="day")
        return self.submit(spec, actor, entry_role="exit")

    def halt(self, actor: str = "user", flatten: bool = False, reason: str | None = None) -> dict:
        """Kill switch: stop new orders, cancel working entry orders and pending tickets. With flatten,
        also cancel protective orders and close every position at market."""
        db.state_set(HALT_KEY, {"halted": True, "at": _iso(_now()), "by": actor, "reason": reason, "flatten": flatten})
        cancelled, closed = [], []
        with db.session_scope() as s:
            for r in s.scalars(select(db.OrderTicketRow).where(db.OrderTicketRow.status == "pending")).all():
                r.status, r.message, r.updated_at = "cancelled", "Trading halted", _now()
        b = self.broker
        if b is not None and b.connected:
            for o in b.orders():
                if flatten or o.get("role") in (None, "entry"):
                    try:
                        b.cancel(o["id"])
                        cancelled.append(o["id"])
                    except Exception as e:
                        log.warning("halt: cancel %s failed: %s", o["id"], e)
            if flatten:
                for p in b.positions():
                    try:
                        t = self.close_position(p["symbol"], ClosePosition(order_type="market", rationale="Flatten (kill switch)"), "user")
                        closed.append(t["id"])
                    except Exception as e:
                        log.warning("halt: close %s failed: %s", p["symbol"], e)
        self._audit(actor, "halt", details={"flatten": flatten, "reason": reason, "cancelled": cancelled, "closed": closed})
        self._publish_status()
        return {"halted": True, "cancelled_orders": cancelled, "closing_tickets": closed}

    def resume(self) -> dict:
        db.state_set(HALT_KEY, {"halted": False, "at": _iso(_now())})
        self._audit("user", "resume")
        self._publish_status()
        return self.status()

    def arm(self, confirm: str) -> dict:
        if confirm != "LIVE":
            raise ValueError('Type LIVE to arm live trading')
        if not get_settings().autobench_allow_live_trading:
            raise ValueError("Live trading is disabled: set AUTOBENCH_ALLOW_LIVE_TRADING=1 and restart")
        self._armed = True
        self._audit("user", "arm")
        self._publish_status()
        return self.status()

    def disarm(self) -> dict:
        self._armed = False
        self._audit("user", "disarm")
        self._publish_status()
        return self.status()

    # --- fills, history, equity -------------------------------------------------------------------
    def _store_fill(self, f: dict) -> bool:
        """Insert or update a fill. Returns True when it is new."""
        st = self.broker.status() if self.broker else {}
        with db.session_scope() as s:
            r = s.get(db.FillRow, f["exec_id"])
            if r is None:
                s.add(db.FillRow(exec_id=f["exec_id"], account=f.get("account") or st.get("account") or "",
                                 broker=st.get("kind") or "", order_id=f.get("order_id"), ref=f.get("ref"),
                                 symbol=f["symbol"], side=f["side"], qty=f["qty"], price=f["price"], time=f["time"],
                                 commission=f.get("commission"), realized_pnl=f.get("realized_pnl")))
                return True
            if f.get("commission") is not None:
                r.commission = f["commission"]
            if f.get("realized_pnl") is not None:
                r.realized_pnl = f["realized_pnl"]
            return False

    def sync_fills(self) -> int:
        b = self.broker
        if b is None or not b.connected:
            return 0
        n = 0
        for f in b.fills():
            n += self._store_fill(f)
        return n

    def _account_filter(self):
        st = self.broker.status() if self.broker else {}
        return st.get("account")

    def fills(self, symbol: str | None = None, since: int | None = None, limit: int = 500, account: str | None = None) -> list[dict]:
        acct = account or self._account_filter()
        with db.session_scope() as s:
            q = select(db.FillRow).order_by(db.FillRow.time.desc()).limit(limit)
            if acct:
                q = q.where(db.FillRow.account == acct)
            if symbol:
                q = q.where(db.FillRow.symbol == symbol.upper())
            if since:
                q = q.where(db.FillRow.time >= since)
            rows = s.scalars(q).all()
            return [{"exec_id": r.exec_id, "order_id": r.order_id, "ref": r.ref, "ticket_id": parse_ref(r.ref)[0],
                     "role": parse_ref(r.ref)[1], "symbol": r.symbol, "side": r.side, "qty": r.qty, "price": r.price,
                     "time": r.time, "commission": r.commission, "realized_pnl": r.realized_pnl, "account": r.account,
                     "asset_class": r.asset_class, "multiplier": r.multiplier, "currency": r.currency,
                     "source": "statement" if r.import_id else "live"}
                    for r in rows]

    def trades(self, symbol: str | None = None, status: str | None = None, limit: int = 200) -> dict:
        fills = self.fills(symbol=symbol, limit=20000)
        trades = round_trips(fills)
        stops = self._ticket_stops({tid for t in trades for tid in t["ticket_ids"]})
        prices = {}
        if self.broker is not None and self.broker.connected:
            try:
                prices = {p["symbol"]: p for p in self.broker.positions()}
            except Exception:
                prices = {}
        for t in trades:
            stop = next((stops[i] for i in t["ticket_ids"] if i in stops), None)
            t["planned_stop"] = stop
            if stop and t["entry_avg"]:
                risk = abs(t["entry_avg"] - stop) * t["max_qty"]
                t["r_multiple"] = round(t["net_pnl"] / risk, 2) if risk and t["status"] == "closed" else None
            else:
                t["r_multiple"] = None
            if t["status"] == "open":
                p = prices.get(t["symbol"])
                t["market_price"] = p["market_price"] if p else None
                t["unrealized_pnl"] = p["unrealized_pnl"] if p else None
        stats = trade_stats(trades)
        if status:
            trades = [t for t in trades if t["status"] == status]
        return {"trades": trades[:limit], "stats": stats}

    def _ticket_stops(self, ids: set[str]) -> dict[str, float]:
        if not ids:
            return {}
        with db.session_scope() as s:
            rows = s.scalars(select(db.OrderTicketRow).where(db.OrderTicketRow.id.in_(ids))).all()
            return {r.id: r.spec.get("stop_loss") for r in rows if r.spec.get("stop_loss")}

    def snapshot_equity(self) -> None:
        b = self.broker
        if b is None or not b.connected:
            return
        a = b.account()
        if a.get("net_liquidation") is None:
            return
        t = int(time.time()) // 60 * 60
        with db.session_scope() as s:
            s.merge(db.EquityRow(account=a["account"], t=t, net_liquidation=a["net_liquidation"], cash=a.get("cash") or 0,
                                 gross_position_value=a.get("gross_position_value") or 0, daily_pnl=a.get("daily_pnl"),
                                 currency=a.get("currency")))
        self._last_snapshot = time.time()

    def equity(self, days: int = 365, account: str | None = None) -> list[dict]:
        acct = account or self._account_filter()
        since = int(time.time()) - days * 86400
        with db.session_scope() as s:
            q = select(db.EquityRow).where(db.EquityRow.t >= since).order_by(db.EquityRow.t)
            if acct:
                q = q.where(db.EquityRow.account == acct)
            return [{"t": r.t, "net_liquidation": r.net_liquidation, "cash": r.cash, "invested": r.gross_position_value,
                     "daily_pnl": r.daily_pnl} for r in s.scalars(q).all()]

    def audit(self, limit: int = 200) -> list[dict]:
        with db.session_scope() as s:
            rows = s.scalars(select(db.TradingAuditRow).order_by(db.TradingAuditRow.id.desc()).limit(limit)).all()
            return [{"id": r.id, "time": _iso(r.time), "actor": r.actor, "action": r.action, "symbol": r.symbol,
                     "ticket_id": r.ticket_id, "details": r.details} for r in rows]

    def chart(self, symbol: str) -> dict:
        """Everything the chart overlays for one symbol: position, working orders, pending tickets, fills, trades."""
        symbol = symbol.upper()
        b = self.broker
        connected = b is not None and b.connected
        pos, orders = None, []
        if connected:
            orders = self.orders(symbol=symbol)
            pos = next((p for p in b.positions() if p["symbol"] == symbol), None)
            if pos is not None:
                acct = b.account()
                _decorate_positions([pos], orders, acct.get("usd_to_base") or 1.0, acct.get("net_liquidation") or 0.0)
        return {"symbol": symbol, "connected": connected,
                "position": pos, "orders": orders,
                "tickets": self.tickets(status="pending", symbol=symbol),
                "fills": self.fills(symbol=symbol, limit=1000),
                "trades": self.trades(symbol=symbol, limit=100)["trades"]}

    # --- broker events ----------------------------------------------------------------------------
    def _on_broker_event(self, kind: str, data) -> None:
        """Called from the broker's thread: persist, then hand broker calls to the worker thread."""
        if kind == "order":
            self._ticket_from_order(data)
            bus.publish("trading.order", data)
        elif kind == "fill":
            if self._store_fill(data):
                self._audit("system", "fill", data["symbol"], parse_ref(data.get("ref"))[0],
                            {k: data[k] for k in ("side", "qty", "price", "order_id")})
                bus.publish("trading.fill", data)
                if not data.get("account") or data["account"] == self._current_account():
                    self._worker.submit(self._after_fill, data["symbol"], data["side"])
        elif kind == "account":
            self._push_account()
        elif kind == "status":
            bus.publish("trading.status", self.status())
            if data and data.get("connected"):
                self._worker.submit(self._on_connected)
        elif kind == "error":
            bus.publish("trading.error", data)

    def _on_connected(self) -> None:
        try:
            self.sync_fills()
            self.snapshot_equity()
        except Exception as e:
            log.info("post-connect sync failed: %s", e)

    def _ticket_from_order(self, o: dict) -> None:
        tid = o.get("ticket_id")
        if not tid or o.get("parent_id") is not None:  # bracket children do not change the ticket
            return
        new = {"pending": "working", "working": "working", "partially_filled": "working", "filled": "filled",
               "cancelled": "cancelled", "rejected": "error", "inactive": "error"}.get(o["status"])
        if new is None:
            return
        with db.session_scope() as s:
            r = s.get(db.OrderTicketRow, tid)
            if r is None or r.status in ("pending", "rejected", "expired") or r.status == new:
                return
            r.status = new
            if new == "error":
                r.message = o.get("message") or "Rejected by the broker"
            r.updated_at = _now()
            t = self._ticket_dict(r)
        bus.publish("trading.ticket", t)

    def _after_fill(self, symbol: str, fill_side: str) -> None:
        """Keep protective orders consistent with the position after a fill that reduced it: cancel them
        when the position is flat, shrink them when it is smaller. Only fills on the same side as the
        protective orders (exits) count, and the position is read twice, because the broker's position
        can lag behind the fill report (an entry fill must never cancel its fresh stop-loss)."""
        b = self.broker
        if b is None or not b.connected:
            return
        try:
            def position() -> float:
                return next((p["qty"] for p in b.positions() if p["symbol"] == symbol), 0.0)

            def protective() -> list[dict]:
                return [o for o in b.orders() if o["symbol"] == symbol and o.get("role") in PROTECTIVE
                        and o["side"] == fill_side and o["status"] in ("working", "partially_filled")]

            if not protective():
                self.snapshot_equity()
                return
            settle = 1.5 if b.kind == "ibkr" else 0.0
            time.sleep(settle)
            pos = position()
            time.sleep(settle)
            if position() != pos:
                return  # still moving; the next fill will reconcile
            for o in protective():
                left = o["qty"] - (o["filled_qty"] or 0)
                if pos == 0 or (pos > 0) == (o["side"] == "buy"):  # flat, or the order no longer reduces
                    b.cancel(o["id"])
                    self._audit("system", "cancel_orphan", symbol, o.get("ticket_id"), {"order_id": o["id"], "role": o["role"]})
                elif left > abs(pos):
                    b.modify(o["id"], qty=abs(pos))
                    self._audit("system", "shrink_protective", symbol, o.get("ticket_id"), {"order_id": o["id"], "qty": abs(pos)})
        except Exception as e:
            log.warning("protective order reconciliation for %s failed: %s", symbol, e)
        self.snapshot_equity()

    def _push_account(self) -> None:
        now = time.time()
        if now - self._last_account_push < 1.0:
            if not self._account_push_pending:
                self._account_push_pending = True
                threading.Timer(1.0, self._push_account_now).start()
            return
        self._push_account_now()

    def _push_account_now(self) -> None:
        self._account_push_pending = False
        self._last_account_push = time.time()
        bus.publish("trading.account", {"ts": self._last_account_push})

    def _publish_status(self) -> None:
        bus.publish("trading.status", self.status())

    def _audit(self, actor: str, action: str, symbol: str | None = None, ticket_id: str | None = None,
               details: dict | None = None) -> None:
        with db.session_scope() as s:
            s.add(db.TradingAuditRow(actor=actor, action=action, symbol=symbol, ticket_id=ticket_id, details=details or {}))

    # --- periodic ---------------------------------------------------------------------------------
    def tick(self) -> None:
        b = self.broker
        if b is None:
            return
        b.tick()
        self.expire_tickets()
        if b.connected:
            now = time.time()
            if now - self._last_fill_sync > 60:
                self._last_fill_sync = now
                self.sync_fills()
            if now - self._last_snapshot > 300:
                self.snapshot_equity()

    # --- simulator helpers (tests / demos) ----------------------------------------------------------
    def sim(self):
        b = self._broker()
        if b.kind != "sim":
            raise ValueError("Only available with the simulator broker")
        return b

    def reset_local_history(self) -> None:
        """Simulator reset: forget the selected simulated account's fills, tickets and equity (never touches
        IBKR data)."""
        acct = self.sim().account_id
        with db.session_scope() as s:
            s.execute(delete(db.FillRow).where(db.FillRow.broker == "sim", db.FillRow.account == acct))
            s.execute(delete(db.EquityRow).where(db.EquityRow.account == acct))
            s.execute(delete(db.OrderTicketRow).where(db.OrderTicketRow.broker == "sim", db.OrderTicketRow.account == acct))


trading = TradingService()
