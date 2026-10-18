"""The market universe: every US-listed common stock with descriptive data, fundamentals,
technicals and live quotes, for the screener, heatmaps, breadth and movers.

Layers, each refreshed on its own schedule and persisted under the data directory:
  listings      Nasdaq listings + S&P 500 membership (GICS) + SEC ticker/CIK map   daily
  fundamentals  SEC XBRL frames (TTM income, balance sheet, cash flow, dividends)  daily
  bars          ~300 daily bars per symbol (Alpaca multi-symbol, else per symbol)   10 min (market hours)
  quotes        Alpaca snapshots (live last price)                                 60 s, on demand

`table()` joins the layers and derives ratios. Everything degrades gracefully: a missing layer
leaves its columns empty instead of failing the screener."""

from __future__ import annotations

import logging
import pickle
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd

from .. import db
from ..config import get_settings
from ..providers import ProviderError, alpaca_provider, reference
from ..timeutil import ET, now_ts

log = logging.getLogger(__name__)

LISTINGS_TTL = 86400
FUNDAMENTALS_TTL = 86400
BARS_TTL_OPEN = 600
BARS_TTL_CLOSED = 6 * 3600
QUOTES_TTL = 60
HISTORY_DAYS = 300  # trading days kept per symbol (52 weeks + 1y beta + margin)

# Nasdaq's sector names -> GICS sectors, used when an industry has no S&P 500 member to vote.
_NASDAQ_TO_GICS = {
    "Technology": "Information Technology", "Telecommunications": "Communication Services",
    "Health Care": "Health Care", "Finance": "Financials", "Real Estate": "Real Estate",
    "Consumer Discretionary": "Consumer Discretionary", "Consumer Staples": "Consumer Staples",
    "Industrials": "Industrials", "Energy": "Energy", "Utilities": "Utilities",
    "Basic Materials": "Materials", "Miscellaneous": None,
}
GICS_SECTORS = ["Information Technology", "Health Care", "Financials", "Consumer Discretionary",
                "Communication Services", "Industrials", "Consumer Staples", "Energy", "Utilities",
                "Real Estate", "Materials"]


def _data_dir() -> Path:
    d = get_settings().db_path.parent / "universe"
    d.mkdir(parents=True, exist_ok=True)
    return d


class _Layer:
    """A persisted DataFrame/object with a refresh timestamp."""

    def __init__(self, name: str):
        self.name = name
        self.value: Any = None
        self.updated: float = 0
        self.error: str | None = None
        self._loaded_from: Path | None = None

    @property
    def path(self) -> Path:
        return _data_dir() / f"{self.name}.pkl"

    def load(self) -> None:
        # the data directory follows the DB path (tests use a fresh one each)
        if self._loaded_from == self.path:
            return
        self._loaded_from = self.path
        self.value, self.updated = None, 0
        if self.path.exists():
            try:
                with self.path.open("rb") as f:
                    self.value, self.updated = pickle.load(f)
            except Exception as e:  # corrupt/old format: rebuild
                log.warning("discarding universe layer %s: %s", self.name, e)

    def store(self, value: Any) -> None:
        self.value, self.updated, self.error = value, time.time(), None
        tmp = self.path.with_suffix(".tmp")
        with tmp.open("wb") as f:
            pickle.dump((value, self.updated), f, protocol=pickle.HIGHEST_PROTOCOL)
        tmp.replace(self.path)

    def age(self) -> float:
        self.load()
        return time.time() - self.updated if self.updated else float("inf")


class Universe:
    def __init__(self):
        self.listings = _Layer("listings")
        self.fundamentals = _Layer("fundamentals")
        self.bars = _Layer("bars")
        self.quotes = _Layer("quotes")
        self._lock = threading.Lock()
        self._refreshing: set[str] = set()
        self._progress: dict[str, str] = {}
        self._table: tuple[tuple, pd.DataFrame] | None = None

    # ---------------------------------------------------------------- refresh orchestration

    def layers(self) -> dict[str, _Layer]:
        return {"listings": self.listings, "fundamentals": self.fundamentals, "bars": self.bars, "quotes": self.quotes}

    def stale(self) -> list[str]:
        ttl = {"listings": LISTINGS_TTL, "fundamentals": FUNDAMENTALS_TTL, "bars": _bars_ttl(), "quotes": QUOTES_TTL}
        return [k for k, layer in self.layers().items() if layer.age() > ttl[k]]

    def ensure(self, parts: list[str] | None = None, wait: bool = False, timeout: float = 600) -> None:
        """Refresh stale layers in the background (or wait for them)."""
        parts = parts or self.stale()
        order = [p for p in ("listings", "fundamentals", "bars", "quotes") if p in parts]
        if not order:
            return
        t = threading.Thread(target=self._refresh_chain, args=(order,), daemon=True)
        t.start()
        if wait:
            t.join(timeout)

    def _refresh_chain(self, parts: list[str]) -> None:
        for p in parts:
            with self._lock:
                if p in self._refreshing:
                    continue
                self._refreshing.add(p)
            try:
                started = time.time()
                getattr(self, f"_refresh_{p}")()
                log.info("universe %s refreshed in %.1fs", p, time.time() - started)
            except Exception as e:
                log.exception("universe %s refresh failed", p)
                self.layers()[p].error = str(e)
            finally:
                with self._lock:
                    self._refreshing.discard(p)
                    self._progress.pop(p, None)

    def status(self) -> dict:
        out = {}
        for k, layer in self.layers().items():
            layer.load()
            out[k] = {
                "updated": datetime.fromtimestamp(layer.updated).isoformat() if layer.updated else None,
                "age_seconds": None if not layer.updated else round(time.time() - layer.updated),
                "refreshing": k in self._refreshing, "progress": self._progress.get(k), "error": layer.error,
                "rows": None if layer.value is None else _rows(layer.value),
            }
        return out

    # ---------------------------------------------------------------- listings

    def _refresh_listings(self) -> None:
        if _fake():
            self.listings.store(_fake_listings())
            return
        self._progress["listings"] = "Nasdaq listings"
        df = reference.nasdaq_listings()
        try:
            sp = reference.sp500_members()
        except ProviderError as e:
            log.warning("S&P 500 list unavailable: %s", e)
            sp = pd.DataFrame(columns=["name", "gics_sector", "gics_industry", "cik"])
        try:
            sec = reference.sec_tickers()
        except ProviderError as e:
            log.warning("SEC tickers unavailable: %s", e)
            sec = pd.DataFrame(columns=["cik", "name", "exchange"])
        # S&P members missing from the Nasdaq file (rare) still belong in the universe
        for sym in sp.index.difference(df.index):
            df.loc[sym, "name"] = sp.loc[sym, "name"]
        df["sp500"] = df.index.isin(sp.index)
        df["gics_sector"] = sp["gics_sector"].reindex(df.index)
        df["gics_industry"] = sp["gics_industry"].reindex(df.index)
        sec_key = df.index.str.replace(".", "-", regex=False)
        df["cik"] = pd.to_numeric(sec["cik"].reindex(sec_key).to_numpy(), errors="coerce")
        df["exchange"] = sec["exchange"].reindex(sec_key).to_numpy()
        df["cik"] = df["cik"].fillna(sp["cik"].reindex(df.index))
        df["sector"], df["industry"] = _unify_taxonomy(df)
        self.listings.store(df)

    # ---------------------------------------------------------------- fundamentals

    def _refresh_fundamentals(self) -> None:
        listings = self.listings.value
        if listings is None:
            self._refresh_listings()
            listings = self.listings.value
        if _fake():
            self.fundamentals.store(_fake_fundamentals(listings))
            return
        self.fundamentals.store(build_fundamentals(listings, lambda msg: self._progress.__setitem__("fundamentals", msg)))

    # ---------------------------------------------------------------- bars

    def _refresh_bars(self) -> None:
        listings = self.listings.value
        if listings is None:
            self._refresh_listings()
            listings = self.listings.value
        symbols = sorted(set(listings.index) | {"SPY"})
        prev = self.bars.value
        a = alpaca_provider()
        if a is not None:
            incremental = prev is not None and self.bars.age() < 5 * 86400
            start = now_ts() - (10 if incremental else int(HISTORY_DAYS * 7 / 5) + 10) * 86400
            frames = self._alpaca_bars(a, symbols, start)
        else:
            # without Alpaca: per-symbol history for the larger names only (Yahoo is slow per symbol)
            ranked = listings.sort_values("listing_market_cap", ascending=False)
            keep = list(dict.fromkeys(["SPY", *listings.index[listings["sp500"]], *ranked.index[:400]]))
            frames = self._provider_bars(keep)
            incremental = False
        fresh = _to_matrix(frames)
        if incremental and prev is not None:
            fresh = _merge_matrix(prev, fresh)
        self.bars.store(_trim_matrix(fresh, HISTORY_DAYS))
        self._table = None

    def _alpaca_bars(self, a, symbols: list[str], start: int) -> dict[str, list[dict]]:
        chunks = [symbols[i:i + 100] for i in range(0, len(symbols), 100)]
        out: dict[str, list[dict]] = {}
        done = 0

        def fetch(chunk):
            try:
                return a.daily_bars_multi(chunk, start, now_ts())
            except ProviderError as e:
                log.warning("bars chunk failed (%s…): %s", chunk[0], e)
                return {}

        with ThreadPoolExecutor(4) as pool:
            for res in pool.map(fetch, chunks):
                out.update(res)
                done += 1
                self._progress["bars"] = f"{done}/{len(chunks)} batches"
        return out

    def _provider_bars(self, symbols: list[str]) -> dict[str, list[dict]]:
        from .market import get_bars

        def fetch(sym):
            try:
                df = get_bars(sym, "1d", lookback="430d").df
            except Exception:
                return sym, []
            return sym, [{"t": int(t), "o": r.o, "h": r.h, "l": r.l, "c": r.c, "v": r.v} for t, r in df.iterrows()]

        out = {}
        with ThreadPoolExecutor(8) as pool:
            for i, (sym, rows) in enumerate(pool.map(fetch, symbols)):
                if rows:
                    out[sym] = rows
                self._progress["bars"] = f"{i + 1}/{len(symbols)} symbols"
        return out

    # ---------------------------------------------------------------- quotes

    def _refresh_quotes(self) -> None:
        listings = self.listings.value
        a = alpaca_provider()
        if listings is None or a is None:
            self.quotes.store(pd.DataFrame(columns=["price", "q_open", "q_high", "q_low", "q_prev_close"], dtype=float))
            return
        snaps = {}
        symbols = sorted(set(listings.index) | {"SPY"})
        chunks = [symbols[i:i + 200] for i in range(0, len(symbols), 200)]

        def fetch(chunk):
            try:
                return a.snapshots(chunk, consolidated=False)
            except ProviderError as e:
                log.warning("snapshot chunk failed: %s", e)
                return {}

        with ThreadPoolExecutor(4) as pool:
            for res in pool.map(fetch, chunks):
                snaps.update(res)
        df = pd.DataFrame.from_dict({s: {"price": q["price"], "q_open": q.get("open"), "q_high": q.get("day_high"),
                                         "q_low": q.get("day_low"), "q_prev_close": q.get("previous_close"),
                                         "q_time": q.get("last_trade_time")}
                                     for s, q in snaps.items()}, orient="index")
        self.quotes.store(df)
        self._table = None

    # ---------------------------------------------------------------- the joined table

    def table(self) -> pd.DataFrame:
        """All layers joined, with derived ratios. Cached until a layer changes."""
        from .scheduler import touch_universe

        touch_universe()
        for layer in self.layers().values():
            layer.load()
        key = tuple(layer.updated for layer in self.layers().values())
        if self._table and self._table[0] == key:
            return self._table[1]
        listings = self.listings.value
        if listings is None:
            return pd.DataFrame()
        df = listings.copy()
        tech = technicals(self.bars.value) if self.bars.value is not None else pd.DataFrame()
        df = df.join(tech, how="left")
        if self.fundamentals.value is not None:
            df = df.join(self.fundamentals.value, how="left", rsuffix="_f")
        quotes = self.quotes.value
        if quotes is not None and len(quotes):
            df = df.join(quotes, how="left")
        df = _derive(df)
        self._table = (key, df)
        return df


def _rows(v: Any) -> int | None:
    if isinstance(v, pd.DataFrame):
        return len(v)
    if isinstance(v, dict) and "close" in v:
        return v["close"].shape[1]
    return None


def _fake() -> bool:
    return get_settings().bar_provider == "fake"


def _bars_ttl() -> int:
    now = datetime.now(ET)
    open_ = now.weekday() < 5 and (9, 30) <= (now.hour, now.minute) <= (16, 20)
    return BARS_TTL_OPEN if open_ else BARS_TTL_CLOSED


def _unify_taxonomy(df: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """GICS sector for every stock: S&P 500 members keep theirs; other stocks take the GICS sector
    most common among members sharing their Nasdaq industry, else a sector-level mapping."""
    votes: dict[str, Counter] = {}
    for ind, gics in zip(df["industry"], df["gics_sector"]):
        if isinstance(ind, str) and isinstance(gics, str):
            votes.setdefault(ind, Counter())[gics] += 1
    sector, industry = [], []
    for nas_sector, ind, gics, gind in zip(df["sector"], df["industry"], df["gics_sector"], df["gics_industry"]):
        if isinstance(gics, str):
            sector.append(gics)
        elif isinstance(ind, str) and ind in votes:
            sector.append(votes[ind].most_common(1)[0][0])
        else:
            sector.append(_NASDAQ_TO_GICS.get(nas_sector) if isinstance(nas_sector, str) else None)
        industry.append(gind if isinstance(gind, str) else ind if isinstance(ind, str) else None)
    return pd.Series(sector, index=df.index), pd.Series(industry, index=df.index)


# --- bar matrices --------------------------------------------------------------------------


def _to_matrix(frames: dict[str, list[dict]]) -> dict | None:
    """{symbol: [bar...]} -> {"dates": DatetimeIndex, "symbols": [...], "open"/...: float32 [dates x symbols]}."""
    if not frames:
        return None
    series = {}
    for sym, rows in frames.items():
        if not rows:
            continue
        t = rows[0]["t"]
        if isinstance(t, str):
            idx = pd.to_datetime([r["t"][:10] for r in rows])
        else:
            idx = pd.to_datetime([r["t"] for r in rows], unit="s").normalize()
        series[sym] = pd.DataFrame({k: [r[k] for r in rows] for k in "ohlcv"}, index=idx)
    if not series:
        return None
    dates = sorted(set().union(*[s.index for s in series.values()]))
    dates = pd.DatetimeIndex(dates)
    syms = sorted(series)
    out = {"dates": dates, "symbols": syms}
    for k, name in zip("ohlcv", ["open", "high", "low", "close", "volume"]):
        m = np.full((len(dates), len(syms)), np.nan, dtype=np.float64)
        for j, s in enumerate(syms):
            col = series[s][k]
            col = col[~col.index.duplicated(keep="last")]
            m[dates.get_indexer(col.index), j] = col.to_numpy(dtype=float)
        out[name] = m
    return out


def _merge_matrix(old: dict, new: dict | None) -> dict:
    if new is None:
        return old
    frames = {}
    for src in (old, new):  # new overwrites old on overlapping dates
        for j, s in enumerate(src["symbols"]):
            for k in ("open", "high", "low", "close", "volume"):
                frames.setdefault(s, {}).setdefault(k, {}).update(
                    {d: v for d, v in zip(src["dates"], src[k][:, j]) if not np.isnan(v)})
    dates = pd.DatetimeIndex(sorted(set(old["dates"]) | set(new["dates"])))
    syms = sorted(frames)
    out = {"dates": dates, "symbols": syms}
    pos = {d: i for i, d in enumerate(dates)}
    for k in ("open", "high", "low", "close", "volume"):
        m = np.full((len(dates), len(syms)), np.nan)
        for j, s in enumerate(syms):
            for d, v in frames[s].get(k, {}).items():
                m[pos[d], j] = v
        out[k] = m
    return out


def _trim_matrix(m: dict | None, n: int) -> dict | None:
    if m is None or len(m["dates"]) <= n:
        return m
    out = {"dates": m["dates"][-n:], "symbols": m["symbols"]}
    for k in ("open", "high", "low", "close", "volume"):
        out[k] = m[k][-n:]
    return out


# --- technicals (vectorized over all symbols) ----------------------------------------------


def _rma(x: np.ndarray, n: int) -> np.ndarray:
    """Column-wise Wilder moving average seeded with the SMA of each column's first n values."""
    rows, cols = x.shape
    out = np.full_like(x, np.nan)
    valid = ~np.isnan(x)
    first = np.where(valid.any(axis=0), valid.argmax(axis=0), rows)
    seed_row = first + n - 1
    a = 1.0 / n
    prev = np.full(cols, np.nan)
    for i in range(rows):
        seed = seed_row == i
        if seed.any():
            for j in np.flatnonzero(seed):
                prev[j] = np.nanmean(x[first[j]:i + 1, j]) if i + 1 - first[j] >= n else np.nan
        upd = (i > seed_row) & ~np.isnan(x[i])
        prev = np.where(upd, prev * (1 - a) + x[i] * a, prev)
        out[i] = np.where(i >= seed_row, prev, np.nan)
    return out


def technicals(m: dict) -> pd.DataFrame:
    """Per-symbol technical fields from the bar matrix (last row = latest session)."""
    syms = m["symbols"]
    close = pd.DataFrame(m["close"], index=m["dates"], columns=syms).ffill()
    high = pd.DataFrame(m["high"], index=m["dates"], columns=syms)
    low = pd.DataFrame(m["low"], index=m["dates"], columns=syms)
    opn = pd.DataFrame(m["open"], index=m["dates"], columns=syms)
    vol = pd.DataFrame(m["volume"], index=m["dates"], columns=syms)
    n = len(close)
    last = close.iloc[-1]
    out = pd.DataFrame(index=syms)
    out["bar_close"] = last
    out["bar_date"] = [m["dates"][-1].date().isoformat()] * len(syms)
    out["bar_prev_close"] = close.iloc[-2] if n > 1 else np.nan
    out["bar_open"] = opn.iloc[-1]
    out["bar_high"] = high.iloc[-1]
    out["bar_low"] = low.iloc[-1]
    out["volume"] = vol.iloc[-1]

    def perf(k):
        return (last / close.iloc[-1 - k] - 1) * 100 if n > k else np.nan

    out["perf_1w"], out["perf_1m"], out["perf_3m"] = perf(5), perf(21), perf(63)
    out["perf_6m"], out["perf_1y"] = perf(126), perf(252)
    year_start = close[close.index.year < close.index[-1].year]
    out["perf_ytd"] = (last / year_start.iloc[-1] - 1) * 100 if len(year_start) else np.nan
    for k in (20, 50, 200):
        ma = close.rolling(k, min_periods=k).mean().iloc[-1]
        out[f"sma{k}"] = ma
        out[f"sma{k}_pct"] = (last / ma - 1) * 100
    out["sma50_vs_sma200"] = (out["sma50"] / out["sma200"] - 1) * 100
    win = min(n, 252)
    hi52 = high.iloc[-win:].max()
    lo52 = low.iloc[-win:].min()
    out["high_52w"], out["low_52w"] = hi52, lo52
    out["from_high_52w"] = (last / hi52 - 1) * 100
    out["from_low_52w"] = (last / lo52 - 1) * 100
    d = close.diff().to_numpy()
    up = _rma(np.clip(d, 0, None), 14)[-1]
    dn = _rma(np.clip(-d, 0, None), 14)[-1]
    with np.errstate(divide="ignore", invalid="ignore"):
        rsi = np.where(dn == 0, np.where(up == 0, 50.0, 100.0), 100 - 100 / (1 + up / dn))
    out["rsi14"] = rsi
    prev_c = close.shift(1)
    tr = np.fmax(high - low, np.fmax((high - prev_c).abs(), (low - prev_c).abs()))
    out["atr14"] = _rma(tr.to_numpy(), 14)[-1]
    out["atr_pct"] = out["atr14"] / last * 100
    rets = close.pct_change(fill_method=None)
    out["volatility_1m"] = rets.iloc[-21:].std() * np.sqrt(252) * 100
    out["range_1w"] = ((high - low) / close).iloc[-5:].mean() * 100
    out["avg_volume"] = vol.iloc[-64:-1].mean()  # 3 months, excluding the current session
    out["avg_volume_20"] = vol.iloc[-21:-1].mean()
    out["rel_volume"] = out["volume"] / out["avg_volume_20"]
    out["dollar_volume"] = out["avg_volume"] * last
    if "SPY" in close.columns:
        spy = rets["SPY"].iloc[-252:]
        r = rets.iloc[-252:]
        cov = r.sub(r.mean()).mul(spy - spy.mean(), axis=0).sum() / (len(r) - 1)
        out["beta"] = cov / spy.var()
    streak = np.sign(close.diff()).iloc[-15:]
    out["streak"] = _streak(streak.to_numpy())
    out["history_days"] = close.notna().sum()
    return out


def _streak(signs: np.ndarray) -> np.ndarray:
    """Consecutive up (+) / down (-) closes ending at the last row, per column."""
    last = signs[-1]
    count = np.zeros(signs.shape[1])
    alive = np.ones(signs.shape[1], dtype=bool)
    for row in signs[::-1]:
        alive &= (row == last) & (last != 0)
        count += alive
    return count * np.sign(np.nan_to_num(last))


# --- derived fields ------------------------------------------------------------------------


_TEXT_COLUMNS = {"name", "sector", "industry", "country", "exchange", "gics_sector", "gics_industry", "bar_date",
                 "q_time", "fiscal_period_end", "index"}


def _derive(df: pd.DataFrame) -> pd.DataFrame:
    # layers may be partially built: make every non-text column numeric (None -> NaN)
    for c in df.columns:
        if c not in _TEXT_COLUMNS and c != "sp500" and df[c].dtype == object:
            df[c] = pd.to_numeric(df[c], errors="coerce")

    def col(name):
        return df[name] if name in df.columns else pd.Series(np.nan, index=df.index)

    price = col("price").fillna(col("bar_close")).fillna(col("listing_price"))
    df["price"] = price
    prev = col("bar_prev_close")
    # if the bar layer already contains today's session, the previous close is one bar back;
    # quotes (when present) carry the official previous close
    prev = col("q_prev_close").fillna(prev)
    df["prev_close"] = prev
    df["change_pct"] = ((price / prev - 1) * 100).fillna(col("listing_change_pct"))
    df["change"] = price - prev
    opn = col("q_open").fillna(col("bar_open"))
    df["gap_pct"] = (opn / prev - 1) * 100
    df["change_from_open"] = (price / opn - 1) * 100
    lp = col("listing_price")
    df["market_cap"] = (col("listing_market_cap") * price / lp).where(lp > 0).fillna(col("shares") * price)
    # per-share figures reported for a different share class (e.g. BRK's Class A EPS on BRK.B)
    # show up as EPS above the share price; drop them rather than report a P/E below 1
    for per_share in ("eps_ttm", "dps_ttm"):
        if per_share in df.columns:
            df[per_share] = df[per_share].where(~(df[per_share].abs() > price))
    with np.errstate(divide="ignore", invalid="ignore"):
        mc = df["market_cap"]
        eps = col("eps_ttm")
        df["pe"] = (price / eps).where(eps > 0)
        df["ps"] = (mc / col("revenue_ttm")).where(col("revenue_ttm") > 0)
        df["pb"] = (mc / col("equity")).where(col("equity") > 0)
        df["pfcf"] = (mc / col("fcf")).where(col("fcf") > 0)
        debt = col("debt").fillna(0)
        cash = col("cash").fillna(0)
        df["ev"] = mc + debt - cash
        df["ev_sales"] = (df["ev"] / col("revenue_ttm")).where(col("revenue_ttm") > 0)
        df["div_yield"] = (col("dps_ttm") / price * 100).where(col("dps_ttm") > 0)
        df["payout"] = (col("dps_ttm") / eps * 100).where((eps > 0) & (col("dps_ttm") > 0))
        df["from_high_52w"] = (price / col("high_52w").clip(lower=price) - 1) * 100
        df["from_low_52w"] = (price / col("low_52w").clip(upper=price) - 1) * 100
        for k in (20, 50, 200):
            if f"sma{k}" in df.columns:
                df[f"sma{k}_pct"] = (price / df[f"sma{k}"] - 1) * 100
        df["perf_today"] = df["change_pct"]
    df["index"] = np.where(col("sp500").fillna(False).astype(bool), "S&P 500", "")
    return df


# --- SEC fundamentals ----------------------------------------------------------------------

_DURATION = {
    "revenue": [("us-gaap", "Revenues", "USD"), ("us-gaap", "RevenueFromContractWithCustomerExcludingAssessedTax", "USD")],
    "net_income": [("us-gaap", "NetIncomeLoss", "USD")],
    "eps": [("us-gaap", "EarningsPerShareDiluted", "USD-per-shares"), ("us-gaap", "EarningsPerShareBasic", "USD-per-shares"),
            ("us-gaap", "EarningsPerShareBasicAndDiluted", "USD-per-shares")],
    "operating_income": [("us-gaap", "OperatingIncomeLoss", "USD")],
    "gross_profit": [("us-gaap", "GrossProfit", "USD")],
    "dps": [("us-gaap", "CommonStockDividendsPerShareDeclared", "USD-per-shares"),
            ("us-gaap", "CommonStockDividendsPerShareCashPaid", "USD-per-shares")],
}
_INSTANT = {
    "assets": ("us-gaap", "Assets", "USD"),
    "liabilities": ("us-gaap", "Liabilities", "USD"),
    "equity": ("us-gaap", "StockholdersEquity", "USD"),
    "current_assets": ("us-gaap", "AssetsCurrent", "USD"),
    "current_liabilities": ("us-gaap", "LiabilitiesCurrent", "USD"),
    "cash": ("us-gaap", "CashAndCashEquivalentsAtCarryingValue", "USD"),
    "lt_debt": ("us-gaap", "LongTermDebtNoncurrent", "USD"),
    "lt_debt_total": ("us-gaap", "LongTermDebt", "USD"),
    "shares": ("dei", "EntityCommonStockSharesOutstanding", "shares"),
}
_ANNUAL_CASH = {
    "ocf": ("us-gaap", "NetCashProvidedByUsedInOperatingActivities", "USD"),
    "capex": ("us-gaap", "PaymentsToAcquirePropertyPlantAndEquipment", "USD"),
}


def _frame(tax: str, concept: str, unit: str, period: str, today: date) -> list[dict]:
    """A cached SEC frame. Closed periods change rarely (late filers), so they are cached longer."""
    year = int(period[2:6])
    recent = year >= today.year - 1
    return db.cached(f"secframe:{tax}:{concept}:{unit}:{period}", 86400 if recent else 7 * 86400,
                     lambda: reference.sec_frame(tax, concept, unit, period)) or []


def _periods(today: date) -> tuple[list[str], list[str], list[str]]:
    y = today.year
    q_now = (today.month - 1) // 3 + 1
    annual = [f"CY{y - k}" for k in (1, 2, 3)]
    quarters = []  # back to Y-3 Q2 so TTM-over-TTM growth works for any fiscal year end
    for yy in (y - 3, y - 2, y - 1, y):
        for q in (1, 2, 3, 4):
            if (y - 3, 2) <= (yy, q) < (y, q_now):
                quarters.append(f"CY{yy}Q{q}")
    instants = [f"CY{yy}Q{q}I" for yy, q in [(y, q_now - 1), (y, q_now - 2), (y - 1, 4), (y - 1, 3)] if q > 0]
    return annual, quarters, instants


def build_fundamentals(listings: pd.DataFrame, progress: Callable[[str], None] = lambda m: None,
                       today: date | None = None) -> pd.DataFrame:
    """Cross-sectional fundamentals for every SEC filer, indexed by ticker (via the listings' CIKs,
    so every share class of a company gets the company's figures)."""
    today = today or date.today()
    annual, quarters, instants = _periods(today)
    per_cik: dict[int, dict] = {}
    total = sum(len(v) for v in _DURATION.values()) * (len(annual) + len(quarters)) + len(_INSTANT) * len(instants) + 2 * 2
    done = 0

    def tick(label):
        nonlocal done
        done += 1
        progress(f"SEC frames {done}/{total} ({label})")

    # duration concepts: TTM = latest annual + quarters reported since − the same quarters a year earlier
    for field, alternatives in _DURATION.items():
        best: dict[int, dict] = {}
        for tax, concept, unit in alternatives:
            ann: dict[int, list] = {}
            qtr: dict[int, list] = {}
            for p in annual:
                for r in _frame(tax, concept, unit, p, today):
                    ann.setdefault(r["cik"], []).append((pd.Timestamp(r["end"]), float(r["val"])))
                tick(f"{concept} {p}")
            for p in quarters:
                for r in _frame(tax, concept, unit, p, today):
                    qtr.setdefault(r["cik"], []).append((pd.Timestamp(r["end"]), float(r["val"])))
                tick(f"{concept} {p}")
            for cik in set(ann) | set(qtr):
                res = _ttm(sorted(ann.get(cik, [])), sorted(qtr.get(cik, [])))
                # prefer a concept that yields a full TTM, then the most recent one
                if res and (cik not in best or (res.get("ttm") is not None, res["end"]) >
                            (best[cik].get("ttm") is not None, best[cik]["end"])):
                    best[cik] = res
        for cik, res in best.items():
            d = per_cik.setdefault(cik, {})
            d[f"{field}_ttm"] = res.get("ttm")
            d[f"{field}_ttm_prev"] = res.get("ttm_prev")
            d[f"{field}_q"] = res.get("q")
            d[f"{field}_q_prev"] = res.get("q_prev")
            d[f"{field}_fy"] = res.get("fy")
            d[f"{field}_fy_prev"] = res.get("fy_prev")
            if field == "revenue":
                d["fiscal_period_end"] = res["end"].date().isoformat()
    for field, (tax, concept, unit) in _INSTANT.items():
        latest: dict[int, tuple] = {}
        for p in instants:
            for r in _frame(tax, concept, unit, p, today):
                end = pd.Timestamp(r["end"])
                if r["cik"] not in latest or end > latest[r["cik"]][0]:
                    latest[r["cik"]] = (end, float(r["val"]))
            tick(f"{concept} {p}")
        for cik, (_, v) in latest.items():
            per_cik.setdefault(cik, {})[field] = v
    for field, (tax, concept, unit) in _ANNUAL_CASH.items():
        latest = {}
        for p in annual[:2]:
            for r in _frame(tax, concept, unit, p, today):
                end = pd.Timestamp(r["end"])
                if r["cik"] not in latest or end > latest[r["cik"]][0]:
                    latest[r["cik"]] = (end, float(r["val"]))
            tick(f"{concept} {p}")
        for cik, (_, v) in latest.items():
            per_cik.setdefault(cik, {})[field] = v

    f = pd.DataFrame.from_dict(per_cik, orient="index")
    f.index.name = "cik"
    links = listings[["cik"]].dropna().reset_index()
    links["cik"] = links["cik"].astype(int)
    f = links.merge(f, left_on="cik", right_index=True, how="inner").set_index("symbol").drop(columns="cik")
    return _fundamental_ratios(f)


def _ttm(ann: list[tuple], qtr: list[tuple]) -> dict | None:
    """TTM from annual and quarterly (end, value) lists."""
    def near(target, items, tol=25):
        for end, v in items:
            if abs((end - target).days) <= tol:
                return v
        return None

    out: dict[str, Any] = {}
    if ann:
        a_end, a_val = ann[-1]
        post = [(e, v) for e, v in qtr if e > a_end + timedelta(days=20)]
        prior = [near(e - timedelta(days=365), qtr) for e, _ in post]
        if all(p is not None for p in prior):
            out["ttm"] = a_val + sum(v for _, v in post) - sum(prior)
            out["end"] = post[-1][0] if post else a_end
        else:
            out["ttm"], out["end"] = a_val, a_end
        out["fy"] = a_val
        prev_a = near(a_end - timedelta(days=365), ann, tol=40)
        out["fy_prev"] = prev_a
        if prev_a is not None:
            prior2 = [near(e - timedelta(days=730), qtr) for e, _ in post]
            if all(p is not None for p in prior2):
                out["ttm_prev"] = prev_a + sum(p for p in prior if p is not None) - sum(prior2)
    elif qtr:
        last4 = qtr[-4:]
        if len(last4) == 4 and (last4[-1][0] - last4[0][0]).days < 300:
            out["ttm"], out["end"] = sum(v for _, v in last4), last4[-1][0]
    if qtr:
        q_end, q_val = qtr[-1]
        out["q"] = q_val
        out["q_prev"] = near(q_end - timedelta(days=365), qtr)
        out.setdefault("end", q_end)
    return out if "ttm" in out or "q" in out else None


def _fundamental_ratios(f: pd.DataFrame) -> pd.DataFrame:
    def c(name):
        return f[name] if name in f.columns else pd.Series(np.nan, index=f.index)

    def growth(cur, prev):
        return ((cur / prev - 1) * 100).where(prev > 0)

    with np.errstate(divide="ignore", invalid="ignore"):
        rev = c("revenue_ttm")
        f["gross_margin"] = (c("gross_profit_ttm") / rev * 100).where(rev > 0)
        f["oper_margin"] = (c("operating_income_ttm") / rev * 100).where(rev > 0)
        f["net_margin"] = (c("net_income_ttm") / rev * 100).where(rev > 0)
        f["roe"] = (c("net_income_ttm") / c("equity") * 100).where(c("equity") > 0)
        f["roa"] = (c("net_income_ttm") / c("assets") * 100).where(c("assets") > 0)
        f["debt"] = c("lt_debt").fillna(c("lt_debt_total"))
        f["debt_eq"] = (f["debt"] / c("equity")).where(c("equity") > 0)
        f["current_ratio"] = (c("current_assets") / c("current_liabilities")).where(c("current_liabilities") > 0)
        f["fcf"] = c("ocf") - c("capex").fillna(0)
        f["sales_qoq"] = growth(c("revenue_q"), c("revenue_q_prev"))
        f["eps_qoq"] = growth(c("eps_q"), c("eps_q_prev").where(c("eps_q_prev") > 0))
        f["sales_growth"] = growth(rev, c("revenue_ttm_prev"))
        f["eps_growth"] = growth(c("eps_ttm"), c("eps_ttm_prev"))
        f["eps_ttm"] = c("eps_ttm")
        f["dps_ttm"] = c("dps_ttm")
    return f


# --- deterministic stand-ins for offline tests ---------------------------------------------

_FAKE_SECTORS = {"AAPL": ("Information Technology", "Technology Hardware"), "MSFT": ("Information Technology", "Software"),
                 "NVDA": ("Information Technology", "Semiconductors"), "AMD": ("Information Technology", "Semiconductors"),
                 "AMZN": ("Consumer Discretionary", "Broadline Retail"), "TSLA": ("Consumer Discretionary", "Automobiles"),
                 "GOOGL": ("Communication Services", "Interactive Media"), "META": ("Communication Services", "Interactive Media"),
                 "JPM": ("Financials", "Banks"), "XOM": ("Energy", "Oil & Gas")}


def _fake_listings() -> pd.DataFrame:
    rows = []
    for i, (sym, (sector, industry)) in enumerate(_FAKE_SECTORS.items()):
        rows.append({"symbol": sym, "name": f"{sym} Fake Corp", "sector": sector, "industry": industry,
                     "gics_sector": sector, "gics_industry": industry, "country": "United States",
                     "ipo_year": 1990 + i, "listing_market_cap": (i + 1) * 1e11, "listing_price": None,
                     "listing_change_pct": None, "listing_volume": None, "sp500": True, "exchange": "Nasdaq",
                     "cik": 1000 + i})
    return pd.DataFrame(rows).set_index("symbol")


def _fake_fundamentals(listings: pd.DataFrame) -> pd.DataFrame:
    rng = np.random.default_rng(7)
    n = len(listings)
    f = pd.DataFrame(index=listings.index)
    f["shares"] = listings["listing_market_cap"] / 150.0
    f["revenue_ttm"] = rng.uniform(5e9, 4e11, n)
    f["revenue_ttm_prev"] = f["revenue_ttm"] / rng.uniform(0.8, 1.4, n)
    f["net_income_ttm"] = f["revenue_ttm"] * rng.uniform(-0.05, 0.35, n)
    f["gross_profit_ttm"] = f["revenue_ttm"] * rng.uniform(0.2, 0.75, n)
    f["operating_income_ttm"] = f["revenue_ttm"] * rng.uniform(0.0, 0.45, n)
    f["eps_ttm"] = rng.uniform(-1, 15, n)
    f["eps_ttm_prev"] = f["eps_ttm"] / rng.uniform(0.7, 1.5, n)
    f["equity"] = f["revenue_ttm"] * rng.uniform(0.2, 1.5, n)
    f["assets"] = f["equity"] * rng.uniform(1.5, 4, n)
    f["cash"] = f["assets"] * 0.1
    f["lt_debt"] = f["equity"] * rng.uniform(0, 1.5, n)
    f["current_assets"] = f["assets"] * 0.3
    f["current_liabilities"] = f["assets"] * 0.2
    f["ocf"] = f["net_income_ttm"] * 1.2
    f["capex"] = f["ocf"] * 0.3
    f["dps_ttm"] = np.where(rng.uniform(size=n) > 0.4, rng.uniform(0.2, 4, n), np.nan)
    f["revenue_q"], f["revenue_q_prev"] = f["revenue_ttm"] / 4, f["revenue_ttm_prev"] / 4
    f["eps_q"], f["eps_q_prev"] = f["eps_ttm"] / 4, f["eps_ttm_prev"] / 4
    return _fundamental_ratios(f)


universe = Universe()
