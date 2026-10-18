"""Provider registry. Bars/quotes come from the configured provider (Alpaca when keys are set);
Yahoo always serves index/futures/FX symbols and fundamentals."""

from __future__ import annotations

import threading

from ..config import get_settings
from .base import BarProvider, ProviderError
from .yahoo import YahooProvider

_lock = threading.Lock()
_instances: dict[str, object] = {}


def _get(name: str):
    with _lock:
        if name not in _instances:
            if name == "yahoo":
                _instances[name] = YahooProvider()
            elif name == "alpaca":
                from .alpaca import AlpacaProvider

                settings = get_settings()
                if not settings.alpaca_enabled:
                    raise ProviderError("Alpaca is selected but ALPACA_API_KEY_ID / ALPACA_API_SECRET_KEY are not set")
                _instances[name] = AlpacaProvider(settings)
            elif name == "fake":
                from .fake import FakeProvider

                _instances[name] = FakeProvider()
            else:
                raise ProviderError(f"Unknown provider '{name}'")
        return _instances[name]


def yahoo_only_symbol(symbol: str) -> bool:
    """Indices (^VIX), futures (CL=F), FX (EURUSD=X) are not available from Alpaca."""
    return symbol.startswith("^") or "=" in symbol


def bar_provider_for(symbol: str) -> BarProvider:
    name = get_settings().bar_provider
    if name == "alpaca" and yahoo_only_symbol(symbol):
        name = "yahoo"
    return _get(name)


def primary_provider():
    return _get(get_settings().bar_provider)


def yahoo() -> YahooProvider:
    if get_settings().bar_provider == "fake":
        return _get("fake")  # type: ignore[return-value]
    return _get("yahoo")  # type: ignore[return-value]


def alpaca_provider():
    """The Alpaca provider, or None when it is not configured."""
    if get_settings().bar_provider == "fake" or not get_settings().alpaca_enabled:
        return None
    return _get("alpaca")


def reset_providers() -> None:
    with _lock:
        _instances.clear()


__all__ = ["BarProvider", "ProviderError", "bar_provider_for", "primary_provider", "yahoo", "alpaca_provider", "reset_providers"]
