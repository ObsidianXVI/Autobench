"""Reference data for the whole market: listings (Nasdaq), index membership (Wikipedia),
cross-sectional fundamentals (SEC XBRL frames), macro series (FRED) and event calendars (Nasdaq).

All of these are public endpoints without keys. Nasdaq's API expects a browser-like request."""

from __future__ import annotations

import io
import logging
import re
import time
from datetime import date
from typing import Any

import httpx
import pandas as pd

from .base import ProviderError

log = logging.getLogger(__name__)

_BROWSER = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/141.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}
_client = httpx.Client(timeout=45, follow_redirects=True)


def _get(url: str, params: dict | None = None, headers: dict | None = None, attempts: int = 3) -> httpx.Response:
    for i in range(attempts):
        try:
            r = _client.get(url, params=params, headers=headers or _BROWSER)
            if r.status_code == 200:
                return r
            if r.status_code in (429, 500, 502, 503, 504) and i < attempts - 1:
                time.sleep(1.5 * (i + 1))
                continue
            raise ProviderError(f"{url} returned HTTP {r.status_code}")
        except httpx.HTTPError as e:
            if i == attempts - 1:
                raise ProviderError(f"request to {url} failed: {e}") from e
            time.sleep(1.5 * (i + 1))
    raise ProviderError(f"request to {url} failed")


def _num(v: Any) -> float | None:
    """'$1,234.5' / '12.3%' / 'NA' -> float or None."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = re.sub(r"[$,%\s]", "", str(v))
    if s in ("", "NA", "N/A", "--", "&nbsp;"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


# --- listings ------------------------------------------------------------------------------

_SKIP_NAME = re.compile(r"\b(warrants?|units?|rights?|preferred|depositary shares? represent\w*|notes due|debentures|"
                       r"convertible)\b", re.I)


def nasdaq_listings() -> pd.DataFrame:
    """All US-listed common stocks (NYSE, Nasdaq, AMEX): name, sector, industry, country, IPO year,
    market cap, last sale, change and volume. Warrants, units, rights and preferreds are dropped."""
    r = _get("https://api.nasdaq.com/api/screener/stocks", {"tableonly": "true", "download": "true"})
    rows = (r.json().get("data") or {}).get("rows") or []
    if not rows:
        raise ProviderError("Nasdaq listings returned no rows")
    out = []
    for x in rows:
        sym = (x.get("symbol") or "").strip().upper()
        name = (x.get("name") or "").strip()
        if not sym or "^" in sym or _SKIP_NAME.search(name) or re.search(r"\d%", name):
            continue
        out.append({
            "symbol": sym.replace("/", "."),  # class shares: BRK/B -> BRK.B (Alpaca's form)
            "name": _clean_name(name),
            "sector": x.get("sector") or None,
            "industry": x.get("industry") or None,
            "country": x.get("country") or None,
            "ipo_year": _num(x.get("ipoyear")),
            "listing_market_cap": _num(x.get("marketCap")),
            "listing_price": _num(x.get("lastsale")),
            "listing_change_pct": _num(x.get("pctchange")),
            "listing_volume": _num(x.get("volume")),
        })
    df = pd.DataFrame(out).drop_duplicates("symbol").set_index("symbol")
    df.loc[df["listing_market_cap"] == 0, "listing_market_cap"] = None
    return df


def _clean_name(name: str) -> str:
    return re.sub(r"\s+(Common Stock|Class [A-C] Common Stock|Ordinary Shares|American Depositary Shares.*|"
                  r"Common Shares|Class [A-C] Ordinary Shares|Common Units).*$", "", name).strip()


def nasdaq_etfs() -> pd.DataFrame:
    r = _get("https://api.nasdaq.com/api/screener/etf", {"tableonly": "true", "download": "true"})
    rows = ((r.json().get("data") or {}).get("data") or {}).get("rows") or []
    return pd.DataFrame([{
        "symbol": (x.get("symbol") or "").upper(), "name": x.get("companyName") or "",
        "price": _num(x.get("lastSalePrice")), "change_pct": _num(x.get("percentageChange")),
        "perf_1y": _num(x.get("oneYearPercentage")),
    } for x in rows if x.get("symbol")]).set_index("symbol")


def sp500_members() -> pd.DataFrame:
    """S&P 500 constituents with GICS sector and sub-industry (Wikipedia's maintained table)."""
    r = _get("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
             headers={"User-Agent": "Autobench/0.2 (stock research tool)"})
    table = pd.read_html(io.StringIO(r.text), attrs={"id": "constituents"})[0]
    table = table.rename(columns={"Symbol": "symbol", "Security": "name", "GICS Sector": "gics_sector",
                                  "GICS Sub-Industry": "gics_industry", "CIK": "cik"})
    table["symbol"] = table["symbol"].str.upper()  # class shares already use dots (BRK.B)
    return table[["symbol", "name", "gics_sector", "gics_industry", "cik"]].set_index("symbol")


# --- calendars -----------------------------------------------------------------------------


def earnings_calendar(day: date) -> list[dict]:
    r = _get("https://api.nasdaq.com/api/calendar/earnings", {"date": day.isoformat()})
    rows = (r.json().get("data") or {}).get("rows") or []
    timing = {"time-pre-market": "BMO", "time-after-hours": "AMC", "time-not-supplied": None}
    return [{
        "date": day.isoformat(), "symbol": (x.get("symbol") or "").upper(), "name": x.get("name"),
        "time": timing.get(x.get("time") or "", None), "fiscal_quarter": x.get("fiscalQuarterEnding"),
        "eps_forecast": _num(x.get("epsForecast")), "estimates": _num(x.get("noOfEsts")),
        "eps_last_year": _num(x.get("lastYearEPS")), "market_cap": _num(x.get("marketCap")),
    } for x in rows if x.get("symbol")]


def economic_calendar(day: date) -> list[dict]:
    r = _get("https://api.nasdaq.com/api/calendar/economicevents", {"date": day.isoformat()})
    rows = (r.json().get("data") or {}).get("rows") or []

    def txt(v):
        v = (v or "").replace("&nbsp;", "").strip()
        return v or None

    return [{
        "date": day.isoformat(), "time_gmt": txt(x.get("gmt")), "country": x.get("country"), "event": x.get("eventName"),
        "actual": txt(x.get("actual")), "consensus": txt(x.get("consensus")), "previous": txt(x.get("previous")),
    } for x in rows if x.get("eventName")]


# --- SEC XBRL frames -----------------------------------------------------------------------


def sec_frame(taxonomy: str, concept: str, unit: str, period: str) -> list[dict]:
    """One concept for every filer and one period (e.g. CY2025, CY2026Q1, CY2026Q1I).
    Returns [] when the frame does not exist (yet)."""
    from ..services.filings import _get as sec_get

    try:
        return sec_get(f"https://data.sec.gov/api/xbrl/frames/{taxonomy}/{concept}/{unit}/{period}.json").json().get("data") or []
    except ProviderError as e:
        if "404" in str(e):
            return []
        raise


def sec_tickers() -> pd.DataFrame:
    """Ticker -> CIK and exchange for SEC filers."""
    from ..services.filings import _get as sec_get

    d = sec_get("https://www.sec.gov/files/company_tickers_exchange.json").json()
    df = pd.DataFrame(d["data"], columns=d["fields"])
    df["ticker"] = df["ticker"].str.upper()
    return df.drop_duplicates("ticker").set_index("ticker")


# --- FRED ----------------------------------------------------------------------------------


def fred_series(ids: list[str], start: str | None = None, timeout: float = 20) -> pd.DataFrame:
    """Daily/weekly/monthly FRED series as columns (index = observation date). Public CSV, no key.
    Keep batches small: FRED throttles clients that request many series at once."""
    params = {"id": ",".join(ids)}
    if start:
        params["cosd"] = start
    try:
        r = _client.get("https://fred.stlouisfed.org/graph/fredgraph.csv", params=params,
                        headers={"User-Agent": _BROWSER["User-Agent"]}, timeout=timeout)
    except httpx.HTTPError as e:
        raise ProviderError(f"FRED request failed: {e}") from e
    if r.status_code != 200:
        raise ProviderError(f"FRED returned HTTP {r.status_code}")
    df = pd.read_csv(io.StringIO(r.text))
    date_col = df.columns[0]
    df[date_col] = pd.to_datetime(df[date_col])
    df = df.set_index(date_col)
    return df.apply(pd.to_numeric, errors="coerce")


# --- Treasury & BLS (fallbacks that do not depend on FRED) ------------------------------------


def treasury_curve(year: int) -> pd.DataFrame:
    """Daily Treasury par yield curve for a year (columns '1 Mo' … '30 Yr', index = date)."""
    r = _get(f"https://home.treasury.gov/resource-center/data-chart-center/interest-rates/daily-treasury-rates.csv/{year}/all",
             {"type": "daily_treasury_yield_curve", "field_tdr_date_value": str(year), "page": "", "_format": "csv"},
             headers={"User-Agent": _BROWSER["User-Agent"]})
    df = pd.read_csv(io.StringIO(r.text))
    df["Date"] = pd.to_datetime(df["Date"], format="%m/%d/%Y")
    return df.set_index("Date").sort_index().apply(pd.to_numeric, errors="coerce")


def bls_series(series: list[str], start_year: int, end_year: int) -> pd.DataFrame:
    """Monthly BLS series (public v1 API, no key; at most 25 series and 10 years per request)."""
    r = _client.post("https://api.bls.gov/publicAPI/v1/timeseries/data/",
                     json={"seriesid": series, "startyear": str(start_year), "endyear": str(end_year)},
                     headers={"User-Agent": _BROWSER["User-Agent"]}, timeout=30)
    if r.status_code != 200:
        raise ProviderError(f"BLS returned HTTP {r.status_code}")
    body = r.json()
    if body.get("status") != "REQUEST_SUCCEEDED":
        raise ProviderError(f"BLS: {'; '.join(body.get('message') or ['request failed'])}")
    cols = {}
    for s in body["Results"]["series"]:
        pts = {}
        for d in s["data"]:
            if not d["period"].startswith("M") or d["period"] == "M13":
                continue
            v = _num(d["value"])
            if v is not None:
                pts[pd.Timestamp(int(d["year"]), int(d["period"][1:]), 1)] = v
        cols[s["seriesID"]] = pd.Series(pts)
    return pd.DataFrame(cols).sort_index()
