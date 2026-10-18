"""Saved analyses/recommendations and their outcome tracking.

Each analysis records the price at creation. Evaluation replays subsequent bars to decide
whether the first target or the stop was hit first (stop wins ties: conservative), measures
max favourable/adverse excursion, return vs SPY over the same window, the realised R-multiple,
and aggregates a track record with confidence calibration (Brier score)."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta

import numpy as np
from sqlalchemy import select

from ..db import AnalysisRow, session_scope, utcnow
from ..schemas import AnalysisIn
from ..timeutil import format_ts, now_ts
from .events import bus
from .market import get_bars, get_quote, normalize_symbol

log = logging.getLogger(__name__)

HORIZON_DAYS = {"intraday": 2, "swing": 30, "position": 120, "long_term": 365}
DIRECTION = {"strong_buy": 1, "buy": 1, "hold": 0, "watch": 0, "sell": -1, "strong_sell": -1}


def to_dict(r: AnalysisRow) -> dict:
    return {
        "id": r.id, "symbol": r.symbol, "created_at": r.created_at.isoformat() + "Z", "action": r.action,
        "horizon": r.horizon, "confidence": r.confidence, "price_at_creation": r.price_at_creation,
        "entry_low": r.entry_low, "entry_high": r.entry_high, "stop": r.stop, "targets": r.targets,
        "summary": r.summary, "rationale": r.rationale, "scores": r.scores, "risks": r.risks,
        "catalysts": r.catalysts, "invalidation": r.invalidation, "annotation_tag": r.annotation_tag,
        "author": r.author,
    }


def save(a: AnalysisIn, author: str = "agent") -> dict:
    symbol = normalize_symbol(a.symbol)
    try:
        price = float(get_quote(symbol)["price"])
    except Exception as e:  # fall back to last daily close
        log.warning("quote failed for %s (%s); using last close", symbol, e)
        price = float(get_bars(symbol, "1d", lookback="10d").df["c"].iloc[-1])
    row = AnalysisRow(
        id=uuid.uuid4().hex[:12], symbol=symbol, created_at=utcnow(), action=a.action, horizon=a.horizon,
        confidence=a.confidence, price_at_creation=price, entry_low=a.entry_low, entry_high=a.entry_high,
        stop=a.stop, targets=a.targets, summary=a.summary, rationale=a.rationale, scores=a.scores,
        risks=a.risks, catalysts=a.catalysts, invalidation=a.invalidation, annotation_tag=a.annotation_tag,
        author=author,
    )
    with session_scope() as s:
        s.add(row)
        s.flush()
        d = to_dict(row)
    bus.publish("analysis.created", {k: v for k, v in d.items() if k != "rationale"})
    return d


def get(analysis_id: str) -> dict:
    with session_scope() as s:
        r = s.get(AnalysisRow, analysis_id)
        if r is None:
            raise KeyError(f"Analysis {analysis_id} not found")
        return to_dict(r)


def list_(symbol: str | None = None, limit: int = 20, include_rationale: bool = False) -> list[dict]:
    q = select(AnalysisRow).order_by(AnalysisRow.created_at.desc()).limit(limit)
    if symbol:
        q = q.where(AnalysisRow.symbol == normalize_symbol(symbol))
    with session_scope() as s:
        rows = [to_dict(r) for r in s.execute(q).scalars().all()]
    if not include_rationale:
        for r in rows:
            r.pop("rationale")
    return rows


def evaluate(analysis_id: str, now: datetime | None = None) -> dict:
    a = get(analysis_id)
    created = datetime.fromisoformat(a["created_at"].rstrip("Z")).replace(tzinfo=UTC)
    now = now or datetime.now(UTC)
    deadline = created + timedelta(days=HORIZON_DAYS[a["horizon"]])
    interval = "5m" if a["horizon"] == "intraday" else "1d"
    start = int(created.timestamp())
    bars = get_bars(a["symbol"], interval, start=start - (0 if interval == "5m" else 86400))
    df = bars.df
    if interval == "1d":
        # the creation-day bar is included only from the next session (we don't know the intraday path)
        df = df[df.index > int(datetime(created.year, created.month, created.day, tzinfo=UTC).timestamp())]
    else:
        df = df[df.index >= start]
    df = df[df.index <= int(min(now, deadline).timestamp())]
    ref = a["price_at_creation"]
    d = DIRECTION[a["action"]]
    out = {"id": a["id"], "symbol": a["symbol"], "action": a["action"], "horizon": a["horizon"],
           "confidence": a["confidence"], "created_at": a["created_at"], "price_at_creation": ref,
           "deadline": deadline.date().isoformat(), "bars_elapsed": len(df)}
    if df.empty or not ref:
        out.update({"status": "pending", "note": "no bars since creation yet"})
        return out
    last = float(df["c"].iloc[-1])
    raw = (last / ref - 1) * 100
    out["last_price"] = round(last, 4)
    out["return_pct"] = round(raw, 2)
    out["directional_return_pct"] = round(raw * d, 2) if d else None
    hi, lo = float(df["h"].max()), float(df["l"].min())
    if d:
        out["max_favorable_pct"] = round(((hi / ref - 1) if d > 0 else (1 - lo / ref)) * 100, 2)
        out["max_adverse_pct"] = round(((lo / ref - 1) if d > 0 else (1 - hi / ref)) * 100, 2)
    # first touch of stop / first target
    stop, target = a["stop"], (a["targets"][0] if a["targets"] else None)
    event, event_t, exit_px = None, None, None
    if d:
        for t, r in df.iterrows():
            stop_hit = stop is not None and (r.l <= stop if d > 0 else r.h >= stop)
            tgt_hit = target is not None and (r.h >= target if d > 0 else r.l <= target)
            if stop_hit:
                event, event_t, exit_px = "stopped_out", int(t), stop
                break
            if tgt_hit:
                event, event_t, exit_px = "target_hit", int(t), target
                break
    expired = now >= deadline
    if event:
        out["status"] = event
        out["event_date"] = format_ts(event_t, interval != "1d")
        out["success"] = event == "target_hit"
    elif expired:
        out["status"] = "expired"
        out["success"] = (raw * d > 0) if d else None
    else:
        out["status"] = "open"
        out["success"] = None
    if d and stop:
        risk = abs(ref - stop)
        px = exit_px if exit_px is not None else last
        out["r_multiple"] = round((px - ref) * d / risk, 2) if risk > 0 else None
    try:
        # SPY close on/before creation vs SPY close at the last evaluated bar
        spy = get_bars("SPY", "1d", start=start - 10 * 86400).df["c"]
        base = spy[spy.index <= start]
        end = spy[spy.index <= int(df.index[-1])]
        if len(base) and len(end):
            spy_ret = (float(end.iloc[-1]) / float(base.iloc[-1]) - 1) * 100
            out["spy_return_pct"] = round(spy_ret, 2)
            if d:
                out["alpha_vs_spy_pct"] = round((raw - spy_ret) * d, 2)
    except Exception as e:
        log.debug("benchmark comparison failed: %s", e)
    return out


def track_record(symbol: str | None = None, limit: int = 200, now: datetime | None = None) -> dict:
    rows = list_(symbol, limit=limit)
    evals = []
    for r in rows:
        try:
            evals.append(evaluate(r["id"], now=now))
        except Exception as e:
            evals.append({"id": r["id"], "symbol": r["symbol"], "status": "error", "error": str(e)})
    directional = [e for e in evals if DIRECTION.get(e.get("action"), 0) != 0]
    resolved = [e for e in directional if e.get("success") is not None]
    summary: dict = {
        "total": len(evals), "directional": len(directional), "resolved": len(resolved),
        "open": sum(1 for e in evals if e.get("status") == "open"),
    }
    if resolved:
        wins = [e for e in resolved if e["success"]]
        summary["hit_rate_pct"] = round(len(wins) / len(resolved) * 100, 1)
        summary["avg_directional_return_pct"] = round(float(np.mean([e.get("directional_return_pct") or 0 for e in resolved])), 2)
        rs = [e["r_multiple"] for e in resolved if e.get("r_multiple") is not None]
        if rs:
            summary["avg_r_multiple"] = round(float(np.mean(rs)), 2)
        alphas = [e["alpha_vs_spy_pct"] for e in resolved if e.get("alpha_vs_spy_pct") is not None]
        if alphas:
            summary["avg_alpha_vs_spy_pct"] = round(float(np.mean(alphas)), 2)
        # calibration: does stated confidence match realised hit rate?
        buckets = {}
        for lo, hi in ((0, 0.5), (0.5, 0.65), (0.65, 0.8), (0.8, 1.01)):
            b = [e for e in resolved if lo <= e["confidence"] < hi]
            if b:
                buckets[f"{lo:.2f}-{min(hi, 1):.2f}"] = {
                    "n": len(b), "mean_confidence": round(float(np.mean([e["confidence"] for e in b])), 2),
                    "hit_rate": round(sum(1 for e in b if e["success"]) / len(b), 2)}
        summary["calibration"] = buckets
        summary["brier_score"] = round(float(np.mean([(e["confidence"] - (1.0 if e["success"] else 0.0)) ** 2 for e in resolved])), 3)
        summary["brier_note"] = "0 = perfect, 0.25 = coin-flip at 50% confidence; lower is better"
        by_action = {}
        for e in resolved:
            ba = by_action.setdefault(e["action"], {"n": 0, "wins": 0})
            ba["n"] += 1
            ba["wins"] += int(bool(e["success"]))
        summary["by_action"] = by_action
    return {"summary": summary, "evaluations": evals, "as_of": format_ts(now_ts(), True)}
