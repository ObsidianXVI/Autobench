"""Broker interface. Implementations (IBKR, simulator) normalise everything to plain dicts:

order    {id, ref, ticket_id, role, parent_id, symbol, side buy|sell, qty, type market|limit|stop|stop_limit,
          limit_price, stop_price, tif day|gtc, outside_rth, status, filled_qty, avg_fill_price, message,
          created_at, updated_at, source autobench|external}
          status: pending (held / not yet acknowledged) | working | partially_filled | filled | cancelled |
                  rejected | inactive
fill     {exec_id, order_id, ref, symbol, side, qty, price, time (epoch s), commission, realized_pnl, account}
account  {account, currency, net_liquidation, cash, buying_power, available_funds, gross_position_value,
          unrealized_pnl, realized_pnl, daily_pnl, usd_to_base}
position {symbol, qty, avg_cost, market_price, market_value, unrealized_pnl, realized_pnl}

Order legs passed to place(): {side, qty, type, limit_price, stop_price, tif, outside_rth}.
Every order carries a ref "ab:<ticket id>:<role>" so its origin survives restarts (role = entry |
take_profit | stop_loss | exit)."""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable

log = logging.getLogger(__name__)

ORDER_TYPES = ("market", "limit", "stop", "stop_limit")
OPEN_STATUSES = ("pending", "working", "partially_filled")
DONE_STATUSES = ("filled", "cancelled", "rejected", "inactive")

Listener = Callable[[str, Any], None]  # (kind: order|fill|account|status|error, data)


class BrokerError(Exception):
    """A broker-side failure (not connected, rejected request, timeout)."""


def make_ref(ticket_id: str, role: str) -> str:
    return f"ab:{ticket_id}:{role}"


def parse_ref(ref: str | None) -> tuple[str | None, str | None]:
    """(ticket id, role) from an order ref; (None, None) for orders placed outside Autobench."""
    if not ref or not ref.startswith("ab:"):
        return None, None
    parts = ref.split(":")
    return (parts[1] if len(parts) > 1 else None), (parts[2] if len(parts) > 2 else None)


class Broker:
    kind = "none"

    def __init__(self):
        self._listeners: list[Listener] = []
        self._lock = threading.RLock()

    # --- events -------------------------------------------------------------------------------
    def add_listener(self, fn: Listener) -> None:
        self._listeners.append(fn)

    def emit(self, kind: str, data: Any) -> None:
        for fn in list(self._listeners):
            try:
                fn(kind, data)
            except Exception:
                log.exception("broker listener failed (%s)", kind)

    # --- lifecycle ----------------------------------------------------------------------------
    def connect(self) -> None:
        raise NotImplementedError

    def disconnect(self) -> None:
        raise NotImplementedError

    def status(self) -> dict:
        """{kind, connected, account, paper (bool|None), message, ...}"""
        raise NotImplementedError

    @property
    def connected(self) -> bool:
        return bool(self.status().get("connected"))

    # --- reads --------------------------------------------------------------------------------
    def account(self) -> dict:
        raise NotImplementedError

    def positions(self) -> list[dict]:
        raise NotImplementedError

    def orders(self, include_done: bool = False) -> list[dict]:
        raise NotImplementedError

    def fills(self) -> list[dict]:
        """Executions the broker still knows about (IBKR: the current session / last day)."""
        raise NotImplementedError

    # --- writes -------------------------------------------------------------------------------
    def place(self, symbol: str, ticket_id: str, entry: dict, take_profit: dict | None = None,
              stop_loss: dict | None = None, entry_role: str = "entry") -> list[dict]:
        """Place an order, with optional take-profit / stop-loss children that activate once the entry
        fills (one cancels the other). Returns the created orders."""
        raise NotImplementedError

    def modify(self, order_id: str, qty: float | None = None, limit_price: float | None = None,
               stop_price: float | None = None) -> dict:
        raise NotImplementedError

    def cancel(self, order_id: str) -> None:
        raise NotImplementedError

    def tick(self) -> None:
        """Periodic housekeeping (the simulator matches resting orders here)."""
