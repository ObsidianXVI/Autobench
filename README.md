# Autobench

A stock research workbench for US equities that an AI agent can operate. It combines
TradingView-style charting (multi-chart layouts, drawing tools, alerts) with FinViz-style market
data: a screener over every US-listed stock, a sector heatmap, market breadth, a macro dashboard and
earnings/economic calendars. It also has 41 technical indicators, fundamentals, SEC filings,
options, news and quantitative testing tools. An MCP server gives an AI agent the same data: it
can screen the market, draw on your live chart, set alerts and open pages for you. Its
recommendations are recorded and later graded against real prices.

```
 Alpaca (bars, quotes, snapshots, news)   ─┐
 Yahoo (fundamentals, options, indices,    │
        futures, FX, crypto)               │
 SEC EDGAR (filings, XBRL frames)          ├─▶ backend (FastAPI) ──REST/WebSocket──▶ web UI (Dart)
 Nasdaq (listings, calendars) · Wikipedia  │      │  SQLite + universe cache: bars, drawings,
   (S&P 500 GICS) · FRED · Treasury · BLS ─┘      │  alerts, analyses, notes, layouts
                                                  └──MCP /mcp──▶ AI agent (Claude Code + skills)
```

## Quick start

```bash
cp .env.example .env            # add ALPACA_API_KEY_ID and ALPACA_API_SECRET_KEY
ln -sf "$PWD/scripts/autobench" ~/.local/bin/autobench   # once: puts `autobench` on your PATH
autobench start                 # UI + API: http://127.0.0.1:8000 · MCP: http://127.0.0.1:8000/mcp
autobench status                # is it running, on which port
autobench logs                  # follow the server log
autobench restart               # stop + start
autobench stop                  # shut it down
```

`autobench start` sets up `backend/.venv` on first run, rebuilds the web UI when `web/` sources
changed, starts the server in the background (logs in `backend/data/autobench.log`) and waits until
it answers. Options: `--port N`, `--rebuild` (force a UI build) and `--fg` (run in the foreground).
Everything (UI, REST API, MCP and the background jobs) runs in that one process. `autobench serve`
and `autobench mcp-stdio` pass through to the Python CLI; without the PATH link, use
`scripts/autobench <command>`.

Manual equivalent:

```bash
(cd web && dart pub get && dart run tool/build.dart)   # build the UI once (needs the Dart SDK)
cd backend
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/autobench serve       # UI + API: http://127.0.0.1:8000 · MCP: http://127.0.0.1:8000/mcp
```

The web UI is written in Dart and compiled to JavaScript (no npm or Node). Its chart engine is
part of the codebase, and its only dependency is `package:web`. For UI development, run
`dart run tool/build.dart --watch` in `web/` and reload the page after each rebuild.

### The workbench
- **Chart**: 1-4 synced charts (symbol, interval, crosshair and range sync), candles, hollow candles,
  bars, line, area and Heikin-Ashi, regular/log/percent scales, compare overlays, earnings markers and
  41 indicators with per-pane legends (hide, settings, remove). Drag the price or time axis to scale,
  drag pane separators, double-click the price axis to auto-scale. Layouts save and load by name.
- **Layers, remembered per symbol**: each symbol's chart is a stack of layers. A layer holds series
  (the price chart, volume bars, indicators, other symbols as a line, area or candles, in the main
  pane, their own pane or another item's pane) and drawings. Layers can be shown/hidden, locked,
  renamed, duplicated, reordered and deleted, and rows dragged between them in the Layers panel. New
  items and drawings go to the active layer; the agent's drawings go to an "Agent" layer. "Save as
  default" makes a symbol's layers the template for symbols you have not set up yet.
- **State**: layouts, the open page and panels, zoom, intervals and every preference (theme, grid,
  colors, favorite intervals, sorting, filters) are saved to the server, mirrored to localStorage
  and flushed when the tab is hidden, so a reload or another browser picks up where you left off.
- **Drawing tools** (left bar): trend line, ray, extended and horizontal/vertical lines, parallel
  channel, Fibonacci, rectangle, arrow, text, callout, marker, measure and long/short positions. They
  have a magnet (snap to OHLC), stay-in-drawing mode and lock/hide-all. Select a drawing to drag it or
  its points. A floating toolbar then sets color, width and style, edits text, adds a line alert,
  locks, clones, hides or deletes it, with undo/redo for all of these.
- **Keyboard** (TradingView-style): start typing a ticker to search, type a number for the interval,
  `/` indicators, `Alt+T/H/J/V/F/L/S` tools, `Alt+A` alert, `Alt+R` reset, `Del`, `Esc`, `⌘Z/⇧⌘Z`,
  `⌘S` save layout, arrows to scroll/zoom, `?` for the full list. Right-click the chart or a drawing
  for context actions.
- **Right dock**: watchlists (sections, sorting, live quotes, keyboard navigation, drag to reorder)
  with symbol details below, plus alerts, news, the agent's activity and graded calls, notes, and
  the Layers panel.
- **Screener**: FinViz-style dropdown filters (descriptive, valuation, financial, performance,
  technical), a filter-expression bar (`pe < 25 and sales_qoq > 15 and sma200_pct > 0`), preset
  signals and screens, column views, chart previews on hover, a Charts tile view, saved screens, CSV
  export and add-to-watchlist.
- **Heatmap**: S&P 500 or the top 1,000 US stocks by sector and industry, sized by market cap and
  colored by 1D…1Y change. Click a sector to drill in, hover for industry peers.
- **Markets**: headline instruments, breadth, signal tables (gainers, losers, highs, lows, unusual
  volume …), sector performance, a cross-asset board (futures, rates, FX, crypto, global, factors,
  bonds), and a macro dashboard (rates and the yield curve, inflation, labor, growth, credit, housing)
  with full history charts. Today's economic releases, this week's earnings and market news are
  alongside.
- **Calendar**: weekly earnings board (before/after market, EPS estimates) and the economic calendar.
- **Agent drawings** are marked ✦ and colored violet. The agent can also switch your chart, open
  the screener with its filters, create alerts, and save screens and notes; everything appears live.

The market universe has about 6,000 stocks. Its first build takes about a minute (listings, SEC
fundamentals, a year of daily bars). After that it stays fresh in the background: prices every
minute while you use the screener, bars every 10 minutes in market hours, and fundamentals daily.
Alerts are checked every 20 seconds.

Without Alpaca keys, bars and quotes fall back to Yahoo automatically. `AUTOBENCH_BAR_PROVIDER=fake`
runs on deterministic synthetic data, which needs no network.

### Connect an AI agent

- **Claude Code**: open this repo. `.mcp.json` registers the `autobench` server and
  `.claude/skills/` provides the research skills. Try: *"research NVDA for a swing trade"*.
- **Other MCP clients over HTTP**: `http://127.0.0.1:8000/mcp`
  (e.g. `claude mcp add --transport http autobench http://127.0.0.1:8000/mcp`).
- **stdio-only clients** (e.g. Claude Desktop): command `backend/.venv/bin/autobench`, args `["mcp-stdio"]`.
  Drawings are relayed to a running `autobench serve`, so the chart still updates live.

## What the agent can do (62 MCP tools)

| Area | Tools |
|---|---|
| Market data | search_symbols, get_quote, get_price_history (OHLC-correct downsampling), get_market_overview, get_market_movers |
| Technicals | get_technical_summary, get_multi_timeframe, get_indicators (41), get_levels (S/R, swings, trendlines, fib, volume profile, pivots), get_patterns |
| Quant | run_backtest (no look-ahead, costs, stops, OOS split, robustness), run_signal_study (forward returns vs baseline, t-stats), scan_symbols, evaluate_expression, get_risk_metrics, get_correlations, calculate_position_size |
| Fundamentals | get_fundamentals, get_financial_statements, get_financial_history (SEC XBRL, 15y), get_earnings, get_analyst_ratings, get_ownership, compare_peers |
| Research | get_news, get_options_overview, get_option_chain, list_sec_filings, read_sec_filing (10-K/10-Q sections), get_filing_documents, get_research_brief |
| Chart | get_chart_view, set_chart_view, draw_annotations (17 drawing types incl. trade plans), list/update/delete_annotations |
| Market-wide | screen_stocks (~6,000 stocks, 70 fields, filters/expressions/signals), get_stock_snapshot, get_market_breadth, get_sector_performance, save_screen |
| Macro & calendars | get_macro_dashboard (FRED/Treasury/BLS), get_macro_series, get_cross_asset_board, get_earnings_calendar, get_economic_calendar |
| Alerts & UI | create_alert (price, crossing, daily change, indicator condition), list_alerts, delete_alert, show_in_ui |
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

The offline suite does the following:
- checks indicators against reference values
- verifies the backtester's exact fills on hand-built price paths
- tests the Alpaca client against mocked HTTP, including feed fallback under concurrency
- checks the TTM fundamentals arithmetic for calendar and off-calendar fiscal years
- checks the vectorized screener technicals against the reference indicators
- covers the API, WebSocket and MCP end to end

The UI suite drives the compiled app in headless Chrome. It covers the agent controlling the chart,
drawing, dragging and undoing, alerts, layouts, every page, mobile width and the light theme.

*Research tooling, not financial advice.*
