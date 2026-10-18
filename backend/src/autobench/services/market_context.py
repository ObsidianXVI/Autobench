"""Market regime context: major indices, volatility, rates, credit, sectors and movers."""

from __future__ import annotations

import logging

import numpy as np

from .. import db
from ..providers import ProviderError, alpaca_provider
from . import indicators as ind
from .market import get_bars

log = logging.getLogger(__name__)

INDICES = {"SPY": "S&P 500", "QQQ": "Nasdaq 100", "IWM": "Russell 2000", "DIA": "Dow 30", "RSP": "S&P 500 equal weight"}
MACRO = {"^VIX": "VIX (S&P implied vol)", "^VIX3M": "VIX 3-month", "^TNX": "10Y Treasury yield",
         "TLT": "20+Y Treasuries", "HYG": "High-yield credit", "IEF": "7-10Y Treasuries",
         "GLD": "Gold", "USO": "Crude oil", "UUP": "US dollar"}
SECTORS = {"XLK": "Technology", "XLF": "Financials", "XLE": "Energy", "XLV": "Health Care",
           "XLY": "Consumer Discretionary", "XLP": "Consumer Staples", "XLI": "Industrials", "XLB": "Materials",
           "XLU": "Utilities", "XLRE": "Real Estate", "XLC": "Communication Services"}
SECTOR_ETF_BY_NAME = {v.lower(): k for k, v in SECTORS.items()} | {
    "consumer cyclical": "XLY", "consumer defensive": "XLP", "financial services": "XLF",
    "healthcare": "XLV", "basic materials": "XLB", "communication services": "XLC",
}


def _snapshot(symbol: str) -> dict:
    df = get_bars(symbol, "1d", lookback="400d").df
    if len(df) < 2:
        raise ProviderError(f"no data for {symbol}")
    c = df["c"]
    last = float(c.iloc[-1])

    def ret(n):
        return round(float((last / c.iloc[-1 - n] - 1) * 100), 2) if len(c) > n else None

    s50, s200 = ind.sma(c, 50).iloc[-1], ind.sma(c, 200).iloc[-1]
    rsi = ind.compute(df, "rsi")["rsi"].iloc[-1]
    return {
        "last": round(last, 2), "1d": ret(1), "1w": ret(5), "1m": ret(21), "3m": ret(63), "6m": ret(126),
        "above_sma50": bool(last > s50) if np.isfinite(s50) else None,
        "above_sma200": bool(last > s200) if np.isfinite(s200) else None,
        "rsi14": round(float(rsi), 1) if np.isfinite(rsi) else None,
    }


def _table(symbols: dict[str, str]) -> dict:
    out = {}
    for s, name in symbols.items():
        try:
            out[s] = {"name": name, **_snapshot(s)}
        except Exception as e:
            out[s] = {"name": name, "error": str(e)}
    return out


def market_overview() -> dict:
    def build():
        idx = _table(INDICES)
        macro = _table(MACRO)
        sectors = _table(SECTORS)
        ok_sectors = {k: v for k, v in sectors.items() if "error" not in v}
        ranked = sorted(ok_sectors, key=lambda k: -(ok_sectors[k].get("1m") or -1e9))
        regime = []
        spy = idx.get("SPY", {})
        if spy.get("above_sma200") is not None:
            regime.append(f"SPY {'above' if spy['above_sma200'] else 'BELOW'} its 200-day SMA "
                          f"({'risk-on backdrop' if spy['above_sma200'] else 'defensive backdrop'})")
        vix, vix3m = macro.get("^VIX", {}).get("last"), macro.get("^VIX3M", {}).get("last")
        if vix:
            lvl = "low/complacent" if vix < 15 else "normal" if vix < 20 else "elevated" if vix < 30 else "stressed"
            curve = ""
            if vix3m:
                curve = ", term structure " + ("in contango (normal)" if vix < vix3m else "INVERTED (near-term fear)")
            regime.append(f"VIX {vix} ({lvl}{curve})")
        rsp, spy3 = idx.get("RSP", {}).get("3m"), spy.get("3m")
        if rsp is not None and spy3 is not None:
            regime.append(f"Equal-weight vs cap-weight 3m: {rsp - spy3:+.1f} pts "
                          f"({'broad participation' if rsp >= spy3 else 'narrow, mega-cap-led market'})")
        iwm3 = idx.get("IWM", {}).get("3m")
        if iwm3 is not None and spy3 is not None:
            regime.append(f"Small caps vs S&P 3m: {iwm3 - spy3:+.1f} pts")
        hyg, ief = macro.get("HYG", {}).get("1m"), macro.get("IEF", {}).get("1m")
        if hyg is not None and ief is not None:
            regime.append(f"Credit (HYG vs IEF 1m): {hyg - ief:+.1f} pts ({'risk appetite' if hyg >= ief else 'credit caution'})")
        breadth = [v.get("above_sma50") for v in ok_sectors.values() if v.get("above_sma50") is not None]
        if breadth:
            regime.append(f"{sum(breadth)}/{len(breadth)} sector ETFs above their 50-day SMA")
        defensives = np.mean([ok_sectors[s]["1m"] for s in ("XLU", "XLP", "XLV") if s in ok_sectors and ok_sectors[s].get("1m") is not None] or [np.nan])
        cyclicals = np.mean([ok_sectors[s]["1m"] for s in ("XLK", "XLY", "XLI", "XLF") if s in ok_sectors and ok_sectors[s].get("1m") is not None] or [np.nan])
        if np.isfinite(defensives) and np.isfinite(cyclicals):
            regime.append(f"Cyclicals vs defensives 1m: {cyclicals - defensives:+.1f} pts")
        return {
            "indices": idx, "macro": macro, "sectors": sectors,
            "sector_leaders_1m": ranked[:3], "sector_laggards_1m": ranked[-3:][::-1],
            "regime_notes": regime,
        }

    return db.cached("market_overview", 900, build)


def movers(top: int = 10) -> dict:
    a = alpaca_provider()
    if a is None:
        raise ProviderError("Market movers require Alpaca credentials")

    def fetch():
        m = a.movers(top)
        act = a.most_actives(top)
        return {"gainers": m.get("gainers", []), "losers": m.get("losers", []),
                "most_active": act.get("most_actives", []), "last_updated": m.get("last_updated")}

    return db.cached(f"movers:{top}", 300, fetch)


def sector_etf_for(sector: str | None) -> str | None:
    if not sector:
        return None
    return SECTOR_ETF_BY_NAME.get(sector.lower())
