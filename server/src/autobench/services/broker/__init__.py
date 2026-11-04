"""Brokers: Interactive Brokers (IB Gateway / TWS) and a built-in simulator."""

from .base import DONE_STATUSES, OPEN_STATUSES, ORDER_TYPES, Broker, BrokerError, make_ref, parse_ref


def create_broker(kind: str, host: str = "127.0.0.1", port: int = 4002, client_id: int = 7,
                  account: str | None = None) -> Broker | None:
    if kind == "sim":
        from .sim import SimBroker

        return SimBroker(account=account)
    if kind == "ibkr":
        from .ibkr import IbkrBroker

        return IbkrBroker(host, port, client_id, account)
    if kind in ("none", ""):
        return None
    raise ValueError(f"Unknown broker {kind!r} (use ibkr, sim or none)")


__all__ = ["Broker", "BrokerError", "create_broker", "make_ref", "parse_ref", "OPEN_STATUSES", "DONE_STATUSES",
           "ORDER_TYPES"]
