"""Portfolio construction and hedging.

optimize_allocation: equal weight, inverse volatility, equal risk contribution (risk parity), long-only
minimum variance (Ledoit-Wolf shrunk covariance) and hierarchical risk parity (López de Prado 2016).
get_hedge_plan: market beta of a set of holdings (or the connected broker account), the SPY hedge that
neutralises it, and a minimum-variance hedge on chosen ETFs with the variance it removes."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from ..market import get_quote, normalize_symbol
from . import stats
from .xsection import closes

METHODS = ("equal", "inverse_vol", "risk_parity", "min_variance", "hrp")


def _r(x, nd=4):
    if x is None:
        return None
    x = float(x)
    return round(x, nd) if math.isfinite(x) else None


# ----------------------------------------------------------------------------- weights


def _erc(cov: np.ndarray, iters: int = 500) -> np.ndarray:
    """Equal risk contribution by cyclical coordinate descent (Griveau-Billion, Richard & Roncalli 2013)."""
    n = len(cov)
    w = 1 / np.sqrt(np.diag(cov))
    w /= w.sum()
    b = np.full(n, 1 / n)
    for _ in range(iters):
        prev = w.copy()
        for i in range(n):
            c = cov[i] @ w - cov[i, i] * w[i]
            w[i] = (-c + math.sqrt(c * c + 4 * cov[i, i] * b[i])) / (2 * cov[i, i])
        if np.abs(w - prev).max() < 1e-10:
            break
    return w / w.sum()


def _min_var(cov: np.ndarray) -> np.ndarray:
    """Long-only minimum variance: solve the unconstrained problem on the active set, drop the most negative weight, repeat."""
    n = len(cov)
    active = list(range(n))
    while True:
        sub = cov[np.ix_(active, active)]
        try:
            x = np.linalg.solve(sub, np.ones(len(active)))
        except np.linalg.LinAlgError:
            x = np.linalg.lstsq(sub, np.ones(len(active)), rcond=None)[0]
        w = x / x.sum()
        if (w >= -1e-12).all() or len(active) == 1:
            out = np.zeros(n)
            out[active] = np.clip(w, 0, None)
            return out / out.sum()
        active.pop(int(np.argmin(w)))


def _hrp(cov: np.ndarray) -> np.ndarray:
    """Hierarchical risk parity: single-linkage tree on correlation distance, quasi-diagonal order,
    recursive bisection with inverse-variance cluster weights."""
    sd = np.sqrt(np.diag(cov))
    corr = np.clip(cov / np.outer(sd, sd), -1, 1)
    dist = np.sqrt(0.5 * (1 - corr))
    clusters = {i: [i] for i in range(len(cov))}
    d = {(i, j): dist[i, j] for i in clusters for j in clusters if i < j}
    nxt = len(cov)
    while len(clusters) > 1:
        (i, j), _ = min(d.items(), key=lambda kv: kv[1])
        merged = clusters.pop(i) + clusters.pop(j)
        d = {k: v for k, v in d.items() if i not in k and j not in k}
        for k, members in clusters.items():
            d[(min(k, nxt), max(k, nxt))] = min(dist[a, b] for a in merged for b in members)  # single linkage
        clusters[nxt] = merged
        nxt += 1
    order = next(iter(clusters.values()))

    def cvar(items):
        sub = cov[np.ix_(items, items)]
        ivp = 1 / np.diag(sub)
        ivp /= ivp.sum()
        return float(ivp @ sub @ ivp)

    w = np.ones(len(cov))
    stack = [order]
    while stack:
        items = stack.pop()
        if len(items) < 2:
            continue
        half = len(items) // 2
        left, right = items[:half], items[half:]
        vl, vr = cvar(left), cvar(right)
        alpha = 1 - vl / (vl + vr)
        w[left] *= alpha
        w[right] *= 1 - alpha
        stack += [left, right]
    return w / w.sum()


def _weights(method: str, cov: np.ndarray) -> np.ndarray:
    n = len(cov)
    if method == "equal":
        return np.full(n, 1 / n)
    if method == "inverse_vol":
        w = 1 / np.sqrt(np.diag(cov))
        return w / w.sum()
    if method == "risk_parity":
        return _erc(cov)
    if method == "min_variance":
        return _min_var(cov)
    if method == "hrp":
        return _hrp(cov)
    raise ValueError(f"Unknown method '{method}'. Use one of {', '.join(METHODS)}")


def _cap(w: np.ndarray, cap: float | None) -> np.ndarray:
    if not cap or cap >= 1:
        return w
    if cap * len(w) < 1:
        raise ValueError(f"max_weight {cap} is infeasible for {len(w)} assets (needs ≥ {1 / len(w):.3f})")
    w = w.copy()
    for _ in range(100):
        over = w > cap + 1e-12
        if not over.any():
            break
        excess = (w[over] - cap).sum()
        w[over] = cap
        free = ~over & (w < cap)
        w[free] += excess * w[free] / w[free].sum()
    return w


def _describe(w: np.ndarray, cov: np.ndarray) -> dict:
    vol = math.sqrt(float(w @ cov @ w))
    mrc = cov @ w
    rc = w * mrc / (vol**2) if vol > 0 else np.zeros_like(w)
    sd = np.sqrt(np.diag(cov))
    return {"vol_pct": _r(vol * math.sqrt(252) * 100, 2), "diversification_ratio": _r(float(w @ sd) / vol, 3) if vol > 0 else None,
            "effective_n": _r(1 / float((w**2).sum()), 2), "risk_contrib": rc}


def optimize_allocation(symbols: list[str], method: str = "risk_parity", lookback: str = "1y", max_weight: float | None = None,
                        capital: float | None = None) -> dict:
    syms = list(dict.fromkeys(normalize_symbol(s) for s in symbols))
    if len(syms) < 2:
        raise ValueError("Give at least two symbols")
    if len(syms) > 40:
        raise ValueError("At most 40 symbols")
    px = closes(syms, lookback)
    R = px.pct_change(fill_method=None).dropna(how="all").dropna(axis=1, thresh=60).dropna()
    if len(R) < 60:
        raise ValueError(f"Only {len(R)} common return days; need 60+")
    names = list(R.columns)
    cov, shrink = stats.ledoit_wolf(R.to_numpy())
    compare = {}
    for m in METHODS:
        w = _cap(_weights(m, cov), max_weight)
        d = _describe(w, cov)
        compare[m] = {"vol_pct": d["vol_pct"], "diversification_ratio": d["diversification_ratio"], "effective_n": d["effective_n"]}
    w = _cap(_weights(method, cov), max_weight)
    d = _describe(w, cov)
    vols = np.sqrt(np.diag(cov)) * math.sqrt(252) * 100
    last = px.iloc[-1]
    rows = []
    for i, s in enumerate(names):
        row = {"symbol": s, "weight_pct": _r(w[i] * 100, 2), "vol_pct": _r(vols[i], 1),
               "risk_contribution_pct": _r(d["risk_contrib"][i] * 100, 1)}
        if capital:
            row["dollars"] = _r(capital * w[i], 2)
            row["shares"] = math.floor(capital * w[i] / float(last[s])) if last[s] > 0 else None
        rows.append(row)
    rows.sort(key=lambda r: -(r["weight_pct"] or 0))
    corr = R.corr().to_numpy()
    avg_corr = (corr.sum() - len(corr)) / (len(corr) ** 2 - len(corr))
    dropped = [s for s in syms if s not in names]
    return {"method": method, "lookback": lookback, "days": len(R), "shrinkage": _r(shrink, 3),
            "portfolio": {"vol_pct": d["vol_pct"], "diversification_ratio": d["diversification_ratio"],
                          "effective_n": d["effective_n"], "average_correlation": _r(avg_corr, 2)},
            "weights": rows, "compare_methods": compare, "dropped": dropped,
            "notes": ["Risk-based weights ignore expected returns on purpose: return estimates are the noisiest input.",
                      "Covariance: Ledoit-Wolf shrinkage toward a scaled identity (shrinkage shown).",
                      "Rebalance monthly or when weights drift > 5 points; correlations rise in sell-offs, so realised "
                      "diversification is lower in a crash than ex-ante."]}


# ----------------------------------------------------------------------------- hedging


def _positions_from_broker() -> dict[str, float]:
    from ..trading import trading

    ov = trading.overview()
    pos = {p["symbol"]: float(p["market_value"] or 0) for p in ov.get("positions", []) if p.get("market_value")}
    if not pos:
        raise ValueError("No open positions in the connected account; pass positions={symbol: dollar value}")
    return pos


def hedge_plan(positions: dict[str, float] | None = None, instruments: list[str] | None = None, lookback: str = "1y") -> dict:
    pos = {normalize_symbol(k): float(v) for k, v in (positions or _positions_from_broker()).items() if v}
    if not pos:
        raise ValueError("positions must contain at least one non-zero dollar value")
    inst = [normalize_symbol(s) for s in (instruments or ["SPY"])]
    px = closes(list(pos) + ["SPY"] + inst, lookback)
    R = px.pct_change(fill_method=None).iloc[1:]
    spy = R["SPY"]
    gross = sum(abs(v) for v in pos.values())
    rows, dollar_beta = [], 0.0
    for s, v in pos.items():
        if s not in R.columns:
            rows.append({"symbol": s, "value": v, "beta": None, "note": "no data"})
            continue
        j = pd.concat([R[s], spy], axis=1).dropna()
        b = float(j.iloc[:, 0].cov(j.iloc[:, 1]) / j.iloc[:, 1].var()) if len(j) > 30 else float("nan")
        b6 = float(j.iloc[-126:, 0].cov(j.iloc[-126:, 1]) / j.iloc[-126:, 1].var()) if len(j) > 126 else None
        dollar_beta += v * (b if math.isfinite(b) else 1.0)
        rows.append({"symbol": s, "value": _r(v, 2), "weight_pct": _r(v / gross * 100, 1), "beta": _r(b, 2),
                     "beta_6m": _r(b6, 2), "dollar_beta": _r(v * b, 2), "correlation_spy": _r(j.iloc[:, 0].corr(j.iloc[:, 1]), 2)})
    have = [s for s in pos if s in R.columns]
    port = (R[have] * pd.Series({s: pos[s] for s in have})).sum(axis=1) / gross  # return on gross
    spy_px = float(px["SPY"].dropna().iloc[-1])
    try:
        spy_px = float(get_quote("SPY").get("price") or spy_px)
    except Exception:
        pass
    vol = float(port.std() * math.sqrt(252))
    hedged_beta = port - (dollar_beta / gross) * spy
    out = {
        "positions": rows, "gross_value": _r(gross, 2), "net_value": _r(sum(pos.values()), 2),
        "portfolio_beta": _r(dollar_beta / gross, 3), "dollar_beta": _r(dollar_beta, 2),
        "beta_hedge": {"instrument": "SPY", "notional": _r(-dollar_beta, 2), "shares": -round(dollar_beta / spy_px),
                       "spy_price": _r(spy_px, 2),
                       "vol_before_pct": _r(vol * 100, 2), "vol_after_pct": _r(hedged_beta.std() * math.sqrt(252) * 100, 2),
                       "alternatives": [
                           f"Inverse ETF: buy ≈ ${abs(dollar_beta):,.0f} of SH (1x inverse S&P 500; daily-reset, fine for weeks, drifts over months).",
                           "Protective puts on SPY (cost = premium; caps downside without capping upside); size so the "
                           f"put delta × 100 × contracts ≈ {abs(dollar_beta) / spy_px:,.0f} SPY shares.",
                           "Partial hedge: hedge 50% of dollar beta to keep some equity premium."]},
    }
    # minimum-variance hedge on the chosen instruments (regression of portfolio returns on instrument returns)
    X = R[[i for i in inst if i in R.columns]]
    j = pd.concat([port.rename("p"), X], axis=1).dropna()
    if len(j) > 60 and X.shape[1]:
        beta, se, resid, r2 = stats.ols(j["p"].to_numpy(), np.column_stack([np.ones(len(j)), j[X.columns].to_numpy()]))
        hedge = [{"instrument": c, "hedge_ratio": _r(beta[i + 1], 3), "t": _r(beta[i + 1] / se[i + 1], 1) if se[i + 1] > 0 else None,
                  "notional": _r(-beta[i + 1] * gross, 2), "shares": -round(beta[i + 1] * gross / float(px[c].dropna().iloc[-1]))}
                 for i, c in enumerate(X.columns)]
        out["min_variance_hedge"] = {"instruments": hedge, "r2": _r(r2, 3),
                                     "vol_after_pct": _r(resid.std(ddof=1) * math.sqrt(252) * 100, 2),
                                     "variance_removed_pct": _r(r2 * 100, 1)}
    out["notes"] = ["Betas from daily returns over the lookback; they drift (compare beta_6m) and rise in sell-offs.",
                    "A beta hedge removes market risk and also the market's expected return: hedge when the thesis is "
                    "stock-specific or the regime is hostile (market-regime skill), not by default.",
                    "Shorting SPY needs margin and shorting enabled in Trading → Risk; the agent cannot change limits."]
    return out
