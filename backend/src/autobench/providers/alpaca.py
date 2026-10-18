"""Alpaca Market Data API v2 (bars, snapshots, news, screener) + Trading API asset list.

Feed handling: with ALPACA_DATA_FEED=sip (consolidated tape) the free plan rejects requests that
touch the most recent 15 minutes. On a 403 we retry with the end clamped to 16 minutes ago, and
if SIP is not permitted at all we fall back to the IEX feed for the rest of the process.
Intraday bars are restricted to the regular session (09:30-16:00 ET) to match Yahoo, and 1h bars
are built from 30-minute bars so they align to 09:30 rather than clock hours.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

import httpx
import pandas as pd

from ..config import Settings
from ..timeutil import ET, Interval, now_ts, to_rfc3339
from .base import ProviderError, empty_bars, finalize_bars, is_regular_session, session_date_ts

log = logging.getLogger(__name__)

_TIMEFRAME = {
    "1m": "1Min", "5m": "5Min", "15m": "15Min", "30m": "30Min", "1h": "30Min",
    "1d": "1Day", "1wk": "1Week", "1mo": "1Month",
}
SIP_DELAY = 16 * 60


class AlpacaProvider:
    name = "alpaca"

    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        self.settings = settings
        self.client = client or httpx.Client(timeout=30)
        self.headers = {
            "APCA-API-KEY-ID": settings.alpaca_api_key_id,
            "APCA-API-SECRET-KEY": settings.alpaca_api_secret_key,
        }
        self.data_url = settings.alpaca_data_base_url.rstrip("/")
        self.trading_url = settings.alpaca_trading_base_url.rstrip("/")
        self._feed = settings.alpaca_data_feed.lower()
        self._sip_recent_blocked = False
        self._lock = threading.Lock()

    @property
    def feed(self) -> str:
        return self._feed

    def max_history_seconds(self, interval: Interval) -> int | None:
        return None

    # --- HTTP ------------------------------------------------------------------------------

    def _get(self, url: str, params: dict[str, Any] | None = None) -> Any:
        for attempt in range(4):
            try:
                r = self.client.get(url, params=params, headers=self.headers)
            except httpx.HTTPError as e:
                if attempt == 3:
                    raise ProviderError(f"Alpaca request failed: {e}") from e
                time.sleep(0.5 * 2**attempt)
                continue
            if r.status_code == 429 and attempt < 3:
                time.sleep(float(r.headers.get("Retry-After", 1.0 * 2**attempt)))
                continue
            if r.status_code >= 400:
                raise AlpacaHTTPError(r.status_code, _error_message(r))
            return r.json()
        raise ProviderError("Alpaca request failed after retries")

    def _get_with_feed(self, url: str, params: dict[str, Any], end_key: str | None = "end") -> tuple[Any, str]:
        """GET with feed fallback. Returns (json, feed_used)."""
        feed = self._feed
        if feed == "sip":
            p = dict(params, feed="sip")
            if self._sip_recent_blocked and end_key:
                p[end_key] = to_rfc3339(min(_ts_from_param(p.get(end_key)), now_ts() - SIP_DELAY))
            try:
                return self._get(url, p), "sip"
            except AlpacaHTTPError as e:
                if e.status != 403:
                    raise
                if end_key and not self._sip_recent_blocked:
                    self._sip_recent_blocked = True
                    p[end_key] = to_rfc3339(min(_ts_from_param(p.get(end_key)), now_ts() - SIP_DELAY))
                    try:
                        return self._get(url, p), "sip"
                    except AlpacaHTTPError as e2:
                        if e2.status != 403:
                            raise
                log.warning("Alpaca SIP feed not permitted (%s); falling back to IEX", e)
                with self._lock:
                    self._feed = "iex"
        return self._get(url, dict(params, feed="iex")), "iex"

    # --- bars ------------------------------------------------------------------------------

    def fetch_bars(self, symbol: str, interval: Interval, start: int, end: int) -> pd.DataFrame:
        params: dict[str, Any] = {
            "timeframe": _TIMEFRAME[interval.name],
            "start": to_rfc3339(start),
            "end": to_rfc3339(min(end, now_ts())),
            "limit": 10000,
            "adjustment": "all",
            "sort": "asc",
        }
        url = f"{self.data_url}/v2/stocks/{symbol.upper()}/bars"
        rows: list[dict] = []
        page_token = None
        for _ in range(500):  # hard stop on runaway pagination
            p = dict(params, page_token=page_token) if page_token else params
            data, _feed = self._get_with_feed(url, p)
            rows.extend(data.get("bars") or [])
            page_token = data.get("next_page_token")
            if not page_token:
                break
        if not rows:
            return empty_bars()
        stamps = pd.to_datetime([b["t"] for b in rows], utc=True)
        df = pd.DataFrame(
            {"o": [b["o"] for b in rows], "h": [b["h"] for b in rows], "l": [b["l"] for b in rows],
             "c": [b["c"] for b in rows], "v": [b["v"] for b in rows]},
        )
        if interval.intraday:
            mask = [is_regular_session(ts) for ts in stamps]
            df = df[mask]
            stamps = stamps[mask]
            df.index = pd.Index([int(ts.timestamp()) for ts in stamps], name="t")
            if interval.name == "1h":
                df = _resample_session_hourly(df)
        else:
            df.index = pd.Index([session_date_ts(ts) for ts in stamps], name="t")
        return finalize_bars(df)

    # --- quotes ----------------------------------------------------------------------------

    def quote(self, symbol: str) -> dict:
        url = f"{self.data_url}/v2/stocks/{symbol.upper()}/snapshot"
        try:
            snap, feed = self._get_with_feed(url, {}, end_key=None)
        except AlpacaHTTPError as e:
            raise ProviderError(f"Alpaca snapshot failed for {symbol}: {e}") from e
        trade = snap.get("latestTrade") or {}
        q = snap.get("latestQuote") or {}
        day = snap.get("dailyBar") or {}
        prev = snap.get("prevDailyBar") or {}
        price = trade.get("p") or day.get("c")
        if price is None:
            raise ProviderError(f"No quote available for {symbol}")
        return {
            "price": float(price),
            "previous_close": prev.get("c"),
            "open": day.get("o"),
            "day_high": day.get("h"),
            "day_low": day.get("l"),
            "volume": day.get("v"),
            "vwap": day.get("vw"),
            "bid": q.get("bp"),
            "ask": q.get("ap"),
            "last_trade_time": trade.get("t"),
            "source": self.name,
            "feed": feed,
        }

    # --- assets / news / screener ----------------------------------------------------------

    def list_assets(self) -> list[dict]:
        data = self._get(f"{self.trading_url}/v2/assets", {"status": "active", "asset_class": "us_equity"})
        return [
            {
                "symbol": a["symbol"],
                "name": a.get("name") or "",
                "exchange": a.get("exchange") or "",
                "asset_class": a.get("class") or "us_equity",
                "tradable": bool(a.get("tradable")),
            }
            for a in data
        ]

    def news(self, symbols: list[str] | None, limit: int = 20, start: int | None = None, end: int | None = None,
             include_content: bool = False) -> list[dict]:
        params: dict[str, Any] = {"limit": min(limit, 50), "sort": "desc", "include_content": str(include_content).lower()}
        if symbols:
            params["symbols"] = ",".join(s.upper() for s in symbols)
        if start:
            params["start"] = to_rfc3339(start)
        if end:
            params["end"] = to_rfc3339(end)
        out: list[dict] = []
        page_token = None
        while len(out) < limit:
            p = dict(params, page_token=page_token) if page_token else params
            data = self._get(f"{self.data_url}/v1beta1/news", p)
            for n in data.get("news") or []:
                item = {
                    "id": n.get("id"),
                    "headline": n.get("headline"),
                    "summary": n.get("summary") or "",
                    "source": n.get("source"),
                    "author": n.get("author"),
                    "published_at": n.get("created_at"),
                    "url": n.get("url"),
                    "symbols": n.get("symbols") or [],
                }
                if include_content:
                    item["content"] = n.get("content") or ""
                out.append(item)
            page_token = data.get("next_page_token")
            if not page_token:
                break
        return out[:limit]

    def movers(self, top: int = 10) -> dict:
        return self._get(f"{self.data_url}/v1beta1/screener/stocks/movers", {"top": top})

    def most_actives(self, top: int = 20, by: str = "volume") -> dict:
        return self._get(f"{self.data_url}/v1beta1/screener/stocks/most-actives", {"top": top, "by": by})


class AlpacaHTTPError(ProviderError):
    def __init__(self, status: int, message: str):
        super().__init__(f"Alpaca HTTP {status}: {message}")
        self.status = status


def _error_message(r: httpx.Response) -> str:
    try:
        body = r.json()
        return body.get("message") or str(body)
    except Exception:
        return r.text[:200]


def _ts_from_param(v: Any) -> int:
    from ..timeutil import parse_time

    return parse_time(v) or now_ts()


def _resample_session_hourly(df: pd.DataFrame) -> pd.DataFrame:
    """Aggregate 30-minute regular-session bars into hourly bars anchored at 09:30 ET."""
    if df.empty:
        return df
    et = pd.to_datetime(df.index, unit="s", utc=True).tz_convert(ET)
    minutes = et.hour * 60 + et.minute - (9 * 60 + 30)
    bucket_start = et - pd.to_timedelta(minutes % 60, unit="min")
    keys = [int(b.timestamp()) for b in bucket_start]
    g = df.groupby(keys)
    out = pd.DataFrame({"o": g["o"].first(), "h": g["h"].max(), "l": g["l"].min(), "c": g["c"].last(), "v": g["v"].sum()})
    out.index.name = "t"
    return out
