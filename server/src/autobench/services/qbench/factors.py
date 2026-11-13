"""Cross-sectional factor scores over the market universe (~6,000 US stocks).

Each factor is a percentile rank (0-100, higher = more of the rewarded characteristic) among
*eligible* stocks: price ≥ $3, market cap ≥ $300M and ≥ $1M average daily dollar volume (micro caps
are excluded because anomalies there are mostly untradeable). Inputs come from the universe table
(SEC XBRL TTM fundamentals + ~300 daily bars):

  value     earnings yield (1/PE), FCF yield, book yield, sales yield         Fama-French; Asness et al.
  quality   ROE, ROA, gross & operating margin, low debt/equity              Novy-Marx; Asness-Frazzini-Pedersen QMJ
  growth    TTM sales & EPS growth, latest-quarter sales growth
  momentum  12-1 month return and nearness to the 52-week high              Jegadeesh-Titman; George-Hwang
  low_vol   low 1-year volatility and low beta                              Ang et al.; Frazzini-Pedersen

q_score = weighted average of the factor ranks (DEFAULT_WEIGHTS), re-ranked to 0-100."""

from __future__ import annotations

import numpy as np
import pandas as pd

FACTORS = ("value", "quality", "growth", "momentum", "low_vol")
DEFAULT_WEIGHTS = {"value": 0.2, "quality": 0.25, "growth": 0.1, "momentum": 0.3, "low_vol": 0.15}
COLUMNS = {f: f"q_{f.replace('_', '')}" for f in FACTORS} | {"score": "q_score"}  # q_value ... q_lowvol

# factor -> [(column, higher_is_better, transform)]
_INPUTS = {
    "value": [("pe", "inv"), ("pfcf", "inv"), ("pb", "inv"), ("ps", "inv")],
    "quality": [("roe", "clip"), ("roa", "clip"), ("gross_margin", "clip"), ("oper_margin", "clip"), ("debt_eq", "neg")],
    "growth": [("sales_growth", "clip"), ("eps_growth", "clip"), ("sales_qoq", "clip")],
    "momentum": [("mom_12_1", "id"), ("from_high_52w", "id")],
    "low_vol": [("volatility_1y", "neg"), ("beta", "neg")],
}


def _col(df: pd.DataFrame, name: str) -> pd.Series:
    return pd.to_numeric(df[name], errors="coerce") if name in df.columns else pd.Series(np.nan, index=df.index)


def eligible(df: pd.DataFrame) -> pd.Series:
    price = _col(df, "price")
    mc = _col(df, "market_cap")
    dv = _col(df, "dollar_volume")
    ok = (price >= 3) & (mc >= 3e8)
    return ok & (dv.isna() | (dv >= 1e6))


def _signal(df: pd.DataFrame, col: str, how: str) -> pd.Series:
    x = _col(df, col)
    if col == "mom_12_1" and x.isna().all():
        x = ((1 + _col(df, "perf_1y") / 100) / (1 + _col(df, "perf_1m") / 100) - 1) * 100
    if col == "volatility_1y" and x.isna().all():
        x = _col(df, "volatility_1m")
    if how == "inv":
        return (1 / x).where(x > 0)
    if how == "neg":
        return -x
    if how == "clip":
        lo, hi = x.quantile(0.01), x.quantile(0.99)
        return x.clip(lo, hi)
    return x


def _rank(s: pd.Series, groups: pd.Series | None = None) -> pd.Series:
    if groups is None:
        return s.rank(pct=True) * 100
    return s.groupby(groups).rank(pct=True) * 100


def factor_frame(df: pd.DataFrame, weights: dict | None = None, sector_neutral: bool = False) -> pd.DataFrame:
    """q_* columns for every row (NaN for ineligible rows or too little data)."""
    out = pd.DataFrame(index=df.index)
    ok = eligible(df)
    sub = df[ok]
    groups = sub["sector"] if sector_neutral and "sector" in sub.columns else None
    for f in FACTORS:
        ranks = []
        for col, how in _INPUTS[f]:
            ranks.append(_rank(_signal(sub, col, how), groups))
        m = pd.concat(ranks, axis=1)
        need = 1 if f in ("low_vol", "momentum") else 2
        avg = m.mean(axis=1).where(m.notna().sum(axis=1) >= need)
        out[COLUMNS[f]] = _rank(avg, groups).reindex(df.index)
    w = {**DEFAULT_WEIGHTS, **(weights or {})}
    w = {k: float(v) for k, v in w.items() if k in FACTORS and v}
    if not w:
        raise ValueError(f"weights must give at least one of {', '.join(FACTORS)} a non-zero weight")
    tot = sum(abs(v) for v in w.values())
    parts = pd.concat([out[COLUMNS[k]] * v for k, v in w.items()], axis=1)
    have = pd.concat([out[COLUMNS[k]].notna() * abs(v) for k, v in w.items()], axis=1).sum(axis=1)
    comp = (parts.sum(axis=1, min_count=1) / have.replace(0, np.nan)).where(have >= 0.5 * tot)
    out["q_score"] = comp.rank(pct=True) * 100
    return out


def add_factor_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Called by the universe table builder: adds q_* (default weights, market-wide ranks)."""
    try:
        f = factor_frame(df)
    except Exception:  # partial universe (layers still building): leave the columns empty
        f = pd.DataFrame(np.nan, index=df.index, columns=list(COLUMNS.values()))
    for c in f.columns:
        df[c] = f[c].round(1)
    return df


def _universe():
    from ..universe import universe

    return universe


def _table() -> pd.DataFrame:
    t = _universe().table()
    if t.empty:
        raise ValueError("The market universe is still being built; retry in a minute")
    return t


def factor_scores(symbol: str) -> dict:
    """One stock's factor ranks (market-wide and within its sector) with the raw inputs."""
    sym = symbol.strip().upper()
    t = _table()
    if sym not in t.index:
        return {"symbol": sym, "in_universe": False,
                "note": "Not in the US common-stock universe (ETF, ADR without XBRL data, index or unknown)."}
    row = t.loc[sym]
    sect = factor_frame(t, sector_neutral=True)
    raw = {}
    for f in FACTORS:
        for col, _ in _INPUTS[f]:
            v = row.get(col) if col in t.columns else None
            if col == "mom_12_1" and (v is None or pd.isna(v)):
                continue
            raw[col] = None if v is None or pd.isna(v) else round(float(v), 3)
    scores = {f: _num(row.get(COLUMNS[f])) for f in FACTORS}
    sector_scores = {f: _num(sect.loc[sym, COLUMNS[f]]) for f in FACTORS}
    out = {
        "symbol": sym, "in_universe": True, "name": row.get("name"), "sector": row.get("sector"),
        "eligible": bool(eligible(t).get(sym, False)),
        "scores": scores, "q_score": _num(row.get("q_score")),
        "sector_relative": {**sector_scores, "q_score": _num(sect.loc[sym, "q_score"])},
        "inputs": raw, "weights": DEFAULT_WEIGHTS,
        "reading": _reading(scores),
    }
    if not out["eligible"]:
        out["note"] = "Below the eligibility bar (price ≥ $3, cap ≥ $300M, $1M/day): no ranks."
    return out


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return round(f, 1) if np.isfinite(f) else None


def _reading(scores: dict) -> list[str]:
    lines = []
    for f, v in scores.items():
        if v is None:
            continue
        if v >= 80:
            lines.append(f"top-quintile {f} ({v:.0f})")
        elif v <= 20:
            lines.append(f"bottom-quintile {f} ({v:.0f})")
    return lines or ["no extreme factor exposure (all ranks between 20 and 80)"]


def factor_screen(weights: dict | None = None, universe_name: str = "all", sector_neutral: bool = False,
                  min_score: float | None = None, sector: str | None = None, limit: int = 30,
                  symbols: list[str] | None = None) -> dict:
    """Rank the universe (or a list) by a weighted factor composite."""
    t = _table()
    f = factor_frame(t, weights, sector_neutral)
    mask = f["q_score"].notna()
    if symbols:
        mask &= t.index.isin([s.upper() for s in symbols])
    elif universe_name == "sp500" and "sp500" in t.columns:
        mask &= t["sp500"].fillna(False).astype(bool)
    elif universe_name.startswith("watchlist:"):
        from ..workspace import get_watchlist

        mask &= t.index.isin([s for s in get_watchlist(universe_name.split(":", 1)[1])["symbols"] if not s.startswith("###")])
    if sector:
        mask &= t["sector"] == sector
    if min_score is not None:
        mask &= f["q_score"] >= min_score
    f = f[mask].sort_values("q_score", ascending=False)
    rows = []
    for sym in f.index[:limit]:
        r = t.loc[sym]
        rows.append({"symbol": sym, "name": r.get("name"), "sector": r.get("sector"),
                     "market_cap": _num(r.get("market_cap")), "price": _num(r.get("price")),
                     **{k: _num(f.loc[sym, c]) for k, c in COLUMNS.items()},
                     "pe": _num(r.get("pe")), "roe": _num(r.get("roe")), "mom_12_1": _num(r.get("mom_12_1")),
                     "volatility_1y": _num(r.get("volatility_1y"))})
    w = {**DEFAULT_WEIGHTS, **(weights or {})}
    return {"weights": w, "sector_neutral": sector_neutral, "universe": "symbols" if symbols else universe_name,
            "total": int(mask.sum()), "rows": rows,
            "note": "Ranks are percentiles among eligible US stocks (price ≥ $3, cap ≥ $300M, ≥ $1M/day)."}
