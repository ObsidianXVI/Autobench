"""MCP server through a real MCP client session (in-memory transport)."""

import json

import pytest
from mcp import Client

from autobench.mcp_server import mcp

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def call(c, name, args=None):
    r = await c.call_tool(name, args or {})
    text = r.content[0].text
    return r.is_error, (text if r.is_error else json.loads(text))


async def test_tool_catalog_is_well_formed():
    async with Client(mcp) as c:
        tools = (await c.list_tools()).tools
        names = {t.name for t in tools}
        assert len(tools) >= 45
        for required in ["get_research_brief", "get_technical_summary", "run_backtest", "run_signal_study",
                         "draw_annotations", "set_chart_view", "save_analysis", "get_track_record", "read_sec_filing",
                         "get_options_overview", "get_market_overview", "save_note", "scan_watchlist"]:
            assert required in names
        for t in tools:
            assert t.description and len(t.description) > 20, t.name
            assert t.input_schema["type"] == "object"
        draw = next(t for t in tools if t.name == "draw_annotations")
        assert "trendline" in json.dumps(draw.input_schema)  # enum of drawing types is exposed
        prompts = {p.name for p in (await c.list_prompts()).prompts}
        assert {"analyze_stock", "review_track_record"} <= prompts


async def test_market_and_technical_tools():
    async with Client(mcp) as c:
        err, h = await call(c, "get_price_history", {"symbol": "AAPL", "lookback": "2y", "max_bars": 100})
        assert not err and h["bars_returned"] <= 100 and h["bars_merged_per_row"] > 1
        assert h["columns"][0] == "date" and len(h["rows"][0]) == 6
        err, ind = await call(c, "get_indicators", {"symbol": "AAPL", "indicators": [
            {"name": "rsi"}, {"name": "bbands", "params": {"length": 20}}], "points": 5})
        assert not err and len(ind["dates"]) == 5 and ind["latest"]["rsi(14)"]["rsi"] is not None
        assert set(ind["series"]["bbands(20,2.0)"]) == {"upper", "middle", "lower", "bandwidth", "percent_b"}
        err, lv = await call(c, "get_levels", {"symbol": "AAPL"})
        assert not err and lv["swing_points"] and "pivots" in lv
        err, pt = await call(c, "get_patterns", {"symbol": "AAPL"})
        assert not err and "structure" in pt
        err, ex = await call(c, "evaluate_expression", {"symbol": "AAPL", "expression": "close > sma(50)"})
        assert not err and all(isinstance(v, bool) for v in ex["values"])


async def test_quant_tools_and_robustness():
    async with Client(mcp) as c:
        err, bt = await call(c, "run_backtest", {"symbol": "AAPL", "entry": "crosses_above(ema(10), ema(30))",
                                                 "exit": "crosses_below(ema(10), ema(30))", "symbols": ["MSFT", "NVDA"]})
        assert not err and set(bt["robustness"]) == {"MSFT", "NVDA"}
        err, st = await call(c, "run_signal_study", {"symbol": "AAPL", "condition": "rsi(14) < 30", "symbols": ["MSFT"]})
        assert not err and "MSFT" in st["robustness"]
        err, sc = await call(c, "scan_symbols", {"condition": "close > 0", "symbols": ["AAPL", "MSFT"]})
        assert not err and len(sc["matches"]) == 2
        err, ps = await call(c, "calculate_position_size", {"account_size": 100000, "entry": 50, "stop": 48,
                                                            "risk_pct": 1, "targets": [56]})
        assert not err and ps["shares"] == 500 and ps["targets"][0]["r_multiple"] == 3.0
        err, rm = await call(c, "get_risk_metrics", {"symbol": "AAPL"})
        assert not err and "vs_benchmark" in rm


async def test_errors_are_actionable():
    async with Client(mcp) as c:
        err, msg = await call(c, "run_signal_study", {"symbol": "AAPL", "condition": "rsi(14) < nope"})
        assert err and "Unknown name 'nope'" in msg
        err, msg = await call(c, "get_indicators", {"symbol": "AAPL", "indicators": [{"name": "rsx"}]})
        assert err and "Unknown indicator" in msg
        err, msg = await call(c, "draw_annotations", {"symbol": "AAPL", "items": [
            {"type": "long_position", "points": [{"time": "2025-01-02", "price": 100}, {"time": "2025-02-01", "price": 105},
                                                 {"time": "2025-02-01", "price": 120}]}]})
        assert err and "stop < entry < target" in msg
        err, msg = await call(c, "get_analysis", {"analysis_id": "nope"})
        assert err and "not found" in msg


async def test_chart_and_memory_workflow():
    async with Client(mcp) as c:
        err, v = await call(c, "set_chart_view", {"symbol": "msft", "interval": "1d",
                                                  "indicators": [{"name": "sma", "params": {"length": 50}}]})
        assert not err and v["symbol"] == "MSFT" and v["indicators"] == [{"name": "sma", "params": {"length": 50}}]
        err, v2 = await call(c, "get_chart_view")
        assert v2["symbol"] == "MSFT"
        err, d = await call(c, "draw_annotations", {"symbol": "MSFT", "items": [
            {"type": "horizontal_line", "points": [{"price": 300}], "text": "support", "tag": "msft-test"},
            {"type": "long_position", "points": [{"time": "2025-01-02", "price": 300}, {"time": "2025-02-03", "price": 290},
                                                 {"time": "2025-02-03", "price": 330}], "tag": "msft-test"},
            {"type": "fib_retracement", "points": [{"time": "2024-06-03", "price": 250}, {"time": "2024-12-02", "price": 350}],
             "tag": "msft-test", "style": {"show_labels": True}}]})
        assert not err and d["created"] == 3
        err, lst = await call(c, "list_annotations", {"symbol": "MSFT", "tag": "msft-test"})
        assert len(lst) == 3 and all(a["author"] == "agent" for a in lst)
        err, up = await call(c, "update_annotation", {"annotation_id": lst[0]["id"], "style": {"color": "#22c55e"}})
        assert not err and up["style"]["color"] == "#22c55e"
        err, sa = await call(c, "save_analysis", {"analysis": {
            "symbol": "MSFT", "action": "buy", "horizon": "swing", "confidence": 0.62, "summary": "Pullback to support",
            "rationale": "## Evidence\n...", "entry_low": 1, "entry_high": 1e9 / 1e6, "stop": 0.5, "targets": [2000],
            "scores": {"technical": 40}, "risks": ["earnings"], "annotation_tag": "msft-test"}})
        assert not err and sa["price_at_creation"] > 0
        err, ga = await call(c, "get_analysis", {"analysis_id": sa["id"]})
        assert not err and ga["evaluation"]["status"] in ("pending", "open")
        err, tr = await call(c, "get_track_record")
        assert not err and tr["summary"]["total"] == 1
        err, n = await call(c, "save_note", {"title": "MSFT thesis", "content": "cloud growth", "symbol": "MSFT", "tags": ["thesis"]})
        err, found = await call(c, "search_notes", {"query": "cloud"})
        assert found[0]["id"] == n["id"]
        err, wl = await call(c, "update_watchlist", {"name": "core", "add": ["AAPL", "MSFT", "NVDA"]})
        err, sw = await call(c, "scan_watchlist", {"name": "core"})
        assert not err and len(sw["ranked"]) == 3
        err, dl = await call(c, "delete_annotations", {"tag": "msft-test", "author": "agent"})
        assert dl["deleted"] == 3


async def test_guides_and_resource():
    async with Client(mcp) as c:
        err, g = await call(c, "get_guide", {"topic": "expressions"})
        assert "crosses_above" in g["text"]
        res = await c.read_resource("autobench://guide/annotations")
        assert "long_position" in res.contents[0].text
