"""Account history from IBKR statements: parsing (Flex XML, Activity CSV, Flex Web Service), idempotent
import, and performance that keeps growth, contributions and returns apart."""

import json

import pytest
from fastapi.testclient import TestClient
from mcp import Client

from autobench import db
from autobench.app import create_app
from autobench.mcp_server import mcp
from autobench.services import portfolio
from autobench.services.statements import StatementError, parse
from autobench.services.trading import round_trips

H = {"X-Autobench": "1"}

# Deposit 10k, +5% day, deposit 5k, -2% day, withdraw 2k. TWR = 1.05 * 0.98 - 1 = 2.9 %.
FLEX = """<FlexQueryResponse queryName="history" type="AF">
<FlexStatements count="1">
<FlexStatement accountId="U1234567" fromDate="20240102" toDate="20240108" period="Custom" whenGenerated="20240109;080000">
<AccountInformation accountId="U1234567" currency="USD" name="Test Owner" />
<ChangeInNAV accountId="U1234567" fromDate="20240102" toDate="20240108" startingValue="0" mtm="147.5"
  depositsWithdrawals="13000" dividends="50" withholdingTax="-7.5" commissions="-2" endingValue="13190" twr="2.9" />
<EquitySummaryInBase>
  <EquitySummaryByReportDateInBase accountId="U1234567" reportDate="20240102" cash="10000" stock="0" options="0" total="10000" />
  <EquitySummaryByReportDateInBase accountId="U1234567" reportDate="20240103" cash="500" stock="10000" options="0" total="10500" />
  <EquitySummaryByReportDateInBase accountId="U1234567" reportDate="20240104" cash="5500" stock="10000" options="0" total="15500" />
  <EquitySummaryByReportDateInBase accountId="U1234567" reportDate="20240105" cash="3190" stock="12000" options="0" total="15190" />
  <EquitySummaryByReportDateInBase accountId="U1234567" reportDate="20240108" cash="1190" stock="12000" options="0" total="13190" />
</EquitySummaryInBase>
<Trades>
  <Trade accountId="U1234567" assetCategory="STK" symbol="AAPL" currency="USD" fxRateToBase="1" multiplier="1"
    tradeDate="20240102" dateTime="20240102;100000" buySell="BUY" quantity="100" tradePrice="100" ibCommission="-1"
    fifoPnlRealized="0" ibExecID="0001.aaaa.01.01" tradeID="11" levelOfDetail="EXECUTION" />
  <Trade accountId="U1234567" assetCategory="STK" symbol="AAPL" currency="USD" fxRateToBase="1" multiplier="1"
    tradeDate="20240103" dateTime="20240103;150000" buySell="SELL" quantity="-100" tradePrice="104" ibCommission="-1"
    fifoPnlRealized="398" ibExecID="0001.aaaa.02.01" tradeID="12" levelOfDetail="EXECUTION" />
  <Trade accountId="U1234567" assetCategory="STK" symbol="AAPL" currency="USD" tradeDate="20240103" dateTime="20240103;150000"
    buySell="SELL" quantity="-100" tradePrice="104" levelOfDetail="CLOSED_LOT" />
  <Trade accountId="U1234567" assetCategory="CASH" symbol="EUR.USD" currency="USD" tradeDate="20240104" dateTime="20240104;100000"
    buySell="BUY" quantity="1000" tradePrice="1.09" ibExecID="0001.fx.01.01" levelOfDetail="EXECUTION" />
  <Trade accountId="U1234567" assetCategory="STK" symbol="MSFT" currency="USD" fxRateToBase="1" multiplier="1"
    tradeDate="20240105" dateTime="20240105;100000" buySell="BUY" quantity="30" tradePrice="400" ibCommission="-1"
    ibExecID="0001.bbbb.01.01" tradeID="13" levelOfDetail="EXECUTION" />
</Trades>
<CashTransactions>
  <CashTransaction accountId="U1234567" type="Deposits/Withdrawals" currency="USD" fxRateToBase="1" amount="10000"
    dateTime="20240102" description="CASH RECEIPTS / ELECTRONIC FUND TRANSFERS" transactionID="901" levelOfDetail="DETAIL" />
  <CashTransaction accountId="U1234567" type="Deposits/Withdrawals" currency="USD" fxRateToBase="1" amount="5000"
    dateTime="20240103;221500" reportDate="20240104" description="CASH RECEIPTS / ELECTRONIC FUND TRANSFERS" transactionID="902"
    levelOfDetail="DETAIL" />
  <CashTransaction accountId="U1234567" type="Dividends" currency="USD" fxRateToBase="1" amount="50" symbol="MSFT"
    dateTime="20240105" description="MSFT CASH DIVIDEND" transactionID="903" levelOfDetail="DETAIL" />
  <CashTransaction accountId="U1234567" type="Withholding Tax" currency="USD" fxRateToBase="1" amount="-7.5" symbol="MSFT"
    dateTime="20240105" description="MSFT US TAX" transactionID="904" levelOfDetail="DETAIL" />
  <CashTransaction accountId="U1234567" type="Deposits/Withdrawals" currency="USD" fxRateToBase="1" amount="-2000"
    dateTime="20240108" description="DISBURSEMENT" transactionID="905" levelOfDetail="DETAIL" />
  <CashTransaction accountId="U1234567" type="Dividends" currency="USD" amount="50" dateTime="20240105" levelOfDetail="SUMMARY" />
</CashTransactions>
<OpenPositions>
  <OpenPosition accountId="U1234567" assetCategory="STK" symbol="MSFT" currency="USD" fxRateToBase="1" position="30"
    markPrice="400" positionValue="12000" costBasisMoney="12001" fifoPnlUnrealized="-1" reportDate="20240108" levelOfDetail="SUMMARY" />
</OpenPositions>
<CashReport>
  <CashReportCurrency accountId="U1234567" currency="BASE_SUMMARY" endingCash="1190" levelOfDetail="Currency" />
  <CashReportCurrency accountId="U1234567" currency="USD" endingCash="1190" toDate="20240108" levelOfDetail="Currency" />
</CashReport>
</FlexStatement>
</FlexStatements>
</FlexQueryResponse>"""

CSV = """﻿Statement,Header,Field Name,Field Value
Statement,Data,Title,Activity Statement
Statement,Data,Period,"January 1, 2024 - January 31, 2024"
Account Information,Header,Field Name,Field Value
Account Information,Data,Name,Test Owner
Account Information,Data,Account,U7654321
Account Information,Data,Base Currency,USD
Change in NAV,Header,Field Name,Field Value
Change in NAV,Data,Starting Value,"20,000"
Change in NAV,Data,Mark-to-Market,600
Change in NAV,Data,Deposits & Withdrawals,1000
Change in NAV,Data,Dividends,10
Change in NAV,Data,Ending Value,"21,610"
Trades,Header,DataDiscriminator,Asset Category,Currency,Symbol,Date/Time,Quantity,T. Price,C. Price,Proceeds,Comm/Fee,Basis,Realized P/L,MTM P/L,Code
Trades,Data,Order,Stocks,USD,NVDA,"2024-01-10, 10:15:00",10,500,505,-5000,-1,5001,0,50,O
Trades,SubTotal,,Stocks,USD,NVDA,,10,,,-5000,-1,5001,0,50,
Trades,Header,DataDiscriminator,Asset Category,Currency,Symbol,Date/Time,Quantity,T. Price,C. Price,Proceeds,Comm/Fee,Basis,Realized P/L,MTM P/L,Code
Trades,Data,Order,Equity and Index Options,USD,SPY 19JAN24 470 C,"2024-01-11, 11:00:00",-1,2.5,2,250,-0.7,-249.3,0,50,O
Deposits & Withdrawals,Header,Currency,Settle Date,Description,Amount
Deposits & Withdrawals,Data,USD,2024-01-05,Electronic Fund Transfer,1000
Deposits & Withdrawals,Data,Total,,,1000
Dividends,Header,Currency,Date,Description,Amount
Dividends,Data,USD,2024-01-20,AAPL(US0378331005) Cash Dividend USD 0.24 per Share (Ordinary Dividend),10
Dividends,Data,Total,,,10
Open Positions,Header,DataDiscriminator,Asset Category,Currency,Symbol,Quantity,Mult,Cost Price,Cost Basis,Close Price,Value,Unrealized P/L,Code
Open Positions,Data,Summary,Stocks,USD,NVDA,10,1,500.1,5001,560,5600,599,
"""


@pytest.fixture
def client():
    with TestClient(create_app()) as c:
        yield c


def test_parse_flex_xml():
    st = parse(FLEX)
    assert st["kind"] == "flex_xml" and st["accounts"]["U1234567"]["currency"] == "USD"
    assert (st["from_date"], st["to_date"]) == ("2024-01-02", "2024-01-08")
    # executions only: closed lots and currency conversions are left out
    assert [(t["symbol"], t["side"], t["qty"], t["commission"]) for t in st["trades"]] == [
        ("AAPL", "buy", 100, 1.0), ("AAPL", "sell", 100, 1.0), ("MSFT", "buy", 30, 1.0)]
    assert {c["kind"] for c in st["cash"]} == {"deposit", "withdrawal", "dividend", "tax"}
    assert len(st["cash"]) == 5  # the SUMMARY row is not counted twice
    assert [n["total"] for n in st["nav"]] == [10000, 10500, 15500, 15190, 13190]
    assert {p["symbol"] for p in st["positions"]} == {"MSFT", "USD"}
    assert st["summary"]["U1234567"]["twr"] == 2.9


def test_flex_web_service_reply_is_not_a_statement():
    with pytest.raises(StatementError, match="Token has expired"):
        parse("<FlexStatementResponse><Status>Fail</Status><ErrorCode>1012</ErrorCode>"
              "<ErrorMessage>Token has expired.</ErrorMessage></FlexStatementResponse>")
    with pytest.raises(StatementError):
        parse("hello,world")


def test_import_is_idempotent_and_separates_returns_from_deposits(client):
    r = client.post("/api/portfolio/import", json={"text": FLEX, "filename": "flex.xml"}, headers=H)
    assert r.status_code == 200, r.text
    assert r.json()["counts"] == {"trades": 3, "cash": 5, "nav": 5, "positions": 2}
    again = client.post("/api/portfolio/import", json={"text": FLEX}, headers=H).json()["counts"]
    assert again.get("trades", 0) == 0 and again.get("cash", 0) == 0 and again["trades_known"] == 3

    p = client.get("/api/portfolio/performance", params={"account": "U1234567"}).json()["summary"]
    assert p["start_value"] == 0 and p["end_value"] == 13190
    assert p["growth"] == 13190
    assert p["contributions"] == {"net": 13000, "deposits": 15000, "withdrawals": -2000, "transfers": 0}
    assert p["investment_gain"] == 190  # what the account earned, deposits excluded
    assert p["income"]["dividends"] == 50 and p["income"]["withholding_tax"] == -7.5 and p["income"]["total"] == 42.5
    assert p["trading"]["total"] == 147.5 and p["trading"]["realized"] == 398 and p["trading"]["commissions"] == -3
    assert p["twr_pct"] == pytest.approx(2.9, abs=0.01)
    assert p["mwr_pct"] is not None and 0 < p["mwr_pct"] < 5

    # a sub-period: only the 2nd deposit and the withdrawal fall inside, the 5% day does not
    q = client.get("/api/portfolio/performance", params={"account": "U1234567", "start": "2024-01-04"}).json()["summary"]
    assert q["start_value"] == 10500 and q["contributions"]["net"] == 3000
    assert q["investment_gain"] == pytest.approx(-310) and q["twr_pct"] == pytest.approx(-2.0, abs=0.01)

    ov = client.get("/api/portfolio/overview", params={"account": "U1234567"}).json()
    assert ov["totals"]["deposit"] == 15000 and ov["totals"]["withdrawal"] == -2000
    assert ov["totals"]["net_contributions"] == 13000 and ov["totals"]["dividend"] == 50
    assert {x["symbol"] for x in ov["statement_positions"]["positions"]} == {"MSFT", "USD"}
    assert ov["lifetime"]["investment_gain"] == 190

    flows = client.get("/api/portfolio/cash-flows", params={"account": "U1234567", "kind": "contributions"}).json()
    assert [f["amount"] for f in flows["items"]] == [-2000, 5000, 10000]

    perf = client.get("/api/portfolio/performance", params={"account": "U1234567"}).json()
    assert perf["series"][-1] == {"date": "2024-01-08", "nav": 13190, "contributed": 13000, "gain": 190,
                                  "twr_pct": pytest.approx(2.9, abs=0.01)}
    assert perf["monthly"][0]["key"] == "2024-01" and perf["statement"]["twr"] == 2.9

    # imported executions join the trade history (the Journal) of that account
    trips = round_trips(portfolio_fills("U1234567"))
    aapl = next(t for t in trips if t["symbol"] == "AAPL")
    assert aapl["status"] == "closed" and aapl["realized_pnl"] == 400 and aapl["net_pnl"] == 398


def portfolio_fills(account):
    from autobench.services.trading import trading

    return trading.fills(account=account, limit=1000)


def test_live_fills_are_not_imported_twice(client):
    with db.session_scope() as s:  # the app recorded this execution live, under its own id
        s.add(db.FillRow(exec_id="live-1", account="U1234567", broker="ibkr", symbol="AAPL", side="buy", qty=100,
                         price=100, time=1704207600, commission=1.0))
    counts = portfolio.import_statement(FLEX)["counts"]
    assert counts["trades"] == 2 and counts["trades_known"] == 1


def test_activity_statement_csv(client):
    st = parse(CSV)
    assert st["kind"] == "activity_csv" and list(st["accounts"]) == ["U7654321"]
    assert [(t["symbol"], t["side"], t["asset_class"], t["multiplier"]) for t in st["trades"]] == [
        ("NVDA", "buy", "STK", 1.0), ("SPY 19JAN24 470 C", "sell", "OPT", 100.0)]
    assert [(c["kind"], c["amount"], c["symbol"]) for c in st["cash"]] == [("deposit", 1000, None), ("dividend", 10, "AAPL")]
    assert [(n["date"], n["total"]) for n in st["nav"]] == [("2023-12-31", 20000), ("2024-01-31", 21610)]
    client.post("/api/portfolio/import", json={"text": CSV, "filename": "activity.csv"}, headers=H)
    p = client.get("/api/portfolio/performance", params={"account": "U7654321"}).json()["summary"]
    assert p["growth"] == 1610 and p["contributions"]["net"] == 1000 and p["investment_gain"] == 610
    assert p["income"]["dividends"] == 10 and p["trading"]["total"] == 600
    # one gap with a mid-month deposit: Modified Dietz weights it by the days it was invested
    assert p["twr_pct"] == pytest.approx(610 / (20000 + 1000 * 27 / 31) * 100, abs=0.01)


def test_single_day_history_has_no_rates(client):
    with db.session_scope() as s:
        s.add(db.NavRow(account="U1", date="2024-03-01", total=5000))
    p = portfolio.performance("U1")["summary"]
    assert p["opening_balance"] == 5000 and p["investment_gain"] == 0
    assert p["twr_pct"] == 0 and p["mwr_pct"] is None


def test_option_round_trip_uses_the_multiplier():
    fills = [{"exec_id": "1", "symbol": "SPY C", "side": "buy", "qty": 1, "price": 2.0, "time": 1, "multiplier": 100},
             {"exec_id": "2", "symbol": "SPY C", "side": "sell", "qty": 1, "price": 2.5, "time": 2, "multiplier": 100}]
    t = round_trips(fills)[0]
    assert t["realized_pnl"] == 50 and t["return_pct"] == 25


def test_delete_import_and_write_guard(client):
    assert client.post("/api/portfolio/import", json={"text": FLEX}).status_code == 403
    assert client.post("/api/portfolio/import", json={"text": "nope"}, headers=H).status_code == 400
    iid = client.post("/api/portfolio/import", json={"text": FLEX}, headers=H).json()["id"]
    assert client.delete(f"/api/portfolio/imports/{iid}", headers=H).json()["rows"] == 15
    assert client.get("/api/portfolio/imports").json()["imports"] == []
    assert client.get("/api/portfolio/cash-flows", params={"account": "U1234567"}).json()["items"] == []


def test_flex_web_service_sync_keeps_the_token_private(client, monkeypatch):
    r = client.post("/api/portfolio/flex", json={"token": "secret-token-123", "query_id": "98765"}, headers=H).json()
    assert r["has_token"] and r["query_id"] == "98765" and "secret-token-123" not in json.dumps(r)
    assert "secret-token-123" not in client.get("/api/portfolio/flex").text

    calls = []

    def fake_get(url):
        calls.append(url)
        if "SendRequest" in url:
            assert "t=secret-token-123" in url and "q=98765" in url
            return ("<FlexStatementResponse><Status>Success</Status><ReferenceCode>555</ReferenceCode>"
                    "<Url>https://example.invalid/GetStatement</Url></FlexStatementResponse>")
        if len(calls) == 2:
            return ("<FlexStatementResponse><Status>Warn</Status><ErrorCode>1019</ErrorCode>"
                    "<ErrorMessage>Statement generation in progress.</ErrorMessage></FlexStatementResponse>")
        assert "q=555" in url
        return FLEX

    res = portfolio.flex_sync(get=fake_get, wait=lambda s: None)
    assert res["source"] == "flex_web" and res["counts"]["nav"] == 5 and len(calls) == 3
    st = client.get("/api/portfolio/flex").json()
    assert st["last_error"] is None and st["last_import"] == res["id"]

    def failing(url):
        return ("<FlexStatementResponse><Status>Fail</Status><ErrorCode>1012</ErrorCode>"
                "<ErrorMessage>Token has expired.</ErrorMessage></FlexStatementResponse>")

    with pytest.raises(StatementError, match="expired"):
        portfolio.flex_sync(get=failing)
    assert "expired" in client.get("/api/portfolio/flex").json()["last_error"]
    client.post("/api/portfolio/flex", json={"clear_token": True}, headers=H)
    assert client.get("/api/portfolio/flex").json()["has_token"] is False


@pytest.mark.anyio
async def test_agent_reads_account_history(client):
    portfolio.import_statement(FLEX)
    async with Client(mcp) as c:
        r = await c.call_tool("get_account_history", {"section": "performance", "account": "U1234567"})
        p = json.loads(r.content[0].text)["summary"]
        assert p["investment_gain"] == 190 and p["contributions"]["net"] == 13000
        r = await c.call_tool("get_account_history", {"section": "cash_flows", "account": "U1234567", "kind": "income"})
        assert {f["kind"] for f in json.loads(r.content[0].text)["items"]} == {"dividend", "tax"}


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_flows_are_dated_by_when_the_account_value_includes_them():
    """IBKR stamps a deposit with the time it was started (often the evening before); the NAV includes it on
    reportDate. Dating it by dateTime would show a one-day loss then a gain and skew the time-weighted return."""
    early = FLEX.replace('reportDate="20240104" ', "")
    portfolio.import_statement(early, "early.xml", "upload")
    assert portfolio.performance("U1234567", "all")["summary"]["twr_pct"] != 2.9
    r = portfolio.import_statement(FLEX, "fixed.xml", "upload")  # a later statement corrects the stored date
    assert r["counts"].get("cash", 0) == 0 and r["counts"]["cash_known"] > 0
    assert [f["date"] for f in portfolio.cash_flows("U1234567", kind="deposit")] == ["2024-01-04", "2024-01-02"]
    assert portfolio.performance("U1234567", "all")["summary"]["twr_pct"] == 2.9

