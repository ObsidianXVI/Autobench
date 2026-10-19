"""Parsers for Interactive Brokers statements, normalised to plain dicts.

Two formats:
- Flex Query XML (Account Management > Reports > Flex Queries, or the Flex Web Service). The complete
  one: daily NAV (EquitySummaryInBase), trades with execution ids, cash transactions with FX rates to
  base, transfers, open positions, cash balances and the statement's change in NAV.
- Activity Statement CSV (Reports > Statements, format CSV). Has trades, deposits/withdrawals,
  dividends, tax, interest, fees, open positions and the period's change in NAV, but no daily NAV and
  no FX rates (amounts not in the base currency are taken as they are).

Result: {kind, accounts {id: {currency, name}}, from_date, to_date, trades [], cash [], nav [],
positions [], summary {account: change in NAV}}. Dates are YYYY-MM-DD, times epoch seconds (statement
times are US/Eastern), cash amounts positive into the account."""

from __future__ import annotations

import csv
import hashlib
import io
import re
import xml.etree.ElementTree as ET
from datetime import datetime

from ..timeutil import ET as EASTERN


class StatementError(ValueError):
    pass


# ----------------------------------------------------------------------------------- helpers

def _num(v) -> float | None:
    if v is None:
        return None
    s = str(v).strip().replace(",", "")
    if s in ("", "--", "-", "N/A"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _date(v) -> str | None:
    """'20240115', '2024-01-15', '01/15/2024', '2024-01-15, 09:30:00', '20240115;093000' -> '2024-01-15'."""
    if not v:
        return None
    s = str(v).strip()
    m = re.match(r"(\d{4})-?(\d{2})-?(\d{2})", s)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)
    if m:
        return f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    return None


def _time(v, fallback_date: str | None = None) -> int | None:
    """Statement date/time (US/Eastern) -> epoch seconds. A bare date means 16:00 (the close)."""
    s = str(v or "").strip()
    d = _date(s) or fallback_date
    if not d:
        return None
    head = re.match(r"(\d{4}-?\d{2}-?\d{2}|\d{1,2}/\d{1,2}/\d{4})(.*)", s)
    m = re.search(r"(\d{2}):?(\d{2}):?(\d{2})?", head.group(2) if head else "")
    hh, mm, ss = (int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)) if m else (16, 0, 0)
    y, mo, dd = (int(x) for x in d.split("-"))
    return int(datetime(y, mo, dd, hh, mm, ss, tzinfo=EASTERN).timestamp())


def _hid(*parts) -> str:
    return hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:20]


def cash_kind(type_: str, amount: float, description: str = "") -> str:
    t = (type_ or "").lower()
    if "deposit" in t or "withdraw" in t:
        return "deposit" if amount >= 0 else "withdrawal"
    if "withholding" in t or t.endswith("tax"):
        return "tax"
    if "dividend" in t or "in lieu" in t:
        return "dividend"
    if "interest" in t:
        return "interest"
    if "fee" in t or "commission" in t:
        return "fee"
    if "transfer" in t:
        return "transfer"
    return "other"


#: kinds that are money the owner put in or took out (not investment results)
CONTRIBUTION_KINDS = ("deposit", "withdrawal", "transfer")


def _empty(kind: str) -> dict:
    return {"kind": kind, "accounts": {}, "from_date": None, "to_date": None, "trades": [], "cash": [], "nav": [],
            "positions": [], "summary": {}}


def _span(res: dict, d0: str | None, d1: str | None) -> None:
    if d0 and (res["from_date"] is None or d0 < res["from_date"]):
        res["from_date"] = d0
    if d1 and (res["to_date"] is None or d1 > res["to_date"]):
        res["to_date"] = d1


def parse(text: str, filename: str | None = None) -> dict:
    head = text.lstrip()[:200]
    if head.startswith("<"):
        return parse_flex_xml(text)
    if "Statement," in head or "Account Information," in text[:5000] or "Trades,Header" in text:
        return parse_activity_csv(text)
    raise StatementError("Not an IBKR statement: upload a Flex Query (XML) or an Activity Statement (CSV)")


# ----------------------------------------------------------------------------------- Flex XML

_TRADE_LEVELS = ("EXECUTION", "")


def parse_flex_xml(text: str) -> dict:
    try:
        root = ET.fromstring(text.encode() if isinstance(text, str) else text)
    except ET.ParseError as e:
        raise StatementError(f"Could not read the XML: {e}") from None
    if root.tag == "FlexStatementResponse":  # Flex Web Service status reply, not a statement
        msg = root.findtext("ErrorMessage") or root.findtext("Status") or "no statement"
        raise StatementError(f"IBKR Flex: {msg}")
    stmts = list(root.iter("FlexStatement"))
    if not stmts:
        raise StatementError("No FlexStatement in this file (is it an Activity Flex Query?)")
    res = _empty("flex_xml")
    for st in stmts:
        acct = st.get("accountId") or ""
        _span(res, _date(st.get("fromDate")), _date(st.get("toDate")))
        info = st.find("AccountInformation")
        base = (info.get("currency") if info is not None else None) or "USD"
        res["accounts"][acct] = {"currency": base, "name": info.get("name") if info is not None else None}

        trades = [t for t in st.iter("Trade")]
        if not any((t.get("levelOfDetail") or "") in _TRADE_LEVELS for t in trades):
            trades = [t for t in trades if (t.get("levelOfDetail") or "") == "ORDER"]
        else:
            trades = [t for t in trades if (t.get("levelOfDetail") or "") in _TRADE_LEVELS]
        for t in trades:
            side = (t.get("buySell") or "").upper()
            if side not in ("BUY", "SELL"):  # cancellations ("BUY (Ca.)") and the like
                continue
            asset = t.get("assetCategory") or "STK"
            if asset == "CASH":  # currency conversions: their result shows up as FX, not as trades
                continue
            qty = abs(_num(t.get("quantity")) or 0)
            price = _num(t.get("tradePrice"))
            if not qty or price is None:
                continue
            day = _date(t.get("tradeDate")) or _date(t.get("dateTime"))
            when = _time(t.get("dateTime") or t.get("tradeTime") or "", day)
            exec_id = t.get("ibExecID") or (f"flex:{t.get('tradeID')}" if t.get("tradeID") else
                                            "flex:" + _hid(acct, t.get("symbol"), when, side, qty, price))
            comm = _num(t.get("ibCommission"))
            res["trades"].append({
                "exec_id": exec_id, "account": t.get("accountId") or acct, "symbol": (t.get("symbol") or "").strip(),
                "side": side.lower(), "qty": qty, "price": price, "time": when,
                "commission": -comm if comm is not None else None, "realized_pnl": _num(t.get("fifoPnlRealized")),
                "asset_class": asset, "multiplier": _num(t.get("multiplier")) or 1.0, "currency": t.get("currency") or base,
                "order_id": t.get("ibOrderID") or None,
            })

        for c in st.iter("CashTransaction"):
            lvl = c.get("levelOfDetail") or "DETAIL"
            if lvl not in ("DETAIL", ""):
                continue
            amt = _num(c.get("amount"))
            if amt is None:
                continue
            fx = _num(c.get("fxRateToBase")) or 1.0
            day = _date(c.get("dateTime")) or _date(c.get("settleDate")) or _date(c.get("reportDate"))
            kind = cash_kind(c.get("type") or "", amt, c.get("description") or "")
            cid = c.get("transactionID") or _hid(acct, day, c.get("type"), amt, c.get("description"))
            res["cash"].append({"id": f"cash:{cid}", "account": c.get("accountId") or acct, "date": day, "kind": kind,
                                "amount": round(amt * fx, 2), "amount_local": amt, "currency": c.get("currency") or base,
                                "fx_rate": fx, "symbol": (c.get("symbol") or "").strip() or None,
                                "description": c.get("description") or c.get("type")})

        for t in st.iter("Transfer"):
            direction = (t.get("direction") or "").upper()
            sign = -1 if direction == "OUT" else 1
            fx = _num(t.get("fxRateToBase")) or 1.0
            value = _num(t.get("positionAmountInBase"))
            if value is None:
                value = (_num(t.get("positionAmount")) or 0) * fx
            value += (_num(t.get("cashTransfer")) or 0) * fx
            if not value:
                continue
            day = _date(t.get("dateTime")) or _date(t.get("date")) or _date(t.get("reportDate"))
            sym = (t.get("symbol") or "").strip() or None
            tid = t.get("transactionID") or _hid(acct, day, sym, t.get("quantity"), value, direction)
            res["cash"].append({"id": f"xfer:{tid}", "account": t.get("accountId") or acct, "date": day, "kind": "transfer",
                                "amount": round(abs(value) * sign, 2), "amount_local": abs(value) * sign,
                                "currency": base, "fx_rate": 1.0, "symbol": sym,
                                "description": f"{t.get('type') or 'Transfer'} {direction.lower()} {sym or 'cash'}"
                                               f"{' x' + t.get('quantity') if t.get('quantity') else ''}".strip()})

        for e in st.iter("EquitySummaryByReportDateInBase"):
            day = _date(e.get("reportDate"))
            total = _num(e.get("total"))
            if not day or total is None:
                continue
            known = sum(_num(e.get(k)) or 0 for k in ("cash", "stock", "options"))
            res["nav"].append({"account": e.get("accountId") or acct, "date": day, "total": total,
                               "cash": _num(e.get("cash")), "stock": _num(e.get("stock")),
                               "options": _num(e.get("options")), "other": round(total - known, 2), "currency": base})

        for p in st.iter("OpenPosition"):
            if (p.get("levelOfDetail") or "SUMMARY") != "SUMMARY":
                continue
            fx = _num(p.get("fxRateToBase")) or 1.0
            value = _num(p.get("positionValue"))
            res["positions"].append({
                "account": p.get("accountId") or acct, "date": _date(p.get("reportDate")) or res["to_date"],
                "symbol": (p.get("symbol") or "").strip(), "asset_class": p.get("assetCategory") or "STK",
                "qty": _num(p.get("position")) or 0, "mark": _num(p.get("markPrice")), "value": value,
                "value_base": round(value * fx, 2) if value is not None else None,
                "cost_basis": _num(p.get("costBasisMoney")), "unrealized": _num(p.get("fifoPnlUnrealized")),
                "currency": p.get("currency") or base, "multiplier": _num(p.get("multiplier")) or 1.0})

        for c in st.iter("CashReportCurrency"):
            cur = c.get("currency") or ""
            if cur in ("BASE_SUMMARY", "") or (c.get("levelOfDetail") or "Currency") not in ("Currency", ""):
                continue
            end = _num(c.get("endingCash"))
            if end is None:
                continue
            res["positions"].append({"account": c.get("accountId") or acct, "date": _date(c.get("toDate")) or res["to_date"],
                                     "symbol": cur, "asset_class": "CASH", "qty": end, "mark": 1.0, "value": end,
                                     "value_base": end if cur == base else None, "cost_basis": None, "unrealized": None,
                                     "currency": cur, "multiplier": 1.0})

        ch = st.find("ChangeInNAV")
        if ch is not None:
            res["summary"][acct] = {k: (_num(v) if k not in ("fromDate", "toDate", "currency", "accountId") else v)
                                    for k, v in ch.attrib.items() if k not in ("accountId", "acctAlias", "model")}
    return res


# ----------------------------------------------------------------------------------- Activity CSV

_CSV_CASH_SECTIONS = {"Deposits & Withdrawals": "Deposits/Withdrawals", "Dividends": "Dividends",
                      "Withholding Tax": "Withholding Tax", "Interest": "Interest", "Fees": "Other Fees",
                      "Payment In Lieu Of Dividends": "Dividends"}


def parse_activity_csv(text: str) -> dict:
    res = _empty("activity_csv")
    headers: dict[str, list[str]] = {}
    acct, base = "", "USD"
    rows: list[tuple[str, dict]] = []
    for rec in csv.reader(io.StringIO(text.lstrip("﻿"))):
        if len(rec) < 2:
            continue
        section, kind, fields = rec[0].strip(), rec[1].strip(), rec[2:]
        if kind == "Header":
            headers[section] = [f.strip() for f in fields]
            continue
        if kind != "Data" or section not in headers:
            continue
        row = dict(zip(headers[section], fields))
        if section == "Account Information":
            name, val = row.get("Field Name", ""), row.get("Field Value", "")
            if name == "Account":
                acct = val.split()[0] if val else ""
            elif name == "Base Currency":
                base = val or base
            elif name == "Name":
                res["accounts"].setdefault("_name", val)
        elif section == "Statement" and row.get("Field Name") == "Period":
            m = re.findall(r"([A-Z][a-z]+ \d{1,2}, \d{4})", row.get("Field Value", ""))
            ds = [datetime.strptime(x, "%B %d, %Y").strftime("%Y-%m-%d") for x in m]
            if ds:
                _span(res, ds[0], ds[-1])
        rows.append((section, row))
    if not acct:
        raise StatementError("No account number in this CSV (is it an IBKR Activity Statement?)")
    name = res["accounts"].pop("_name", None)
    res["accounts"][acct] = {"currency": base, "name": name}

    change: dict[str, float | None] = {}
    for section, row in rows:
        if section == "Trades":
            if row.get("DataDiscriminator", "Order") not in ("Order", "Trade", "Execution"):
                continue
            asset = row.get("Asset Category", "")
            if asset.startswith("Forex"):
                continue
            q = _num(row.get("Quantity"))
            price = _num(row.get("T. Price") or row.get("Price"))
            if not q or price is None:
                continue
            when = _time(row.get("Date/Time"))
            sym = row.get("Symbol", "").strip()
            comm = _num(row.get("Comm/Fee") or row.get("Comm in USD"))
            cat = {"Stocks": "STK", "Equity and Index Options": "OPT", "Futures": "FUT", "Options On Futures": "FOP",
                   "Bonds": "BOND", "Warrants": "WAR"}.get(asset, asset or "STK")
            res["trades"].append({
                "exec_id": "csv:" + _hid(acct, sym, row.get("Date/Time"), q, price, row.get("Proceeds")),
                "account": acct, "symbol": sym, "side": "buy" if q > 0 else "sell", "qty": abs(q), "price": price,
                "time": when, "commission": -comm if comm is not None else None,
                "realized_pnl": _num(row.get("Realized P/L")), "asset_class": cat,
                "multiplier": 100.0 if cat in ("OPT", "FOP") else 1.0, "currency": row.get("Currency") or base,
                "order_id": None})
        elif section in _CSV_CASH_SECTIONS:
            cur = row.get("Currency", "")
            if not cur or cur.startswith("Total"):
                continue
            amt = _num(row.get("Amount"))
            if amt is None:
                continue
            day = _date(row.get("Settle Date") or row.get("Date"))
            desc = row.get("Description", "")
            kind = cash_kind(_CSV_CASH_SECTIONS[section], amt, desc)
            sym = desc.split("(")[0].strip() if kind in ("dividend", "tax") else None
            res["cash"].append({"id": "cash:" + _hid(acct, section, day, desc, amt), "account": acct, "date": day,
                                "kind": kind, "amount": round(amt, 2), "amount_local": amt, "currency": cur,
                                "fx_rate": 1.0 if cur == base else None, "symbol": sym or None, "description": desc})
        elif section == "Open Positions":
            if row.get("DataDiscriminator", "Summary") != "Summary":
                continue
            asset = row.get("Asset Category", "")
            cat = {"Stocks": "STK", "Equity and Index Options": "OPT", "Futures": "FUT"}.get(asset, asset or "STK")
            value = _num(row.get("Value"))
            cur = row.get("Currency") or base
            res["positions"].append({
                "account": acct, "date": res["to_date"], "symbol": row.get("Symbol", "").strip(), "asset_class": cat,
                "qty": _num(row.get("Quantity")) or 0, "mark": _num(row.get("Close Price")), "value": value,
                "value_base": value if cur == base else None, "cost_basis": _num(row.get("Cost Basis")),
                "unrealized": _num(row.get("Unrealized P/L")), "currency": cur,
                "multiplier": _num(row.get("Mult")) or 1.0})
        elif section == "Change in NAV":
            change[row.get("Field Name", "")] = _num(row.get("Field Value"))

    if change:
        res["summary"][acct] = {
            "startingValue": change.get("Starting Value"), "endingValue": change.get("Ending Value"),
            "mtm": change.get("Mark-to-Market"), "depositsWithdrawals": change.get("Deposits & Withdrawals"),
            "dividends": change.get("Dividends"), "withholdingTax": change.get("Withholding Tax"),
            "interest": change.get("Interest"), "commissions": change.get("Commissions"),
            "otherFees": change.get("Other Fees"), "fromDate": res["from_date"], "toDate": res["to_date"]}
        # the CSV has no daily NAV: keep the period's start and end values
        if res["from_date"] and change.get("Starting Value") is not None:
            res["nav"].append({"account": acct, "date": _day_before(res["from_date"]), "total": change["Starting Value"],
                               "cash": None, "stock": None, "options": None, "other": None, "currency": base})
        if res["to_date"] and change.get("Ending Value") is not None:
            res["nav"].append({"account": acct, "date": res["to_date"], "total": change["Ending Value"], "cash": None,
                               "stock": None, "options": None, "other": None, "currency": base})
    return res


def _day_before(d: str) -> str:
    from datetime import date, timedelta

    return (date.fromisoformat(d) - timedelta(days=1)).isoformat()
