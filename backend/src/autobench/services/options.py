"""Options-derived sentiment and expectations (Yahoo option chains): ATM implied volatility term
structure, expected move, put/call ratios, skew, max pain and unusual activity."""

from __future__ import annotations

from datetime import UTC, date, datetime

import numpy as np
import pandas as pd
import yfinance as yf

from .. import db
from ..providers.base import ProviderError
from ..providers.yahoo import yahoo_symbol
from .fundamentals import clean
from .market import get_quote, normalize_symbol


def _chain(symbol: str, expiration: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    oc = yf.Ticker(yahoo_symbol(symbol)).option_chain(expiration)
    return oc.calls.copy(), oc.puts.copy()


def _mid(df: pd.DataFrame) -> pd.Series:
    bid, ask = df["bid"].fillna(0), df["ask"].fillna(0)
    mid = (bid + ask) / 2
    return mid.where((bid > 0) & (ask > 0), df["lastPrice"])


def _expirations(symbol: str) -> list[str]:
    def fetch():
        try:
            return list(yf.Ticker(yahoo_symbol(symbol)).options)
        except Exception as e:
            raise ProviderError(f"Options unavailable for {symbol}: {e}") from e

    return db.cached(f"opt_exp:{symbol}", 3600, fetch) or []


def _max_pain(calls: pd.DataFrame, puts: pd.DataFrame) -> float | None:
    strikes = np.union1d(calls["strike"], puts["strike"])
    if len(strikes) == 0:
        return None
    ck, cv = calls["strike"].to_numpy(float), calls["openInterest"].fillna(0).to_numpy(float)
    pk, pv = puts["strike"].to_numpy(float), puts["openInterest"].fillna(0).to_numpy(float)
    # total intrinsic value paid to option holders if the underlying settles at each strike
    pain = [(np.clip(s - ck, 0, None) * cv).sum() + (np.clip(pk - s, 0, None) * pv).sum() for s in strikes]
    return float(strikes[int(np.argmin(pain))])


def options_overview(symbol: str, max_expirations: int = 6) -> dict:
    symbol = normalize_symbol(symbol)
    exps = _expirations(symbol)
    if not exps:
        raise ProviderError(f"No listed options for {symbol}")
    spot = get_quote(symbol)["price"]
    today = datetime.now(UTC).date()

    def fetch():
        term = []
        tot = {"call_volume": 0.0, "put_volume": 0.0, "call_oi": 0.0, "put_oi": 0.0}
        front = None
        unusual = []
        for exp in exps[:max_expirations]:
            dte = (date.fromisoformat(exp) - today).days
            try:
                calls, puts = _chain(symbol, exp)
            except Exception:
                continue
            if calls.empty or puts.empty:
                continue
            for k, df in (("call", calls), ("put", puts)):
                tot[f"{k}_volume"] += float(df["volume"].fillna(0).sum())
                tot[f"{k}_oi"] += float(df["openInterest"].fillna(0).sum())
                act = df[(df["volume"].fillna(0) > 2 * df["openInterest"].fillna(0)) & (df["volume"].fillna(0) >= 1000)]
                for _, r in act.iterrows():
                    unusual.append({"expiration": exp, "type": k, "strike": float(r.strike), "volume": int(r.volume),
                                    "open_interest": int(r.openInterest or 0), "iv_pct": round(float(r.impliedVolatility) * 100, 1)})
            ks = calls["strike"].to_numpy()
            atm_k = float(ks[np.argmin(np.abs(ks - spot))])
            c_atm = calls[calls["strike"] == atm_k]
            p_atm = puts[puts["strike"] == atm_k]
            iv = np.nanmean([c_atm["impliedVolatility"].mean(), p_atm["impliedVolatility"].mean()])
            straddle = float(_mid(c_atm).mean() + _mid(p_atm).mean()) if not c_atm.empty and not p_atm.empty else np.nan
            # skew proxy: IV of ~10% OTM put minus ~10% OTM call
            put_k = puts.iloc[(puts["strike"] - spot * 0.9).abs().argsort()[:1]]
            call_k = calls.iloc[(calls["strike"] - spot * 1.1).abs().argsort()[:1]]
            skew = float(put_k["impliedVolatility"].iloc[0] - call_k["impliedVolatility"].iloc[0]) * 100 if len(put_k) and len(call_k) else None
            row = {
                "expiration": exp, "dte": dte, "atm_strike": atm_k,
                "atm_iv_pct": round(float(iv) * 100, 1) if np.isfinite(iv) else None,
                "straddle_price": round(straddle, 2) if np.isfinite(straddle) else None,
                "expected_move_pct": round(straddle / spot * 100, 2) if np.isfinite(straddle) else None,
                "skew_10pct_otm_put_minus_call_iv": round(skew, 1) if skew is not None and np.isfinite(skew) else None,
            }
            term.append(row)
            if front is None and dte >= 1:
                front = {"expiration": exp, "max_pain": _max_pain(calls, puts),
                         "call_oi_top_strikes": calls.nlargest(3, "openInterest")["strike"].tolist(),
                         "put_oi_top_strikes": puts.nlargest(3, "openInterest")["strike"].tolist()}
        return {"term": term, "totals": tot, "front": front, "unusual": sorted(unusual, key=lambda u: -u["volume"])[:10]}

    d = db.cached(f"opt_overview:{symbol}:{max_expirations}", 900, fetch)
    tot = d["totals"]
    return clean({
        "symbol": symbol, "spot": spot,
        "expirations_available": len(exps),
        "term_structure": d["term"],
        "put_call_volume_ratio": round(tot["put_volume"] / tot["call_volume"], 2) if tot["call_volume"] else None,
        "put_call_oi_ratio": round(tot["put_oi"] / tot["call_oi"], 2) if tot["call_oi"] else None,
        "front_expiry": d["front"],
        "unusual_activity": d["unusual"],
        "notes": "expected_move_pct = ATM straddle / spot (market-implied ±move by expiry). Rising IV into a date "
                 "usually marks an event (earnings). Positive skew = puts richer than calls (downside hedging demand). "
                 "Volume/OI data from Yahoo may be stale outside market hours.",
    })


def option_chain(symbol: str, expiration: str | None = None, strikes_around_spot: int = 8) -> dict:
    symbol = normalize_symbol(symbol)
    exps = _expirations(symbol)
    if not exps:
        raise ProviderError(f"No listed options for {symbol}")
    exp = expiration or exps[0]
    if exp not in exps:
        raise ValueError(f"Expiration {exp} not listed. Available: {', '.join(exps[:12])}…")
    spot = get_quote(symbol)["price"]
    calls, puts = _chain(symbol, exp)
    strikes = np.union1d(calls["strike"], puts["strike"])
    center = int(np.argmin(np.abs(strikes - spot)))
    keep = strikes[max(0, center - strikes_around_spot) : center + strikes_around_spot + 1]

    def rows(df):
        df = df[df["strike"].isin(keep)]
        return [{"strike": float(r.strike), "bid": clean(r.bid), "ask": clean(r.ask), "last": clean(r.lastPrice),
                 "volume": clean(r.volume), "open_interest": clean(r.openInterest),
                 "iv_pct": round(float(r.impliedVolatility) * 100, 1) if pd.notna(r.impliedVolatility) else None,
                 "itm": bool(r.inTheMoney)} for r in df.itertuples()]

    return {"symbol": symbol, "expiration": exp, "spot": spot, "calls": rows(calls), "puts": rows(puts),
            "other_expirations": exps[:20]}
