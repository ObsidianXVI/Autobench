# Autobench

Stock charting + research workbench for US equities. A FastAPI backend serves market data, technicals,
fundamentals, filings and quant tools to a web UI (REST + WebSocket) and to AI agents (MCP at `/mcp`).
Agents can draw on the user's live chart; drawings, analyses, notes and watchlists persist in SQLite.

## Using it as the research agent
- Start the server: `cd server && .venv/bin/autobench serve` (http://127.0.0.1:8000, MCP at `/mcp`).
  `.mcp.json` registers it for Claude Code as `autobench`.
- Skills in `.claude/skills/` define the research method: start with `stock-research`; it delegates to
  `technical-analysis`, `quant-validation`, `fundamental-analysis`, `trade-planning`, `trade-execution`, `chart-annotation`,
  `earnings-preview`, `market-regime`, `watchlist-screening`, `recommendation-review`, `quant-research` (QBench).
- Rule of the house: no edge without a test (`run_signal_study` / `run_backtest`), record every call with
  `save_analysis`, keep durable findings in `save_note`.

## Layout
```
server/src/autobench/
  app.py            FastAPI app: REST /api/*, WebSocket /ws, MCP routes, static app/build
  mcp_server.py     MCP tools/prompts/resources (compact JSON results, ToolError messages)
  guides.py         agent-facing reference text (server instructions, get_guide topics)
  schemas.py        pydantic models shared by REST and MCP (annotations, analyses, view, notes)
  config.py db.py timeutil.py
  providers/        alpaca.py (bars/quotes/news/assets), yahoo.py (fallback + index symbols), fake.py (tests)
  services/         market (bar cache), indicators (registry), expressions (safe DSL), backtest,
                    levels, patterns, technicals, risk, fundamentals, options, filings (SEC), news,
                    market_context, annotations, analyses (outcome tracking), workspace (view, notes,
                    watchlists, layouts, ui-state), events (bus), universe (all-US-stock layers:
                    listings, SEC-frame fundamentals, bar matrix, live quotes), screener (fields,
                    filters, expressions, heatmap, breadth), macro (FRED/Treasury/BLS, cross-asset,
                    calendars), alerts, scheduler (background refresh + alert checks),
                    qbench/ (quant workstation: stats, strategy library + walk-forward lab, quant profile,
                    factor ranks q_*, cross-sectional backtests, pairs, allocation/hedging, TA+FA+quant confluence)
                    financier (Financier mode: serves its Flutter build at /financier/, reads its vral-altos
                    Firestore data read-only for the links — bound Portfolio, L-Fund etc., risk plans)
  providers/reference.py  Nasdaq listings/calendars, Wikipedia S&P 500, SEC frames, FRED, Treasury, BLS
server/tests/       pytest suite (fake provider, no network); live smoke tests: `pytest -m live`
app/                Dart web UI compiled to JS with dart2js (only dependency: package:web)
  lib/src/core/     pure Dart, no browser APIs: models, NY time/DST, scales/ticks, formatting, markdown
  lib/src/chart/    engine.dart (canvas chart), annotations.dart (drawings + handles), chart_panel.dart
                    (one chart), chart_page.dart (toolbars, grid, shortcuts), drawing_ops.dart (undo)
  lib/src/ui/       app.dart (shell, routing, events), store.dart (state), kit/ (Obs, overlays, icons,
                    sparklines), panels/ (dock, watchlist, details, alerts, news, agent/notes/objects),
                    pages/ (screener, heatmap, markets, calendar, qbench/ = Quant Lab, Q-Signals, Arb & Hedge,
                    Strategies; hidden unless the "Show QBench Controls" pref `qbench.on` is set), pickers.dart, api.dart
                    financier_mode.dart (Mode selector → Financier: the Flutter console in an iframe, postMessage
                    bridge: theme/navigate in, ready/route/openSymbol/openPage out; pref `mode`)
  static/           index.html, styles.css, favicon (copied into build/)
  tool/build.dart   build script; test/core_test.dart unit tests (plain `dart run`)
```

## Commands
- Install: `cd server && python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"`
- Test: `cd server && .venv/bin/python -m pytest -q` (offline) · `-m live` hits Yahoo/SEC/Alpaca
- Run with synthetic data: `AUTOBENCH_BAR_PROVIDER=fake .venv/bin/autobench serve`
- Web UI: `cd app && dart pub get && dart run tool/build.dart` (release) or `--watch` (rebuilds on change;
  the backend serves app/build from disk, so reload the page). Unit tests: `dart run test/core_test.dart`.
- UI end-to-end tests (headless Chrome, fake data, MCP as the agent): `pip install -e ".[ui-test]"`, build the
  UI, then `pytest -q tests/test_ui_e2e.py`. `window.__autobench` exposes chart hooks for these tests.
- No npm/Node anywhere: non-Python code is Dart. The web UI's only pub dependency is `package:web`.

## Conventions
- Bar frames: index = epoch-second bar start (UTC), columns o,h,l,c,v; daily+ bars stamped 00:00 UTC of the
  session date; intraday = regular session only. Prices adjusted (`adjustment=all`).
- New indicator: decorate a function with `@indicator(...)` in `services/indicators.py`; it is automatically
  available to the API, MCP, the UI and the expression language. Add a reference test.
- New MCP tool: `@tool()` in `mcp_server.py`; give it a description that tells the agent *when* to use it,
  return plain dicts (they are serialised compactly), raise `ValueError` for user-fixable input problems.
- Service functions raise `ValueError` (bad input → 400 / tool error), `KeyError` (not found → 404),
  `ProviderError` (upstream failure → 502).
- Background work runs only under `autobench serve` (AUTOBENCH_BACKGROUND=1, default); tests set it to 0.
- Universe layers persist under `<db dir>/universe/*.pkl`; `POST /api/universe/refresh?parts=` rebuilds.
- Dart/JS interop gotchas: compiled Dart lists are JS arrays and pass `is Node`/`is JSObject` checks (test
  iterables first); package:web types `MouseEvent.clientX` as int (read the raw double) and `hidden` as JSAny
  (use the `isHidden` extension); `toJS` closures must have JS-compatible signatures (no Future returns —
  dart analyze does not catch this, dart2js does); `.then(v, onError: (_) {})` on a non-nullable future
  throws at runtime – use `attempt()` from api.dart. When checking a rebuilt UI in a browser, reload with
  a query string: a hash-only navigation keeps the old bundle.
- Chart layers: per-symbol configs live in state rows `chartcfg:<SYM>` (`_default` = template), served by
  `/api/chart-config/{symbol}`; the model and pane assignment are in `app/lib/src/core/layers.dart`.
  Drawings carry `layer` (NULL = main; agent drawings default to `agent`). UI prefs/state: `/api/ui-state`
  (v3, `savedAt`), mirrored to localStorage; `store.flush()` beacons on pagehide.
- Trading: `services/trading.py` (risk checks, tickets, approvals, kill switch, fills → round trips, equity)
  over `services/broker/` (`ibkr.py`: ib_async on a private event-loop thread, every call marshalled onto it;
  `sim.py`: simulator). Order refs are `ab:<ticket>:<role>` (entry | stop_loss | take_profit | exit).
  Broker callbacks run on the broker thread: never call the broker from them (use `trading._worker`).
  Only `autobench serve` connects (`trading.enabled_here` is False in mcp-stdio). Tests use
  `AUTOBENCH_BROKER=sim` (conftest) — never point tests at a real Gateway. Trading writes require the
  `X-Autobench: 1` header and a loopback Host. The agent must never get tools to approve, change limits,
  arm live or resume.
- Financier mode: Financier (tools/Financier, Flutter) is Autobench's second mode, in the same Firebase project
  (`vral-altos`: Financier's data in `(default)`, Autobench's portfolio copy in `platform`). Build it with
  `cd ../Financier/app && flutter build web --release --dart-define-from-file=.env`; Autobench serves that build
  (`AUTOBENCH_FINANCIER_DIR`) with its base href rewritten. Its theme mirrors these CSS tokens
  (`Financier/app/lib/ui/core/theme/app_theme.dart`): change both together. Tests stub the build and never
  read the real Firestore (`AUTOBENCH_FIREBASE_PROJECT` is overridden in conftest).
- **Cloud (production):** Autobench runs on Cloud Run (`deploy/deploy.sh`, Terraform in `Vral_Altos/infra/autobench.tf`).
  State is in Firestore (`state_store.py`: SQLite is a working copy loaded at start, every commit written through;
  caches `bars`/`bar_coverage`/`kv_cache`/`assets` are not state; a lease `autobench_meta/lease` stops a superseded
  revision writing during rollouts). Unit tests run without a state project or lease. IB
  Gateway runs in the container on demand (`services/broker/gateway.py`, IBC + Xvfb); Claude Code is in the
  image, signed in with the subscription token from `claude-tokens` (`sk-ant-oat…`, auth method `oauth_token`; API
  keys are refused). IAP admits only the owner and `iap.py` verifies its header; the in-container MCP uses a
  per-process token. Credentials: `tool/set_autobench_credentials.sh` → Secret Manager `autobench-credentials`.
  One-off state copy: `AUTOBENCH_STATE_PROJECT=vral-altos autobench state-upload --from <db> --yes`.
- Config via `.env` at the repo root (see `.env.example`). Never commit `.env`.
