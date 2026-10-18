"""Macro and cross-asset views: FRED economic indicators and the Treasury curve, a
cross-asset board (indices, futures, rates, FX, crypto, global, sectors, factors, bonds),
and earnings / economic calendars."""

from __future__ import annotations

import logging
import math
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import numpy as np
import pandas as pd

from .. import db
from ..config import get_settings
from ..providers import ProviderError, reference
from .market import get_bars

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Indicator:
    id: str
    name: str
    group: str
    unit: str  # "%", "index", "k", "$B", "bp" …
    transform: str = "level"  # level | yoy | mom_diff | yoy_diff
    better: str = ""  # "up" / "down" when a direction is clearly good (for coloring)
    note: str = ""


INDICATORS: list[Indicator] = [
    Indicator("DFF", "Fed funds rate", "Rates", "%"),
    Indicator("DGS2", "2Y Treasury", "Rates", "%"),
    Indicator("DGS10", "10Y Treasury", "Rates", "%"),
    Indicator("DGS30", "30Y Treasury", "Rates", "%"),
    Indicator("T10Y2Y", "10Y − 2Y spread", "Rates", "%", note="Negative = inverted curve"),
    Indicator("T10Y3M", "10Y − 3M spread", "Rates", "%", note="Recession signal when inverted"),
    Indicator("DFII10", "10Y real yield (TIPS)", "Rates", "%"),
    Indicator("T10YIE", "10Y breakeven inflation", "Rates", "%"),
    Indicator("MORTGAGE30US", "30Y mortgage rate", "Rates", "%"),
    Indicator("CPIAUCSL", "CPI inflation", "Inflation", "% y/y", "yoy", "down"),
    Indicator("CPILFESL", "Core CPI inflation", "Inflation", "% y/y", "yoy", "down"),
    Indicator("PCEPILFE", "Core PCE inflation", "Inflation", "% y/y", "yoy", "down", "The Fed's target measure (2%)"),
    Indicator("PPIFIS", "PPI final demand", "Inflation", "% y/y", "yoy", "down"),
    Indicator("UNRATE", "Unemployment rate", "Labor", "%", better="down"),
    Indicator("PAYEMS", "Nonfarm payrolls (m/m)", "Labor", "k", "mom_diff", "up"),
    Indicator("ICSA", "Initial jobless claims", "Labor", "k", better="down"),
    Indicator("JTSJOL", "Job openings (JOLTS)", "Labor", "k"),
    Indicator("A191RL1Q225SBEA", "Real GDP growth (q/q ann.)", "Growth", "%", better="up"),
    Indicator("INDPRO", "Industrial production", "Growth", "% y/y", "yoy", "up"),
    Indicator("RSAFS", "Retail sales", "Growth", "% y/y", "yoy", "up"),
    Indicator("UMCSENT", "Consumer sentiment (UMich)", "Growth", "index", better="up"),
    Indicator("HOUST", "Housing starts", "Housing", "k"),
    Indicator("CSUSHPINSA", "Home prices (Case-Shiller)", "Housing", "% y/y", "yoy"),
    Indicator("BAMLH0A0HYM2", "High-yield credit spread", "Credit & liquidity", "%", better="down"),
    Indicator("BAMLC0A0CM", "Investment-grade spread", "Credit & liquidity", "%", better="down"),
    Indicator("NFCI", "Financial conditions (NFCI)", "Credit & liquidity", "index", better="down",
              note="Positive = tighter than average"),
    Indicator("WALCL", "Fed balance sheet", "Credit & liquidity", "$B"),
    Indicator("M2SL", "M2 money supply", "Credit & liquidity", "% y/y", "yoy"),
    Indicator("DTWEXBGS", "Trade-weighted dollar", "Credit & liquidity", "index"),
]
INDICATOR_MAP = {i.id: i for i in INDICATORS}

CURVE = [("1M", "DGS1MO"), ("3M", "DGS3MO"), ("6M", "DGS6MO"), ("1Y", "DGS1"), ("2Y", "DGS2"), ("3Y", "DGS3"),
         ("5Y", "DGS5"), ("7Y", "DGS7"), ("10Y", "DGS10"), ("20Y", "DGS20"), ("30Y", "DGS30")]

MARKET_GROUPS: dict[str, dict[str, str]] = {
    "US indices": {"^GSPC": "S&P 500", "^NDX": "Nasdaq 100", "^DJI": "Dow Jones", "^RUT": "Russell 2000",
                   "^VIX": "VIX"},
    "Futures": {"ES=F": "S&P 500 fut", "NQ=F": "Nasdaq fut", "YM=F": "Dow fut", "RTY=F": "Russell fut",
                "CL=F": "Crude oil (WTI)", "BZ=F": "Brent", "NG=F": "Natural gas", "GC=F": "Gold",
                "SI=F": "Silver", "HG=F": "Copper", "ZC=F": "Corn", "ZW=F": "Wheat"},
    "Rates": {"^IRX": "13W T-bill", "^FVX": "5Y yield", "^TNX": "10Y yield", "^TYX": "30Y yield"},
    "Currencies": {"DX-Y.NYB": "US dollar index", "EURUSD=X": "EUR/USD", "USDJPY=X": "USD/JPY",
                   "GBPUSD=X": "GBP/USD", "USDCNY=X": "USD/CNY", "AUDUSD=X": "AUD/USD", "USDCAD=X": "USD/CAD"},
    "Crypto": {"BTC-USD": "Bitcoin", "ETH-USD": "Ethereum", "SOL-USD": "Solana"},
    "Global": {"^STOXX50E": "Euro Stoxx 50", "^GDAXI": "DAX", "^FTSE": "FTSE 100", "^N225": "Nikkei 225",
               "^HSI": "Hang Seng", "000001.SS": "Shanghai", "^BSESN": "Sensex"},
    "Sectors": {"XLK": "Technology", "XLC": "Communication", "XLY": "Cons. discretionary", "XLF": "Financials",
                "XLV": "Health care", "XLI": "Industrials", "XLE": "Energy", "XLP": "Cons. staples",
                "XLU": "Utilities", "XLB": "Materials", "XLRE": "Real estate"},
    "Factors": {"SPY": "S&P 500", "RSP": "Equal weight", "QQQ": "Nasdaq 100", "IWM": "Small caps",
                "MTUM": "Momentum", "QUAL": "Quality", "VLUE": "Value", "USMV": "Min volatility",
                "IWF": "Growth", "IWD": "Value (R1000)"},
    "Bonds & credit": {"SHY": "1-3Y Treasuries", "IEF": "7-10Y Treasuries", "TLT": "20+Y Treasuries",
                       "TIP": "TIPS", "LQD": "IG corporate", "HYG": "High yield", "EMB": "EM bonds"},
}


def _clean(v: Any, digits: int = 4) -> Any:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return v
    return None if math.isnan(f) or math.isinf(f) else round(f, digits)


# ------------------------------------------------------------------------------- FRED


# BLS equivalents for the FRED series that matter most (used when FRED is unreachable)
_BLS = {"CPIAUCSL": "CUSR0000SA0", "CPILFESL": "CUSR0000SA0L1E", "UNRATE": "LNS14000000", "PAYEMS": "CES0000000001"}
_TREASURY = {"DGS1MO": "1 Mo", "DGS3MO": "3 Mo", "DGS6MO": "6 Mo", "DGS1": "1 Yr", "DGS2": "2 Yr", "DGS3": "3 Yr",
             "DGS5": "5 Yr", "DGS7": "7 Yr", "DGS10": "10 Yr", "DGS20": "20 Yr", "DGS30": "30 Yr"}


def _fred_frame() -> pd.DataFrame:
    """All dashboard series as columns. Sources, in order: FRED (small batches), then the
    Treasury yield-curve CSV and BLS for the series they cover; anything still missing comes
    from the last successful download (persisted), so a flaky source never blanks the page."""
    if get_settings().bar_provider == "fake":
        payload = _fake_fred([i.id for i in INDICATORS] + list(_TREASURY))
        return pd.DataFrame(payload["columns"], index=pd.to_datetime(payload["index"]))
    payload = db.cache_get("macro:frame:v2")
    if payload is None:
        payload = _download_macro()
        # retry sooner when FRED was unreachable and fallbacks filled in
        db.cache_set("macro:frame:v2", payload, 6 * 3600 if payload.get("fred_ok") else 3600)
    return _frame_from_payload(payload)


def _frame_from_payload(payload: dict) -> pd.DataFrame:
    return pd.DataFrame(payload["columns"], index=pd.to_datetime(payload["index"])).sort_index()


def _download_macro() -> dict:
    ids = list(dict.fromkeys([i.id for i in INDICATORS] + list(_TREASURY)))
    start = (date.today() - timedelta(days=6 * 366)).isoformat()
    cols: dict[str, pd.Series] = {}
    sources: dict[str, str] = {}
    fred_ok = True
    batches = [ids[k:k + 4] for k in range(0, len(ids), 4)]
    for batch in batches:
        if not fred_ok:
            break
        try:
            frame = reference.fred_series(batch, start=start)
        except ProviderError as e:
            log.warning("FRED unavailable (%s); using fallbacks", e)
            fred_ok = False  # do not hammer a host that is timing out
            break
        for c in frame.columns:
            cols[c] = frame[c].dropna()
            sources[c] = "FRED"
    missing_curve = [k for k in _TREASURY if k not in cols]
    if missing_curve:
        try:
            years = [date.today().year - k for k in range(6, -1, -1)]
            tr = pd.concat([reference.treasury_curve(y) for y in years]).sort_index()
            for sid in missing_curve:
                if _TREASURY[sid] in tr.columns:
                    cols[sid] = tr[_TREASURY[sid]].dropna()
                    sources[sid] = "U.S. Treasury"
            if "DGS10" in cols and "DGS2" in cols and "T10Y2Y" not in cols:
                cols["T10Y2Y"] = (cols["DGS10"] - cols["DGS2"]).dropna()
                sources["T10Y2Y"] = "U.S. Treasury"
            if "DGS10" in cols and "DGS3MO" in cols and "T10Y3M" not in cols:
                cols["T10Y3M"] = (cols["DGS10"] - cols["DGS3MO"]).dropna()
                sources["T10Y3M"] = "U.S. Treasury"
        except ProviderError as e:
            log.warning("Treasury curve unavailable: %s", e)
    missing_bls = {k: v for k, v in _BLS.items() if k not in cols}
    if missing_bls:
        try:
            y = date.today().year
            bls = reference.bls_series(list(missing_bls.values()), y - 6, y)
            for fid, bid in missing_bls.items():
                if bid in bls.columns:
                    cols[fid] = bls[bid].dropna()
                    sources[fid] = "BLS"
        except ProviderError as e:
            log.warning("BLS unavailable: %s", e)
    last_good = db.state_get("macro_last_good", None)
    if last_good:
        old = _frame_from_payload(last_good["frame"])
        for c in old.columns:
            if c not in cols:
                cols[c] = old[c].dropna()
                sources[c] = (last_good.get("sources", {}).get(c, "FRED")) + " (cached)"
    if not cols:
        raise ProviderError("No macro source reachable (FRED, Treasury, BLS)")
    df = pd.DataFrame(cols).sort_index()
    payload = {"index": [d.strftime("%Y-%m-%d") for d in df.index],
               "columns": {c: [None if pd.isna(v) else float(v) for v in df[c]] for c in df.columns},
               "sources": sources, "fred_ok": fred_ok}
    if any("(cached)" not in src for src in sources.values()):
        db.state_set("macro_last_good", {"frame": payload, "sources": sources})
    return payload


def _transform(s: pd.Series, how: str) -> pd.Series:
    s = s.dropna()
    if how == "yoy":
        if len(s) < 3:
            return s.iloc[0:0]
        # monthly/quarterly/weekly: compare with the observation one year earlier
        prior = s.reindex(s.index - pd.DateOffset(years=1), method="nearest", tolerance=pd.Timedelta(days=20))
        out = (s.to_numpy() / prior.to_numpy() - 1) * 100
        return pd.Series(out, index=s.index).dropna()
    if how == "mom_diff":
        return s.diff().dropna()
    return s


def macro_dashboard() -> dict:
    df = _fred_frame()
    sources = {}
    if get_settings().bar_provider != "fake":
        sources = (db.cache_get("macro:frame:v2") or {}).get("sources", {})
    out_groups: dict[str, list] = {}
    for ind in INDICATORS:
        if ind.id not in df.columns:
            continue
        s = _transform(df[ind.id], ind.transform)
        if ind.id == "WALCL":
            s = s / 1000  # millions -> billions
        if s.empty:
            continue
        last_date = s.index[-1]
        prev = s.iloc[-2] if len(s) > 1 else None
        year_ago = s.asof(last_date - pd.DateOffset(years=1)) if len(s) > 1 else None
        spark = s[s.index >= last_date - pd.DateOffset(years=2)]
        if len(spark) > 120:
            spark = spark.iloc[:: max(1, len(spark) // 120)]
        out_groups.setdefault(ind.group, []).append({
            "id": ind.id, "name": ind.name, "unit": ind.unit, "better": ind.better, "note": ind.note,
            "value": _clean(s.iloc[-1]), "date": last_date.date().isoformat(), "previous": _clean(prev),
            "change": _clean(s.iloc[-1] - prev) if prev is not None else None,
            "year_ago": _clean(year_ago), "spark": [_clean(v, 3) for v in spark.tolist()],
            "source": sources.get(ind.id, "FRED"),
        })
    curve = []
    for label, sid in CURVE:
        if sid not in df.columns:
            continue
        s = df[sid].dropna()
        if s.empty:
            continue
        last = s.index[-1]
        curve.append({"tenor": label, "now": _clean(s.iloc[-1]),
                      "month_ago": _clean(s.asof(last - pd.DateOffset(months=1))),
                      "year_ago": _clean(s.asof(last - pd.DateOffset(years=1)))})
    return {"groups": [{"name": g, "indicators": v} for g, v in out_groups.items()], "curve": curve,
            "as_of": df.index[-1].date().isoformat() if len(df.index) else None,
            "sources": sorted(set(sources.values())) or ["FRED"]}


def macro_series(series_id: str, start: str | None = None) -> dict:
    """History for one FRED series (any id, transformed when it is a dashboard indicator)."""
    sid = series_id.upper()
    ind = INDICATOR_MAP.get(sid)
    if get_settings().bar_provider == "fake":
        df = _fred_frame()
        if sid not in df.columns:
            raise KeyError(f"Unknown FRED series {sid}")
        s = df[sid]
    else:
        def fetch():
            frame = reference.fred_series([sid], start=start or "1990-01-01")
            col = frame[frame.columns[0]]
            return {"index": [d.strftime("%Y-%m-%d") for d in col.index], "values": [None if pd.isna(v) else float(v) for v in col]}

        try:
            payload = db.cached(f"fred:series:{sid}:{start}", 6 * 3600, fetch)
        except ProviderError as e:
            raise KeyError(f"FRED series {sid} not available: {e}") from e
        s = pd.Series(payload["values"], index=pd.to_datetime(payload["index"]))
    s = _transform(s, ind.transform if ind else "level")
    if start:
        s = s[s.index >= pd.Timestamp(start)]
    return {"id": sid, "name": ind.name if ind else sid, "unit": ind.unit if ind else "",
            "points": [{"time": int(pd.Timestamp(d).timestamp()), "value": _clean(v)} for d, v in s.items()]}


def _fake_fred(ids: list[str]) -> dict:
    idx = pd.date_range(end=pd.Timestamp.today().normalize(), periods=72, freq="MS")
    rng = np.random.default_rng(3)
    cols = {}
    for k, sid in enumerate(ids):
        base = 2 + (k % 7)
        cols[sid] = list(np.round(base * np.cumprod(1 + rng.normal(0.002, 0.01, len(idx))), 3))
    return {"index": [d.strftime("%Y-%m-%d") for d in idx], "columns": cols}


# ------------------------------------------------------------------------------- cross-asset board


def _instrument(symbol: str, name: str) -> dict:
    try:
        df = get_bars(symbol, "1d", lookback="400d").df
    except Exception as e:  # one bad symbol must not break the board
        return {"symbol": symbol, "name": name, "error": str(e)}
    if len(df) < 2:
        return {"symbol": symbol, "name": name, "error": "no data"}
    c = df["c"]
    last = float(c.iloc[-1])

    def ret(n):
        return _clean((last / c.iloc[-1 - n] - 1) * 100, 2) if len(c) > n else None

    idx = pd.to_datetime(df.index, unit="s")
    prior_year = c[idx.year < idx[-1].year]
    return {
        "symbol": symbol, "name": name, "last": _clean(last), "change": _clean(last - c.iloc[-2]),
        "change_pct": ret(1), "1w": ret(5), "1m": ret(21), "3m": ret(63),
        "ytd": _clean((last / prior_year.iloc[-1] - 1) * 100, 2) if len(prior_year) else None, "1y": ret(252),
        "spark": [_clean(v, 4) for v in c.iloc[-90:].tolist()],
        "date": idx[-1].date().isoformat(),
    }


def markets_board(groups: list[str] | None = None) -> dict:
    wanted = [g for g in MARKET_GROUPS if not groups or g in groups]

    def build():
        jobs = [(g, s, n) for g in wanted for s, n in MARKET_GROUPS[g].items()]
        with ThreadPoolExecutor(8) as pool:
            results = list(pool.map(lambda j: (j[0], _instrument(j[1], j[2])), jobs))
        out: dict[str, list] = {g: [] for g in wanted}
        for g, row in results:
            out[g].append(row)
        return {"groups": [{"name": g, "items": out[g]} for g in wanted]}

    return db.cached(f"markets:board:{','.join(wanted)}", 180, build)


# ------------------------------------------------------------------------------- calendars


def earnings_calendar(days: int = 7, start: str | None = None, universe_name: str = "all",
                      symbols: list[str] | None = None, min_market_cap: float = 0) -> dict:
    """Upcoming earnings (Nasdaq calendar) joined with the universe's sector and market cap."""
    from .universe import universe

    first = date.fromisoformat(start) if start else date.today()
    rows: list[dict] = []
    if get_settings().bar_provider == "fake":
        rows = [{"date": (first + timedelta(days=i % max(1, days))).isoformat(), "symbol": s, "name": f"{s} Fake Corp",
                 "time": "AMC" if i % 2 else "BMO", "eps_forecast": 1.0 + i / 10, "estimates": 5, "eps_last_year": 1.0,
                 "fiscal_quarter": "Sep/2026", "market_cap": None}
                for i, s in enumerate(["AAPL", "MSFT", "NVDA", "AMZN", "META", "JPM"])]
    else:
        for i in range(days):
            d = first + timedelta(days=i)
            if d.weekday() >= 5:
                continue
            try:
                rows += db.cached(f"earnings:{d.isoformat()}", 6 * 3600, lambda d=d: reference.earnings_calendar(d)) or []
            except ProviderError as e:
                log.warning("earnings calendar %s: %s", d, e)
    table = universe.table()
    want = {s.upper() for s in symbols} if symbols else None
    out = []
    for r in rows:
        sym = r["symbol"]
        if want and sym not in want:
            continue
        info = table.loc[sym] if not table.empty and sym in table.index else None
        if universe_name == "sp500" and (info is None or not bool(info.get("sp500"))):
            continue
        if universe_name.startswith("watchlist:"):
            from .workspace import get_watchlist

            if sym not in set(get_watchlist(universe_name.split(":", 1)[1])["symbols"]):
                continue
        mc = _clean(info.get("market_cap")) if info is not None else r.get("market_cap")
        if min_market_cap and (mc or 0) < min_market_cap:
            continue
        out.append({**r, "market_cap": mc, "sector": info.get("sector") if info is not None else None,
                    "price": _clean(info.get("price")) if info is not None else None,
                    "change_pct": _clean(info.get("change_pct"), 2) if info is not None else None})
    out.sort(key=lambda r: (r["date"], -(r.get("market_cap") or 0)))
    return {"start": first.isoformat(), "days": days, "count": len(out), "events": out}


def economic_calendar(days: int = 7, start: str | None = None, country: str | None = "United States") -> dict:
    first = date.fromisoformat(start) if start else date.today()
    rows: list[dict] = []
    if get_settings().bar_provider == "fake":
        rows = [{"date": first.isoformat(), "time_gmt": "12:30", "country": "United States", "event": "CPI (MoM)",
                 "actual": None, "consensus": "0.3%", "previous": "0.2%"}]
    else:
        for i in range(days):
            d = first + timedelta(days=i)
            if d.weekday() >= 5:
                continue
            try:
                rows += db.cached(f"econcal:{d.isoformat()}", 3 * 3600, lambda d=d: reference.economic_calendar(d)) or []
            except ProviderError as e:
                log.warning("economic calendar %s: %s", d, e)
    if country:
        rows = [r for r in rows if (r.get("country") or "").lower() == country.lower()]
    return {"start": first.isoformat(), "days": days, "count": len(rows), "events": rows}
