"""Rule-based backtesting, forward-return signal studies and condition scans.

Execution model (no look-ahead): signals are evaluated on a bar's close and filled at the NEXT
bar's open, with slippage and commission charged on both sides. Stops/targets are checked
intrabar against high/low; if a bar gaps through a level the fill is the open. If stop and target
are both inside one bar the stop is assumed to fill first (conservative).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from . import indicators as ind
from .expressions import evaluate_bool, max_lookback_hint
from .market import get_bars
from ..timeutil import format_ts, lookback_seconds, now_ts


@dataclass
class Trade:
    entry_i: int
    entry_price: float
    exit_i: int | None = None
    exit_price: float | None = None
    reason: str = ""

    def ret(self, side: int) -> float | None:
        if self.exit_price is None:
            return None
        return side * (self.exit_price / self.entry_price - 1)


def _load(symbol: str, interval: str, lookback: str, expr_list: list[str]):
    warm = max([max_lookback_hint(e) for e in expr_list if e] + [60])
    bars = get_bars(symbol, interval, lookback=lookback)
    if bars.interval.intraday:
        return bars, 0
    # fetch extra history for indicator warm-up, then remember where the test window starts
    secs = lookback_seconds(lookback)
    if secs is None:
        return bars, warm
    start = now_ts() - secs
    extra_days = int(warm * 1.5) + 10 if bars.interval.name == "1d" else warm * (7 if bars.interval.name == "1wk" else 31)
    full = get_bars(symbol, interval, start=start - extra_days * 86400)
    first = int(np.searchsorted(full.df.index.to_numpy(), start))
    return full, first


def _metrics(rets: np.ndarray, equity: np.ndarray, bars_per_year: float, n_bars: int) -> dict:
    out: dict = {"trades": int(len(rets))}
    if len(rets):
        wins, losses = rets[rets > 0], rets[rets <= 0]
        out.update({
            "win_rate_pct": round(len(wins) / len(rets) * 100, 1),
            "avg_trade_pct": round(rets.mean() * 100, 3),
            "median_trade_pct": round(float(np.median(rets)) * 100, 3),
            "avg_win_pct": round(wins.mean() * 100, 3) if len(wins) else None,
            "avg_loss_pct": round(losses.mean() * 100, 3) if len(losses) else None,
            "best_trade_pct": round(rets.max() * 100, 2),
            "worst_trade_pct": round(rets.min() * 100, 2),
            "profit_factor": round(wins.sum() / abs(losses.sum()), 2) if losses.sum() < 0 else None,
            "expectancy_pct": round(rets.mean() * 100, 3),
        })
        if len(rets) > 1 and rets.std(ddof=1) > 0:
            out["t_stat"] = round(float(rets.mean() / (rets.std(ddof=1) / math.sqrt(len(rets)))), 2)
    if len(equity) > 1:
        total = equity[-1] / equity[0] - 1
        years = n_bars / bars_per_year
        out["total_return_pct"] = round(total * 100, 2)
        out["cagr_pct"] = round(((1 + total) ** (1 / years) - 1) * 100, 2) if years > 0 and total > -1 else None
        er = np.diff(equity) / equity[:-1]
        if er.std() > 0:
            out["sharpe"] = round(float(er.mean() / er.std() * math.sqrt(bars_per_year)), 2)
        peak = np.maximum.accumulate(equity)
        out["max_drawdown_pct"] = round(float(((equity - peak) / peak).min()) * 100, 2)
    return out


def run_backtest(
    symbol: str,
    entry: str,
    exit: str | None = None,
    interval: str = "1d",
    lookback: str = "5y",
    side: str = "long",
    stop_loss_pct: float | None = None,
    take_profit_pct: float | None = None,
    stop_atr: float | None = None,
    target_atr: float | None = None,
    max_hold_bars: int | None = None,
    commission_bps: float = 1.0,
    slippage_bps: float = 5.0,
    oos_fraction: float = 0.3,
) -> dict:
    if side not in ("long", "short"):
        raise ValueError("side must be 'long' or 'short'")
    if exit is None and not any([stop_loss_pct, take_profit_pct, stop_atr, target_atr, max_hold_bars]):
        raise ValueError("Provide an exit condition and/or a stop, target or max_hold_bars")
    bars, first = _load(symbol, interval, lookback, [entry, exit or ""])
    df = bars.df
    if len(df) - first < 30:
        raise ValueError(f"Not enough bars for a backtest ({len(df) - first}); use a longer lookback")
    sgn = 1 if side == "long" else -1
    entry_sig = evaluate_bool(df, entry).to_numpy()
    exit_sig = evaluate_bool(df, exit).to_numpy() if exit else np.zeros(len(df), bool)
    atr = ind._atr(df, 14).to_numpy()
    o, h, l, c = (df[k].to_numpy() for k in ("o", "h", "l", "c"))  # noqa: E741
    cost = (commission_bps + slippage_bps) / 10000

    trades: list[Trade] = []
    pos: Trade | None = None
    stop = target = None
    equity = np.ones(len(df))
    eq = 1.0
    in_mkt = np.zeros(len(df), bool)
    for i in range(first, len(df)):
        # 1) pending entry from previous bar's signal
        if pos is None and i > first and entry_sig[i - 1]:
            fill = o[i] * (1 + sgn * cost)
            pos = Trade(i, fill)
            a = atr[i - 1]
            stop = target = None
            if stop_loss_pct:
                stop = fill * (1 - sgn * stop_loss_pct / 100)
            if stop_atr and np.isfinite(a):
                s2 = fill - sgn * stop_atr * a
                stop = s2 if stop is None else (max(stop, s2) if sgn == 1 else min(stop, s2))
            if take_profit_pct:
                target = fill * (1 + sgn * take_profit_pct / 100)
            if target_atr and np.isfinite(a):
                t2 = fill + sgn * target_atr * a
                target = t2 if target is None else (min(target, t2) if sgn == 1 else max(target, t2))
        # 2) manage open position within bar i
        if pos is not None:
            in_mkt[i] = True
            exit_px = reason = None
            if stop is not None:
                hit = l[i] <= stop if sgn == 1 else h[i] >= stop
                if hit:
                    gap = o[i] <= stop if sgn == 1 else o[i] >= stop
                    exit_px, reason = (o[i] if gap else stop), "stop"
            if exit_px is None and target is not None:
                hit = h[i] >= target if sgn == 1 else l[i] <= target
                if hit:
                    gap = o[i] >= target if sgn == 1 else o[i] <= target
                    exit_px, reason = (o[i] if gap else target), "target"
            if exit_px is None and i + 1 < len(df):
                held = i - pos.entry_i + 1
                if exit_sig[i] or (max_hold_bars and held >= max_hold_bars):
                    exit_px, reason = o[i + 1], "signal" if exit_sig[i] else "max_hold"
                    # filled at next open: account for it on bar i+1 below by closing now at that price
            if exit_px is not None:
                pos.exit_price = exit_px * (1 - sgn * cost)
                pos.exit_i = i if reason in ("stop", "target") else i + 1
                pos.reason = reason
                trades.append(pos)
                mark_from = c[i - 1] if i > pos.entry_i else pos.entry_price
                if reason in ("stop", "target"):
                    eq *= 1 + sgn * (pos.exit_price / mark_from - 1)
                else:
                    eq *= 1 + sgn * (c[i] / mark_from - 1)
                    # the move from close[i] to the next open is realised on the next bar
                    eq *= 1 + sgn * (pos.exit_price / c[i] - 1)
                pos = None
            else:
                mark_from = c[i - 1] if i > pos.entry_i else pos.entry_price
                eq *= 1 + sgn * (c[i] / mark_from - 1)
        equity[i] = eq
    open_trade = None
    if pos is not None:
        open_trade = {"entry": format_ts(int(df.index[pos.entry_i]), bars.interval.intraday),
                      "entry_price": round(pos.entry_price, 4),
                      "unrealized_pct": round(sgn * (c[-1] / pos.entry_price - 1) * 100, 2)}

    eqw = equity[first:]
    n = len(eqw)
    rets = np.array([t.ret(sgn) for t in trades])
    bpy = bars.interval.bars_per_year
    result = {
        "symbol": bars.symbol, "interval": bars.interval.name, "side": side,
        "period": {"start": format_ts(int(df.index[first]), bars.interval.intraday),
                   "end": format_ts(int(df.index[-1]), bars.interval.intraday), "bars": n},
        "rules": {"entry": entry, "exit": exit, "stop_loss_pct": stop_loss_pct, "take_profit_pct": take_profit_pct,
                  "stop_atr": stop_atr, "target_atr": target_atr, "max_hold_bars": max_hold_bars,
                  "costs_bps_per_side": commission_bps + slippage_bps},
        "performance": _metrics(rets, eqw, bpy, n),
        "exposure_pct": round(float(in_mkt[first:].mean()) * 100, 1),
        "avg_bars_held": round(float(np.mean([t.exit_i - t.entry_i for t in trades])), 1) if trades else None,
        "exit_reasons": pd.Series([t.reason for t in trades]).value_counts().to_dict() if trades else {},
        "open_trade": open_trade,
    }
    bh_eq = c[first:] / o[first]
    result["buy_and_hold"] = {
        "total_return_pct": round(float(c[-1] / o[first] - 1) * 100, 2),
        "max_drawdown_pct": round(float(((bh_eq - np.maximum.accumulate(bh_eq)) / np.maximum.accumulate(bh_eq)).min()) * 100, 2),
    }
    # out-of-sample split by time: does the edge survive in the most recent part?
    if trades and 0 < oos_fraction < 1:
        split_i = first + int(n * (1 - oos_fraction))
        ins = np.array([t.ret(sgn) for t in trades if t.entry_i < split_i])
        oos = np.array([t.ret(sgn) for t in trades if t.entry_i >= split_i])
        result["in_sample"] = _metrics(ins, equity[first:split_i], bpy, split_i - first)
        result["out_of_sample"] = _metrics(oos, equity[split_i - 1 :], bpy, len(df) - split_i)
        result["out_of_sample"]["starts"] = format_ts(int(df.index[split_i]), bars.interval.intraday)
    result["recent_trades"] = [
        {"entry": format_ts(int(df.index[t.entry_i]), bars.interval.intraday),
         "exit": format_ts(int(df.index[min(t.exit_i, len(df) - 1)]), bars.interval.intraday),
         "return_pct": round(t.ret(sgn) * 100, 2), "reason": t.reason}
        for t in trades[-15:]
    ]
    result["warnings"] = _warnings(result)
    return result


def _warnings(r: dict) -> list[str]:
    w = []
    t = r["performance"].get("trades", 0)
    if t < 20:
        w.append(f"Only {t} trades: results are not statistically meaningful.")
    ts = r["performance"].get("t_stat")
    if ts is not None and abs(ts) < 2:
        w.append(f"t-stat {ts} < 2: average trade return is not distinguishable from zero.")
    oos = r.get("out_of_sample")
    ins = r.get("in_sample")
    if oos and ins and ins.get("avg_trade_pct") and oos.get("avg_trade_pct") is not None:
        if ins["avg_trade_pct"] > 0 and oos["avg_trade_pct"] < 0.5 * ins["avg_trade_pct"]:
            w.append("Edge degrades out-of-sample (recent trades much weaker): possible overfitting or regime change.")
    bh = r["buy_and_hold"]["total_return_pct"]
    tot = r["performance"].get("total_return_pct")
    if tot is not None and tot < bh and r["side"] == "long":
        w.append(f"Underperformed buy-and-hold ({tot}% vs {bh}%); consider risk-adjusted metrics and exposure.")
    return w


def signal_study(
    symbol: str,
    condition: str,
    interval: str = "1d",
    lookback: str = "10y",
    horizons: list[int] | None = None,
    first_only: bool = True,
) -> dict:
    """Forward returns after `condition` becomes true, vs. the unconditional baseline.

    first_only=True counts only bars where the condition turns true (not every bar it stays true),
    which avoids overlapping, highly correlated samples."""
    horizons = horizons or [5, 10, 20]
    bars, first = _load(symbol, interval, lookback, [condition])
    df = bars.df
    sig = evaluate_bool(df, condition)
    if first_only:
        sig = sig & ~sig.shift(1, fill_value=False)
    sig.iloc[:first] = False
    c = df["c"]
    n = len(df)
    out_h = {}
    sig_idx = np.flatnonzero(sig.to_numpy())
    for hz in horizons:
        fwd = (c.shift(-hz) / c - 1) * 100
        base = fwd.iloc[first:].dropna()
        vals = fwd.iloc[sig_idx].dropna()
        # max adverse / favourable excursion within the horizon
        lows = df["l"].rolling(hz).min().shift(-hz)
        highs = df["h"].rolling(hz).max().shift(-hz)
        mae = ((lows / c - 1) * 100).iloc[sig_idx].dropna()
        mfe = ((highs / c - 1) * 100).iloc[sig_idx].dropna()
        entry = {"samples": int(len(vals))}
        if len(vals):
            entry.update({
                "mean_pct": round(vals.mean(), 3), "median_pct": round(vals.median(), 3),
                "win_rate_pct": round((vals > 0).mean() * 100, 1),
                "std_pct": round(vals.std(), 3) if len(vals) > 1 else None,
                "avg_max_adverse_pct": round(mae.mean(), 3) if len(mae) else None,
                "avg_max_favorable_pct": round(mfe.mean(), 3) if len(mfe) else None,
                "baseline_mean_pct": round(base.mean(), 3),
                "baseline_win_rate_pct": round((base > 0).mean() * 100, 1),
                "edge_vs_baseline_pct": round(vals.mean() - base.mean(), 3),
            })
            if len(vals) > 1 and vals.std() > 0:
                # Welch t-stat of conditional vs baseline mean (samples overlap, so treat as indicative)
                se = math.sqrt(vals.var() / len(vals) + base.var() / len(base))
                entry["t_stat_vs_baseline"] = round(float((vals.mean() - base.mean()) / se), 2) if se > 0 else None
        out_h[f"{hz}_bars"] = entry
    last_true = bool(evaluate_bool(df, condition).iloc[-1])
    occurrences = [
        {"date": format_ts(int(df.index[i]), bars.interval.intraday),
         "close": round(float(c.iloc[i]), 4),
         **{f"fwd_{hz}": (round(float((c.iloc[i + hz] / c.iloc[i] - 1) * 100), 2) if i + hz < n else None)
            for hz in horizons}}
        for i in sig_idx[-10:]
    ]
    warnings = []
    if len(sig_idx) < 20:
        warnings.append(f"Only {len(sig_idx)} occurrences: treat statistics as anecdotal.")
    return {
        "symbol": bars.symbol, "interval": bars.interval.name, "condition": condition,
        "period": {"start": format_ts(int(df.index[first]), bars.interval.intraday),
                   "end": format_ts(int(df.index[-1]), bars.interval.intraday)},
        "occurrences": int(len(sig_idx)), "first_only": first_only,
        "condition_true_now": last_true,
        "forward_returns": out_h,
        "recent_occurrences": occurrences,
        "warnings": warnings,
    }


def scan(symbols: list[str], condition: str, interval: str = "1d") -> dict:
    """Evaluate a condition on the latest bar of each symbol."""
    matches, rest, errors = [], [], {}
    lb = "2y" if interval in ("1d", "1wk", "1mo") else "30d"
    if interval == "1wk":
        lb = "10y"
    for sym in symbols:
        try:
            bars = get_bars(sym, interval, lookback=lb)
            if len(bars) < 2:
                errors[sym] = "no data"
                continue
            ok = bool(evaluate_bool(bars.df, condition).iloc[-1])
            c = bars.df["c"]
            row = {"symbol": bars.symbol, "close": round(float(c.iloc[-1]), 4),
                   "change_pct": round(float((c.iloc[-1] / c.iloc[-2] - 1) * 100), 2)}
            (matches if ok else rest).append(row)
        except ValueError:
            raise
        except Exception as e:  # provider errors per symbol should not abort the scan
            errors[sym] = str(e)
    return {"condition": condition, "interval": interval, "matches": matches,
            "non_matches": [r["symbol"] for r in rest], "errors": errors}
