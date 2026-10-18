"""Alpaca Market Data API v2 (bars, snapshots, news, screener) + Trading API asset list.

Feed handling: with ALPACA_DATA_FEED=sip (consolidated tape) the free plan rejects requests that
touch the most recent 15 minutes. Historical requests that get a 403 are retried with the end
clamped to 16 minutes ago; only if that also fails does the process fall back to IEX. Realtime
endpoints (snapshots) cannot be delayed, so they fall back to IEX on their own while bars keep
using SIP. Because IEX carries only a few percent of US volume, quotes combine the realtime IEX
last trade with the (delayed) consolidated daily bars for volume, day range and previous close.
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
        self._sip_realtime_blocked = False
        self._lock = threading.Lock()
        self._daily_cache: dict[str, tuple[float, dict]] = {}

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
        """GET with feed fallback. Returns (json, feed_used). `end_key=None` marks a realtime endpoint."""
        if self._feed == "sip":
            if end_key is None:
                return self._get_realtime(url, params)
            p = dict(params, feed="sip")
            if self._sip_recent_blocked:
                p[end_key] = to_rfc3339(min(_ts_from_param(p.get(end_key)), now_ts() - SIP_DELAY))
            try:
                return self._get(url, p), "sip"
            except AlpacaHTTPError as e:
                if e.status != 403:
                    raise
                if not self._sip_recent_blocked:
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

    def _get_realtime(self, url: str, params: dict[str, Any]) -> tuple[Any, str]:
        """Realtime endpoint on a SIP account: SIP if the plan allows it, otherwise IEX for this call
        (and later realtime calls) without affecting historical bars."""
        if not self._sip_realtime_blocked:
            try:
                return self._get(url, dict(params, feed="sip")), "sip"
            except AlpacaHTTPError as e:
                if e.status != 403:
                    raise
                log.info("Alpaca realtime SIP not permitted; using IEX for snapshots (bars stay on SIP)")
                self._sip_realtime_blocked = True
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
        return self.snapshots([symbol], strict=True)[symbol.upper()]

    def snapshots(self, symbols: list[str], strict: bool = False) -> dict[str, dict]:
        """Quotes for many symbols (batched). Unknown symbols are omitted unless `strict`."""
        syms = sorted({s.upper() for s in symbols if s})
        snaps: dict[str, dict] = {}
        feed = self._feed
        for chunk in _chunks(syms, 200):
            try:
                if len(chunk) == 1 and strict:
                    data, feed = self._get_with_feed(f"{self.data_url}/v2/stocks/{chunk[0]}/snapshot", {}, end_key=None)
                    snaps[chunk[0]] = data
                else:
                    data, feed = self._get_with_feed(f"{self.data_url}/v2/stocks/snapshots",
                                                     {"symbols": ",".join(chunk)}, end_key=None)
                    snaps.update({k: v for k, v in data.items() if v})
            except AlpacaHTTPError as e:
                raise ProviderError(f"Alpaca snapshot failed for {','.join(chunk[:5])}: {e}") from e
        consolidated = self.consolidated_daily(list(snaps)) if feed == "iex" and self._feed == "sip" else {}
        out: dict[str, dict] = {}
        for sym, snap in snaps.items():
            trade = snap.get("latestTrade") or {}
            q = snap.get("latestQuote") or {}
            day = dict(snap.get("dailyBar") or {})
            prev = dict(snap.get("prevDailyBar") or {})
            cons = consolidated.get(sym)
            volume_feed = feed
            if cons:
                today, before = cons
                # the snapshot's day bar belongs to the same session as the consolidated one
                if today and day.get("t", "")[:10] == today["t"][:10]:
                    day.update({k: today[k] for k in ("o", "h", "l", "v", "vw") if k in today})
                    volume_feed = "sip (delayed 16 min)"
                    if before:
                        prev = before
                elif today and day.get("t", "")[:10] > today["t"][:10]:
                    prev = today  # consolidated data has not reached today's session yet
            price = trade.get("p") or day.get("c")
            if price is None:
                if strict:
                    raise ProviderError(f"No quote available for {sym}")
                continue
            # keep the day range consistent with the realtime last price
            if day.get("h") is not None:
                day["h"] = max(day["h"], price)
            if day.get("l") is not None:
                day["l"] = min(day["l"], price)
            out[sym] = {
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
                "volume_feed": volume_feed,
            }
        if strict and syms and syms[0] not in out:
            raise ProviderError(f"No quote available for {syms[0]}")
        return out

    def consolidated_daily(self, symbols: list[str]) -> dict[str, tuple[dict | None, dict | None]]:
        """Latest two consolidated (SIP, delayed) daily bars per symbol: (latest, previous). Cached 60s."""
        now = time.time()
        out: dict[str, tuple[dict | None, dict | None]] = {}
        missing = []
        for s in symbols:
            hit = self._daily_cache.get(s)
            if hit and now - hit[0] < 60:
                out[s] = hit[1]["pair"]
            else:
                missing.append(s)
        if missing:
            try:
                bars = self.daily_bars_multi(missing, now_ts() - 10 * 86400, now_ts())
            except ProviderError as e:
                log.info("consolidated daily bars unavailable: %s", e)
                bars = {}
            for s in missing:
                rows = bars.get(s) or []
                pair = (rows[-1] if rows else None, rows[-2] if len(rows) > 1 else None)
                self._daily_cache[s] = (now, {"pair": pair})
                out[s] = pair
        return out

    def daily_bars_multi(self, symbols: list[str], start: int, end: int, timeframe: str = "1Day") -> dict[str, list[dict]]:
        """Raw daily bars for many symbols (adjusted, paginated). {symbol: [bar, ...]} oldest first."""
        out: dict[str, list[dict]] = {}
        for chunk in _chunks(sorted({s.upper() for s in symbols}), 100):
            params: dict[str, Any] = {
                "symbols": ",".join(chunk), "timeframe": timeframe, "start": to_rfc3339(start),
                "end": to_rfc3339(min(end, now_ts())), "limit": 10000, "adjustment": "all", "sort": "asc",
            }
            page_token = None
            for _ in range(1000):
                p = dict(params, page_token=page_token) if page_token else params
                data, _ = self._get_with_feed(f"{self.data_url}/v2/stocks/bars", p)
                for sym, rows in (data.get("bars") or {}).items():
                    out.setdefault(sym, []).extend(rows)
                page_token = data.get("next_page_token")
                if not page_token:
                    break
        return out

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


def _chunks(items: list[str], size: int) -> list[list[str]]:
    return [items[i:i + size] for i in range(0, len(items), size)]


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
