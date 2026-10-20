"""Account history from broker statements: import (file upload or IBKR Flex Web Service), the current
account state, and performance that keeps three things apart:

- growth: the change in account value (NAV);
- contributions: money you put in or took out (deposits, withdrawals, position transfers), which grow the
  account without being a return;
- investment gain = growth - contributions, split into trading & market P&L (realised and the rest:
  unrealised change, FX) and income/costs (dividends, withholding tax, interest, fees).

Returns: time-weighted (TWR, chain-linked Modified Dietz between NAV points, so deposits and their
timing do not count) and money-weighted (MWR / IRR of your actual cash flows).

NAV history comes from statements (daily with a Flex Query) and, after the last statement, from the
app's own snapshots of the connected account."""

from __future__ import annotations

import logging
import time
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import delete, func, select

from .. import db
from ..timeutil import ET as EASTERN
from .events import bus
from .statements import CONTRIBUTION_KINDS, StatementError, parse

log = logging.getLogger(__name__)

INCOME_KINDS = ("dividend", "tax", "interest", "fee", "other")
FLEX_KEY = "ibkr_flex"
FLEX_BASE = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService"


def _r(v: float | None, nd: int = 2) -> float | None:
    return None if v is None else round(v, nd)


def _today() -> str:
    return datetime.now(EASTERN).date().isoformat()


def _et_date(ts: int) -> str:
    return datetime.fromtimestamp(ts, EASTERN).date().isoformat()


def _days(d0: str, d1: str) -> int:
    return (date.fromisoformat(d1) - date.fromisoformat(d0)).days


# ===================================================================================== import

def _duplicate_fill(s, t: dict) -> bool:
    """An imported execution the app already recorded live under another id (CSV rows have no
    execution ids, and an order-level row stands for several executions)."""
    if t["time"] is None:
        return False
    near = s.scalars(select(db.FillRow).where(
        db.FillRow.account == t["account"], db.FillRow.symbol == t["symbol"], db.FillRow.side == t["side"],
        db.FillRow.time >= t["time"] - 300, db.FillRow.time <= t["time"] + 300)).all()
    if not near:
        return False
    if any(abs(f.qty - t["qty"]) < 1e-6 and abs(f.price - t["price"]) <= 1e-4 * t["price"] for f in near):
        return True
    q = sum(f.qty for f in near)
    vwap = sum(f.qty * f.price for f in near) / q if q else 0
    return abs(q - t["qty"]) < 1e-6 and abs(vwap - t["price"]) <= 1e-4 * t["price"]


def import_statement(text: str, filename: str | None = None, source: str = "upload") -> dict:
    """Parse and store a statement. Re-importing the same or an overlapping statement is safe: rows are
    matched by their ids (and executions also by symbol/side/qty/price/time) and only new ones added;
    NAV and positions for a date are replaced by the newer statement."""
    st = parse(text, filename)
    if not st["accounts"]:
        raise StatementError("The statement has no account")
    iid = uuid.uuid4().hex[:12]
    counts = defaultdict(int)
    with db.session_scope() as s:
        for t in st["trades"]:
            if not t["symbol"] or t["time"] is None:
                continue
            r = s.get(db.FillRow, t["exec_id"])
            if r is not None:  # recorded live: add what the statement knows better
                r.asset_class = r.asset_class or t["asset_class"]
                r.multiplier = r.multiplier or t["multiplier"]
                r.currency = r.currency or t["currency"]
                if r.commission is None:
                    r.commission = t["commission"]
                if r.realized_pnl is None:
                    r.realized_pnl = t["realized_pnl"]
                counts["trades_known"] += 1
                continue
            if _duplicate_fill(s, t):
                counts["trades_known"] += 1
                continue
            s.add(db.FillRow(exec_id=t["exec_id"], account=t["account"], broker="ibkr", order_id=t["order_id"], ref=None,
                             symbol=t["symbol"], side=t["side"], qty=t["qty"], price=t["price"], time=t["time"],
                             commission=t["commission"], realized_pnl=t["realized_pnl"], asset_class=t["asset_class"],
                             multiplier=t["multiplier"], currency=t["currency"], import_id=iid))
            s.flush()
            counts["trades"] += 1
        for c in st["cash"]:
            if not c["date"]:
                continue
            known = s.get(db.CashFlowRow, c["id"])
            if known is not None:  # keep the row, but take the newer statement's values (e.g. a corrected date)
                for k, v in c.items():
                    setattr(known, k, v)
                counts["cash_known"] += 1
                continue
            s.add(db.CashFlowRow(**c, import_id=iid))
            s.flush()
            counts["cash"] += 1
        for n in st["nav"]:
            r = s.get(db.NavRow, (n["account"], n["date"]))
            if r is None:
                s.add(db.NavRow(**n, import_id=iid))
                counts["nav"] += 1
            else:
                for k, v in n.items():
                    setattr(r, k, v)
                r.import_id = iid
                counts["nav_updated"] += 1
            s.flush()
        # a statement lists every open position on its date: what it leaves out was closed, so replace
        # that date's snapshot instead of merging into it
        for acct, day in {(p["account"], p["date"]) for p in st["positions"] if p["date"]}:
            s.execute(delete(db.PositionSnapshotRow).where(db.PositionSnapshotRow.account == acct,
                                                           db.PositionSnapshotRow.date == day))
        for p in st["positions"]:
            if not p["date"] or not p["symbol"]:
                continue
            s.merge(db.PositionSnapshotRow(**p, import_id=iid))
            counts["positions"] += 1
        s.add(db.StatementImportRow(id=iid, kind=st["kind"], source=source, filename=filename,
                                    accounts=sorted(st["accounts"]), from_date=st["from_date"], to_date=st["to_date"],
                                    counts=dict(counts), summary={**st["summary"], "_accounts": st["accounts"]}))
    out = imports(limit=1)[0]
    bus.publish("portfolio.imported", out)
    return out


def _import_dict(r: db.StatementImportRow) -> dict:
    return {"id": r.id, "kind": r.kind, "source": r.source, "filename": r.filename, "accounts": r.accounts,
            "from_date": r.from_date, "to_date": r.to_date, "counts": r.counts,
            "created_at": r.created_at.replace(tzinfo=UTC).isoformat()}


def imports(limit: int = 50) -> list[dict]:
    with db.session_scope() as s:
        rows = s.scalars(select(db.StatementImportRow).order_by(db.StatementImportRow.created_at.desc()).limit(limit)).all()
        return [_import_dict(r) for r in rows]


def delete_import(import_id: str) -> dict:
    """Remove what an import added (rows another import also contained go with it; re-import that one)."""
    with db.session_scope() as s:
        if s.get(db.StatementImportRow, import_id) is None:
            raise KeyError(import_id)
        n = 0
        for model in (db.FillRow, db.CashFlowRow, db.NavRow, db.PositionSnapshotRow):
            n += s.execute(delete(model).where(model.import_id == import_id)).rowcount or 0
        s.execute(delete(db.StatementImportRow).where(db.StatementImportRow.id == import_id))
    bus.publish("portfolio.imported", {"id": import_id, "deleted": True})
    return {"deleted": import_id, "rows": n}


# ===================================================================================== data access

def accounts() -> list[dict]:
    """Accounts with history (statements, recorded fills or account snapshots)."""
    out: dict[str, dict] = {}
    with db.session_scope() as s:
        for acct, d0, d1, cur in s.execute(select(db.NavRow.account, func.min(db.NavRow.date), func.max(db.NavRow.date),
                                                  func.max(db.NavRow.currency)).group_by(db.NavRow.account)):
            out[acct] = {"account": acct, "currency": cur, "from": d0, "to": d1}
        for acct, t0, t1 in s.execute(select(db.EquityRow.account, func.min(db.EquityRow.t), func.max(db.EquityRow.t))
                                      .group_by(db.EquityRow.account)):
            a = out.setdefault(acct, {"account": acct, "currency": None, "from": _et_date(t0), "to": _et_date(t1)})
            a["from"], a["to"] = min(a["from"], _et_date(t0)), max(a["to"], _et_date(t1))
        for (acct,) in s.execute(select(db.CashFlowRow.account).distinct()):
            out.setdefault(acct, {"account": acct, "currency": None, "from": None, "to": None})
    return sorted(out.values(), key=lambda a: a["to"] or "", reverse=True)


def default_account() -> str | None:
    from .trading import trading

    have = [a["account"] for a in accounts()]
    try:
        cur = trading.status().get("account")
    except Exception:
        cur = None
    if cur and cur in have:
        return cur
    return have[0] if have else cur


def nav_series(account: str) -> list[tuple[str, float]]:
    """End-of-day NAV: statement values, then the app's own snapshots for later days (last of each day)."""
    with db.session_scope() as s:
        stmt = {r.date: r.total for r in s.scalars(select(db.NavRow).where(db.NavRow.account == account))}
        snaps = s.execute(select(db.EquityRow.t, db.EquityRow.net_liquidation).where(db.EquityRow.account == account)
                          .order_by(db.EquityRow.t)).all()
    last = max(stmt) if stmt else ""
    live: dict[str, float] = {}
    for t, v in snaps:
        d = _et_date(t)
        if d > last and v:
            live[d] = v
    return sorted({**stmt, **live}.items())


def cash_flows(account: str | None = None, kind: str | None = None, start: str | None = None, end: str | None = None,
               limit: int = 1000) -> list[dict]:
    account = account or default_account()
    with db.session_scope() as s:
        q = select(db.CashFlowRow).where(db.CashFlowRow.account == account).order_by(db.CashFlowRow.date.desc()).limit(limit)
        if kind:
            kinds = ("deposit", "withdrawal", "transfer") if kind == "contributions" else \
                    INCOME_KINDS if kind == "income" else (kind,)
            q = q.where(db.CashFlowRow.kind.in_(kinds))
        if start:
            q = q.where(db.CashFlowRow.date >= start)
        if end:
            q = q.where(db.CashFlowRow.date <= end)
        return [{"id": r.id, "date": r.date, "kind": r.kind, "amount": r.amount, "amount_local": r.amount_local,
                 "currency": r.currency, "symbol": r.symbol, "description": r.description} for r in s.scalars(q).all()]


def _fills(account: str) -> list[dict]:
    with db.session_scope() as s:
        return [{"time": r.time, "commission": r.commission or 0, "realized_pnl": r.realized_pnl}
                for r in s.scalars(select(db.FillRow).where(db.FillRow.account == account))]


# ===================================================================================== returns

def _xirr(flows: list[tuple[str, float]]) -> float | None:
    """Annualised internal rate of return of dated investor cash flows (out negative, in positive)."""
    if len(flows) < 2 or not any(v > 0 for _, v in flows) or not any(v < 0 for _, v in flows):
        return None
    d0 = min(d for d, _ in flows)
    ts = [(_days(d0, d) / 365.0, v) for d, v in flows]
    if max(t for t, _ in ts) <= 0:  # no time passed: no rate
        return None

    def npv(r: float) -> float:
        return sum(v / (1 + r) ** t for t, v in ts)

    lo, hi = -0.9999, 100.0
    f_lo, f_hi = npv(lo), npv(hi)
    if f_lo * f_hi > 0:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2
        f = npv(mid)
        if abs(f) < 1e-7:
            break
        if (f > 0) == (f_lo > 0):
            lo, f_lo = mid, f
        else:
            hi = mid
    return (lo + hi) / 2


def _dietz(v0: float, v1: float, d0: str, d1: str, flows: list[tuple[str, float]]) -> float | None:
    """Modified Dietz return over (d0, d1]. Flows are dated by the day the NAV first includes them, so between
    consecutive trading days (gaps of up to a long weekend) they count from the start of the interval: IBKR's
    own daily-linked TWR, which this reproduces. Over longer gaps (sparse NAV, e.g. an Activity Statement's start
    and end values) each flow is weighted by the share of the interval it was in the account."""
    span = _days(d0, d1)
    cf = sum(v for _, v in flows)
    weighted = cf if span <= 5 else sum(v * (_days(d, d1) + 1) / span for d, v in flows)
    denom = v0 + weighted
    if denom <= 1e-9:
        return None
    return (v1 - v0 - cf) / denom


class _History:
    """An account's NAV points, contributions and other flows, for any period."""

    def __init__(self, account: str):
        self.account = account
        self.nav = nav_series(account)
        self.flows = cash_flows(account, limit=100000)
        self.contrib: dict[str, float] = defaultdict(float)
        for f in self.flows:
            if f["kind"] in CONTRIBUTION_KINDS:
                self.contrib[f["date"]] += f["amount"]
        self.fills = _fills(account)

    def _contrib_between(self, a: str | None, b: str) -> list[tuple[str, float]]:
        return sorted((d, v) for d, v in self.contrib.items() if (a is None or d > a) and d <= b and v)

    def period(self, start: str, end: str) -> dict | None:
        before = [p for p in self.nav if p[0] < start]
        inside = [p for p in self.nav if start <= p[0] <= end]
        if not inside:
            return None
        d_start, v0 = before[-1] if before else (None, 0.0)
        d_end, v1 = inside[-1]
        flows = self._contrib_between(d_start, d_end)
        # first NAV point of the history with no deposits before it: treat it as the opening balance
        opening = 0.0
        if d_start is None and not [f for f in flows if f[0] <= inside[0][0]]:
            d_start, v0 = inside[0]
            inside = inside[1:]
            opening = v0
            flows = self._contrib_between(d_start, d_end)
        # time-weighted: chain the sub-periods between NAV points
        twr, prev_d, prev_v = 1.0, d_start, v0
        for d, v in inside:
            sub = self._contrib_between(prev_d, d)
            if prev_d is None:
                prev_d = (date.fromisoformat(sub[0][0]) - timedelta(days=1)).isoformat() if sub else d
            r = _dietz(prev_v, v, prev_d, d, sub)
            if r is not None:
                twr *= 1 + r
            prev_d, prev_v = d, v
        net = sum(v for _, v in flows)
        growth = v1 - v0
        gain = growth - net
        lo = d_start or "0000-00-00"
        by_kind: dict[str, float] = defaultdict(float)
        for f in self.flows:
            if lo < f["date"] <= d_end:
                by_kind[f["kind"]] += f["amount"]
        income = sum(by_kind[k] for k in INCOME_KINDS)
        t_lo = int(datetime.fromisoformat(lo).replace(tzinfo=EASTERN).timestamp()) + 86400 if d_start else 0
        t_hi = int(datetime.fromisoformat(d_end).replace(tzinfo=EASTERN).timestamp()) + 86400
        fills = [f for f in self.fills if t_lo <= f["time"] < t_hi]
        realized = sum(f["realized_pnl"] or 0 for f in fills)
        market = gain - income
        days = _days(d_start or (flows[0][0] if flows else d_end), d_end)
        investor = ([(d_start, -v0)] if d_start and v0 else []) + [(d, -v) for d, v in flows] + [(d_end, v1)]
        irr = _xirr(investor)
        mwr = ((1 + irr) ** (max(days, 1) / 365.0) - 1) if irr is not None else None
        twr_pct = (twr - 1) * 100
        return {
            "start": d_start, "end": d_end, "days": days, "start_value": _r(v0), "end_value": _r(v1),
            "opening_balance": _r(opening) if opening else None,
            "growth": _r(growth), "growth_pct": _r(growth / v0 * 100) if v0 else None,
            "contributions": {"net": _r(net), "deposits": _r(by_kind["deposit"]), "withdrawals": _r(by_kind["withdrawal"]),
                              "transfers": _r(by_kind["transfer"])},
            "investment_gain": _r(gain),
            "trading": {"total": _r(market), "realized": _r(realized), "unrealized_fx_other": _r(market - realized),
                        "commissions": _r(-sum(f["commission"] for f in fills)), "executions": len(fills)},
            "income": {"total": _r(income), "dividends": _r(by_kind["dividend"]), "withholding_tax": _r(by_kind["tax"]),
                       "interest": _r(by_kind["interest"]), "fees": _r(by_kind["fee"]), "other": _r(by_kind["other"])},
            "twr_pct": _r(twr_pct), "twr_annualized_pct": _r((twr ** (365.0 / days) - 1) * 100) if days >= 365 else None,
            "mwr_pct": _r(mwr * 100) if mwr is not None else None,
            "mwr_annualized_pct": _r(irr * 100) if irr is not None and days >= 365 else None,
            "nav_points": len(inside) + (1 if d_start else 0),
        }

    def series(self, start: str, end: str) -> list[dict]:
        """Per NAV point: value, money put in so far, gain so far, cumulative TWR from the period start."""
        out = []
        contributed = 0.0
        dates = sorted(self.contrib)
        i = 0
        first = True
        twr, prev_d, prev_v = 1.0, None, 0.0
        for d, v in self.nav:
            while i < len(dates) and dates[i] <= d:
                contributed += self.contrib[dates[i]]
                i += 1
            if first:
                if contributed == 0:
                    contributed = v  # opening balance of a history that starts mid-life
                first = False
            if d >= start and prev_d is not None and prev_d >= start:
                r = _dietz(prev_v, v, prev_d, d, self._contrib_between(prev_d, d))
                twr *= 1 + (r or 0)
            if start <= d <= end:
                out.append({"date": d, "nav": _r(v), "contributed": _r(contributed), "gain": _r(v - contributed),
                            "twr_pct": _r((twr - 1) * 100)})
            prev_d, prev_v = d, v
        return out

    def breakdown(self, by: str, start: str, end: str) -> list[dict]:
        if not self.nav:
            return []
        d0, d1 = max(start, self.nav[0][0]), min(end, self.nav[-1][0])
        rows = []
        cur = date.fromisoformat(d0).replace(day=1, month=1 if by == "year" else date.fromisoformat(d0).month)
        while cur.isoformat() <= d1:
            nxt = cur.replace(year=cur.year + 1) if by == "year" else \
                (cur.replace(year=cur.year + 1, month=1) if cur.month == 12 else cur.replace(month=cur.month + 1))
            p = self.period(cur.isoformat(), (nxt - timedelta(days=1)).isoformat())
            if p:
                rows.append({"key": str(cur.year) if by == "year" else cur.strftime("%Y-%m"), "start_value": p["start_value"],
                             "end_value": p["end_value"], "contributions": p["contributions"]["net"],
                             "investment_gain": p["investment_gain"], "trading": p["trading"]["total"],
                             "income": p["income"]["total"], "twr_pct": p["twr_pct"], "mwr_pct": p["mwr_pct"]})
            cur = nxt
        return rows


def _range(period: str | None, start: str | None, end: str | None, first: str | None) -> tuple[str, str]:
    end = end or _today()
    if start:
        return start, end
    e = date.fromisoformat(end)
    d = {"1m": e - timedelta(days=30), "3m": e - timedelta(days=91), "6m": e - timedelta(days=182),
         "ytd": e.replace(month=1, day=1), "1y": e - timedelta(days=365), "3y": e - timedelta(days=3 * 365),
         "5y": e - timedelta(days=5 * 365)}.get((period or "all").lower())
    return (d.isoformat() if d else (first or "1900-01-01")), end


def performance(account: str | None = None, period: str | None = "all", start: str | None = None,
                end: str | None = None) -> dict:
    account = account or default_account()
    if not account:
        return {"account": None, "summary": None, "series": [], "monthly": [], "yearly": []}
    h = _History(account)
    first = h.nav[0][0] if h.nav else None
    s, e = _range(period, start, end, first)
    summary = h.period(s, e)
    base = (summary["start"] if summary else None) or s  # the chart starts at the summary's start value
    return {"account": account, "period": period, "from": s, "to": e, "summary": summary,
            "series": h.series(base, e), "monthly": h.breakdown("month", s, e), "yearly": h.breakdown("year", s, e),
            "statement": _statement_check(account, summary)}


def _statement_check(account: str, summary: dict | None) -> dict | None:
    """IBKR's own figures for the latest statement of this account (to compare with ours)."""
    with db.session_scope() as s:
        rows = s.scalars(select(db.StatementImportRow).order_by(db.StatementImportRow.created_at.desc())).all()
        for r in rows:
            ch = (r.summary or {}).get(account)
            if ch:
                return {"from": r.from_date, "to": r.to_date, "starting_value": ch.get("startingValue"),
                        "ending_value": ch.get("endingValue"), "deposits_withdrawals": ch.get("depositsWithdrawals"),
                        "mtm": ch.get("mtm"), "dividends": ch.get("dividends"), "interest": ch.get("interest"),
                        "commissions": ch.get("commissions"), "twr": ch.get("twr")}
    return None


def overview(account: str | None = None) -> dict:
    """Current state (live from the connected account when it is this one, otherwise the latest
    statement) and lifetime totals."""
    from .trading import trading

    account = account or default_account()
    out: dict = {"account": account, "accounts": accounts(), "live": None, "statement_positions": None,
                 "lifetime": None, "totals": None, "imports": imports(limit=5)}
    if not account:
        return out
    try:
        st = trading.status()
        if st.get("connected") and st.get("account") == account:
            ov = trading.overview()
            out["live"] = {"account": ov["account"], "capital": ov["capital"], "positions": ov["positions"]}
    except Exception as e:  # broker trouble must not hide the history
        log.info("live account unavailable: %s", e)
    with db.session_scope() as s:
        last = s.scalar(select(func.max(db.PositionSnapshotRow.date)).where(db.PositionSnapshotRow.account == account))
        if last:
            rows = s.scalars(select(db.PositionSnapshotRow).where(db.PositionSnapshotRow.account == account,
                                                                  db.PositionSnapshotRow.date == last)).all()
            out["statement_positions"] = {"date": last, "positions": [
                {"symbol": r.symbol, "asset_class": r.asset_class, "qty": r.qty, "mark": r.mark, "value": r.value,
                 "value_base": r.value_base, "cost_basis": r.cost_basis, "unrealized": r.unrealized,
                 "currency": r.currency} for r in rows]}
    h = _History(account)
    if h.nav:
        out["lifetime"] = h.period(h.nav[0][0], _today())
        out["nav"] = {"date": h.nav[-1][0], "value": h.nav[-1][1], "first_date": h.nav[0][0]}
    tot: dict[str, float] = defaultdict(float)
    for f in h.flows:
        tot[f["kind"]] += f["amount"]
    out["totals"] = {k: _r(tot[k]) for k in ("deposit", "withdrawal", "transfer", *INCOME_KINDS)}
    out["totals"]["net_contributions"] = _r(sum(tot[k] for k in CONTRIBUTION_KINDS))
    out["totals"]["commissions"] = _r(-sum(f["commission"] for f in h.fills))
    out["coverage"] = {"nav": [h.nav[0][0], h.nav[-1][0]] if h.nav else None,
                       "cash_flows": [h.flows[-1]["date"], h.flows[0]["date"]] if h.flows else None,
                       "executions": len(h.fills)}
    return out


# ===================================================================================== Flex Web Service

def _flex_state() -> dict:
    return db.state_get(FLEX_KEY, {}) or {}


def _flex_env() -> tuple[str, str]:
    """Token and query id from the environment or .env (they override what the UI saved)."""
    from ..config import get_settings

    cfg = get_settings()
    return cfg.autobench_ibkr_flex_token.strip(), cfg.autobench_ibkr_flex_query.strip()


def flex_settings() -> dict:
    """What the UI may see: never the token itself."""
    st = _flex_state()
    env_token, env_query = _flex_env()
    return {"has_token": bool(env_token or st.get("token")), "token_from_env": bool(env_token),
            "query_id": env_query or st.get("query_id"), "auto_sync": bool(st.get("auto_sync")),
            "last_sync": st.get("last_sync"), "last_error": st.get("last_error"), "last_import": st.get("last_import")}


def set_flex_settings(token: str | None = None, query_id: str | None = None, auto_sync: bool | None = None,
                      clear_token: bool = False) -> dict:
    st = _flex_state()
    if token:
        st["token"] = token.strip()
    if clear_token:
        st.pop("token", None)
    if query_id is not None:
        st["query_id"] = query_id.strip()
    if auto_sync is not None:
        st["auto_sync"] = auto_sync
    db.state_set(FLEX_KEY, st)
    return flex_settings()


def _http_get(url: str) -> str:
    # urllib on purpose: it logs nothing, and the URL carries the token
    req = urllib.request.Request(url, headers={"User-Agent": "autobench/0.2"})
    with urllib.request.urlopen(req, timeout=60) as r:  # noqa: S310 - fixed https host
        return r.read().decode("utf-8", "replace")


def flex_download(token: str, query_id: str, get=_http_get, wait=time.sleep, tries: int = 20) -> str:
    """Run a Flex Query through the Flex Web Service and return the statement XML."""
    q = urllib.parse.urlencode({"t": token, "q": query_id, "v": "3"})
    reply = ET.fromstring(get(f"{FLEX_BASE}/SendRequest?{q}"))
    if (reply.findtext("Status") or "") != "Success":
        raise StatementError(f"IBKR Flex: {reply.findtext('ErrorMessage') or 'request failed'} "
                             f"(code {reply.findtext('ErrorCode') or '?'})")
    ref = reply.findtext("ReferenceCode") or ""
    url = reply.findtext("Url") or f"{FLEX_BASE}/GetStatement"
    for _ in range(tries):
        text = get(f"{url}?{urllib.parse.urlencode({'t': token, 'q': ref, 'v': '3'})}")
        if "<FlexQueryResponse" in text[:500]:
            return text
        r = ET.fromstring(text)
        code = r.findtext("ErrorCode") or ""
        if code in ("1019", "1021"):  # statement generation in progress / try again shortly
            wait(3)
            continue
        raise StatementError(f"IBKR Flex: {r.findtext('ErrorMessage') or 'download failed'} (code {code or '?'})")
    raise StatementError("IBKR Flex: the statement was not ready in time; try again in a minute")


def flex_sync(get=_http_get, wait=time.sleep) -> dict:
    cfg = _flex_state()
    env_token, env_query = _flex_env()
    token = env_token or cfg.get("token")
    query_id = env_query or cfg.get("query_id")
    if not token or not query_id:
        raise StatementError("Set the Flex token and query id first")
    cfg["last_sync"] = datetime.now(UTC).isoformat()
    try:
        res = import_statement(flex_download(token, query_id, get=get, wait=wait), filename=f"flex-{query_id}.xml",
                               source="flex_web")
    except Exception as e:
        cfg["last_error"] = str(e)
        db.state_set(FLEX_KEY, cfg)
        raise
    cfg["last_error"] = None
    cfg["last_import"] = res["id"]
    db.state_set(FLEX_KEY, cfg)
    return res


def maybe_auto_sync() -> None:
    """Daily Flex download (scheduler): once a day, after 07:00 New York time, when turned on."""
    cfg = _flex_state()
    if not cfg.get("auto_sync"):
        return
    now = datetime.now(EASTERN)
    last = cfg.get("last_sync")
    if now.hour < 7 or (last and datetime.fromisoformat(last).astimezone(EASTERN).date() == now.date()):
        return
    try:
        r = flex_sync()
        log.info("IBKR Flex sync: %s", r["counts"])
    except Exception as e:
        log.warning("IBKR Flex sync failed: %s", e)
