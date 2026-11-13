"""Cross-sectional (relative) strategies over a list of stocks, e.g. a watchlist: each month rank the
list by a factor computed from data up to that day and hold the top k equally weighted.

Benchmarks: the equal-weighted list (same names, monthly rebalanced) and SPY. Beating the equal-weighted
list is the real test: it isolates the ranking skill from the list's own drift (survivorship caveat: a
watchlist is chosen today, so its history is biased upward; compare to it, not to cash)."""

from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd

from ..market import get_bars, normalize_symbol
from ...timeutil import format_ts
from . import stats

FACTORS = {
    "momentum_12_1": "12-1 month return (Jegadeesh-Titman)",
    "residual_momentum": "12-1 month residual return vs SPY / residual vol (Blitz-Huij-Martens)",
    "reversal_1m": "lowest 1-month return first (short-term reversal)",
    "low_vol": "lowest 1-year volatility first",
    "high52": "closest to the 52-week high (George-Hwang)",
    "trend_strength": "price / 200-day average",
}


def closes(symbols: list[str], lookback: str) -> pd.DataFrame:
    def one(s):
        try:
            return s, get_bars(s, "1d", lookback=lookback).df["c"]
        except Exception:
            return s, None

    with ThreadPoolExecutor(6) as ex:
        res = list(ex.map(one, symbols))
    cols = {s: c for s, c in res if c is not None and len(c) > 60}
    if not cols:
        raise ValueError("No price history for the given symbols")
    df = pd.concat(cols, axis=1).sort_index()
    return df


def _signal(px: pd.DataFrame, i: int, factor: str, spy: pd.Series | None) -> pd.Series:
    """Factor value per symbol using rows ≤ i only. Higher = preferred."""
    if factor == "momentum_12_1":
        if i < 252:
            return pd.Series(dtype=float)
        return px.iloc[i - 21] / px.iloc[i - 252] - 1
    if factor == "reversal_1m":
        return -(px.iloc[i] / px.iloc[i - 21] - 1)
    if factor == "low_vol":
        w = px.iloc[max(0, i - 252):i + 1].pct_change(fill_method=None)
        return -w.std()
    if factor == "high52":
        return px.iloc[i] / px.iloc[max(0, i - 252):i + 1].max()
    if factor == "trend_strength":
        if i < 200:
            return pd.Series(dtype=float)
        return px.iloc[i] / px.iloc[i - 199:i + 1].mean()
    if factor == "residual_momentum":
        if i < 252 or spy is None:
            return pd.Series(dtype=float)
        r = px.iloc[i - 252:i - 20].pct_change(fill_method=None).iloc[1:]
        m = spy.iloc[i - 252:i - 20].pct_change(fill_method=None).iloc[1:].to_numpy()
        out = {}
        X = np.column_stack([np.ones(len(m)), m])
        for s in r.columns:
            y = r[s].to_numpy()
            ok = np.isfinite(y) & np.isfinite(m)
            if ok.sum() < 150:
                continue
            beta, _, resid, _ = stats.ols(y[ok], X[ok])
            sd = resid.std(ddof=1)
            out[s] = resid.sum() / sd if sd > 0 else np.nan
        return pd.Series(out, dtype=float)
    raise ValueError(f"Unknown factor '{factor}'. Known: {', '.join(FACTORS)}")


def run_factor_backtest(symbols: list[str], factor: str = "momentum_12_1", top_k: int = 5, lookback: str = "8y",
                        cost_bps: float = 10.0) -> dict:
    syms = list(dict.fromkeys(normalize_symbol(s) for s in symbols))
    if factor not in FACTORS:
        raise ValueError(f"Unknown factor '{factor}'. Known: {', '.join(FACTORS)}")
    if len(syms) < top_k + 2:
        raise ValueError(f"Need at least top_k + 2 = {top_k + 2} symbols for a ranking test (got {len(syms)})")
    px = closes(syms + ["SPY"], lookback)
    spy = px.pop("SPY") if "SPY" in px.columns and "SPY" not in syms else (px["SPY"] if "SPY" in px.columns else None)
    px = px[[s for s in syms if s in px.columns]]
    rets = px.pct_change(fill_method=None)
    dates = pd.to_datetime(np.asarray(px.index, dtype="int64"), unit="s")
    month = dates.year * 12 + dates.month
    rebal = [i for i in range(len(px) - 1) if month[i] != month[i + 1]]  # last session of each month
    warm = 252 if factor in ("momentum_12_1", "residual_momentum", "low_vol", "high52") else 200
    rebal = [i for i in rebal if i >= warm]
    if len(rebal) < 12:
        raise ValueError("Less than 12 monthly rebalances after the 1-year warm-up: use a longer lookback")
    n = len(px)
    w_top = np.zeros((n, px.shape[1]))
    w_bot = np.zeros_like(w_top)
    w_ew = np.zeros_like(w_top)
    holdings = []
    for k, i in enumerate(rebal):
        end = rebal[k + 1] if k + 1 < len(rebal) else n - 1
        sig = _signal(px, i, factor, spy).dropna()
        live = px.iloc[i].notna()
        sig = sig[[s for s in sig.index if live.get(s, False)]]
        if len(sig) < top_k + 1:
            continue
        order = sig.sort_values(ascending=False)
        top, bot = list(order.index[:top_k]), list(order.index[-top_k:])
        cols = {s: j for j, s in enumerate(px.columns)}
        for s in top:
            w_top[i:end, cols[s]] = 1 / len(top)
        for s in bot:
            w_bot[i:end, cols[s]] = 1 / len(bot)
        alive = [cols[s] for s in px.columns if live[s]]
        w_ew[i:end, alive] = 1 / len(alive)
        holdings.append({"date": format_ts(int(px.index[i]), False), "top": top})
    R = rets.to_numpy()
    R = np.where(np.isfinite(R), R, 0.0)

    def path(w):
        held = w[:-1]
        gross = (held * R[1:]).sum(axis=1)
        turn = np.abs(np.diff(np.vstack([np.zeros(w.shape[1]), held]), axis=0)).sum(axis=1)
        return gross - turn * cost_bps / 1e4

    s0 = rebal[0]
    top_r, bot_r, ew_r = path(w_top)[s0:], path(w_bot)[s0:], path(w_ew)[s0:]
    spy_r = spy.pct_change().to_numpy()[1:][s0:] if spy is not None else None
    excess = top_r - ew_r
    d = [format_ts(int(t), False) for t in px.index[1:]][s0:]
    cur_sig = _signal(px, n - 1, factor, spy).dropna().sort_values(ascending=False)
    step = max(1, len(d) // 300)
    idx = list(range(0, len(d), step)) + ([len(d) - 1] if (len(d) - 1) % step else [])
    eq = lambda r: np.cumprod(1 + r) * 100  # noqa: E731
    out = {
        "factor": factor, "description": FACTORS[factor], "symbols": list(px.columns), "top_k": top_k,
        "period": {"start": d[0], "end": d[-1]}, "rebalances": len(holdings), "cost_bps": cost_bps,
        "top_k_portfolio": stats.perf_stats(top_r), "bottom_k_portfolio": stats.perf_stats(bot_r),
        "equal_weight_list": stats.perf_stats(ew_r),
        "spy": stats.perf_stats(spy_r) if spy_r is not None else None,
        "excess_vs_equal_weight": {
            "annual_pct": round(float(excess.mean() * 252 * 100), 2),
            "tracking_error_pct": round(float(excess.std(ddof=1) * math.sqrt(252) * 100), 2),
            "information_ratio": round(float(excess.mean() / excess.std(ddof=1) * math.sqrt(252)), 2) if excess.std() > 0 else None,
            "psr": round(stats.probabilistic_sharpe(excess), 3),
            "monthly_hit_rate_pct": round(float((pd.Series(excess, index=pd.to_datetime(d)).resample("ME").sum() > 0).mean() * 100), 1),
        },
        "current_ranking": [{"symbol": s, "signal": round(float(v), 4), "selected": i < top_k}
                            for i, (s, v) in enumerate(cur_sig.items())],
        "recent_holdings": holdings[-6:],
        "equity_curve": {"dates": [d[i] for i in idx], "top_k": [round(float(eq(top_r)[i]), 2) for i in idx],
                         "equal_weight": [round(float(eq(ew_r)[i]), 2) for i in idx]},
        "notes": ["Survivorship: the list is chosen today, so all its histories are of survivors; judge the "
                  "top-k against the equal-weighted list, not against cash or SPY.",
                  "Lists below ~20 names make ranking tests noisy; prefer factor_screen on the full universe "
                  "for factor evidence and use this to see how the factor would have picked within your list."],
    }
    return out
