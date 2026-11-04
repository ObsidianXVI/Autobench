"""Return/risk statistics, correlation and position sizing."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from .market import get_bars, normalize_symbol
from ..timeutil import format_ts


def _daily_returns(symbol: str, lookback: str) -> pd.Series:
    bars = get_bars(symbol, "1d", lookback=lookback)
    c = bars.df["c"]
    return np.log(c / c.shift(1)).dropna()


def risk_metrics(symbol: str, benchmark: str = "SPY", lookback: str = "3y", risk_free_rate: float = 0.04) -> dict:
    symbol, benchmark = normalize_symbol(symbol), normalize_symbol(benchmark)
    bars = get_bars(symbol, "1d", lookback=lookback)
    c = bars.df["c"]
    if len(c) < 60:
        raise ValueError(f"Not enough daily history for {symbol} ({len(c)} bars)")
    r = np.log(c / c.shift(1)).dropna()
    simple = c.pct_change().dropna()
    ann = 252
    vol = float(r.std() * math.sqrt(ann))
    years = len(r) / ann
    total = float(c.iloc[-1] / c.iloc[0] - 1)
    cagr = (1 + total) ** (1 / years) - 1 if years > 0 else float("nan")
    rf_daily = risk_free_rate / ann
    excess = simple - rf_daily
    downside = simple[simple < 0]
    peak = c.cummax()
    dd = c / peak - 1
    trough_t = dd.idxmin()
    peak_t = c.loc[:trough_t].idxmax()
    var95 = float(np.percentile(simple, 5))
    out = {
        "symbol": symbol,
        "period": {"start": format_ts(int(c.index[0]), False), "end": format_ts(int(c.index[-1]), False), "days": len(c)},
        "total_return_pct": round(total * 100, 2),
        "cagr_pct": round(cagr * 100, 2),
        "annualized_volatility_pct": round(vol * 100, 2),
        "sharpe": round(float(excess.mean() / simple.std() * math.sqrt(ann)), 2) if simple.std() > 0 else None,
        "sortino": round(float(excess.mean() / downside.std() * math.sqrt(ann)), 2) if len(downside) > 1 else None,
        "max_drawdown_pct": round(float(dd.min()) * 100, 2),
        "max_drawdown_window": {"peak": format_ts(int(peak_t), False), "trough": format_ts(int(trough_t), False)},
        "current_drawdown_pct": round(float(dd.iloc[-1]) * 100, 2),
        "var_95_daily_pct": round(var95 * 100, 2),
        "cvar_95_daily_pct": round(float(simple[simple <= var95].mean()) * 100, 2),
        "skew": round(float(simple.skew()), 2),
        "excess_kurtosis": round(float(simple.kurt()), 2),
        "best_day_pct": round(float(simple.max()) * 100, 2),
        "worst_day_pct": round(float(simple.min()) * 100, 2),
        "up_days_pct": round(float((simple > 0).mean()) * 100, 1),
        "volatility_recent_20d_pct": round(float(r.iloc[-20:].std() * math.sqrt(ann)) * 100, 2),
    }
    if benchmark != symbol:
        b = _daily_returns(benchmark, lookback)
        j = pd.concat([r, b], axis=1, keys=["s", "b"]).dropna()
        if len(j) > 30 and j["b"].var() > 0:
            beta = float(j["s"].cov(j["b"]) / j["b"].var())
            corr = float(j["s"].corr(j["b"]))
            bc = get_bars(benchmark, "1d", lookback=lookback).df["c"]
            b_total = float(bc.iloc[-1] / bc.iloc[0] - 1)
            b_cagr = (1 + b_total) ** (1 / years) - 1 if years > 0 else float("nan")
            out["vs_benchmark"] = {
                "benchmark": benchmark,
                "beta": round(beta, 2),
                "correlation": round(corr, 2),
                "benchmark_cagr_pct": round(b_cagr * 100, 2),
                "alpha_annual_pct": round((cagr - (risk_free_rate + beta * (b_cagr - risk_free_rate))) * 100, 2),
                "r_squared": round(corr**2, 2),
                "relative_return_pct": round((total - b_total) * 100, 2),
                "rolling_beta_6m": round(float(j["s"].iloc[-126:].cov(j["b"].iloc[-126:]) / j["b"].iloc[-126:].var()), 2)
                if len(j) >= 126 else None,
            }
    return out


def correlation_matrix(symbols: list[str], lookback: str = "1y") -> dict:
    syms = [normalize_symbol(s) for s in symbols]
    if len(syms) < 2:
        raise ValueError("Provide at least two symbols")
    rets = pd.concat({s: _daily_returns(s, lookback) for s in syms}, axis=1).dropna()
    corr = rets.corr().round(2)
    vol = (rets.std() * math.sqrt(252) * 100).round(2)
    return {
        "lookback": lookback, "observations": len(rets),
        "correlation": {a: {b: float(corr.loc[a, b]) for b in syms} for a in syms},
        "annualized_volatility_pct": {s: float(vol[s]) for s in syms},
    }


def position_size(
    account_size: float,
    entry: float,
    stop: float,
    risk_pct: float = 1.0,
    max_position_pct: float = 25.0,
    targets: list[float] | None = None,
) -> dict:
    """Fixed-fractional sizing: risk `risk_pct` of the account between entry and stop."""
    if account_size <= 0 or entry <= 0 or stop <= 0:
        raise ValueError("account_size, entry and stop must be positive")
    if entry == stop:
        raise ValueError("entry and stop must differ")
    side = "long" if stop < entry else "short"
    per_share = abs(entry - stop)
    risk_dollars = account_size * risk_pct / 100
    shares = math.floor(risk_dollars / per_share)
    cap = math.floor(account_size * max_position_pct / 100 / entry)
    capped = shares > cap
    shares = min(shares, cap)
    out = {
        "side": side,
        "shares": shares,
        "position_value": round(shares * entry, 2),
        "position_pct_of_account": round(shares * entry / account_size * 100, 2),
        "risk_per_share": round(per_share, 4),
        "dollar_risk": round(shares * per_share, 2),
        "risk_pct_of_account": round(shares * per_share / account_size * 100, 3),
        "stop_distance_pct": round(per_share / entry * 100, 2),
        "capped_by_max_position": capped,
    }
    if targets:
        out["targets"] = [
            {"price": t, "r_multiple": round((t - entry) / (entry - stop), 2),
             "profit": round(shares * (t - entry) * (1 if side == "long" else -1), 2)}
            for t in targets
        ]
    return out
