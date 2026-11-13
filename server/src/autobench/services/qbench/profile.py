"""Quant profile of one stock: return distribution, volatility regime, trending vs mean-reverting
diagnostics, factor exposures (ETF proxies), seasonality and the overnight/intraday split.

The regime verdict decides which strategy families deserve testing first:
  trending        Hurst > 0.58 (≈ 2 sd above a random walk on 2y) or variance ratios significantly > 1
  mean-reverting  Hurst < 0.42, VR significantly < 1, or ADF rejects a unit root → reversion
  random walk     nothing significant → no timing edge from price alone; use factors, sizing, hedging"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from ..market import get_bars, normalize_symbol
from ...timeutil import format_ts
from . import stats

# Factor proxies built from liquid ETFs (long-short as ETF minus SPY): good enough to read exposures.
FACTOR_PROXIES = {
    "market": ("SPY", None),
    "size": ("IWM", "SPY"),       # small minus large
    "value": ("IWD", "IWF"),      # Russell 1000 value minus growth
    "momentum": ("MTUM", "SPY"),
    "quality": ("QUAL", "SPY"),
    "low_vol": ("USMV", "SPY"),
}


def _r(x, nd=3):
    if x is None:
        return None
    x = float(x)
    return round(x, nd) if math.isfinite(x) else None


def _closes(symbol: str, lookback: str) -> pd.Series:
    return get_bars(symbol, "1d", lookback=lookback).df["c"]


def factor_exposures(rets: pd.Series, lookback: str = "2y") -> dict | None:
    cols = {}
    for name, (a, b) in FACTOR_PROXIES.items():
        try:
            ra = _closes(a, lookback).pct_change()
            cols[name] = ra if b is None else ra - _closes(b, lookback).pct_change()
        except Exception:
            continue
    if "market" not in cols:
        return None
    X = pd.concat(cols, axis=1)
    j = pd.concat([rets.rename("y"), X], axis=1).dropna()
    if len(j) < 120:
        return None
    names = [c for c in X.columns if c in j.columns]
    beta, se, resid, r2 = stats.ols(j["y"].to_numpy(), np.column_stack([np.ones(len(j)), j[names].to_numpy()]))
    out = {"observations": len(j), "r2": _r(r2), "alpha_annual_pct": _r(beta[0] * 252 * 100, 2),
           "alpha_t": _r(beta[0] / se[0], 2) if se[0] > 0 else None,
           "idiosyncratic_vol_pct": _r(resid.std(ddof=1) * math.sqrt(252) * 100, 2),
           "betas": {n: {"beta": _r(beta[i + 1], 2), "t": _r(beta[i + 1] / se[i + 1], 1) if se[i + 1] > 0 else None}
                     for i, n in enumerate(names)},
           "proxies": {n: f"{a}{'-' + b if b else ''}" for n, (a, b) in FACTOR_PROXIES.items() if n in names}}
    return out


def _regime(h: float, vrs: list[dict], adf_p: float | None) -> dict:
    sig_up = [v for v in vrs if v.get("z") is not None and v["z"] > 1.96]
    sig_dn = [v for v in vrs if v.get("z") is not None and v["z"] < -1.96]
    vr10 = next((v["vr"] for v in vrs if v["q"] == 10), None)
    reasons = []
    if (math.isfinite(h) and h > 0.58) or (sig_up and not sig_dn):
        label = "trending"
        families = ["trend", "momentum"]
        reasons.append(f"Hurst {h:.2f}" if math.isfinite(h) else "")
        reasons += [f"VR({v['q']})={v['vr']} z={v['z']}" for v in sig_up]
    elif (math.isfinite(h) and h < 0.42) or (sig_dn and not sig_up) or (adf_p is not None and adf_p < 0.05):
        label = "mean-reverting"
        families = ["mean_reversion"]
        reasons.append(f"Hurst {h:.2f}" if math.isfinite(h) else "")
        reasons += [f"VR({v['q']})={v['vr']} z={v['z']}" for v in sig_dn]
        if adf_p is not None and adf_p < 0.05:
            reasons.append(f"ADF p={adf_p}")
    else:
        label = "random walk"
        families = ["factor", "volatility", "seasonality", "allocation"]
        reasons.append("no significant persistence or reversion"
                       + (f" (Hurst {h:.2f}, VR(10) {vr10})" if math.isfinite(h) else ""))
    return {"label": label, "favoured_families": families, "evidence": [r for r in reasons if r]}


def quant_profile(symbol: str, lookback: str = "5y", factors: bool = True) -> dict:
    sym = normalize_symbol(symbol)
    bars = get_bars(sym, "1d", lookback=lookback)
    df = bars.df.dropna()
    if len(df) < 260:
        raise ValueError(f"{sym}: {len(df)} daily bars; the quant profile needs at least 260 (~1 year)")
    c, o = df["c"], df["o"]
    r = c.pct_change().dropna()
    lr = np.log(c).diff().dropna()
    recent = np.log(c.iloc[-504:]).to_numpy()  # 2 years for the regime tests
    vrs = [stats.variance_ratio(recent, q) for q in (2, 5, 10, 20)]
    h = stats.hurst(recent)
    a = stats.adf(recent)
    hl = stats.half_life(np.log(c.iloc[-504:]) - np.log(c.iloc[-504:]).rolling(50).mean())
    vol21 = lr.rolling(21).std() * math.sqrt(252)
    vol_now = float(vol21.iloc[-1])
    hist = vol21.iloc[-756:].dropna()
    var95 = float(np.percentile(r, 5))
    out: dict = {
        "symbol": sym, "as_of": format_ts(int(df.index[-1]), False),
        "period": {"start": format_ts(int(df.index[0]), False), "bars": len(df)},
        "price": _r(c.iloc[-1], 4),
        "returns": {**stats.perf_stats(r.to_numpy()),
                    "var_95_daily_pct": _r(var95 * 100, 2), "cvar_95_daily_pct": _r(r[r <= var95].mean() * 100, 2)},
        "volatility": {
            "realized_21d_pct": _r(vol_now * 100, 1),
            "realized_63d_pct": _r(lr.iloc[-63:].std() * math.sqrt(252) * 100, 1),
            "realized_1y_pct": _r(lr.iloc[-252:].std() * math.sqrt(252) * 100, 1),
            "percentile_3y": _r((hist < vol_now).mean() * 100, 0) if len(hist) > 20 else None,
            "vol_of_vol": _r(hist.pct_change(21).std(), 2) if len(hist) > 42 else None,
            "expected_move_1sd_pct": {k: _r(vol_now / math.sqrt(252) * math.sqrt(n) * 100, 2)
                                      for k, n in (("1d", 1), ("1w", 5), ("1m", 21))},
        },
        "regime_tests": {
            "window": "last 2 years of log prices",
            "hurst": _r(h, 3),
            "variance_ratios": vrs,
            "adf_log_price": a,
            "half_life_vs_sma50_bars": _r(hl, 1),
            "autocorr_1d": _r(stats.autocorr(lr.iloc[-504:].to_numpy(), 1)),
            "autocorr_1w": _r(stats.autocorr(np.log(c.iloc[-504::5]).diff().dropna().to_numpy(), 1)),
        },
        "trend_state": {
            "above_sma200": bool(c.iloc[-1] > c.rolling(200).mean().iloc[-1]),
            "return_12_1_pct": _r((c.iloc[-22] / c.iloc[-253] - 1) * 100, 2) if len(c) > 253 else None,
            "return_1m_pct": _r((c.iloc[-1] / c.iloc[-22] - 1) * 100, 2),
            "from_52w_high_pct": _r((c.iloc[-1] / df["h"].iloc[-252:].max() - 1) * 100, 2),
        },
    }
    out["regime"] = _regime(h, vrs, a.get("p_value"))
    # overnight (prev close -> open) vs intraday (open -> close)
    on = np.log(o / c.shift(1)).dropna()
    intra = np.log(c / o).loc[on.index]
    out["overnight_vs_intraday"] = {
        "overnight_annual_pct": _r(on.mean() * 252 * 100, 2), "intraday_annual_pct": _r(intra.mean() * 252 * 100, 2),
        "overnight_share_of_vol_pct": _r(on.var() / (on.var() + intra.var()) * 100, 1),
        "note": "Mean log returns. Most US stocks earn their drift overnight (Lou-Polk-Skouras); "
                "harvesting it costs two trades a day.",
    }
    out["seasonality"] = _seasonality(df)
    if factors:
        try:
            out["factor_exposures"] = factor_exposures(r)
        except Exception as e:  # proxies unavailable (offline / provider)
            out["factor_exposures"] = {"error": str(e)}
    return out


def _seasonality(df: pd.DataFrame) -> dict:
    c = df["c"]
    r = c.pct_change()
    d = pd.to_datetime(np.asarray(df.index, dtype="int64"), unit="s")
    s = pd.DataFrame({"r": r.to_numpy() * 100, "dow": d.day_of_week, "month": d.month, "ym": d.year * 12 + d.month})
    s = s.dropna()
    first = s.groupby("ym").cumcount() + 1
    last = s[::-1].groupby(s["ym"][::-1]).cumcount()[::-1] + 1
    tom = (first <= 3) | (last <= 1)

    def t_of(a, b):
        if len(a) < 10 or len(b) < 10:
            return None
        se = math.sqrt(a.var() / len(a) + b.var() / len(b))
        return _r((a.mean() - b.mean()) / se, 2) if se > 0 else None

    names = ["Mon", "Tue", "Wed", "Thu", "Fri"]
    return {
        "day_of_week_mean_pct": {names[k]: _r(v, 3) for k, v in s.groupby("dow")["r"].mean().items() if k < 5},
        "turn_of_month": {"tom_mean_pct": _r(s.loc[tom, "r"].mean(), 3), "other_mean_pct": _r(s.loc[~tom, "r"].mean(), 3),
                          "t_stat": t_of(s.loc[tom, "r"], s.loc[~tom, "r"]),
                          "window": "last trading day + first 3 of the month"},
        "month_of_year_mean_pct": {int(k): _r(v, 2) for k, v in
                                   (s.assign(m=s["month"]).groupby(["ym", "m"])["r"].sum().groupby(level=1).mean()).items()},
        "note": "Calendar effects are weak on single stocks; treat |t| < 2 as noise.",
    }
