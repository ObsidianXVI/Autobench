# Autobench

A stock research workbench for US equities that an AI agent can operate. It combines
TradingView-style charting (multi-chart layouts, drawing tools, alerts) with FinViz-style market
data: a screener over every US-listed stock, a sector heatmap, market breadth, a macro dashboard and
earnings/economic calendars. It also has 41 technical indicators, fundamentals, SEC filings,
options, news and quantitative testing tools. An MCP server gives an AI agent the same data: it
can screen the market, draw on your live chart, set alerts and open pages for you. Its
recommendations are recorded and later graded against real prices. With Interactive Brokers (or the
built-in simulator) connected, you trade from the chart and the agent proposes risk-checked orders
that you approve.

```
 Alpaca (bars, quotes, snapshots, news)   ─┐
 Yahoo (fundamentals, options, indices,    │
        futures, FX, crypto)               │
 SEC EDGAR (filings, XBRL frames)          ├─▶ backend (FastAPI) ──REST/WebSocket──▶ web UI (Dart)
 Nasdaq (listings, calendars) · Wikipedia  │      │  SQLite + universe cache: bars, drawings,
   (S&P 500 GICS) · FRED · Treasury · BLS ─┘      │  alerts, analyses, notes, layouts
                                                  ├──MCP /mcp──▶ AI agent (Claude Code + skills)
 IB Gateway / TWS (ib_async) ◀── orders, fills, positions, account ──┘  or the built-in simulator
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
  41 indicators with per-pane legends (hide, settings, remove). Drag or scroll over the price or time axis to scale that axis only,
  drag pane separators, double-click the price axis to auto-scale. Layouts save and load by name.
- **Layers, remembered per symbol**: each symbol's chart is a stack of layers. A layer holds series
  (the price chart, volume bars, indicators, other symbols as a line, area or candles, in the main
  pane, their own pane or another item's pane) and drawings. Layers can be shown/hidden, locked,
  renamed, duplicated, reordered and deleted, and rows dragged between them in the Layers panel. New
  items and drawings go to the active layer; the agent's drawings go to an "Agent" layer. "Save as
  default" makes a symbol's layers the template for symbols you have not set up yet.
- **Indicator settings**: typed inputs (numbers, price source), and per output line its color, width,
  solid/dashed/dotted, line/histogram/dots and visibility, plus custom levels (e.g. RSI 80/20). What you
  apply is kept on that indicator for the symbol and becomes your default for every new instance of it
  (*Built-in defaults* resets both).
- **A setup per timeframe**: give any layer its own price chart at an interval (layer ⋯ → *Add price
  chart*), e.g. a *Daily* layer with SMA 50/200 and an *Entry* layer at 30m with VWAP and RSI. The chart
  shows the layer for its interval (or the untied default), so switching the interval switches the
  setup, and clicking a layer's chip switches the interval. Layers without a chart can be tied to an
  interval too (*Show at*), or show everywhere.
- **State**: layouts, the open page and panels, zoom, intervals and every preference (theme, grid,
  colors, favorite intervals, sorting, filters) are saved to the server, mirrored to localStorage
  and flushed when the tab is hidden, so a reload or another browser picks up where you left off.
- **Drawing tools** (left bar): trend line, ray, extended and horizontal/vertical lines, parallel
  channel, Fibonacci, rectangle, arrow, text, callout, marker, measure and long/short positions. They
  have a magnet (snap to OHLC), stay-in-drawing mode and lock/hide-all. Select a drawing to drag it or
  its points. A floating toolbar then sets color, width and style, edits text, adds a line alert,
  locks, clones, hides or deletes it, with undo/redo for all of these. Text and callouts are always
  shown (the toolbar's **Text** checkbox hides them); text attached to other drawings appears when you
  hover the drawing. Labels from drawings in any layer, order lines, fills and the price axis move
  aside instead of covering each other.
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
Alerts are checked every 20 seconds while the server runs. A fired alert pops up as a toast with a
chime in every open tab, and is kept in the inbox (the tray button in the top bar, with an unread
count). Alerts that fired while no tab was open show as a toast the next time you open the app.

Without Alpaca keys, bars and quotes fall back to Yahoo automatically. `AUTOBENCH_BAR_PROVIDER=fake`
runs on deterministic synthetic data, which needs no network.

### Trading (Interactive Brokers or the simulator)

1. Start **IB Gateway** (or TWS) and log in to your **paper** account. In *Configure → Settings → API
   → Settings*: enable socket clients, add `127.0.0.1` to trusted IPs, uncheck *Read-Only API*.
   Gateway paper listens on 4002 (live 4001; TWS paper 7497, live 7496).
2. `autobench start`. It connects to `IBKR_HOST:IBKR_PORT` (default `127.0.0.1:4002`) and reconnects on
   its own when Gateway restarts. Change the broker, port or client id on the **Trading** page
   (*Connection*), or pick the **Simulator** to try everything without IBKR.
3. If the login holds several accounts (individual + IRA, advisor sub-accounts, …), pick one from the
   account menu in the Trading page header or in *Connection*; positions, orders, history, the order
   ticket and agent proposals all follow it, and the choice is remembered (`IBKR_ACCOUNT` sets the
   default). Switching disarms live trading, and a proposal can only be approved on the account it was
   made for. Paper and live are separate Gateway logins (ports 4002 / 4001); switch between them with
   the port presets in *Connection*. The simulator has two accounts to try this.

What you get:
- **Order ticket** (right dock, ⇅): buy/sell, limit / market / stop / stop-limit, sizing by shares,
  dollar value or risk % to the stop, stop-loss and take-profit (sent as a bracket: both activate when
  the entry fills and cancel each other), day/GTC, and a live risk check before you send.
- **On the chart**: the position line (size, average cost, open P&L, × to close), working order lines
  (drag to change the price, × to cancel, P&L at the stop/target while dragging), the agent's
  proposals (click to review), ▲/▼ execution markers and entry→exit lines for closed trades with
  their P&L and R. Right-click for *Trade at price* (buy/sell limit or stop) and position actions
  (close, close half, add or move the stop, breakeven). A long/short position drawing becomes an
  order with *Create order from this*.
- **Journal page**: every round-trip trade of the selected account with its outcome (P&L, return, R,
  holding time), the best and worst price while in it and how much of that move the exit kept, what
  price did 1, 5 and 20 sessions after the exit ("+$120 if held" / "saved $80"), and what other exits
  would have made (planned stop or target, best or worst price, holding longer), with a chart of the
  trade and notes per trade. Also your chart drawings of the last 7/30/90 days, aggregate performance
  (win rate, profit factor, expectancy, drawdown, streaks, exit efficiency, P&L by symbol, side, month,
  weekday and agent vs. your own ideas) and your notes per symbol.
- **Trading page**: net liquidation, day/unrealized/realized P&L, cash vs invested vs committed to
  orders vs free cash, open risk to stops, capital allocation, the equity curve, and tabs for
  positions, orders, agent proposals, trade history (round trips with R multiples, win rate, profit
  factor, expectancy), fills, the activity log and the risk limits.

Safety:
- **Agent orders need your approval** by default. They show up as a toast, a ✦ line on the chart and
  a card on the Trading page. *Auto* mode sends agent orders that pass the limits straight away, and
  only on paper accounts.
- **Risk limits are enforced on the server** for every order, yours and the agent's: max order value,
  max position % of equity, max risk to the stop, daily loss limit, orders per day, shorting, market
  entries, extended hours, and symbol allow/block lists. Orders that reduce a position skip the sizing
  limits so you can always get out.
- **Kill switch**: *Halt* cancels working entries and pending proposals and blocks new orders (your
  closing orders still work). *Halt & flatten* also cancels every order and closes all positions.
- **Live accounts are read-only** unless `AUTOBENCH_ALLOW_LIVE_TRADING=1` is set *and* you arm live
  trading on the Trading page (typing LIVE), each server session.
- Leftover stop-loss/take-profit orders are cancelled (or shrunk) when a position is closed (or
  reduced), so they cannot open an unintended position. Every action is in the activity log.
- The agent has no tools to approve orders, change limits or the mode, arm live trading or lift a
  halt. Trading API writes are accepted only from the local UI.

### Connect an AI agent

- **Claude Code**: open this repo. `.mcp.json` registers the `autobench` server and
  `.claude/skills/` provides the research skills. Try: *"research NVDA for a swing trade"*.
- **Other MCP clients over HTTP**: `http://127.0.0.1:8000/mcp`
  (e.g. `claude mcp add --transport http autobench http://127.0.0.1:8000/mcp`).
- **stdio-only clients** (e.g. Claude Desktop): command `backend/.venv/bin/autobench`, args `["mcp-stdio"]`.
  Drawings are relayed to a running `autobench serve`, so the chart still updates live.

## What the agent can do (84 MCP tools)

| Area | Tools |
|---|---|
| Market data | search_symbols, get_quote, get_price_history (OHLC-correct downsampling), get_market_overview, get_market_movers |
| Technicals | get_technical_summary, get_multi_timeframe, get_indicators (41), get_levels (S/R, swings, trendlines, fib, volume profile, pivots), get_patterns |
| Quant | run_backtest (no look-ahead, costs, stops, OOS split, robustness), run_signal_study (forward returns vs baseline, t-stats), scan_symbols, evaluate_expression, get_risk_metrics, get_correlations, calculate_position_size |
| Fundamentals | get_fundamentals, get_financial_statements, get_financial_history (SEC XBRL, 15y), get_earnings, get_analyst_ratings, get_ownership, compare_peers |
| Research | get_news, get_options_overview, get_option_chain, list_sec_filings, read_sec_filing (10-K/10-Q sections), get_filing_documents, get_research_brief |
| Chart | get_chart_view, set_chart_view (symbol, interval, range, chart type, scale), get_chart_layers, edit_chart_add / edit_chart_item / edit_chart_remove (indicators with params and per-output styles, other symbols, volume, panes), edit_chart_layer (layers per interval), set_chart_layout and manage_layouts (multi-chart grid, saved layouts), draw_annotations (17 drawing types incl. trade plans), list/update/delete_annotations |
| Market-wide | screen_stocks (~6,000 stocks, 70 fields, filters/expressions/signals), get_stock_snapshot, get_market_breadth, get_sector_performance, save_screen, list_screens, delete_screen |
| Macro & calendars | get_macro_dashboard (FRED/Treasury/BLS), get_macro_series, get_cross_asset_board, get_earnings_calendar, get_economic_calendar |
| Alerts & UI | create_alert (price, crossing, daily change, indicator condition), update_alert (move, pause, re-arm), list_alerts, delete_alert, get_alert_inbox, show_in_ui (pages, side panels, Trading/Journal tabs) |
| Trading | get_portfolio, get_trade_history, preview_order, propose_order (bracket orders for your approval), propose_close_position, propose_order_change (trail a stop, move a limit; for your approval), get_order_tickets, cancel_order, get_journal |
| Memory | save_analysis, list_analyses, get_analysis, get_track_record (hit rate, R, alpha, Brier calibration), save_note, search_notes, delete_note, list_watchlists, update_watchlist (sections, order, rename), delete_watchlist, scan_watchlist |

Strategies and conditions use a safe expression language, for example
`crosses_above(ema(20), ema(50)) and rvol(20) > 1.5` or `close < bbands(20,2).lower and close > sma(200)`.

## Skills (the research method)

`stock-research` (master workflow) · `technical-analysis` · `quant-validation` · `fundamental-analysis` ·
`trade-planning` · `trade-execution` · `chart-annotation` · `earnings-preview` · `market-regime` · `watchlist-screening` ·
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
