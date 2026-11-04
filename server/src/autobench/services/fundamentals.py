"""Fundamental data from Yahoo Finance (yfinance): profile, valuation, profitability, growth,
balance-sheet health, financial statements, earnings & estimates, analyst views, ownership."""

from __future__ import annotations

import math
from datetime import date, datetime
from typing import Any

import numpy as np
import pandas as pd
import yfinance as yf

from .. import db
from ..providers.base import ProviderError
from ..providers.yahoo import yahoo_symbol
from .market import normalize_symbol

_TTL = 6 * 3600


def _ticker(symbol: str):
    return yf.Ticker(yahoo_symbol(normalize_symbol(symbol)))


def clean(v: Any) -> Any:
    """Make yfinance values JSON-friendly: NaN -> None, numpy -> python, timestamps -> ISO."""
    if v is None:
        return None
    if isinstance(v, dict):
        return {str(k): clean(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [clean(x) for x in v]
    if isinstance(v, (pd.Timestamp, datetime)):
        return v.date().isoformat() if (v.hour, v.minute, v.second) == (0, 0, 0) else v.isoformat()
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.floating, float)):
        f = float(v)
        return None if math.isnan(f) or math.isinf(f) else (round(f, 6) if abs(f) < 1e6 else round(f))
    if isinstance(v, (np.bool_,)):
        return bool(v)
    if v is pd.NA or v is pd.NaT:
        return None
    return v


def _pct(x):
    """Yahoo ratio (0.25) -> percent (25.0)."""
    return None if x is None else round(float(x) * 100, 2)


def _frame_records(df: pd.DataFrame | None, limit: int | None = None, index_name: str | None = None) -> list[dict]:
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return []
    d = df.reset_index(drop=isinstance(df.index, pd.RangeIndex))
    if index_name and not isinstance(df.index, pd.RangeIndex):
        d = d.rename(columns={d.columns[0]: index_name})
    if limit:
        d = d.head(limit)
    return [clean(r) for r in d.to_dict(orient="records")]


def _info(symbol: str) -> dict:
    def fetch():
        try:
            info = _ticker(symbol).info or {}
        except Exception as e:
            raise ProviderError(f"Yahoo fundamentals failed for {symbol}: {e}") from e
        if not info or (info.get("quoteType") is None and info.get("longName") is None):
            raise ProviderError(f"No fundamental data for {symbol}")
        return clean({k: v for k, v in info.items() if k not in ("companyOfficers", "executiveTeam", "corporateActions")})

    return db.cached(f"yf_info:{normalize_symbol(symbol)}", _TTL, fetch)


def get_fundamentals(symbol: str) -> dict:
    symbol = normalize_symbol(symbol)
    i = _info(symbol)
    g = i.get
    price = g("currentPrice") or g("regularMarketPrice")
    mcap = g("marketCap")
    fcf = g("freeCashflow")
    target = g("targetMeanPrice")
    summary = g("longBusinessSummary") or ""
    return {
        "symbol": symbol,
        "profile": {
            "name": g("longName") or g("shortName"), "type": g("quoteType"), "sector": g("sector"),
            "industry": g("industry"), "country": g("country"), "employees": g("fullTimeEmployees"),
            "website": g("website"), "exchange": g("fullExchangeName") or g("exchange"),
            "summary": summary[:1500] + ("…" if len(summary) > 1500 else ""),
        },
        "size": {
            "price": price, "market_cap": mcap, "enterprise_value": g("enterpriseValue"),
            "shares_outstanding": g("sharesOutstanding"), "float_shares": g("floatShares"),
            "avg_volume_3m": g("averageDailyVolume3Month") or g("averageVolume"),
        },
        "valuation": {
            "trailing_pe": g("trailingPE"), "forward_pe": g("forwardPE"),
            "peg": g("trailingPegRatio") or g("pegRatio"),
            "price_to_sales": g("priceToSalesTrailing12Months"), "price_to_book": g("priceToBook"),
            "ev_to_ebitda": g("enterpriseToEbitda"), "ev_to_revenue": g("enterpriseToRevenue"),
            "fcf_yield_pct": round(fcf / mcap * 100, 2) if fcf and mcap else None,
            "earnings_yield_pct": round(100 / g("trailingPE"), 2) if g("trailingPE") else None,
            "eps_ttm": g("trailingEps") or g("epsTrailingTwelveMonths"), "eps_forward": g("forwardEps") or g("epsForward"),
        },
        "profitability": {
            "gross_margin_pct": _pct(g("grossMargins")), "operating_margin_pct": _pct(g("operatingMargins")),
            "ebitda_margin_pct": _pct(g("ebitdaMargins")), "net_margin_pct": _pct(g("profitMargins")),
            "roe_pct": _pct(g("returnOnEquity")), "roa_pct": _pct(g("returnOnAssets")),
        },
        "growth": {
            "revenue_growth_yoy_pct": _pct(g("revenueGrowth")), "earnings_growth_yoy_pct": _pct(g("earningsGrowth")),
            "earnings_quarterly_growth_yoy_pct": _pct(g("earningsQuarterlyGrowth")),
            "revenue_ttm": g("totalRevenue"), "net_income_ttm": g("netIncomeToCommon"), "ebitda_ttm": g("ebitda"),
        },
        "financial_health": {
            "total_cash": g("totalCash"), "total_debt": g("totalDebt"),
            "net_debt": (g("totalDebt") - g("totalCash")) if g("totalDebt") is not None and g("totalCash") is not None else None,
            "debt_to_equity_pct": g("debtToEquity"), "current_ratio": g("currentRatio"), "quick_ratio": g("quickRatio"),
            "operating_cash_flow_ttm": g("operatingCashflow"), "free_cash_flow_ttm": fcf,
        },
        "dividends": {
            "dividend_rate": g("dividendRate"), "dividend_yield_pct": g("dividendYield"),
            "payout_ratio_pct": _pct(g("payoutRatio")), "ex_dividend_date": _epoch_date(g("exDividendDate")),
            "five_year_avg_yield_pct": g("fiveYearAvgDividendYield"),
        },
        "short_interest_and_ownership": {
            "short_pct_of_float": _pct(g("shortPercentOfFloat")), "short_ratio_days": g("shortRatio"),
            "shares_short": g("sharesShort"), "shares_short_prior_month": g("sharesShortPriorMonth"),
            "insiders_pct": _pct(g("heldPercentInsiders")), "institutions_pct": _pct(g("heldPercentInstitutions")),
        },
        "analysts": {
            "recommendation": g("recommendationKey"), "recommendation_mean": g("recommendationMean"),
            "rating_scale": "1 = strong buy ... 5 = strong sell",
            "analyst_count": g("numberOfAnalystOpinions"),
            "target_mean": target, "target_median": g("targetMedianPrice"),
            "target_high": g("targetHighPrice"), "target_low": g("targetLowPrice"),
            "upside_to_mean_target_pct": round((target / price - 1) * 100, 2) if target and price else None,
        },
        "other": {"beta": g("beta"), "next_earnings": _epoch_date(g("earningsTimestamp")),
                  "fifty_two_week_change_pct": g("fiftyTwoWeekChangePercent") or _pct(g("52WeekChange"))},
        "source": "yahoo",
    }


def _epoch_date(v) -> str | None:
    if not v:
        return None
    try:
        return datetime.fromtimestamp(int(v)).date().isoformat()
    except (TypeError, ValueError, OSError):
        return str(v)


_KEY_ROWS = {
    "income": ["Total Revenue", "Cost Of Revenue", "Gross Profit", "Research And Development",
               "Selling General And Administration", "Operating Income", "EBITDA", "Interest Expense",
               "Pretax Income", "Tax Provision", "Net Income", "Diluted EPS", "Diluted Average Shares"],
    "balance": ["Total Assets", "Current Assets", "Cash And Cash Equivalents", "Cash Cash Equivalents And Short Term Investments",
                "Receivables", "Inventory", "Total Liabilities Net Minority Interest", "Current Liabilities",
                "Total Debt", "Long Term Debt", "Net Debt", "Stockholders Equity", "Retained Earnings",
                "Working Capital", "Tangible Book Value", "Ordinary Shares Number"],
    "cashflow": ["Operating Cash Flow", "Capital Expenditure", "Free Cash Flow", "Stock Based Compensation",
                 "Depreciation And Amortization", "Change In Working Capital", "Repurchase Of Capital Stock",
                 "Cash Dividends Paid", "Issuance Of Debt", "Repayment Of Debt", "Investing Cash Flow",
                 "Financing Cash Flow"],
}


def get_financial_statements(symbol: str, statement: str = "income", period: str = "annual", rows: str = "key") -> dict:
    """statement: income | balance | cashflow; period: annual | quarterly; rows: key | all."""
    symbol = normalize_symbol(symbol)
    if statement not in _KEY_ROWS:
        raise ValueError("statement must be income, balance or cashflow")
    if period not in ("annual", "quarterly"):
        raise ValueError("period must be annual or quarterly")

    def fetch():
        t = _ticker(symbol)
        attr = {"income": "income_stmt", "balance": "balance_sheet", "cashflow": "cashflow"}[statement]
        df = getattr(t, ("quarterly_" if period == "quarterly" else "") + attr)
        if df is None or df.empty:
            return None
        df = df.loc[:, sorted(df.columns)]  # oldest -> newest
        periods = [c.date().isoformat() if hasattr(c, "date") else str(c) for c in df.columns]
        return {"periods": periods, "rows": {str(k): [clean(x) for x in v] for k, v in df.iterrows()}}

    data = db.cached(f"yf_stmt:{symbol}:{statement}:{period}", _TTL, fetch)
    if not data:
        raise ProviderError(f"No {period} {statement} statement for {symbol}")
    all_rows = data["rows"]
    if rows == "key":
        sel = {k: all_rows[k] for k in _KEY_ROWS[statement] if k in all_rows}
    else:
        sel = all_rows
    out = {"symbol": symbol, "statement": statement, "period": period, "periods": data["periods"], "rows": sel}
    out["derived"] = _derived(statement, data["periods"], all_rows, period)
    if rows == "key":
        out["other_rows_available"] = sorted(set(all_rows) - set(sel))[:80]
    return out


def _derived(statement: str, periods: list[str], rows: dict, period: str) -> dict:
    def series(name):
        return rows.get(name)

    def ratio(a, b, pct=True):
        if not a or not b:
            return None
        return [round(x / y * (100 if pct else 1), 2) if x is not None and y not in (None, 0) else None for x, y in zip(a, b)]

    def growth(a):
        if not a:
            return None
        lag = 4 if period == "quarterly" else 1
        return [None if i < lag or a[i] is None or not a[i - lag] else round((a[i] / a[i - lag] - 1) * 100, 2)
                for i in range(len(a))]

    d: dict = {}
    if statement == "income":
        rev = series("Total Revenue")
        d["gross_margin_pct"] = ratio(series("Gross Profit"), rev)
        d["operating_margin_pct"] = ratio(series("Operating Income"), rev)
        d["net_margin_pct"] = ratio(series("Net Income"), rev)
        d["revenue_growth_pct" + ("_yoy" if period == "quarterly" else "")] = growth(rev)
        d["eps_growth_pct" + ("_yoy" if period == "quarterly" else "")] = growth(series("Diluted EPS"))
        ebit, interest = series("EBIT") or series("Operating Income"), series("Interest Expense")
        d["interest_coverage_x"] = ratio(ebit, interest, pct=False)
    elif statement == "balance":
        d["debt_to_equity"] = ratio(series("Total Debt"), series("Stockholders Equity"), pct=False)
        d["current_ratio"] = ratio(series("Current Assets"), series("Current Liabilities"), pct=False)
        sh = series("Ordinary Shares Number")
        d["share_count_change_pct"] = growth(sh)
    else:
        ocf, capex, fcf = series("Operating Cash Flow"), series("Capital Expenditure"), series("Free Cash Flow")
        d["fcf_growth_pct"] = growth(fcf)
        sbc = series("Stock Based Compensation")
        d["sbc_pct_of_ocf"] = ratio(sbc, ocf)
        d["capex_pct_of_ocf"] = ratio([abs(x) if x is not None else None for x in capex] if capex else None, ocf)
    return {k: v for k, v in d.items() if v}


def get_earnings(symbol: str) -> dict:
    """Earnings history & surprises, next date, consensus estimates and revision trends."""
    symbol = normalize_symbol(symbol)

    def fetch():
        t = _ticker(symbol)
        out: dict = {}
        try:
            ed = t.get_earnings_dates(limit=12)
            out["earnings_dates"] = _frame_records(ed, index_name="date")
        except Exception:
            out["earnings_dates"] = []
        for name in ("earnings_estimate", "revenue_estimate", "eps_trend", "eps_revisions", "growth_estimates"):
            try:
                out[name] = _frame_records(getattr(t, name), index_name="period")
            except Exception:
                out[name] = []
        try:
            out["calendar"] = clean(t.calendar or {})
        except Exception:
            out["calendar"] = {}
        return out

    data = db.cached(f"yf_earn:{symbol}", _TTL, fetch)
    hist = [r for r in data["earnings_dates"] if r.get("Reported EPS") is not None]
    beats = [r for r in hist if (r.get("Surprise(%)") or 0) > 0]
    return {
        "symbol": symbol,
        "next_earnings": data.get("calendar", {}).get("Earnings Date"),
        "calendar": data.get("calendar"),
        "history": hist[:8],
        "beat_rate_pct": round(len(beats) / len(hist) * 100, 1) if hist else None,
        "avg_surprise_pct": round(float(np.mean([r.get("Surprise(%)") or 0 for r in hist])), 2) if hist else None,
        "estimates": {"eps": data["earnings_estimate"], "revenue": data["revenue_estimate"]},
        "eps_trend": data["eps_trend"],
        "eps_revisions": data["eps_revisions"],
        "growth_estimates": data["growth_estimates"],
        "notes": "period codes: 0q = current quarter, +1q = next quarter, 0y = current fiscal year, +1y = next year. "
                 "eps_trend shows how consensus moved over 7-90 days (rising = positive revisions).",
    }


def get_analyst_ratings(symbol: str, limit: int = 15) -> dict:
    symbol = normalize_symbol(symbol)

    def fetch():
        t = _ticker(symbol)
        out = {}
        for name in ("recommendations", "upgrades_downgrades", "analyst_price_targets"):
            try:
                v = getattr(t, name)
                out[name] = clean(v) if isinstance(v, dict) else _frame_records(v, index_name="date" if name == "upgrades_downgrades" else None)
            except Exception:
                out[name] = [] if name != "analyst_price_targets" else {}
        return out

    d = db.cached(f"yf_analyst:{symbol}", _TTL, fetch)
    ud = d["upgrades_downgrades"][:limit]
    return {
        "symbol": symbol,
        "price_targets": d["analyst_price_targets"],
        "recommendation_trend": d["recommendations"],
        "recent_actions": ud,
        "action_codes": "up = upgrade, down = downgrade, main = maintain, reit = reiterate, init = initiate",
    }


def get_ownership(symbol: str) -> dict:
    """Insider transactions (last ~6 months), insider buy/sell summary, top institutions."""
    symbol = normalize_symbol(symbol)

    def fetch():
        t = _ticker(symbol)
        out = {}
        for name in ("insider_transactions", "insider_purchases", "institutional_holders", "mutualfund_holders", "major_holders"):
            try:
                out[name] = _frame_records(getattr(t, name))
            except Exception:
                out[name] = []
        return out

    d = db.cached(f"yf_own:{symbol}", _TTL, fetch)
    tx = d["insider_transactions"]
    buys = [r for r in tx if "purchase" in str(r.get("Text", "")).lower() or "buy" in str(r.get("Transaction", "")).lower()]
    sells = [r for r in tx if "sale" in str(r.get("Text", "")).lower()]
    return {
        "symbol": symbol,
        "insider_summary": {
            "open_market_buys": len(buys), "sales": len(sells),
            "buy_value": round(sum(r.get("Value") or 0 for r in buys)),
            "sale_value": round(sum(r.get("Value") or 0 for r in sells)),
            "note": "Sales are often scheduled (10b5-1) or for taxes; clustered open-market BUYS are the stronger signal.",
        },
        "insider_transactions": [{k: v for k, v in r.items() if k not in ("URL",)} for r in tx[:20]],
        "insider_purchases_6m": d["insider_purchases"],
        "top_institutions": d["institutional_holders"][:10],
        "top_funds": d["mutualfund_holders"][:5],
        "major_holders": d["major_holders"],
    }


def compare_fundamentals(symbols: list[str]) -> dict:
    """Side-by-side key metrics for peer comparison."""
    rows = {}
    errors = {}
    for s in symbols:
        try:
            f = get_fundamentals(s)
            rows[f["symbol"]] = {
                "name": f["profile"]["name"], "industry": f["profile"]["industry"],
                "market_cap": f["size"]["market_cap"],
                **{k: f["valuation"][k] for k in ("trailing_pe", "forward_pe", "peg", "price_to_sales", "ev_to_ebitda", "fcf_yield_pct")},
                **{k: f["profitability"][k] for k in ("gross_margin_pct", "operating_margin_pct", "net_margin_pct", "roe_pct")},
                **{k: f["growth"][k] for k in ("revenue_growth_yoy_pct", "earnings_growth_yoy_pct")},
                "debt_to_equity_pct": f["financial_health"]["debt_to_equity_pct"],
                "upside_to_mean_target_pct": f["analysts"]["upside_to_mean_target_pct"],
                "recommendation_mean": f["analysts"]["recommendation_mean"],
            }
        except Exception as e:
            errors[normalize_symbol(s)] = str(e)
    medians = {}
    if rows:
        keys = next(iter(rows.values())).keys()
        for k in keys:
            vals = [r[k] for r in rows.values() if isinstance(r.get(k), (int, float))]
            if vals and k not in ("market_cap",):
                medians[k] = round(float(np.median(vals)), 2)
    return {"companies": rows, "peer_medians": medians, "errors": errors}
