"""Trading journal: the account's round-trip trades with what the market did during and after each one,
the user's recent chart drawings, aggregate performance, and notes per symbol / trade.

Per trade (from trading.trades(), i.e. the broker's fills for the selected account):
- in the trade: the best and worst price reached (MFE / MAE) and how much of the best move the exit kept;
- after the exit: the price 1, 5 and 20 sessions later and the best / worst within those 20 sessions, as
  "what holding on would have made or cost" against the actual exit;
- alternative exits: at the planned stop / target, at the best and worst price while in the trade.
"""

from __future__ import annotations

import logging
import math
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from .. import db
from ..db import AnnotationRow, session_scope
from . import workspace
from .annotations import to_dict as annotation_dict
from .market import get_bars

log = logging.getLogger(__name__)

POST_SESSIONS = 20  # how far after the exit the "what if I had stayed in" view looks
TRADE_NOTE_TAG = "trade:"  # notes tagged trade:<trade id> belong to one trade


def _r(v: float | None, nd: int = 2) -> float | None:
    return None if v is None or (isinstance(v, float) and math.isnan(v)) else round(float(v), nd)


def _ticket_levels(ticket_ids: set[str]) -> dict[str, dict]:
    if not ticket_ids:
        return {}
    with session_scope() as s:
        rows = s.scalars(select(db.OrderTicketRow).where(db.OrderTicketRow.id.in_(ticket_ids))).all()
        return {r.id: {"stop": r.spec.get("stop_loss"), "target": r.spec.get("take_profit"), "source": r.source,
                       "rationale": r.rationale, "analysis_id": r.analysis_id} for r in rows}


def analyze_trade(t: dict, now: int | None = None) -> dict:
    """Excursions in the trade, the path after the exit, and alternative exits, for one round trip."""
    now = now or int(time.time())
    d = 1 if t["side"] == "long" else -1
    entry, exit_ = t.get("entry_avg"), t.get("exit_avg")
    qty = t.get("exit_qty") or t.get("max_qty") or 0
    closed = t["status"] == "closed"
    end = t["closed_at"] or now
    out: dict = {}
    if not entry:
        return out
    # inside the trade: intraday bars for short holds, daily otherwise
    hold = end - t["opened_at"]
    iv = "5m" if hold <= 3 * 86400 and now - t["opened_at"] < 50 * 86400 else "1d"
    try:
        start = t["opened_at"] - (0 if iv == "5m" else 86400)
        df = get_bars(t["symbol"], iv, start=start, end=end + (300 if iv == "5m" else 86400)).df
        df = df[(df.index >= t["opened_at"] - (300 if iv == "5m" else 86400)) & (df.index <= end)]
    except Exception as e:  # missing data must not hide the trade
        log.info("journal: bars for %s unavailable: %s", t["symbol"], e)
        df = None
    fill_px = [f["price"] for f in t.get("fills", [])]
    if (df is not None and len(df)) or fill_px:
        # fills happened at real prices: the excursion is never smaller than the fills themselves (and with no
        # bars in the window, e.g. a trade outside market hours, the fills are all there is)
        has_bars = df is not None and len(df)
        hi = max([float(df["h"].max())] * bool(has_bars) + fill_px)
        lo = min([float(df["l"].min())] * bool(has_bars) + fill_px)
        best, worst = (hi, lo) if d > 0 else (lo, hi)
        out["in_trade"] = {
            "interval": iv if has_bars else "fills", "best_price": _r(best), "worst_price": _r(worst),
            "mfe": _r((best - entry) * d * qty), "mae": _r((worst - entry) * d * qty),
            "mfe_pct": _r((best - entry) * d / entry * 100), "mae_pct": _r((worst - entry) * d / entry * 100),
        }
        if closed and exit_ is not None and (best - entry) * d > 0:
            out["in_trade"]["exit_efficiency_pct"] = _r((exit_ - entry) * d / ((best - entry) * d) * 100, 1)
    if closed and exit_ is not None:
        try:
            post = get_bars(t["symbol"], "1d", start=end - 86400, end=min(now, end + 40 * 86400)).df
            post = post[post.index > end - 86400 + 1].iloc[: POST_SESSIONS + 1]
            # the exit day's own bar is the first one when the exit was during that session
            if len(post) and datetime.fromtimestamp(int(post.index[0]), UTC).date() == datetime.fromtimestamp(end, UTC).date():
                post = post.iloc[1:]
        except Exception as e:
            log.info("journal: post-exit bars for %s unavailable: %s", t["symbol"], e)
            post = None
        if post is not None and len(post):
            def held(px: float) -> float:
                return _r((px - exit_) * d * qty)  # + = staying in would have made more

            marks = {}
            for n in (1, 5, POST_SESSIONS):
                if len(post) >= n:
                    px = float(post["c"].iloc[n - 1])
                    marks[f"{n}d"] = {"price": _r(px), "vs_exit": held(px), "pct": _r((px - exit_) * d / exit_ * 100)}
            hi, lo = float(post["h"].max()), float(post["l"].min())
            best_px, worst_px = (hi, lo) if d > 0 else (lo, hi)
            last = float(post["c"].iloc[-1])
            out["after_exit"] = {
                "sessions": len(post), "marks": marks,
                "best_price": _r(best_px), "best_vs_exit": held(best_px),
                "worst_price": _r(worst_px), "worst_vs_exit": held(worst_px),
                "last_price": _r(last), "last_vs_exit": held(last),
                "verdict": ("exited early: price kept going your way" if held(last) > 0 and held(best_px) > abs(held(worst_px))
                            else "good exit: price went against the trade afterwards" if held(last) < 0
                            else "neutral"),
            }
    # alternative exits
    alts = []
    if closed and exit_ is not None:
        def alt(label: str, px: float | None):
            if px:
                pnl = (px - entry) * d * qty - (t.get("commission") or 0)
                alts.append({"label": label, "price": _r(px), "net_pnl": _r(pnl), "vs_actual": _r(pnl - t["net_pnl"])})

        alt("At the planned stop", t.get("planned_stop"))
        alt("At the planned target", t.get("planned_target"))
        if out.get("in_trade"):
            alt("At the best price in the trade", out["in_trade"]["best_price"])
            alt("At the worst price in the trade", out["in_trade"]["worst_price"])
        if out.get("after_exit"):
            for k, m in out["after_exit"]["marks"].items():
                alt(f"Held {k.removesuffix('d')} more session{'s' if k != '1d' else ''}", m["price"])
    out["alternatives"] = alts
    return out


def price_path(symbol: str, opened_at: int, closed_at: int | None, now: int | None = None) -> list[list[float]]:
    """Daily closes from ~10 sessions before the entry to POST_SESSIONS after the exit (for the mini chart)."""
    now = now or int(time.time())
    end = min(now, (closed_at or now) + 40 * 86400)
    try:
        df = get_bars(symbol, "1d", start=opened_at - 20 * 86400, end=end).df
    except Exception:
        return []
    before = df[df.index < opened_at].iloc[-10:]
    during = df[(df.index >= opened_at) & (df.index <= (closed_at or now))]
    after = df[df.index > (closed_at or now)].iloc[:POST_SESSIONS]
    rows = list(before["c"].items()) + list(during["c"].items()) + list(after["c"].items())
    return [[int(k), round(float(v), 4)] for k, v in rows]


def trades(symbol: str | None = None, status: str | None = None, limit: int = 100, analyze: bool = True) -> dict:
    from .trading import trading

    res = trading.trades(symbol, status, limit)
    levels = _ticket_levels({tid for t in res["trades"] for tid in t["ticket_ids"]})
    notes = _trade_notes()
    for t in res["trades"]:
        lv = next((levels[i] for i in t["ticket_ids"] if i in levels), {})
        t["planned_target"] = lv.get("target")
        t["proposed_by"] = lv.get("source")
        t["analysis_id"] = lv.get("analysis_id")
        t["notes"] = notes.get(t["id"], [])
    if analyze and res["trades"]:
        now = int(time.time())
        with ThreadPoolExecutor(max_workers=6) as pool:
            for t, a in zip(res["trades"], pool.map(lambda x: analyze_trade(x, now), res["trades"])):
                t["analysis"] = a
    res["account"] = trading.status().get("account")
    return res


def trade_detail(trade_id: str) -> dict:
    res = trades(limit=100000)
    t = next((x for x in res["trades"] if x["id"] == trade_id), None)
    if t is None:
        raise KeyError(f"Trade {trade_id} not found")
    t["path"] = price_path(t["symbol"], t["opened_at"], t["closed_at"])
    return t


def _trade_notes() -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = defaultdict(list)
    for n in workspace.list_notes(limit=100000, include_content=True):
        for tag in n.get("tags") or []:
            if tag.startswith(TRADE_NOTE_TAG):
                out[tag[len(TRADE_NOTE_TAG):]].append({"id": n["id"], "title": n["title"], "content": n["content"],
                                                       "updated_at": n["updated_at"]})
    return out


def drawings(days: int = 30, symbol: str | None = None, author: str | None = None) -> dict:
    """Chart drawings created or changed in the last [days] days, newest first, grouped by symbol."""
    since = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=days)
    q = select(AnnotationRow).where(AnnotationRow.updated_at >= since).order_by(AnnotationRow.updated_at.desc())
    if symbol:
        q = q.where(AnnotationRow.symbol == symbol.upper())
    if author:
        q = q.where(AnnotationRow.author == author)
    with session_scope() as s:
        items = [annotation_dict(r) for r in s.scalars(q).all()]
    groups: dict[str, list[dict]] = defaultdict(list)
    for a in items:
        groups[a["symbol"]].append(a)
    return {"days": days, "count": len(items),
            "symbols": [{"symbol": k, "count": len(v), "last": v[0]["updated_at"], "items": v} for k, v in groups.items()]}


def _max_drawdown(values: list[float]) -> tuple[float, float]:
    peak, dd, dd_pct = -math.inf, 0.0, 0.0
    for v in values:
        peak = max(peak, v)
        dd = min(dd, v - peak)
        if peak > 0:
            dd_pct = min(dd_pct, (v - peak) / peak * 100)
    return dd, dd_pct


def performance(days: int = 365) -> dict:
    """Aggregate metrics over the closed trades (and the account's equity curve)."""
    from .trading import trading

    res = trades(limit=100000, analyze=True)
    allt = res["trades"]
    since = int(time.time()) - days * 86400
    closed = sorted([t for t in allt if t["status"] == "closed" and (t["closed_at"] or 0) >= since],
                    key=lambda t: t["closed_at"])
    stats = dict(res["stats"])
    # cumulative realised P&L and its drawdown
    cum, curve = 0.0, []
    for t in closed:
        cum += t["net_pnl"]
        curve.append({"t": t["closed_at"], "pnl": round(cum, 2), "symbol": t["symbol"]})
    dd, _ = _max_drawdown([0.0] + [c["pnl"] for c in curve])
    # streaks
    best_streak = worst_streak = cur = 0
    for t in closed:
        if t["net_pnl"] > 0:
            cur = cur + 1 if cur > 0 else 1
        else:
            cur = cur - 1 if cur < 0 else -1
        best_streak, worst_streak = max(best_streak, cur), min(worst_streak, cur)

    def group(key) -> list[dict]:
        g: dict[str, list[dict]] = defaultdict(list)
        for t in closed:
            g[key(t)].append(t)
        rows = []
        for k, ts in g.items():
            wins = [x for x in ts if x["net_pnl"] > 0]
            rows.append({"key": k, "trades": len(ts), "net_pnl": round(sum(x["net_pnl"] for x in ts), 2),
                         "win_rate": round(len(wins) / len(ts) * 100, 1)})
        return rows

    by_symbol = sorted(group(lambda t: t["symbol"]), key=lambda r: -r["net_pnl"])
    by_side = group(lambda t: t["side"])
    by_month = sorted(group(lambda t: datetime.fromtimestamp(t["closed_at"], UTC).strftime("%Y-%m")), key=lambda r: r["key"])
    weekdays = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    by_weekday = sorted(group(lambda t: weekdays[datetime.fromtimestamp(t["opened_at"], UTC).weekday()]),
                        key=lambda r: weekdays.index(r["key"]))
    by_source = group(lambda t: "agent proposal" if t.get("proposed_by") == "agent" else "your own")

    def avg(xs):
        xs = [x for x in xs if x is not None]
        return round(sum(xs) / len(xs), 2) if xs else None

    wins = [t for t in closed if t["net_pnl"] > 0]
    losses = [t for t in closed if t["net_pnl"] <= 0]
    after = [t["analysis"].get("after_exit") for t in closed if t.get("analysis", {}).get("after_exit")]
    eff = [t["analysis"]["in_trade"].get("exit_efficiency_pct") for t in closed if t.get("analysis", {}).get("in_trade")]
    exits = {
        "avg_exit_efficiency_pct": avg(eff),
        "left_on_table_5d": round(sum(max(0, (a["marks"].get("5d") or {}).get("vs_exit") or 0) for a in after), 2),
        "saved_by_exit_5d": round(-sum(min(0, (a["marks"].get("5d") or {}).get("vs_exit") or 0) for a in after), 2),
        "exited_too_early": sum(1 for a in after if a["verdict"].startswith("exited early")),
        "good_exits": sum(1 for a in after if a["verdict"].startswith("good exit")),
        "avg_mfe": avg([t["analysis"]["in_trade"]["mfe"] for t in closed if t.get("analysis", {}).get("in_trade")]),
        "avg_mae": avg([t["analysis"]["in_trade"]["mae"] for t in closed if t.get("analysis", {}).get("in_trade")]),
    }
    eq = trading.equity(days)
    eq_dd, eq_dd_pct = _max_drawdown([e["net_liquidation"] for e in eq if e.get("net_liquidation")])
    return {
        "days": days, "account": res.get("account"), "stats": stats,
        "period": {"closed_trades": len(closed), "net_pnl": round(sum(t["net_pnl"] for t in closed), 2),
                   "max_drawdown": round(dd, 2), "best_streak": best_streak, "worst_streak": -worst_streak,
                   "avg_hold_hours_winners": avg([t["holding_seconds"] / 3600 for t in wins]),
                   "avg_hold_hours_losers": avg([t["holding_seconds"] / 3600 for t in losses]),
                   "avg_return_pct": avg([t.get("return_pct") for t in closed])},
        "exits": exits, "curve": curve,
        "equity": {"points": eq[-500:], "max_drawdown": round(eq_dd, 2), "max_drawdown_pct": round(eq_dd_pct, 2)},
        "by_symbol": by_symbol, "by_side": by_side, "by_month": by_month, "by_weekday": by_weekday, "by_source": by_source,
    }


def notes(symbol: str | None = None) -> dict:
    """Notes grouped by symbol (notes without a symbol under ''), with trade notes marked."""
    items = workspace.list_notes(symbol=symbol, limit=100000)
    groups: dict[str, list[dict]] = defaultdict(list)
    for n in items:
        n["trade_id"] = next((t[len(TRADE_NOTE_TAG):] for t in n.get("tags") or [] if t.startswith(TRADE_NOTE_TAG)), None)
        groups[n["symbol"] or ""].append(n)
    return {"symbols": [{"symbol": k, "count": len(v), "items": v} for k, v in sorted(groups.items(), key=lambda kv: kv[0] or "~")]}
