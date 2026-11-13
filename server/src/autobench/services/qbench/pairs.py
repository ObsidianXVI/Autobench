"""Pairs trading / statistical arbitrage: scan a list for cointegrated pairs and backtest one pair.

Spread = log(A) - β·log(B) - α. The backtest re-estimates α, β, the spread's mean/std and half-life
every 21 bars from the trailing `formation` bars only (no look-ahead), trades the z-score:
  z < -entry  → long spread (buy A, sell β-weighted B)      z > +entry → short spread
  exit when z crosses exit_z toward 0; stop at |z| > stop_z; time stop after 3 half-lives. No entry beyond
  the stop, and after a stop/time stop no re-entry until |z| is back inside the entry band.
Legs are sized so gross exposure = 1 (weights 1/(1+|β|) and β/(1+|β|)); costs per unit turnover."""

from __future__ import annotations

import itertools
import math

import numpy as np
import pandas as pd

from ..market import normalize_symbol
from ...timeutil import format_ts
from . import stats
from .xsection import closes


def _r(x, nd=3):
    if x is None:
        return None
    x = float(x)
    return round(x, nd) if math.isfinite(x) else None


def _p(x) -> float:
    return 1.0 if x is None else float(x)  # NOT `x or 1`: a p-value of exactly 0.0 is the strongest result


def _pair_stats(la: np.ndarray, lb: np.ndarray, max_lag: int = 4) -> dict:
    beta, _, resid, r2 = stats.ols(la, np.column_stack([np.ones(len(lb)), lb]))
    t = stats.adf(resid, max_lag=max_lag, n_series=2)
    sd = resid.std(ddof=1)
    return {"alpha": float(beta[0]), "beta": float(beta[1]), "r2": r2, "p_value": t["p_value"], "adf": t["stat"],
            "half_life": stats.half_life(resid), "z_now": float((resid[-1] - resid.mean()) / sd) if sd > 0 else float("nan"),
            "spread_sd": float(sd)}


def find_pairs(symbols: list[str], lookback: str = "2y", min_corr: float = 0.6, limit: int = 20) -> dict:
    syms = list(dict.fromkeys(normalize_symbol(s) for s in symbols))
    if len(syms) < 2:
        raise ValueError("Give at least two symbols")
    if len(syms) > 60:
        raise ValueError("At most 60 symbols per scan (1,770 pairs)")
    px = closes(syms, lookback).dropna(axis=1, thresh=200).ffill().dropna()
    if len(px) < 200:
        raise ValueError(f"Only {len(px)} common trading days; need 200+")
    lp = np.log(px)
    corr = px.pct_change().corr()
    found, tested = [], 0
    for a, b in itertools.combinations(px.columns, 2):
        c = corr.loc[a, b]
        if not np.isfinite(c) or c < min_corr:
            continue
        tested += 1
        s1 = _pair_stats(lp[a].to_numpy(), lp[b].to_numpy())
        s2 = _pair_stats(lp[b].to_numpy(), lp[a].to_numpy())
        (y, x, s) = (a, b, s1) if _p(s1["p_value"]) <= _p(s2["p_value"]) else (b, a, s2)
        hl = s["half_life"]
        tradeable = _p(s["p_value"]) < 0.05 and math.isfinite(hl) and 2 <= hl <= 60
        z = s["z_now"]
        found.append({"a": y, "b": x, "correlation": _r(c, 2), "p_value": _r(s["p_value"], 4), "hedge_ratio": _r(s["beta"], 3),
                      "half_life_days": _r(hl, 1), "z_now": _r(z, 2), "tradeable": bool(tradeable),
                      "signal": ("long spread (buy {0}, sell {1})".format(y, x) if tradeable and z < -2 else
                                 "short spread (sell {0}, buy {1})".format(y, x) if tradeable and z > 2 else None)})
    found.sort(key=lambda r: (not r["tradeable"], _p(r["p_value"])))
    n_pairs = len(px.columns) * (len(px.columns) - 1) // 2
    return {"symbols": list(px.columns), "lookback": lookback, "days": len(px), "pairs_total": n_pairs,
            "pairs_tested": tested, "min_corr": min_corr, "pairs": found[:limit],
            "notes": [f"Multiple testing: with {tested} pairs tested, ~{max(0, round(tested * 0.05))} would show p < 0.05 "
                      "by chance. Prefer pairs with an economic link (same industry, share classes, supplier/customer) "
                      "and confirm with analyze_pair's rolling out-of-sample backtest.",
                      "tradeable = Engle-Granger p < 0.05 and half-life between 2 and 60 days."]}


def analyze_pair(a: str, b: str, lookback: str = "5y", formation: int = 252, entry_z: float = 2.0, exit_z: float = 0.0,
                 stop_z: float = 4.0, cost_bps: float = 10.0, notional: float | None = None) -> dict:
    a, b = normalize_symbol(a), normalize_symbol(b)
    if a == b:
        raise ValueError("Pick two different symbols")
    if not (0 <= exit_z < entry_z < stop_z):
        raise ValueError("Need 0 ≤ exit_z < entry_z < stop_z")
    px = closes([a, b], lookback).ffill().dropna()
    if a not in px.columns or b not in px.columns:
        raise ValueError(f"No common history for {a} and {b}")
    if len(px) < formation + 120:
        raise ValueError(f"{len(px)} common days; need formation ({formation}) + 120")
    la, lb = np.log(px[a].to_numpy()), np.log(px[b].to_numpy())
    ra, rb = np.r_[0, np.diff(px[a].to_numpy()) / px[a].to_numpy()[:-1]], np.r_[0, np.diff(px[b].to_numpy()) / px[b].to_numpy()[:-1]]
    n = len(px)
    z = np.full(n, np.nan)
    beta_s = np.full(n, np.nan)
    hl_s = np.full(n, np.nan)
    est = None
    for i in range(formation, n):
        if est is None or (i - formation) % 21 == 0:
            w = slice(i - formation, i)
            coef, _, resid, _ = stats.ols(la[w], np.column_stack([np.ones(formation), lb[w]]))
            est = (coef[0], coef[1], resid.mean(), resid.std(ddof=1), stats.half_life(resid))
        al, be, mu, sd, hl = est
        z[i] = ((la[i] - be * lb[i] - al) - mu) / sd if sd > 0 else np.nan
        beta_s[i], hl_s[i] = be, hl
    pos = np.zeros(n)
    wa = np.zeros(n)
    wb = np.zeros(n)
    trades = []
    cur, opened, t_open = 0, 0, None
    blocked = False  # after a stop: no new entry until |z| is back inside the entry band (same divergence)
    for i in range(formation, n):
        zi = z[i]
        if not np.isfinite(zi):
            pos[i] = cur
            continue
        hl = hl_s[i] if np.isfinite(hl_s[i]) else 30
        reason = None
        if cur == 0:
            if blocked and abs(zi) < entry_z:
                blocked = False
            if blocked or abs(zi) >= stop_z:  # never open beyond the stop
                pass
            elif zi < -entry_z:
                cur, opened, t_open = 1, i, zi
            elif zi > entry_z:
                cur, opened, t_open = -1, i, zi
        else:
            if (cur == 1 and zi >= -exit_z) or (cur == -1 and zi <= exit_z):
                reason = "reverted"
            elif abs(zi) > stop_z:
                reason = "stop"
            elif i - opened > 3 * min(max(hl, 2), 60):
                reason = "time"
            if reason:
                blocked = reason in ("stop", "time")
                trades.append({"open_i": opened, "close_i": i, "side": "long spread" if cur == 1 else "short spread",
                               "z_open": t_open, "z_close": zi, "reason": reason})
                cur = 0
        pos[i] = cur
        be = beta_s[i]
        wa[i], wb[i] = 1 / (1 + abs(be)), -be / (1 + abs(be))
    # returns: exposure decided at close t over bar t+1
    held_a, held_b = (pos * wa)[:-1], (pos * wb)[:-1]
    gross = held_a * ra[1:] + held_b * rb[1:]
    turn = np.abs(np.diff(np.r_[0, held_a])) + np.abs(np.diff(np.r_[0, held_b]))
    rets = (gross - turn * cost_bps / 1e4)[formation:]
    dates = [format_ts(int(t), False) for t in px.index]
    tr_out = []
    for t in trades:
        seg = rets[t["open_i"] - formation:t["close_i"] - formation]
        tr_out.append({"open": dates[t["open_i"]], "close": dates[t["close_i"]], "side": t["side"],
                       "z_open": _r(t["z_open"], 2), "z_close": _r(t["z_close"], 2), "reason": t["reason"],
                       "days": t["close_i"] - t["open_i"], "return_pct": _r((np.prod(1 + seg) - 1) * 100, 2)})
    full = _pair_stats(la[-formation:], lb[-formation:], max_lag=6)
    half = formation // 2
    b1 = stats.ols(la[-formation:-half], np.column_stack([np.ones(formation - half), lb[-formation:-half]]))[0][1]
    b2 = stats.ols(la[-half:], np.column_stack([np.ones(half), lb[-half:]]))[0][1]
    closed = np.array([t["return_pct"] for t in tr_out if t["return_pct"] is not None])
    zn = z[-1]
    be = beta_s[-1]
    signal = ("long spread" if -stop_z < zn < -entry_z else "short spread" if entry_z < zn < stop_z else None)
    if blocked and signal:
        signal = None  # still the divergence that stopped out the last trade
    if cur != 0:
        signal = f"holding {'long' if cur == 1 else 'short'} spread since {dates[opened]}"
    out = {
        "a": a, "b": b, "period": {"start": dates[formation], "end": dates[-1]},
        "settings": {"formation_days": formation, "entry_z": entry_z, "exit_z": exit_z, "stop_z": stop_z, "cost_bps": cost_bps},
        "current": {"z": _r(zn, 2), "hedge_ratio": _r(be, 3), "half_life_days": _r(hl_s[-1], 1), "signal": signal,
                    "cointegration_p_last_year": _r(full["p_value"], 4),
                    "hedge_ratio_first_vs_second_half": [_r(b1, 3), _r(b2, 3)],
                    "correlation_1y": _r(np.corrcoef(ra[-252:], rb[-252:])[0, 1], 2),
                    "prices": {a: _r(px[a].iloc[-1], 4), b: _r(px[b].iloc[-1], 4)}},
        "performance": stats.perf_stats(rets, exposure=pos[formation:-1]),
        "trades": {"count": len(tr_out), "win_rate_pct": _r((closed > 0).mean() * 100, 1) if len(closed) else None,
                   "avg_return_pct": _r(closed.mean(), 3) if len(closed) else None,
                   "stops": sum(t["reason"] == "stop" for t in tr_out), "time_stops": sum(t["reason"] == "time" for t in tr_out)},
        "recent_trades": tr_out[-12:],
        "zscore_series": {"dates": dates[-min(n - formation, 504):], "z": [_r(v, 3) for v in z[-min(n - formation, 504):]]},
        "equity_curve": {"dates": dates[formation + 1:][:: max(1, len(rets) // 300)],
                         "pair": [_r(v * 100, 2) for v in np.cumprod(1 + rets)[:: max(1, len(rets) // 300)]]},
    }
    if notional and signal and cur == 0 and np.isfinite(be):
        sa, sb = 1 / (1 + abs(be)), abs(be) / (1 + abs(be))
        side = 1 if zn < 0 else -1
        out["current"]["legs"] = [
            {"symbol": a, "action": "buy" if side == 1 else "sell short", "shares": math.floor(notional * sa / px[a].iloc[-1])},
            {"symbol": b, "action": ("sell short" if side == 1 else "buy") if be > 0 else ("buy" if side == 1 else "sell short"),
             "shares": math.floor(notional * sb / px[b].iloc[-1])}]
    warn = []
    if _p(full["p_value"]) > 0.1:
        warn.append(f"Not cointegrated over the last year (p={_r(full['p_value'], 3)}): the spread may not mean-revert.")
    if b1 and abs(b2 - b1) / max(abs(b1), 1e-9) > 0.3:
        warn.append("Hedge ratio moved > 30% between half-years: unstable relationship.")
    if len(tr_out) < 10:
        warn.append(f"Only {len(tr_out)} round trips: anecdotal.")
    warn.append("Needs a short leg. Without shorting: use the z-score to time buying the cheap leg only "
                "(a mean-reversion entry), or hedge with the sector ETF instead.")
    out["warnings"] = warn
    return out
