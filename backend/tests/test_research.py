"""Fundamentals, options and SEC filings logic with the network replaced by fixtures."""

import httpx
import numpy as np
import pandas as pd
import pytest
import respx

from autobench.services import filings, fundamentals, options


class FakeTicker:
    def __init__(self, sym):
        self.sym = sym
        self.info = {"longName": f"{sym} Inc", "quoteType": "EQUITY", "sector": "Technology", "industry": "Software",
                     "currentPrice": 100.0, "marketCap": 1e11, "freeCashflow": 5e9, "trailingPE": 25.0,
                     "forwardPE": 20.0, "grossMargins": 0.7, "operatingMargins": 0.3, "returnOnEquity": 0.25,
                     "revenueGrowth": 0.12, "totalDebt": 2e10, "totalCash": 3e10, "targetMeanPrice": 120.0,
                     "recommendationMean": 1.9, "shortPercentOfFloat": 0.02, "dividendYield": 0.5, "pegRatio": float("nan")}
        cols = pd.to_datetime(["2023-12-31", "2024-12-31", "2022-12-31"])  # unordered on purpose
        self.income_stmt = pd.DataFrame({cols[0]: [100.0, 60.0, 30.0, 20.0, 2.0], cols[1]: [120.0, 75.0, 40.0, 26.0, 2.6],
                                         cols[2]: [80.0, 45.0, 20.0, 12.0, 1.2]},
                                        index=["Total Revenue", "Gross Profit", "Operating Income", "Net Income", "Diluted EPS"])
        self.quarterly_income_stmt = self.income_stmt
        self.insider_transactions = pd.DataFrame({"Text": ["Sale at price 100", "Purchase at price 90"], "Value": [1e6, 2e5],
                                                  "Insider": ["A", "B"], "URL": ["", ""]})
        self.insider_purchases = pd.DataFrame()
        self.institutional_holders = pd.DataFrame({"Holder": ["Big Fund"], "pctHeld": [0.08]})
        self.mutualfund_holders = pd.DataFrame()
        self.major_holders = pd.DataFrame()


@pytest.fixture
def fake_yf(monkeypatch):
    monkeypatch.setattr("autobench.services.fundamentals.yf.Ticker", FakeTicker)


def test_fundamentals_mapping(fake_yf):
    f = fundamentals.get_fundamentals("abc")
    assert f["profile"]["name"] == "ABC Inc"
    assert f["valuation"]["fcf_yield_pct"] == 5.0 and f["valuation"]["earnings_yield_pct"] == 4.0
    assert f["valuation"]["peg"] is None  # NaN cleaned
    assert f["profitability"]["gross_margin_pct"] == 70.0
    assert f["financial_health"]["net_debt"] == -1e10
    assert f["analysts"]["upside_to_mean_target_pct"] == 20.0


def test_statements_sorted_with_derived_ratios(fake_yf):
    s = fundamentals.get_financial_statements("ABC", "income", "annual")
    assert s["periods"] == ["2022-12-31", "2023-12-31", "2024-12-31"]
    assert s["rows"]["Total Revenue"] == [80.0, 100.0, 120.0]
    assert s["derived"]["gross_margin_pct"] == [56.25, 60.0, 62.5]
    assert s["derived"]["revenue_growth_pct"] == [None, 25.0, 20.0]


def test_ownership_and_peers(fake_yf):
    o = fundamentals.get_ownership("ABC")
    assert o["insider_summary"]["open_market_buys"] == 1 and o["insider_summary"]["sales"] == 1
    assert "URL" not in o["insider_transactions"][0]
    p = fundamentals.compare_fundamentals(["ABC", "XYZ"])
    assert set(p["companies"]) == {"ABC", "XYZ"} and p["peer_medians"]["trailing_pe"] == 25.0


def test_options_max_pain():
    calls = pd.DataFrame({"strike": [90.0, 100.0, 110.0], "openInterest": [100, 500, 1000]})
    puts = pd.DataFrame({"strike": [90.0, 100.0, 110.0], "openInterest": [1000, 500, 100]})
    # settlement at 100: calls pay 10*100, puts pay 10*100 -> minimum pain
    assert options._max_pain(calls, puts) == 100.0


TENK = """<html><head><title>x</title></head><body><ix:header><p>hidden xbrl</p></ix:header>
<p>TABLE OF CONTENTS</p><p>Item 1. Business</p><p>Item 1A. Risk Factors</p><p>Item 7. Management's Discussion</p>
<p>PART I</p><p>Item 1. Business</p><p>We make widgets. """ + "Widgets are great. " * 60 + """</p>
<p>Item 1A. Risk Factors</p><p>""" + "Competition may hurt us. " * 80 + """</p>
<table><tr><td>Revenue</td><td>100</td></tr></table>
<p>Item 7. Management&#8217;s Discussion and Analysis</p><p>""" + "Revenue grew. " * 70 + """</p></body></html>"""


def test_html_to_text_and_sections():
    text = filings.html_to_text(TENK)
    assert "hidden xbrl" not in text and "Revenue | 100" in text
    secs = {s["item"]: s for s in filings._sections(text)}
    assert secs["1A"]["chars"] > 1000, "body occurrence (not the TOC line) must win"
    assert "Competition" in text[secs["1A"]["start"]:secs["1A"]["end"]]


@respx.mock
def test_read_filing_section_and_pagination():
    url = "https://www.sec.gov/Archives/edgar/data/1/000000000125000001/x-10k.htm"
    respx.get(url).mock(return_value=httpx.Response(200, text=TENK))
    r = filings.read_filing(url, "risk_factors", max_chars=1000)
    assert r["section"]["item"] == "1A" and r["text"].startswith("Item 1A")
    assert r["next_offset"] == 1000 and r["total_chars"] > 1000
    r2 = filings.read_filing(url, "risk_factors", offset=r["next_offset"], max_chars=40000)
    assert r2["next_offset"] is None
    with pytest.raises(ValueError, match="not found"):
        filings.read_filing(url, "executive_compensation")


def test_only_sec_urls_allowed():
    for bad in ["https://evil.com/x", "http://localhost:8000/api", "https://sec.gov.evil.com/x", "file:///etc/passwd"]:
        with pytest.raises(ValueError, match="sec.gov"):
            filings._get(bad)


def _fact(start, end, val, fp, form="10-K", filed="2025-02-01"):
    return {"start": start, "end": end, "val": val, "fy": int(end[:4]), "fp": fp, "form": form, "filed": filed}


@respx.mock
def test_financial_history_annual_quarterly_and_derived_q4(monkeypatch):
    monkeypatch.setattr("autobench.services.filings.cik_for", lambda s: 1)
    rev = [
        _fact("2023-01-01", "2023-12-31", 400, "FY"),
        _fact("2024-01-01", "2024-12-31", 480, "FY"),
        _fact("2024-01-01", "2024-03-31", 100, "Q1", "10-Q"),
        _fact("2024-04-01", "2024-06-30", 110, "Q2", "10-Q"),
        _fact("2024-07-01", "2024-09-30", 120, "Q3", "10-Q"),
        _fact("2024-01-01", "2024-09-30", 330, "Q3", "10-Q"),  # 9-month YTD: must be ignored
        # restated FY2023 in a later filing: newest filing wins
        _fact("2023-01-01", "2023-12-31", 405, "FY", filed="2025-02-01"),
    ]
    rev[0]["filed"] = "2024-02-01"
    facts = {"entityName": "Test Co", "facts": {"us-gaap": {
        "Revenues": {"units": {"USD": rev}},
        "Assets": {"units": {"USD": [{"end": "2024-12-31", "val": 999, "fp": "FY", "form": "10-K", "filed": "2025-02-01"}]}},
    }}}
    respx.get("https://data.sec.gov/api/xbrl/companyfacts/CIK0000000001.json").mock(return_value=httpx.Response(200, json=facts))
    a = filings.financial_history("TST", ["revenue", "total_assets"], "annual")
    assert a["metrics"]["revenue"] == {"2023-12-31": 405, "2024-12-31": 480}
    assert a["metrics"]["total_assets"] == {"2024-12-31": 999}
    q = filings.financial_history("TST", ["revenue"], "quarterly")
    assert q["metrics"]["revenue"]["2024-12-31"] == 480 - 330  # derived Q4
    assert q["derived_q4"] == {"revenue": ["2024-12-31"]}
    with pytest.raises(ValueError, match="Unknown metrics"):
        filings.financial_history("TST", ["bogus"])


def test_clean_handles_numpy_and_nan():
    assert fundamentals.clean({"a": np.float64("nan"), "b": np.int64(3), "c": pd.Timestamp("2024-01-02"), "d": [np.inf]}) == \
        {"a": None, "b": 3, "c": "2024-01-02", "d": [None]}
