# Autobench

Stock charting + research workbench for US equities. A FastAPI backend serves market data, technicals,
fundamentals, filings and quant tools to a web UI (REST + WebSocket) and to AI agents (MCP at `/mcp`).
Agents can draw on the user's live chart; drawings, analyses, notes and watchlists persist in SQLite.

## Using it as the research agent
- Start the server: `cd backend && .venv/bin/autobench serve` (http://127.0.0.1:8000, MCP at `/mcp`).
  `.mcp.json` registers it for Claude Code as `autobench`.
- Skills in `.claude/skills/` define the research method: start with `stock-research`; it delegates to
  `technical-analysis`, `quant-validation`, `fundamental-analysis`, `trade-planning`, `chart-annotation`,
  `earnings-preview`, `market-regime`, `watchlist-screening`, `recommendation-review`.
- Rule of the house: no edge without a test (`run_signal_study` / `run_backtest`), record every call with
  `save_analysis`, keep durable findings in `save_note`.

## Layout
```
backend/src/autobench/
  app.py            FastAPI app: REST /api/*, WebSocket /ws, MCP routes, static web/dist
  mcp_server.py     MCP tools/prompts/resources (compact JSON results, ToolError messages)
  guides.py         agent-facing reference text (server instructions, get_guide topics)
  schemas.py        pydantic models shared by REST and MCP (annotations, analyses, view, notes)
  config.py db.py timeutil.py
  providers/        alpaca.py (bars/quotes/news/assets), yahoo.py (fallback + index symbols), fake.py (tests)
  services/         market (bar cache), indicators (registry), expressions (safe DSL), backtest,
                    levels, patterns, technicals, risk, fundamentals, options, filings (SEC), news,
                    market_context, annotations, analyses (outcome tracking), workspace, events (bus)
backend/tests/      pytest suite (fake provider, no network); live smoke tests: `pytest -m live`
web/                React + Vite UI
```

## Commands
- Install: `cd backend && python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"`
- Test: `cd backend && .venv/bin/python -m pytest -q` (offline) · `-m live` hits Yahoo/SEC/Alpaca
- Run with synthetic data: `AUTOBENCH_BAR_PROVIDER=fake .venv/bin/autobench serve`
- Web UI dev: `cd web && npm install && npm run dev` (proxies /api, /ws, /mcp to :8000)

## Conventions
- Bar frames: index = epoch-second bar start (UTC), columns o,h,l,c,v; daily+ bars stamped 00:00 UTC of the
  session date; intraday = regular session only. Prices adjusted (`adjustment=all`).
- New indicator: decorate a function with `@indicator(...)` in `services/indicators.py`; it is automatically
  available to the API, MCP, the UI and the expression language. Add a reference test.
- New MCP tool: `@tool()` in `mcp_server.py`; give it a description that tells the agent *when* to use it,
  return plain dicts (they are serialised compactly), raise `ValueError` for user-fixable input problems.
- Service functions raise `ValueError` (bad input → 400 / tool error), `KeyError` (not found → 404),
  `ProviderError` (upstream failure → 502).
- Config via `.env` at the repo root (see `.env.example`). Never commit `.env`.
