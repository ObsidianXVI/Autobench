"""Bars (with SQLite cache), quotes, symbol search and bar downsampling."""

from __future__ import annotations

import logging
import math
import threading
from collections import defaultdict
from dataclasses import dataclass

import numpy as np
import pandas as pd
from sqlalchemy import delete, insert, select

from .. import db
from ..db import AssetRow, BarCoverage, BarRow, session_scope
from ..providers import ProviderError, alpaca_provider, bar_provider_for, primary_provider, yahoo, yahoo_only_symbol
from ..providers.base import BAR_COLUMNS, empty_bars
from ..timeutil import Interval, format_ts, get_interval, now_ts, resolve_range

log = logging.getLogger(__name__)

_fetch_locks: dict[tuple[str, str], threading.Lock] = defaultdict(threading.Lock)

# A close that differs from the cached one by more than this means history was re-adjusted
# (split/dividend) and the cached series must be refetched.
_READJUST_TOLERANCE = 0.005


def normalize_symbol(symbol: str) -> str:
    s = symbol.strip().upper()
    if not s:
        raise ValueError("symbol is required")
    return s


def _ttl(interval: Interval) -> int:
    if interval.intraday:
        return max(60, interval.seconds)
    return 900 if interval.name == "1d" else 3600


@dataclass
class Bars:
    symbol: str
    interval: Interval
    df: pd.DataFrame
    source: str

    @property
    def close(self) -> pd.Series:
        return self.df["c"]

    def __len__(self) -> int:
        return len(self.df)


# --- cache ---------------------------------------------------------------------------------


def _load_cached(symbol: str, interval: str, start: int, end: int) -> pd.DataFrame:
    with session_scope() as s:
        rows = s.execute(
            select(BarRow.t, BarRow.o, BarRow.h, BarRow.l, BarRow.c, BarRow.v)
            .where(BarRow.symbol == symbol, BarRow.interval == interval, BarRow.t >= start, BarRow.t <= end)
            .order_by(BarRow.t)
        ).all()
    if not rows:
        return empty_bars()
    df = pd.DataFrame(rows, columns=["t", *BAR_COLUMNS]).set_index("t")
    return df.astype(float)


def _store(symbol: str, interval: str, df: pd.DataFrame) -> None:
    if df.empty:
        return
    records = [
        {"symbol": symbol, "interval": interval, "t": int(t), "o": r.o, "h": r.h, "l": r.l, "c": r.c, "v": r.v}
        for t, r in df.iterrows()
    ]
    with session_scope() as s:
        for i in range(0, len(records), 5000):
            s.execute(insert(BarRow).prefix_with("OR REPLACE"), records[i : i + 5000])


def _clear(symbol: str, interval: str) -> None:
    with session_scope() as s:
        s.execute(delete(BarRow).where(BarRow.symbol == symbol, BarRow.interval == interval))
        s.execute(delete(BarCoverage).where(BarCoverage.symbol == symbol, BarCoverage.interval == interval))


def _get_coverage(symbol: str, interval: str) -> BarCoverage | None:
    with session_scope() as s:
        return s.get(BarCoverage, (symbol, interval))


def _set_coverage(symbol: str, interval: str, start: int, end: int, source: str) -> None:
    with session_scope() as s:
        s.merge(BarCoverage(symbol=symbol, interval=interval, start=start, end=end, fetched_at=now_ts(), source=source))


def _fetch(provider, symbol: str, interval: Interval, start: int, end: int) -> pd.DataFrame:
    df = provider.fetch_bars(symbol, interval, start, end)
    if df.empty and provider.name == "alpaca" and not yahoo_only_symbol(symbol):
        # e.g. OTC names or funds Alpaca does not carry
        return empty_bars()
    return df


def get_bars(
    symbol: str,
    interval: str = "1d",
    start: str | int | None = None,
    end: str | int | None = None,
    lookback: str | None = None,
    use_cache: bool = True,
) -> Bars:
    symbol = normalize_symbol(symbol)
    iv = get_interval(interval)
    start_ts, end_ts = resolve_range(start, end, lookback, default_lookback="1y" if not iv.intraday else "5d")
    provider = bar_provider_for(symbol)
    limit = provider.max_history_seconds(iv)
    now = now_ts()
    if limit is not None:
        start_ts = max(start_ts, now - limit)
    end_ts = min(end_ts, now)

    if not use_cache:
        df = _fetch(provider, symbol, iv, start_ts, end_ts)
        return Bars(symbol, iv, df[(df.index >= start_ts) & (df.index <= end_ts)], provider.name)

    with _fetch_locks[(symbol, iv.name)]:
        cov = _get_coverage(symbol, iv.name)
        if cov is not None and cov.source != provider.name:
            _clear(symbol, iv.name)
            cov = None
        # Requests reaching up to "now" are served from cache while it is younger than the TTL.
        hit = cov is not None and cov.start <= start_ts and cov.end >= min(end_ts, now - _ttl(iv))
        if not hit:
            _refresh(provider, symbol, iv, start_ts, end_ts, cov)
    df = _load_cached(symbol, iv.name, start_ts, end_ts)
    return Bars(symbol, iv, df, provider.name)


def _refresh(provider, symbol: str, iv: Interval, start_ts: int, end_ts: int, cov: BarCoverage | None) -> None:
    overlap = 3 * iv.seconds + (0 if iv.intraday else 7 * 86400)
    if cov is not None and cov.start <= start_ts and cov.end >= start_ts:
        # Top up the tail, re-fetching a few bars so the last (possibly partial) bar is replaced.
        fetch_start = max(cov.start, cov.end - overlap)
        new = _fetch(provider, symbol, iv, fetch_start, end_ts)
        if _readjusted(symbol, iv.name, new):
            log.info("%s %s history re-adjusted (split/dividend); refetching", symbol, iv.name)
            _clear(symbol, iv.name)
            new = _fetch(provider, symbol, iv, cov.start, end_ts)
            _store(symbol, iv.name, new)
            _set_coverage(symbol, iv.name, cov.start, max(end_ts, cov.end), provider.name)
            return
        _store(symbol, iv.name, new)
        _set_coverage(symbol, iv.name, cov.start, max(end_ts, cov.end), provider.name)
        return
    if cov is not None and cov.start > start_ts and cov.end >= end_ts - _ttl(iv) and cov.start <= end_ts:
        # Extend the head only.
        new = _fetch(provider, symbol, iv, start_ts, cov.start + overlap)
        if _readjusted(symbol, iv.name, new):
            _clear(symbol, iv.name)
            new = _fetch(provider, symbol, iv, start_ts, end_ts)
            _store(symbol, iv.name, new)
            _set_coverage(symbol, iv.name, start_ts, end_ts, provider.name)
            return
        _store(symbol, iv.name, new)
        _set_coverage(symbol, iv.name, start_ts, cov.end, provider.name)
        return
    new = _fetch(provider, symbol, iv, start_ts, end_ts)
    if cov is not None and (cov.end < start_ts or cov.start > end_ts):
        _clear(symbol, iv.name)  # disjoint: keep the cache a single contiguous range
        cov_start, cov_end = start_ts, end_ts
    elif cov is not None:
        if _readjusted(symbol, iv.name, new):
            _clear(symbol, iv.name)
            cov_start, cov_end = start_ts, end_ts
        else:
            cov_start, cov_end = min(cov.start, start_ts), max(cov.end, end_ts)
    else:
        cov_start, cov_end = start_ts, end_ts
    _store(symbol, iv.name, new)
    _set_coverage(symbol, iv.name, cov_start, cov_end, provider.name)


def _readjusted(symbol: str, interval: str, new: pd.DataFrame) -> bool:
    if new.empty:
        return False
    old = _load_cached(symbol, interval, int(new.index[0]), int(new.index[-1]))
    common = old.index.intersection(new.index)
    if len(common) < 2:
        return False
    # Ignore the most recent bar: it may legitimately still be forming.
    common = common[:-1]
    a, b = old.loc[common, "c"].to_numpy(), new.loc[common, "c"].to_numpy()
    return bool(np.any(np.abs(a / b - 1) > _READJUST_TOLERANCE))


# --- presentation helpers ------------------------------------------------------------------


def aggregate_bars(df: pd.DataFrame, max_bars: int) -> tuple[pd.DataFrame, int]:
    """Merge consecutive bars so at most max_bars remain (OHLC-correct). Groups are aligned to
    the end so the most recent bar is never merged with partial data. Returns (df, group_size)."""
    n = len(df)
    if max_bars <= 0 or n <= max_bars:
        return df, 1
    k = math.ceil(n / max_bars)
    from_end = (n - 1 - np.arange(n)) // k  # 0 for the newest group
    keys = from_end.max() - from_end  # ascending in time
    g = df.groupby(keys, sort=True)
    out = pd.DataFrame({
        "o": g["o"].first(), "h": g["h"].max(), "l": g["l"].min(), "c": g["c"].last(), "v": g["v"].sum(),
    })
    out.index = pd.Index(g.apply(lambda x: x.index[0], include_groups=False).to_numpy(dtype="int64"), name="t")
    return out.sort_index(), k


def price_round(x: float) -> float:
    if x is None or not np.isfinite(x):
        return x
    ax = abs(x)
    return round(x, 2 if ax >= 10 else 3 if ax >= 1 else 4)


def bars_to_rows(df: pd.DataFrame, intraday: bool) -> list[list]:
    return [
        [format_ts(int(t), intraday), price_round(r.o), price_round(r.h), price_round(r.l), price_round(r.c), int(r.v)]
        for t, r in df.iterrows()
    ]


def bars_to_records(df: pd.DataFrame) -> list[dict]:
    """Full-precision records with epoch time for the web UI."""
    return [
        {"time": int(t), "open": r.o, "high": r.h, "low": r.l, "close": r.c, "volume": r.v}
        for t, r in df.iterrows()
    ]


# --- quotes --------------------------------------------------------------------------------


def get_quote(symbol: str) -> dict:
    symbol = normalize_symbol(symbol)

    def fetch():
        q = bar_provider_for(symbol).quote(symbol)
        price, prev = q.get("price"), q.get("previous_close")
        if price is not None and prev:
            q["change"] = round(price - prev, 4)
            q["change_pct"] = round((price / prev - 1) * 100, 3)
        q["symbol"] = symbol
        q["as_of"] = now_ts()
        return q

    return db.cached(f"quote:{symbol}", 15, fetch)


def get_quotes(symbols: list[str]) -> dict[str, dict]:
    """Quotes for many symbols: one batched Alpaca snapshot request for stocks/ETFs, per-symbol
    Yahoo for indices/futures/FX/crypto. Symbols without data are omitted."""
    syms = list(dict.fromkeys(normalize_symbol(s) for s in symbols if s and s.strip()))
    out: dict[str, dict] = {}
    pending = []
    for s in syms:
        hit = db.cache_get(f"quote:{s}")
        if hit is not None:
            out[s] = hit
        else:
            pending.append(s)
    a = alpaca_provider()
    batchable = [s for s in pending if a is not None and not yahoo_only_symbol(s)]
    if batchable:
        try:
            snaps = a.snapshots(batchable)
        except ProviderError as e:
            log.warning("batched quotes failed: %s", e)
            snaps = {}
        for s, q in snaps.items():
            price, prev = q.get("price"), q.get("previous_close")
            if price is not None and prev:
                q["change"] = round(price - prev, 4)
                q["change_pct"] = round((price / prev - 1) * 100, 3)
            q["symbol"] = s
            q["as_of"] = now_ts()
            db.cache_set(f"quote:{s}", q, 15)
            out[s] = q
    rest = [s for s in pending if s not in out]
    if rest:
        from concurrent.futures import ThreadPoolExecutor

        def one(s):
            try:
                return s, get_quote(s)
            except Exception:
                return s, None

        with ThreadPoolExecutor(min(8, len(rest))) as pool:
            for s, q in pool.map(one, rest):
                if q:
                    out[s] = q
    return {s: out[s] for s in syms if s in out}


# --- search --------------------------------------------------------------------------------

_ASSET_REFRESH_SECONDS = 86400


def _ensure_assets() -> bool:
    """Load Alpaca's asset directory into SQLite (daily). Returns False if Alpaca is unavailable."""
    a = alpaca_provider()
    if a is None:
        return False
    state = db.state_get("assets_refreshed") or {}
    if state.get("at", 0) > now_ts() - _ASSET_REFRESH_SECONDS:
        return True
    try:
        assets = a.list_assets()
    except ProviderError as e:
        log.warning("asset list refresh failed: %s", e)
        with session_scope() as s:
            return s.scalar(select(AssetRow.symbol).limit(1)) is not None
    with session_scope() as s:
        s.execute(delete(AssetRow))
        s.execute(insert(AssetRow), assets)
    db.state_set("assets_refreshed", {"at": now_ts(), "count": len(assets)})
    return True


def search_symbols(query: str, limit: int = 10) -> list[dict]:
    q = query.strip()
    if not q:
        return []
    if _ensure_assets():
        uq = q.upper()
        with session_scope() as s:
            rows = s.execute(
                select(AssetRow).where(
                    (AssetRow.symbol.like(f"{uq}%")) | (AssetRow.name.ilike(f"%{q}%"))
                ).limit(500)
            ).scalars().all()

        def rank(a: AssetRow) -> tuple:
            name = a.name.upper()
            return (
                a.symbol != uq,
                not a.symbol.startswith(uq),
                not any(w.startswith(uq) for w in name.split()),
                not a.tradable,
                len(a.symbol),
                a.symbol,
            )

        rows = sorted(rows, key=rank)[:limit]
        return [{"symbol": a.symbol, "name": a.name, "exchange": a.exchange, "type": "equity", "tradable": a.tradable}
                for a in rows]
    return yahoo().search(q, limit)


def provider_status() -> dict:
    p = primary_provider()
    out = {"bar_provider": p.name}
    if p.name == "alpaca":
        out["feed"] = p.feed
    return out
