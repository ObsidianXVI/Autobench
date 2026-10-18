"""Yahoo Finance via yfinance: bars, quotes and search (no API key needed).

Also used for index symbols (^VIX, ^TNX, ...) that Alpaca does not carry."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import pandas as pd
import yfinance as yf

from ..timeutil import Interval
from .base import ProviderError, empty_bars, finalize_bars, session_date_ts

log = logging.getLogger(__name__)

_YF_INTERVAL = {"1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m", "1h": "1h", "1d": "1d", "1wk": "1wk", "1mo": "1mo"}

# Yahoo only serves recent intraday history; requests outside the window fail.
_MAX_HISTORY = {"1m": 29 * 86400, "5m": 59 * 86400, "15m": 59 * 86400, "30m": 59 * 86400, "1h": 729 * 86400}
# 1m data must be requested in chunks of at most 7 days.
_CHUNK = {"1m": 7 * 86400}


def yahoo_symbol(symbol: str) -> str:
    # Class shares: BRK.B (Alpaca/common) -> BRK-B (Yahoo)
    return symbol.replace(".", "-") if "." in symbol and not symbol.startswith("^") else symbol


class YahooProvider:
    name = "yahoo"

    def max_history_seconds(self, interval: Interval) -> int | None:
        return _MAX_HISTORY.get(interval.name)

    def fetch_bars(self, symbol: str, interval: Interval, start: int, end: int) -> pd.DataFrame:
        limit = self.max_history_seconds(interval)
        now = int(datetime.now(UTC).timestamp())
        if limit is not None:
            start = max(start, now - limit)
        if start >= end:
            return empty_bars()
        chunk = _CHUNK.get(interval.name, end - start)
        frames = []
        s = start
        while s < end:
            e = min(end, s + chunk)
            frames.append(self._fetch_once(symbol, interval, s, e))
            s = e
        frames = [f for f in frames if not f.empty]
        if not frames:
            return empty_bars()
        out = pd.concat(frames)
        return finalize_bars(out[(out.index >= start - 86400) & (out.index <= end)])

    def _fetch_once(self, symbol: str, interval: Interval, start: int, end: int) -> pd.DataFrame:
        s_dt = datetime.fromtimestamp(start, UTC)
        e_dt = datetime.fromtimestamp(end, UTC)
        if not interval.intraday:
            # yfinance's end is exclusive and date-based
            s_dt, e_dt = s_dt - timedelta(days=1), e_dt + timedelta(days=1)
        try:
            raw = yf.Ticker(yahoo_symbol(symbol)).history(
                start=s_dt, end=e_dt, interval=_YF_INTERVAL[interval.name], auto_adjust=True, actions=False,
            )
        except Exception as e:  # yfinance raises a zoo of exception types
            msg = str(e)
            if "delisted" in msg.lower() or "no data" in msg.lower() or "no price data" in msg.lower():
                return empty_bars()
            raise ProviderError(f"Yahoo bars request failed for {symbol}: {msg}") from e
        if raw is None or raw.empty:
            return empty_bars()
        idx = raw.index
        if idx.tz is None:
            idx = idx.tz_localize("America/New_York")
        if interval.intraday:
            ts = [int(x.timestamp()) for x in idx]
        else:
            ts = [session_date_ts(x) for x in idx]
        df = pd.DataFrame(
            {"o": raw["Open"].values, "h": raw["High"].values, "l": raw["Low"].values,
             "c": raw["Close"].values, "v": raw["Volume"].values},
            index=pd.Index(ts, name="t"),
        )
        return finalize_bars(df)

    def quote(self, symbol: str) -> dict:
        try:
            fi = yf.Ticker(yahoo_symbol(symbol)).fast_info
            last = fi.get("lastPrice") if hasattr(fi, "get") else fi.last_price
            prev = fi.get("previousClose") if hasattr(fi, "get") else fi.previous_close
            data = {
                "price": _f(last),
                "previous_close": _f(prev),
                "open": _f(fi.get("open")),
                "day_high": _f(fi.get("dayHigh")),
                "day_low": _f(fi.get("dayLow")),
                "volume": _f(fi.get("lastVolume")),
                "market_cap": _f(fi.get("marketCap")),
                "year_high": _f(fi.get("yearHigh")),
                "year_low": _f(fi.get("yearLow")),
                "currency": fi.get("currency"),
                "source": self.name,
            }
        except Exception as e:
            raise ProviderError(f"Yahoo quote failed for {symbol}: {e}") from e
        if data["price"] is None:
            raise ProviderError(f"No quote available for {symbol}")
        return data

    def search(self, query: str, limit: int = 10) -> list[dict]:
        try:
            res = yf.Search(query, max_results=max(limit * 2, 10), news_count=0, lists_count=0)
            quotes = res.quotes or []
        except Exception as e:
            raise ProviderError(f"Yahoo search failed: {e}") from e
        out = []
        for q in quotes:
            qt = (q.get("quoteType") or "").upper()
            if qt not in {"EQUITY", "ETF", "INDEX", "MUTUALFUND"}:
                continue
            out.append({
                "symbol": q.get("symbol"),
                "name": q.get("longname") or q.get("shortname") or "",
                "exchange": q.get("exchDisp") or q.get("exchange") or "",
                "type": qt.lower(),
            })
            if len(out) >= limit:
                break
        return out

    def news(self, symbol: str, limit: int = 20) -> list[dict]:
        out = []
        try:
            items = yf.Ticker(yahoo_symbol(symbol)).get_news(count=limit) or []
        except Exception:
            items = []
        for it in items:
            c = it.get("content", it)
            url = (c.get("canonicalUrl") or {}).get("url") or (c.get("clickThroughUrl") or {}).get("url") or c.get("link")
            out.append({
                "headline": c.get("title"),
                "summary": c.get("summary") or c.get("description") or "",
                "source": (c.get("provider") or {}).get("displayName") or c.get("publisher"),
                "published_at": c.get("pubDate") or c.get("displayTime"),
                "url": url,
                "symbols": [symbol],
            })
        if not out:
            # the per-ticker news endpoint is frequently empty; search news is a usable fallback
            try:
                found = yf.Search(symbol, news_count=limit, max_results=1).news or []
            except Exception as e:
                raise ProviderError(f"Yahoo news failed for {symbol}: {e}") from e
            for n in found:
                ts = n.get("providerPublishTime")
                out.append({
                    "headline": n.get("title"),
                    "summary": "",
                    "source": n.get("publisher"),
                    "published_at": datetime.fromtimestamp(ts, UTC).isoformat() if ts else None,
                    "url": n.get("link"),
                    "symbols": n.get("relatedTickers") or [symbol],
                })
        return out[:limit]

def _f(x) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if pd.isna(v) else v
