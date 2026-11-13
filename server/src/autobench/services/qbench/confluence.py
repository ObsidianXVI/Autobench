"""Confluence: one view that combines technicals (TA), fundamentals (FA, as factor ranks) and quant
evidence, plus quant-derived entry/exit levels and position sizing.

Pillars are 0-100 (50 = neutral):
  technical  (0.6·trend + 0.4·momentum score from get_technical_summary, -100..100) mapped to 0-100
  fundamental  mean of value, quality and growth factor ranks (universe percentiles)
  quant      momentum rank + regime / trend fit + the Strategy Lab's current signal (if the lab has been run)
Composite = weighted mean of available pillars (TA 0.35, FA 0.30, quant 0.35)."""

from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from .. import indicators as ind
from ..market import get_bars, normalize_symbol
from ...timeutil import format_ts
from . import factors, lab, profile

WEIGHTS = {"technical": 0.35, "fundamental": 0.30, "quant": 0.35}


def _r(x, nd=2):
    if x is None:
        return None
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return round(x, nd) if math.isfinite(x) else None


def _technical(symbol: str) -> dict | None:
    from ..technicals import technical_summary

    try:
        t = technical_summary(symbol, "1d", detail="brief")
    except Exception:
        return None
    ts, ms = t["trend"].get("score"), t["momentum"].get("score")
    if ts is None or ms is None:
        return None
    comp = 0.6 * ts + 0.4 * ms
    return {"score": _r((comp + 100) / 2, 1), "trend_score": ts, "momentum_score": ms,
            "trend_strength": t["trend"].get("trend_strength"), "structure": t["trend"].get("structure"),
            "rsi14": t["momentum"].get("rsi14") or t["momentum"].get("rsi"),
            "supports": [s["price"] for s in t["levels"]["supports"][:2]],
            "resistances": [s["price"] for s in t["levels"]["resistances"][:2]]}


def _fundamental(fs: dict) -> dict | None:
    if not fs.get("in_universe") or not fs.get("eligible"):
        return None
    sc = fs["scores"]
    vals = [sc[k] for k in ("value", "quality", "growth") if sc.get(k) is not None]
    if not vals:
        return None
    return {"score": _r(sum(vals) / len(vals), 1), "value": sc.get("value"), "quality": sc.get("quality"),
            "growth": sc.get("growth"), "sector_relative": {k: fs["sector_relative"].get(k) for k in ("value", "quality", "growth")}}


def _quant(sym: str, fs: dict, prof: dict | None, lab_res: dict | None) -> dict:
    parts, notes = [], []
    mom = (fs.get("scores") or {}).get("momentum") if fs.get("eligible") else None
    if mom is not None:
        parts.append(mom)
        notes.append(f"momentum rank {mom:.0f}")
    if (fs.get("scores") or {}).get("low_vol") is not None and fs.get("eligible"):
        lv = fs["scores"]["low_vol"]
        parts.append(50 + (lv - 50) * 0.3)  # low-vol is a risk premium; tilt only
    regime = prof["regime"]["label"] if prof else None
    if prof:
        ts = prof["trend_state"]
        r121 = ts.get("return_12_1_pct")
        tsm = 70 if (r121 or 0) > 0 and ts["above_sma200"] else 30 if (r121 or 0) < 0 and not ts["above_sma200"] else 50
        parts.append(tsm)
        notes.append(f"time-series momentum {'positive' if tsm > 50 else 'negative' if tsm < 50 else 'mixed'}")
    best = None
    if lab_res:
        for s in lab_res.get("strategies", []):
            if s.get("verdict") in ("robust", "promising"):
                best = s
                break
    if best:
        on = (best["current"]["position"] or 0) > 0
        conf = 1.0 if best["verdict"] == "robust" else 0.6
        parts.append(50 + (35 if on else -20) * conf)
        notes.append(f"lab: {best['name']} ({best['verdict']}) is {'LONG' if on else 'flat'} since {best['current']['since']}")
    elif lab_res:
        notes.append("lab: no strategy beat data-mining odds on this symbol")
    score = sum(parts) / len(parts) if parts else None
    return {"score": _r(score, 1), "regime": regime, "momentum_rank": mom, "notes": notes,
            "lab_best": best and {"id": best["id"], "name": best["name"], "verdict": best["verdict"],
                                  "position": best["current"]["position"], "since": best["current"]["since"],
                                  "params": best["current"]["params"], "oos_sharpe": (best.get("oos") or {}).get("sharpe"),
                                  "dsr": (best.get("oos") or {}).get("dsr")},
            "lab_as_of": lab_res and lab_res.get("as_of")}


def _levels(sym: str, regime: str | None, lab_best: dict | None, lab_res: dict | None) -> dict:
    bars = get_bars(sym, "1d", lookback="2y")
    df = bars.df
    c = df["c"]
    last = float(c.iloc[-1])
    atr = float(ind.compute(df, "atr", {"length": 14})["atr"].iloc[-1])
    sd20 = float(c.rolling(20).std(ddof=0).iloc[-1])
    sma20, sma50, sma200 = (float(ind.sma(c, n).iloc[-1]) for n in (20, 50, 200))
    ema21 = float(ind.ema(c, 21).iloc[-1])
    vol = float(np.log(c).diff().iloc[-63:].std() * math.sqrt(252))
    plans = {}
    plans["trend_pullback"] = {
        "use_when": "trending regime / positive momentum",
        "entry_zone": [_r(ema21 - 0.5 * atr), _r(ema21 + 0.25 * atr)],
        "stop": _r(min(ema21 - 2.5 * atr, sma50 - 0.5 * atr)),
        "target": _r(last + 3 * atr), "note": "Buy the pullback to the 21-EMA; stop 2.5 ATR below or under the 50-SMA."}
    plans["mean_reversion"] = {
        "use_when": "mean-reverting regime or RSI(2)/z-score signal",
        "entry": _r(sma20 - 2 * sd20), "target": _r(sma20), "stop": _r(sma20 - 3.5 * sd20),
        "note": "Bollinger reversion: buy 2σ below the 20-day mean, exit at the mean; the stop sits 1.5σ further."}
    plans["breakout"] = {
        "use_when": "base near highs with volume",
        "trigger": _r(float(df["h"].iloc[-56:-1].max())), "stop": _r(float(df["l"].iloc[-21:-1].min())),
        "note": "Donchian 55: buy a close above the prior 55-day high; trail with the 20-day low."}
    preferred = ("mean_reversion" if regime == "mean-reverting" else "trend_pullback" if regime == "trending" else None)
    if lab_best:
        fam = next((s["family"] for s in (lab_res or {}).get("strategies", []) if s["id"] == lab_best["id"]), None)
        preferred = {"mean_reversion": "mean_reversion", "trend": "trend_pullback", "momentum": "trend_pullback"}.get(fam, preferred)
        if lab_best["id"] == "donchian":
            preferred = "breakout"
    sizing = {"realized_vol_pct": _r(vol * 100, 1),
              "vol_target_weight_pct": _r(min(1.0, 0.10 / vol) * 100 if vol > 0 else None, 1),
              "note": "vol_target_weight = 10% annual risk budget / stock volatility (cap 100%): the weight at which this "
                      "position alone would run 10% annualised vol. Combine with the risk-per-trade rule (stop distance)."}
    if lab_res and lab_best:
        s = next((x for x in lab_res["strategies"] if x["id"] == lab_best["id"]), None)
        o = (s or {}).get("oos") or {}
        if o.get("cagr_pct") is not None and o.get("vol_pct"):
            mu, sig = o["cagr_pct"] / 100, o["vol_pct"] / 100
            kelly = mu / sig**2 if sig > 0 else None
            sizing["kelly_fraction"] = _r(kelly, 2)
            sizing["half_kelly_capped_pct"] = _r(max(0.0, min(0.25, (kelly or 0) / 2)) * 100, 1)
            sizing["kelly_note"] = ("Kelly f = μ/σ² of the strategy's OOS returns. Full Kelly is far too aggressive given "
                                    "estimation error; use ≤ half, capped at 25% of equity.")
    return {"price": _r(last, 4), "atr14": _r(atr, 4), "sigma20": _r(sd20, 4), "sma20": _r(sma20), "ema21": _r(ema21),
            "sma50": _r(sma50), "sma200": _r(sma200), "plans": plans, "preferred_plan": preferred, "sizing": sizing,
            "as_of": format_ts(int(df.index[-1]), False)}


def confluence(symbol: str, run_lab: bool = False, include_profile: bool = True) -> dict:
    sym = normalize_symbol(symbol)
    try:
        fs = factors.factor_scores(sym)
    except ValueError:
        fs = {"symbol": sym, "in_universe": False, "note": "universe still building"}
    prof = None
    if include_profile:
        try:
            prof = profile.quant_profile(sym, lookback="3y", factors=False)
        except ValueError:
            prof = None
    lab_res = lab.run_lab(sym) if run_lab else lab.last_lab(sym)
    ta = _technical(sym)
    fa = _fundamental(fs)
    qu = _quant(sym, fs, prof, lab_res)
    pillars = {"technical": ta, "fundamental": fa, "quant": qu}
    avail = {k: v["score"] for k, v in pillars.items() if v and v.get("score") is not None}
    comp = sum(avail[k] * WEIGHTS[k] for k in avail) / sum(WEIGHTS[k] for k in avail) if avail else None
    spread = (max(avail.values()) - min(avail.values())) if len(avail) > 1 else 0
    stance = (None if comp is None else "bullish" if comp >= 65 else "leaning bullish" if comp >= 55
              else "bearish" if comp <= 35 else "leaning bearish" if comp <= 45 else "neutral")
    agree = "aligned" if spread < 20 else "mixed" if spread < 40 else "conflicting"
    out = {"symbol": sym, "composite": _r(comp, 1), "stance": stance, "agreement": agree, "pillars": pillars,
           "weights": WEIGHTS, "factor_scores": fs.get("scores"), "q_score": fs.get("q_score"),
           "sector": fs.get("sector"), "name": fs.get("name")}
    try:
        out["levels"] = _levels(sym, qu.get("regime"), qu.get("lab_best"), lab_res)
    except Exception as e:
        out["levels"] = {"error": str(e)}
    if prof:
        out["regime"] = prof["regime"]
        out["volatility"] = prof["volatility"]
    out["reading"] = _reading(out)
    return out


def _reading(o: dict) -> list[str]:
    p = o["pillars"]
    lines = []
    if o["composite"] is not None:
        lines.append(f"Composite {o['composite']:.0f}/100 ({o['stance']}); pillars {o['agreement']}.")
    if p["technical"]:
        lines.append(f"TA {p['technical']['score']:.0f}: trend {p['technical']['trend_score']}, momentum {p['technical']['momentum_score']}.")
    if p["fundamental"]:
        f = p["fundamental"]
        lines.append(f"FA {f['score']:.0f}: value {f['value']}, quality {f['quality']}, growth {f['growth']} (universe percentiles).")
    else:
        lines.append("FA: no factor ranks (not an eligible US common stock or universe building).")
    if p["quant"]["score"] is not None:
        lines.append(f"Quant {p['quant']['score']:.0f}: " + "; ".join(p["quant"]["notes"]) + ".")
    if not p["quant"].get("lab_as_of"):
        lines.append("Run the Strategy Lab (run_strategy_lab) to add a tested strategy signal.")
    if o["agreement"] == "conflicting":
        lines.append("Pillars disagree strongly: size down or wait for alignment.")
    return lines


def rank(symbols: list[str]) -> dict:
    syms = list(dict.fromkeys(normalize_symbol(s) for s in symbols))
    if not syms:
        return {"count": 0, "rows": [], "weights": WEIGHTS, "note": "No symbols to rank (empty watchlist)."}
    if len(syms) > 80:
        raise ValueError("At most 80 symbols")

    def one(s):
        try:
            c = confluence(s, run_lab=False)
            p = c["pillars"]
            lb = p["quant"].get("lab_best")
            return {"symbol": s, "name": c.get("name"), "sector": c.get("sector"), "composite": c["composite"],
                    "stance": c["stance"], "agreement": c["agreement"],
                    "technical": (p["technical"] or {}).get("score"), "fundamental": (p["fundamental"] or {}).get("score"),
                    "quant": p["quant"].get("score"), "q_score": c.get("q_score"), "regime": p["quant"].get("regime"),
                    "momentum_rank": p["quant"].get("momentum_rank"),
                    "lab_signal": lb and f"{lb['name']}: {'LONG' if (lb['position'] or 0) > 0 else 'flat'}",
                    "vol_pct": (c.get("volatility") or {}).get("realized_63d_pct"),
                    "preferred_plan": (c.get("levels") or {}).get("preferred_plan")}
        except Exception as e:
            return {"symbol": s, "error": str(e)}

    with ThreadPoolExecutor(4) as ex:
        rows = list(ex.map(one, syms))
    rows.sort(key=lambda r: -(r.get("composite") or -1))
    return {"count": len(rows), "rows": rows, "weights": WEIGHTS,
            "note": "Lab signals appear only for symbols whose Strategy Lab has been run (cached per symbol)."}
