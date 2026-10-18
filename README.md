# Autobench

A stock research workbench for US equities that an AI agent can operate. It covers charts,
41 technical indicators, fundamentals, SEC filings, options and news. It also includes
quantitative testing tools, and an MCP server through which an agent reads all of it, draws
on your live chart and records recommendations that are later graded against real prices.

```
 Alpaca (bars, quotes, news, symbols)  ─┐
 Yahoo (fundamentals, options, indices) ├─▶  backend (FastAPI)  ──REST/WebSocket──▶  web UI (chart)
 SEC EDGAR (filings, XBRL history)     ─┘     │  SQLite: bar cache, drawings, analyses, notes
                                              └──MCP /mcp──▶  AI agent (Claude Code + skills)
```

## Quick start

```bash
cp .env.example .env            # add ALPACA_API_KEY_ID and ALPACA_API_SECRET_KEY
(cd web && dart pub get && dart run tool/build.dart)   # build the UI once (needs the Dart SDK)
cd backend
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/autobench serve       # UI + API: http://127.0.0.1:8000 · MCP: http://127.0.0.1:8000/mcp
```

The web UI is written in Dart and compiled to JavaScript (no npm or Node). Its chart engine is
part of the codebase, and its only dependency is `package:web`. For UI development, run
`dart run tool/build.dart --watch` in `web/` and reload the page after each rebuild.

### Using the chart
- Search a ticker, pick an interval, and add indicators with **ƒx Indicators**. The indicator set is
  shared state, so the agent can change it too.
- Draw with the left toolbar: trend line, ray, horizontal and vertical lines, zone, Fibonacci,
  measure, arrow, long/short position, and text. Clicks snap to the nearest OHLC price.
  **Delete** removes the selected drawing and **Esc** cancels.
- Agent drawings are marked ✦ and colored violet by default. The **Agent** tab shows live activity,
  saved recommendations with their graded outcomes, and the track record.

Without Alpaca keys, bars and quotes fall back to Yahoo automatically. `AUTOBENCH_BAR_PROVIDER=fake`
runs on deterministic synthetic data, which needs no network.

### Connect an AI agent

- **Claude Code**: open this repo. `.mcp.json` registers the `autobench` server and
  `.claude/skills/` provides the research skills. Try: *"research NVDA for a swing trade"*.
- **Other MCP clients over HTTP**: `http://127.0.0.1:8000/mcp`
  (e.g. `claude mcp add --transport http autobench http://127.0.0.1:8000/mcp`).
- **stdio-only clients** (e.g. Claude Desktop): command `backend/.venv/bin/autobench`, args `["mcp-stdio"]`.
  Drawings are relayed to a running `autobench serve`, so the chart still updates live.

## What the agent can do (48 MCP tools)

| Area | Tools |
|---|---|
| Market data | search_symbols, get_quote, get_price_history (OHLC-correct downsampling), get_market_overview, get_market_movers |
| Technicals | get_technical_summary, get_multi_timeframe, get_indicators (41), get_levels (S/R, swings, trendlines, fib, volume profile, pivots), get_patterns |
| Quant | run_backtest (no look-ahead, costs, stops, OOS split, robustness), run_signal_study (forward returns vs baseline, t-stats), scan_symbols, evaluate_expression, get_risk_metrics, get_correlations, calculate_position_size |
| Fundamentals | get_fundamentals, get_financial_statements, get_financial_history (SEC XBRL, 15y), get_earnings, get_analyst_ratings, get_ownership, compare_peers |
| Research | get_news, get_options_overview, get_option_chain, list_sec_filings, read_sec_filing (10-K/10-Q sections), get_filing_documents, get_research_brief |
| Chart | get_chart_view, set_chart_view, draw_annotations (17 drawing types incl. trade plans), list/update/delete_annotations |
| Memory | save_analysis, list_analyses, get_analysis, get_track_record (hit rate, R, alpha, Brier calibration), save_note, search_notes, watchlists, scan_watchlist |

Strategies and conditions use a safe expression language, for example
`crosses_above(ema(20), ema(50)) and rvol(20) > 1.5` or `close < bbands(20,2).lower and close > sma(200)`.

## Skills (the research method)

`stock-research` (master workflow) · `technical-analysis` · `quant-validation` · `fundamental-analysis` ·
`trade-planning` · `chart-annotation` · `earnings-preview` · `market-regime` · `watchlist-screening` ·
`recommendation-review`

## Tests

```bash
cd backend && .venv/bin/python -m pytest -q        # offline suite
.venv/bin/python -m pytest -q -m live               # real Yahoo / SEC / Alpaca smoke tests
```

UI: `cd web && dart run test/core_test.dart` (unit tests). For end-to-end tests in headless Chrome, run
`pip install -e ".[ui-test]"` and then `pytest -q tests/test_ui_e2e.py`.

The offline suite checks indicators against reference values, verifies the backtester's exact fills
on hand-built price paths, tests the Alpaca client against mocked HTTP, and covers the API,
WebSocket and MCP end to end.

*Research tooling, not financial advice.*
