"""Company and market news: Alpaca (Benzinga feed) when configured, Yahoo otherwise."""

from __future__ import annotations

from .. import db
from ..providers import ProviderError, alpaca_provider, yahoo
from ..timeutil import now_ts
from .market import normalize_symbol


def get_news(symbol: str | None = None, limit: int = 15, days: int = 14, include_content: bool = False) -> dict:
    sym = normalize_symbol(symbol) if symbol else None
    limit = max(1, min(limit, 50))
    a = alpaca_provider()
    key = f"news:{sym}:{limit}:{days}:{include_content}:{'alpaca' if a else 'yahoo'}"

    def fetch():
        if a is not None:
            try:
                items = a.news([sym] if sym else None, limit=limit, start=now_ts() - days * 86400,
                               include_content=include_content)
                if include_content:
                    for it in items:
                        it["content"] = _strip_html(it.get("content", ""))[:4000]
                return {"source": "alpaca", "items": items}
            except ProviderError:
                if not sym:
                    raise
        if not sym:
            raise ProviderError("Market-wide news requires Alpaca credentials")
        return {"source": "yahoo", "items": yahoo().news(sym, limit)}

    data = db.cached(key, 600, fetch)
    return {"symbol": sym, **data, "count": len(data["items"])}


def _strip_html(s: str) -> str:
    from .filings import html_to_text

    return html_to_text(s) if "<" in s else s
