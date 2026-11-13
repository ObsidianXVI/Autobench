"""Strategy Lab: run library strategies on one symbol with honest evaluation.

Simulation (daily, vectorised): the position decided at close t earns the close-to-close return of
bar t+1 (i.e. executed with a market-on-close order at t); costs are charged on every change of
exposure (cost_bps per unit of turnover, default 5 bps = spread + commission for liquid US stocks).

Walk-forward (anchored): history after a one-year indicator warm-up is cut into yearly test windows.
For each window, every parameter combination is scored on all data BEFORE the window (Sharpe) and the
best one is traded in the window. Concatenated test windows = the out-of-sample (OOS) track record.
The Deflated Sharpe Ratio then asks whether that OOS Sharpe beats what the best of N random tries
would show (N = every combination of every strategy tried on this symbol in this run)."""

from __future__ import annotations

import math
import time
from collections import Counter

import numpy as np
import pandas as pd

from ... import db
from ..market import get_bars, normalize_symbol
from ...timeutil import format_ts
from . import stats
from . import strategies as lib

WARMUP = 252
TEST = 252
DEFAULT_COST_BPS = 5.0
_STATE = "qbench:lab:"


def load(symbol: str, lookback: str = "15y", benchmark: str = "SPY") -> tuple[pd.DataFrame, pd.Series | None]:
    bars = get_bars(symbol, "1d", lookback=lookback)
    df = bars.df.dropna()
    if len(df) < WARMUP + 120:
        raise ValueError(f"{bars.symbol}: only {len(df)} daily bars; the lab needs at least {WARMUP + 120} (~1.5 years)")
    bench = None
    if benchmark and normalize_symbol(benchmark) != bars.symbol:
        try:
            bench = get_bars(benchmark, "1d", lookback=lookback).df["c"]
        except Exception:
            bench = None
    return df, bench


def simulate(df: pd.DataFrame, pos: np.ndarray, cost_bps: float = DEFAULT_COST_BPS) -> np.ndarray:
    """Strategy return of each bar 1..n-1 (aligned with df.index[1:])."""
    c = df["c"].to_numpy(float)
    ar = c[1:] / c[:-1] - 1
    pos = np.asarray(pos, float)
    held = pos[:-1]
    turnover = np.abs(np.diff(np.r_[0.0, held]))
    return held * ar - turnover * cost_bps / 1e4


def _dates(df: pd.DataFrame) -> list[str]:
    return [format_ts(int(t), False) for t in df.index]


def _entries(pos: np.ndarray) -> int:
    on = pos > 1e-9
    return int((on[1:] & ~on[:-1]).sum() + (1 if len(on) and on[0] else 0))


def _trades(df: pd.DataFrame, pos: np.ndarray, start: int = 0, limit: int = 40) -> tuple[list[dict], dict]:
    """Round trips of a 0/1-style position (continuous exposure counts as in-market while > 0)."""
    c = df["c"].to_numpy(float)
    dates = df.index
    out = []
    i = max(start, 0)
    n = len(pos)
    while i < n:
        if pos[i] > 1e-9 and (i == 0 or pos[i - 1] <= 1e-9 or i == start):
            j = i
            while j + 1 < n and pos[j + 1] > 1e-9:
                j += 1
            exit_i = min(j + 1, n - 1)
            open_ = j + 1 >= n
            out.append({"entry": format_ts(int(dates[i]), False), "entry_price": round(float(c[i]), 4),
                        "exit": None if open_ else format_ts(int(dates[exit_i]), False),
                        "exit_price": None if open_ else round(float(c[exit_i]), 4),
                        "bars": exit_i - i, "return_pct": round((c[exit_i] / c[i] - 1) * 100, 2), "open": open_})
            i = j + 1
        else:
            i += 1
    closed = np.array([t["return_pct"] for t in out if not t["open"]])
    summary = {"round_trips": len(closed)}
    if len(closed):
        summary.update({"win_rate_pct": round(float((closed > 0).mean() * 100), 1),
                        "avg_trade_pct": round(float(closed.mean()), 3),
                        "avg_bars_held": round(float(np.mean([t["bars"] for t in out if not t["open"]])), 1)})
        if len(closed) > 1 and closed.std(ddof=1) > 0:
            summary["t_stat"] = round(float(closed.mean() / (closed.std(ddof=1) / math.sqrt(len(closed)))), 2)
    return out[-limit:], summary


def _curve(dates: list[str], rets: np.ndarray, bh: np.ndarray, points: int = 320) -> dict:
    eq = np.cumprod(1 + rets) * 100
    beq = np.cumprod(1 + bh) * 100
    step = max(1, len(eq) // points)
    idx = list(range(0, len(eq), step))
    if idx and idx[-1] != len(eq) - 1:
        idx.append(len(eq) - 1)
    return {"dates": [dates[i] for i in idx], "strategy": [round(float(eq[i]), 2) for i in idx],
            "buy_hold": [round(float(beq[i]), 2) for i in idx]}


def _yearly(dates: list[str], rets: np.ndarray, bh: np.ndarray) -> list[dict]:
    s = pd.DataFrame({"s": rets, "b": bh}, index=pd.to_datetime(dates))
    g = (1 + s).groupby(s.index.year).prod() - 1
    return [{"year": int(y), "strategy_pct": round(float(r["s"]) * 100, 2), "buy_hold_pct": round(float(r["b"]) * 100, 2)}
            for y, r in g.iterrows()]


def _per_period_sr(r: np.ndarray) -> float:
    m, s, _, _ = stats.moments(r)
    return m / s if s and s > 0 else float("nan")


def _alpha_series(r: np.ndarray, bh: np.ndarray) -> tuple[np.ndarray, float, float, float]:
    """Timing alpha vs holding the stock: r = a + b·bh + e. Returns (a + e per bar, a annualised, t(a), b).
    A long-only timing rule on a rising stock has a positive Sharpe for free; only alpha is skill."""
    ok = np.isfinite(r) & np.isfinite(bh)
    r, bh = r[ok], bh[ok]
    if len(r) < 30 or np.std(bh) == 0:
        return r, float("nan"), float("nan"), float("nan")
    beta, se, resid, _ = stats.ols(r, np.column_stack([np.ones(len(bh)), bh]))
    return beta[0] + resid, beta[0] * 252, (beta[0] / se[0] if se[0] > 0 else float("nan")), beta[1]


# ----------------------------------------------------------------------------- single strategy


def run_strategy(symbol: str, strategy_id: str, params: dict | None = None, lookback: str = "10y",
                 cost_bps: float = DEFAULT_COST_BPS, allow_short: bool = False, oos_fraction: float = 0.3) -> dict:
    """One strategy with fixed parameters over the whole window, with an in-sample / out-of-sample time split."""
    st = lib.get(strategy_id)
    df, bench = load(symbol, lookback)
    pos = lib.positions(st, df, params, bench, allow_short)
    rets = simulate(df, pos, cost_bps)
    c = df["c"].to_numpy(float)
    bh = c[1:] / c[:-1] - 1
    dates = _dates(df)[1:]
    s0 = WARMUP
    r, b, d = rets[s0:], bh[s0:], dates[s0:]
    split = int(len(r) * (1 - oos_fraction))
    trades, tsum = _trades(df, pos, start=s0)
    out = {
        "symbol": normalize_symbol(symbol), "strategy": st.id, "name": st.name, "params": {**st.params, **(params or {})},
        "period": {"start": d[0], "end": d[-1]}, "cost_bps": cost_bps, "allow_short": allow_short,
        "performance": stats.perf_stats(r, exposure=pos[s0:-1]), "buy_and_hold": stats.perf_stats(b),
        "in_sample": stats.perf_stats(r[:split]), "out_of_sample": {**stats.perf_stats(r[split:]), "starts": d[split]},
        "trades": tsum, "recent_trades": trades[-15:],
        "position_now": round(float(pos[-1]), 3),
        "position_changed": format_ts(int(df.index[_last_change(pos)]), False),
        "equity_curve": _curve(d, r, b), "yearly": _yearly(d, r, b),
        "expression": st.expression,
        "evidence": {"strength": st.evidence, "note": st.evidence_note},
    }
    out["warnings"] = _warnings(out)
    return out


def _last_change(pos: np.ndarray) -> int:
    ch = np.flatnonzero(np.abs(np.diff(pos)) > 1e-9)
    return int(ch[-1] + 1) if len(ch) else 0


def _warnings(r: dict) -> list[str]:
    w = []
    p, b = r["performance"], r["buy_and_hold"]
    rt = r["trades"].get("round_trips", 0)
    if rt and rt < 20:
        w.append(f"Only {rt} round trips: anecdotal.")
    if p.get("psr") is not None and p["psr"] < 0.9:
        w.append(f"PSR {p['psr']}: less than 90% probability the true Sharpe is above zero.")
    if (p.get("sharpe") or 0) < (b.get("sharpe") or 0):
        w.append(f"Sharpe {p.get('sharpe')} below buy-and-hold {b.get('sharpe')}: no risk-adjusted edge over holding.")
    ins, oos = r.get("in_sample", {}), r.get("out_of_sample", {})
    if (ins.get("sharpe") or 0) > 0.3 and (oos.get("sharpe") or 0) < 0.5 * (ins.get("sharpe") or 0):
        w.append("Sharpe halves out-of-sample: overfit or regime change.")
    return w


# ----------------------------------------------------------------------------- walk-forward lab


def _walk_forward(df, combos_rets: list[np.ndarray], n_test_windows_min: int = 2) -> dict | None:
    n = len(combos_rets[0])
    usable = n - WARMUP
    train0 = 3 * 252 if usable >= 3 * 252 + n_test_windows_min * TEST else 2 * 252
    if usable < train0 + TEST:
        return None
    folds, oos_parts, chosen = [], [], []
    start = WARMUP + train0
    while start < n:
        end = min(start + TEST, n)
        if end - start < 40:
            break
        train_sr = [_per_period_sr(r[WARMUP:start]) for r in combos_rets]
        best = int(np.nanargmax(np.where(np.isfinite(train_sr), train_sr, -np.inf)))
        test = combos_rets[best][start:end]
        oos_parts.append(test)
        chosen.append(best)
        folds.append({"start": start, "end": end, "combo": best,
                      "train_sharpe": round(train_sr[best] * math.sqrt(252), 2) if np.isfinite(train_sr[best]) else None,
                      "test_sharpe": round(_per_period_sr(test) * math.sqrt(252), 2) if np.isfinite(_per_period_sr(test)) else None,
                      "test_return_pct": round(float(np.prod(1 + test) - 1) * 100, 2)})
        start = end
    return {"oos": np.concatenate(oos_parts), "oos_start": WARMUP + train0, "folds": folds, "chosen": chosen}


def run_lab(symbol: str, strategy_ids: list[str] | None = None, lookback: str = "15y", cost_bps: float = DEFAULT_COST_BPS,
            allow_short: bool = False, save: bool = True) -> dict:
    """Walk-forward test of every single-asset library strategy (or the given ones) on `symbol`."""
    t0 = time.time()
    sym = normalize_symbol(symbol)
    df, bench = load(sym, lookback)
    sts = [lib.get(s) for s in strategy_ids] if strategy_ids else lib.SINGLE
    sts = [s for s in sts if s.kind == "single"]
    if not sts:
        raise ValueError("No single-asset strategies selected (cross-sectional/pairs/portfolio ones have their own tools)")
    c = df["c"].to_numpy(float)
    bh = c[1:] / c[:-1] - 1
    dates = _dates(df)[1:]
    runs = []
    all_sr: list[float] = []
    for st in sts:
        combos = st.combos()
        positions = [lib.positions(st, df, p, bench, allow_short) for p in combos]
        rets = [simulate(df, p, cost_bps) for p in positions]
        full_sr = [_per_period_sr(r[WARMUP:]) for r in rets]
        all_sr += [x for x in full_sr if np.isfinite(x)]
        runs.append((st, combos, positions, rets, full_sr))
    n_trials = sum(len(r[1]) for r in runs)
    sr_var = float(np.var(all_sr, ddof=1)) if len(all_sr) > 1 else 0.0

    results = []
    for st, combos, positions, rets, full_sr in runs:
        best_full = int(np.nanargmax(np.where(np.isfinite(full_sr), full_sr, -np.inf)))
        wf = _walk_forward(df, rets)
        entry: dict = {"id": st.id, "name": st.name, "family": st.family, "evidence": st.evidence,
                       "combinations": len(combos)}
        pos_now = positions[best_full]
        entry["current"] = {"params": combos[best_full], "position": round(float(pos_now[-1]), 3),
                            "since": format_ts(int(df.index[_last_change(pos_now)]), False)}
        full = rets[best_full][WARMUP:]
        entry["in_sample_best"] = {k: v for k, v in stats.perf_stats(full).items()
                                   if k in ("sharpe", "cagr_pct", "max_drawdown_pct")}
        if wf is None:
            entry["walk_forward"] = None
            entry["verdict"] = "insufficient history"
            entry["oos"] = None
        else:
            oos = wf["oos"]
            s = wf["oos_start"]
            bh_oos = bh[s:s + len(oos)]
            exp = np.concatenate([positions[f["combo"]][f["start"]:f["end"]] for f in wf["folds"]])
            ps = stats.perf_stats(oos, exposure=exp)
            bhs = stats.perf_stats(bh_oos)
            # Null for the deflation: N unskilled trials whose Sharpe estimates scatter with the sampling variance
            # of a zero-skill Sharpe over this test length (≈ 1/T). The cross-sectional variance of the trials'
            # Sharpes would be inflated by genuine differences between strategy families (reported, not used).
            null_var = 1.0 / max(len(oos), 1)
            dsr = stats.deflated_sharpe(oos, n_trials, null_var)
            aser, a_ann, a_t, a_beta = _alpha_series(oos, bh_oos)
            adsr = stats.deflated_sharpe(aser, n_trials, null_var)
            modal = Counter(wf["chosen"]).most_common(1)[0][1] / len(wf["chosen"])
            entry["oos"] = {**ps, "dsr": round(dsr, 3) if math.isfinite(dsr) else None,
                            "alpha_annual_pct": round(a_ann * 100, 2) if math.isfinite(a_ann) else None,
                            "alpha_t": round(a_t, 2) if math.isfinite(a_t) else None,
                            "beta_to_stock": round(a_beta, 2) if math.isfinite(a_beta) else None,
                            "alpha_dsr": round(adsr, 3) if math.isfinite(adsr) else None,
                            "buy_hold_sharpe": bhs.get("sharpe"), "buy_hold_cagr_pct": bhs.get("cagr_pct"),
                            "buy_hold_max_drawdown_pct": bhs.get("max_drawdown_pct"),
                            "entries": _entries(exp), "param_stability": round(modal, 2),
                            "start": dates[s], "end": dates[s + len(oos) - 1]}
            entry["walk_forward"] = [{**{k: v for k, v in f.items() if k not in ("start", "end", "combo")},
                                      "test_start": dates[f["start"]], "test_end": dates[f["end"] - 1],
                                      "params": combos[f["combo"]]} for f in wf["folds"]]
            entry["equity_curve"] = _curve(dates[s:s + len(oos)], oos, bh_oos)
            entry["verdict"] = _verdict(entry["oos"])
        results.append(entry)

    order = {"robust": 4, "promising": 3, "de-risks only": 2, "not significant": 1}

    def rank_key(e):
        o = e.get("oos") or {}
        return (order.get(e.get("verdict"), 0), o.get("alpha_dsr") if o.get("alpha_dsr") is not None else -1,
                o.get("sharpe") or -9)

    results.sort(key=rank_key, reverse=True)
    out = {
        "symbol": sym, "as_of": format_ts(int(df.index[-1]), False),
        "history": {"start": dates[0], "end": dates[-1], "bars": len(df)},
        "settings": {"lookback": lookback, "cost_bps": cost_bps, "allow_short": allow_short,
                     "execution": "position from close t held over bar t+1 (MOC); costs per unit turnover"},
        "trials": n_trials, "deflation": "N = every parameter combination of every strategy tried (conservative: "
                                         "variants are correlated); null Sharpe variance = 1/T of the test window",
        "trial_sharpe_std_annual": round(math.sqrt(sr_var) * math.sqrt(252), 3) if sr_var > 0 else None,
        "buy_and_hold": stats.perf_stats(bh[WARMUP:]),
        "strategies": results,
        "best": next((r["id"] for r in results if r.get("verdict") in ("robust", "promising")), None),
        "elapsed_s": round(time.time() - t0, 2), "saved_at": int(time.time()),
        "notes": ["Skill is measured as timing alpha vs holding the stock (OOS returns regressed on buy-and-hold), "
                  "deflated for every combination tried (alpha_dsr). A long-only rule on a rising stock gets a positive "
                  "Sharpe for free, so dsr (raw Sharpe vs luck) alone is not evidence of an edge.",
                  "Verdicts: robust = alpha_dsr ≥ 0.9; promising = alpha_dsr ≥ 0.6 with positive alpha; de-risks only = "
                  "no significant alpha but a similar Sharpe with a much smaller drawdown; no edge = Sharpe ≤ buy-and-hold; "
                  "otherwise not significant (indistinguishable from data mining).",
                  "param_stability = share of walk-forward windows that picked the same parameters (low = fragile)."],
    }
    if save:
        db.state_set(_STATE + sym, out)
    return out


def _verdict(o: dict) -> str:
    adsr, alpha, sh, bsh = o.get("alpha_dsr"), o.get("alpha_annual_pct"), o.get("sharpe"), o.get("buy_hold_sharpe")
    if adsr is None or sh is None:
        return "inconclusive"
    if adsr >= 0.9 and (alpha or 0) > 0:
        return "robust"
    if adsr >= 0.6 and (alpha or 0) > 0:
        return "promising"
    dd, bdd = o.get("max_drawdown_pct"), o.get("buy_hold_max_drawdown_pct")
    if (sh > 0 and bsh is not None and sh >= bsh - 0.15 and dd is not None and bdd
            and abs(dd) <= 0.7 * abs(bdd)):
        return "de-risks only"
    if bsh is not None and sh <= bsh:
        return "no edge"
    return "not significant"


def last_lab(symbol: str) -> dict | None:
    return db.state_get(_STATE + normalize_symbol(symbol))
