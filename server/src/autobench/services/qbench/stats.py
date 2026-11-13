"""Statistics for QBench: performance ratios with their sampling error (PSR, Deflated Sharpe, MinTRL),
stationarity / mean-reversion diagnostics (ADF with MacKinnon p-values, Engle-Granger, variance ratio,
Hurst exponent, Ornstein-Uhlenbeck half-life), OLS helpers and Ledoit-Wolf covariance shrinkage.

numpy only (no scipy/statsmodels). Formulas follow the cited papers; each is unit-tested against a
known case in tests/test_qbench.py."""

from __future__ import annotations

import math
from statistics import NormalDist

import numpy as np

_N = NormalDist()
EULER_GAMMA = 0.5772156649015329


def ncdf(x: float) -> float:
    return _N.cdf(x)


def ninv(p: float) -> float:
    return _N.inv_cdf(min(max(p, 1e-12), 1 - 1e-12))


def _r(x, nd: int = 3):
    if x is None:
        return None
    x = float(x)
    return round(x, nd) if math.isfinite(x) else None


# ----------------------------------------------------------------------------- performance


def moments(r: np.ndarray) -> tuple[float, float, float, float]:
    """mean, std (ddof=1), skew, kurtosis (NOT excess; normal = 3)."""
    r = np.asarray(r, float)
    r = r[np.isfinite(r)]
    if len(r) < 3:
        return float("nan"), float("nan"), float("nan"), float("nan")
    m, s = r.mean(), r.std(ddof=1)
    if s == 0:
        return m, 0.0, 0.0, 3.0
    z = (r - m) / r.std(ddof=0)
    return float(m), float(s), float((z**3).mean()), float((z**4).mean())


def sharpe(r: np.ndarray, periods: float = 252) -> float:
    m, s, _, _ = moments(r)
    return m / s * math.sqrt(periods) if s and math.isfinite(s) and s > 0 else float("nan")


def _sr_denominator(sr: float, skew: float, kurt: float) -> float:
    return max(1e-12, 1 - skew * sr + (kurt - 1) / 4 * sr**2)


def probabilistic_sharpe(r: np.ndarray, sr_benchmark: float = 0.0) -> float:
    """PSR (Bailey & López de Prado 2012): P(true per-period Sharpe > sr_benchmark) given the sample,
    adjusting for track length, skewness and fat tails. sr_benchmark is per period (not annualised)."""
    r = np.asarray(r, float)
    r = r[np.isfinite(r)]
    m, s, sk, ku = moments(r)
    if not (s > 0) or len(r) < 10:
        return float("nan")
    sr = m / s
    z = (sr - sr_benchmark) * math.sqrt(len(r) - 1) / math.sqrt(_sr_denominator(sr, sk, ku))
    return ncdf(z)


def expected_max_sharpe(n_trials: int, sr_var: float) -> float:
    """E[max SR] of n_trials unskilled strategies whose Sharpe ratios have variance sr_var
    (Bailey & López de Prado 2014, the 'False Strategy' theorem)."""
    if n_trials <= 1 or sr_var <= 0:
        return 0.0
    return math.sqrt(sr_var) * ((1 - EULER_GAMMA) * ninv(1 - 1 / n_trials) + EULER_GAMMA * ninv(1 - 1 / (n_trials * math.e)))


def deflated_sharpe(r: np.ndarray, n_trials: int, trial_sr_var: float) -> float:
    """DSR: PSR against the Sharpe the best of `n_trials` tries would show by luck alone. Per-period units.
    DSR > 0.95 ≈ significant after accounting for the search; < 0.5 = indistinguishable from data mining."""
    return probabilistic_sharpe(r, expected_max_sharpe(n_trials, trial_sr_var))


def min_track_record(r: np.ndarray, sr_benchmark: float = 0.0, confidence: float = 0.95) -> float:
    """MinTRL: observations needed before the observed Sharpe beats sr_benchmark at `confidence`."""
    m, s, sk, ku = moments(r)
    if not (s > 0):
        return float("nan")
    sr = m / s
    if sr <= sr_benchmark:
        return float("inf")
    return 1 + _sr_denominator(sr, sk, ku) * (ninv(confidence) / (sr - sr_benchmark)) ** 2


def drawdown(equity: np.ndarray) -> tuple[float, int]:
    """(max drawdown as a negative fraction, longest underwater stretch in bars)."""
    eq = np.asarray(equity, float)
    if len(eq) == 0:
        return 0.0, 0
    peak = np.maximum.accumulate(eq)
    dd = eq / peak - 1
    under = dd < -1e-12
    longest = cur = 0
    for u in under:
        cur = cur + 1 if u else 0
        longest = max(longest, cur)
    return float(dd.min()), longest


def perf_stats(r: np.ndarray, periods: float = 252, exposure: np.ndarray | None = None) -> dict:
    """Return-series summary: CAGR, vol, Sharpe, Sortino, Calmar, max DD, skew, kurtosis, hit rate, PSR."""
    r = np.asarray(r, float)
    r = np.where(np.isfinite(r), r, 0.0)
    n = len(r)
    if n < 2:
        return {"bars": n}
    eq = np.cumprod(1 + r)
    years = n / periods
    total = eq[-1] - 1
    cagr = (eq[-1]) ** (1 / years) - 1 if years > 0 and eq[-1] > 0 else float("nan")
    m, s, sk, ku = moments(r)
    down = r[r < 0]
    dsd = math.sqrt((down**2).sum() / n) if len(down) else float("nan")
    mdd, under = drawdown(eq)
    active = r[r != 0]
    out = {
        "bars": n, "years": _r(years, 2),
        "total_return_pct": _r(total * 100, 2), "cagr_pct": _r(cagr * 100, 2),
        "vol_pct": _r(s * math.sqrt(periods) * 100, 2),
        "sharpe": _r(m / s * math.sqrt(periods), 2) if s > 0 else None,
        "sortino": _r(m / dsd * math.sqrt(periods), 2) if dsd and dsd > 0 else None,
        "max_drawdown_pct": _r(mdd * 100, 2), "longest_underwater_bars": under,
        "calmar": _r(cagr / abs(mdd), 2) if mdd < 0 and math.isfinite(cagr) else None,
        "skew": _r(sk, 2), "kurtosis": _r(ku, 2),
        "hit_rate_pct": _r((active > 0).mean() * 100, 1) if len(active) else None,
        "psr": _r(probabilistic_sharpe(r), 3),
    }
    if exposure is not None:
        e = np.abs(np.asarray(exposure, float))
        out["exposure_pct"] = _r(np.nanmean(e > 1e-9) * 100, 1)
        out["avg_leverage"] = _r(np.nanmean(e), 2)
    return out


# ----------------------------------------------------------------------------- regression


def ols(y: np.ndarray, X: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray, float]:
    """OLS with classical standard errors: (beta, se, residuals, r2). X must include a constant if wanted."""
    y = np.asarray(y, float)
    X = np.asarray(X, float)
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    resid = y - X @ beta
    n, k = X.shape
    dof = max(1, n - k)
    s2 = float(resid @ resid) / dof
    try:
        cov = s2 * np.linalg.inv(X.T @ X)
        se = np.sqrt(np.clip(np.diag(cov), 0, None))
    except np.linalg.LinAlgError:
        se = np.full(k, np.nan)
    tss = float(((y - y.mean()) ** 2).sum())
    r2 = 1 - float(resid @ resid) / tss if tss > 0 else float("nan")
    return beta, se, resid, r2


# ----------------------------------------------------------------------------- stationarity

# MacKinnon (1994) response-surface coefficients for approximate ADF p-values, constant-only regression,
# as used by statsmodels' mackinnonp. Index = number of integrated series (1 = ADF, 2 = Engle-Granger pair).
_TAU_MAX = {1: 2.74, 2: 0.92}
_TAU_MIN = {1: -18.83, 2: -18.86}
_TAU_STAR = {1: -1.61, 2: -2.62}
_SMALLP = {1: (2.1659, 1.4412, 0.038269), 2: (2.92, 1.5012, 0.039796)}
_LARGEP = {1: (1.7339, 0.93202, -0.12745, -0.010368), 2: (2.1945, 0.64695, -0.29198, -0.042377)}
ADF_CRIT = {1: {"1%": -3.43, "5%": -2.86, "10%": -2.57}, 2: {"1%": -3.90, "5%": -3.34, "10%": -3.04}}


def mackinnon_p(tau: float, n_series: int = 1) -> float:
    if not math.isfinite(tau):
        return float("nan")
    if tau > _TAU_MAX[n_series]:
        return 1.0
    if tau < _TAU_MIN[n_series]:
        return 0.0
    coef = _SMALLP[n_series] if tau <= _TAU_STAR[n_series] else _LARGEP[n_series]
    return ncdf(sum(c * tau**i for i, c in enumerate(coef)))


def adf(x: np.ndarray, max_lag: int | None = None, n_series: int = 1) -> dict:
    """Augmented Dickey-Fuller test with a constant; lag order chosen by AIC.
    H0: unit root (random walk). Small p-value = stationary / mean-reverting."""
    x = np.asarray(x, float)
    x = x[np.isfinite(x)]
    n = len(x)
    if n < 30:
        return {"stat": None, "p_value": None, "lags": None, "n": n}
    if max_lag is None:
        max_lag = min(12, int(12 * (n / 100) ** 0.25))
    dx = np.diff(x)
    best = None
    for lag in range(0, max_lag + 1):
        y = dx[max_lag:]
        cols = [np.ones(len(y)), x[max_lag:-1]]
        for i in range(1, lag + 1):
            cols.append(dx[max_lag - i : len(dx) - i])
        X = np.column_stack(cols)
        beta, se, resid, _ = ols(y, X)
        nobs = len(y)
        aic = nobs * math.log(max(float(resid @ resid) / nobs, 1e-300)) + 2 * X.shape[1]
        if best is None or aic < best[0]:
            best = (aic, lag, beta[1] / se[1] if se[1] > 0 else float("nan"))
    _, lag, tau = best
    return {"stat": _r(tau, 3), "p_value": _r(mackinnon_p(tau, n_series), 4), "lags": lag, "n": n,
            "critical": ADF_CRIT[n_series]}


def half_life(x: np.ndarray) -> float:
    """Ornstein-Uhlenbeck half-life in bars from Δx_t = a + λ x_{t-1}: ln2 / -λ (inf if not mean-reverting)."""
    x = np.asarray(x, float)
    x = x[np.isfinite(x)]
    if len(x) < 20:
        return float("nan")
    beta, *_ = ols(np.diff(x), np.column_stack([np.ones(len(x) - 1), x[:-1]]))
    lam = beta[1]
    return -math.log(2) / lam if lam < 0 else float("inf")


def variance_ratio(logp: np.ndarray, q: int) -> dict:
    """Lo-MacKinlay (1988) variance ratio VR(q) with the heteroskedasticity-robust z*. VR < 1 = mean
    reversion at horizon q, > 1 = trending (momentum); |z| > 1.96 is significant at 5%."""
    p = np.asarray(logp, float)
    p = p[np.isfinite(p)]
    r = np.diff(p)
    n = len(r)
    if n < q * 10:
        return {"q": q, "vr": None, "z": None}
    mu = r.mean()
    var1 = ((r - mu) ** 2).sum() / (n - 1)
    rq = p[q:] - p[:-q]
    m = q * (n - q + 1) * (1 - q / n)
    varq = ((rq - q * mu) ** 2).sum() / m
    vr = varq / var1 if var1 > 0 else float("nan")
    # heteroskedasticity-consistent asymptotic variance
    d2 = (r - mu) ** 2
    denom = d2.sum() ** 2
    theta = 0.0
    for j in range(1, q):
        delta = n * float((d2[j:] * d2[:-j]).sum()) / denom
        theta += (2 * (q - j) / q) ** 2 * delta
    # theta is n·Var(VR) (δ is n-scaled), so z* = √n (VR - 1) / √theta; homoskedastic theta = 2(2q-1)(q-1)/(3q)
    z = (vr - 1) * math.sqrt(n) / math.sqrt(theta) if theta > 0 else float("nan")
    return {"q": q, "vr": _r(vr, 3), "z": _r(z, 2)}


def hurst(logp: np.ndarray, lags: tuple[int, ...] = (1, 2, 4, 8, 16)) -> float:
    """Hurst exponent from the scaling of lagged differences: std(x_{t+τ} - x_t) ∝ τ^H.
    H ≈ 0.5 random walk, < 0.5 mean-reverting, > 0.5 trending. Short lags keep it unbiased; on 2 years of daily
    data a random walk scatters with sd ≈ 0.04 (longer lags bias it down and double the noise)."""
    x = np.asarray(logp, float)
    x = x[np.isfinite(x)]
    use = [lag for lag in lags if lag < len(x) // 4]
    if len(use) < 3:
        return float("nan")
    sd = [np.std(x[lag:] - x[:-lag]) for lag in use]
    if min(sd) <= 0:
        return float("nan")
    slope = np.polyfit(np.log(use), np.log(sd), 1)[0]
    return float(slope)


def autocorr(r: np.ndarray, lag: int = 1) -> float:
    r = np.asarray(r, float)
    r = r[np.isfinite(r)]
    if len(r) <= lag + 2:
        return float("nan")
    a, b = r[lag:], r[:-lag]
    if a.std() == 0 or b.std() == 0:
        return float("nan")
    return float(np.corrcoef(a, b)[0, 1])


def engle_granger(y: np.ndarray, x: np.ndarray) -> dict:
    """Two-step Engle-Granger cointegration test of log prices y on x: hedge ratio β (y = α + βx + ε),
    ADF on the residual spread with Engle-Granger critical values, half-life of the spread."""
    y = np.asarray(y, float)
    x = np.asarray(x, float)
    ok = np.isfinite(y) & np.isfinite(x)
    y, x = y[ok], x[ok]
    beta, _, resid, r2 = ols(y, np.column_stack([np.ones(len(x)), x]))
    t = adf(resid, n_series=2)
    return {"alpha": float(beta[0]), "beta": float(beta[1]), "r2": _r(r2, 3), "adf_stat": t["stat"],
            "p_value": t["p_value"], "half_life": half_life(resid), "spread": resid}


# ----------------------------------------------------------------------------- covariance


def ledoit_wolf(X: np.ndarray) -> tuple[np.ndarray, float]:
    """Ledoit-Wolf (2004) shrinkage of the sample covariance toward a scaled identity.
    X: observations x assets (returns). Returns (covariance, shrinkage intensity 0..1)."""
    X = np.asarray(X, float)
    n, p = X.shape
    Xc = X - X.mean(axis=0)
    S = Xc.T @ Xc / n
    mu = np.trace(S) / p
    F = mu * np.eye(p)
    delta = float(((S - F) ** 2).sum()) / p
    # mean over rows of ||x x' - S||_F^2 / p
    sq = (Xc**2).sum(axis=1)
    quad = np.einsum("ij,jk,ik->i", Xc, S, Xc)
    beta_bar = float((sq**2 - 2 * quad + (S**2).sum()).sum()) / (n**2) / p
    beta = min(beta_bar, delta)
    k = beta / delta if delta > 0 else 1.0
    return k * F + (1 - k) * S, float(k)
