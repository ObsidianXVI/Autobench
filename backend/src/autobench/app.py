"""FastAPI application: REST API for the web UI, WebSocket event stream, and the MCP endpoint
(/mcp, streamable HTTP) served from the same process so agent actions reach the UI instantly."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import db
from .config import REPO_DIR, get_settings
from .mcp_server import mcp
from .providers.base import ProviderError
from .schemas import AnalysisIn, AnnotationIn, AnnotationPatch, ChartView, NoteIn
from .services import analyses, annotations, backtest, fundamentals, market, market_context, technicals, workspace
from .services import indicators as ind
from .services import news as news_svc
from .services import options as options_svc
from .services.events import bus
from .timeutil import get_interval, resolve_range

log = logging.getLogger(__name__)


def create_app() -> FastAPI:
    settings = get_settings()
    db.init_db()
    mcp_app = mcp.streamable_http_app(streamable_http_path="/mcp", host=settings.autobench_host)

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI):
        bus.bind_loop(asyncio.get_running_loop())
        async with mcp.session_manager.run():
            yield

    app = FastAPI(title="Autobench", version="0.1.0", lifespan=lifespan)
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_methods=["*"], allow_headers=["*"])

    @app.exception_handler(ValueError)
    async def _value_error(_r: Request, e: ValueError):
        return JSONResponse({"detail": str(e)}, status_code=400)

    @app.exception_handler(KeyError)
    async def _key_error(_r: Request, e: KeyError):
        return JSONResponse({"detail": str(e).strip("'\"")}, status_code=404)

    @app.exception_handler(ProviderError)
    async def _provider_error(_r: Request, e: ProviderError):
        return JSONResponse({"detail": str(e)}, status_code=502)

    # --- meta -----------------------------------------------------------------------------

    @app.get("/api/health")
    def health():
        return {"ok": True, **market.provider_status(), "alpaca_configured": settings.alpaca_enabled}

    # --- market data ----------------------------------------------------------------------

    @app.get("/api/symbols/search")
    def search(q: str, limit: int = Query(10, ge=1, le=50)):
        return market.search_symbols(q, limit)

    @app.get("/api/quote/{symbol}")
    def quote(symbol: str):
        return market.get_quote(symbol)

    @app.get("/api/bars/{symbol}")
    def bars(symbol: str, interval: str = "1d", lookback: str | None = None, start: str | None = None, end: str | None = None):
        b = market.get_bars(symbol, interval, start=start, end=end, lookback=lookback)
        return {"symbol": b.symbol, "interval": b.interval.name, "source": b.source, "bars": market.bars_to_records(b.df)}

    @app.get("/api/indicators")
    def indicator_list():
        return ind.list_indicators()

    class IndicatorItem(BaseModel):
        name: str
        params: dict[str, Any] = Field(default_factory=dict)

    class IndicatorQuery(BaseModel):
        interval: str = "1d"
        lookback: str | None = None
        start: str | None = None
        end: str | None = None
        indicators: list[IndicatorItem]

    @app.post("/api/indicators/{symbol}")
    def compute_indicators(symbol: str, q: IndicatorQuery):
        """Indicator series aligned to the bars of the requested range (warm-up is fetched separately)."""
        iv = get_interval(q.interval)
        start_ts, end_ts = resolve_range(q.start, q.end, q.lookback, "1y" if not iv.intraday else "5d")
        specs = [(ind.get_spec(i.name), i.params) for i in q.indicators]
        resolved = [(s, ind.resolve_params(s, p)) for s, p in specs]
        warm = max([s.warmup(p) for s, p in resolved] + [0])
        extra = warm * iv.seconds * (1.6 if iv.name == "1d" else 1.0) if not iv.intraday else warm * iv.seconds * 4
        b = market.get_bars(symbol, iv.name, start=int(start_ts - extra), end=end_ts)
        df = b.df
        mask = df.index >= start_ts
        out = []
        for spec, p in resolved:
            res = ind.compute(df, spec.name, p)[mask]
            out.append({"name": spec.name, "label": ind.label(spec, p), "params": p, "overlay": spec.overlay,
                        "outputs": spec.outputs, "levels": spec.levels,
                        "values": {c: [None if v != v else float(v) for v in res[c]] for c in res.columns}})
        return {"symbol": b.symbol, "interval": iv.name, "time": [int(t) for t in df.index[mask]], "results": out}

    @app.get("/api/summary/{symbol}")
    def summary(symbol: str, interval: str = "1d"):
        return technicals.technical_summary(symbol, interval)

    @app.get("/api/multi-timeframe/{symbol}")
    def mtf(symbol: str):
        return technicals.multi_timeframe(symbol)

    @app.get("/api/fundamentals/{symbol}")
    def fund(symbol: str):
        return fundamentals.get_fundamentals(symbol)

    @app.get("/api/statements/{symbol}")
    def statements(symbol: str, statement: str = "income", period: str = "quarterly"):
        return fundamentals.get_financial_statements(symbol, statement, period)

    @app.get("/api/earnings/{symbol}")
    def earnings(symbol: str):
        return fundamentals.get_earnings(symbol)

    @app.get("/api/analysts/{symbol}")
    def analysts(symbol: str):
        return fundamentals.get_analyst_ratings(symbol)

    @app.get("/api/options/{symbol}")
    def options(symbol: str):
        return options_svc.options_overview(symbol)

    @app.get("/api/news")
    def news(symbol: str | None = None, limit: int = Query(15, ge=1, le=50), days: int = 14):
        return news_svc.get_news(symbol, limit, days)

    @app.get("/api/market/overview")
    def overview():
        return market_context.market_overview()

    # --- quant ----------------------------------------------------------------------------

    class BacktestBody(BaseModel):
        symbol: str
        entry: str
        exit: str | None = None
        interval: str = "1d"
        lookback: str = "5y"
        side: str = "long"
        stop_loss_pct: float | None = None
        take_profit_pct: float | None = None
        stop_atr: float | None = None
        target_atr: float | None = None
        max_hold_bars: int | None = None

    @app.post("/api/backtest")
    def run_bt(b: BacktestBody):
        return backtest.run_backtest(**b.model_dump())

    class StudyBody(BaseModel):
        symbol: str
        condition: str
        interval: str = "1d"
        lookback: str = "10y"
        horizons: list[int] = [5, 10, 20]

    @app.post("/api/signal-study")
    def run_study(b: StudyBody):
        return backtest.signal_study(b.symbol, b.condition, b.interval, b.lookback, b.horizons)

    # --- annotations ----------------------------------------------------------------------

    @app.get("/api/annotations")
    def ann_list(symbol: str, interval: str | None = None):
        return annotations.list_(symbol, interval)

    @app.post("/api/annotations/{symbol}")
    def ann_create(symbol: str, item: AnnotationIn):
        return annotations.create(symbol, [item], author="user")[0]

    @app.patch("/api/annotations/{annotation_id}")
    def ann_update(annotation_id: str, patch: AnnotationPatch):
        return annotations.update(annotation_id, patch)

    @app.delete("/api/annotations/{annotation_id}")
    def ann_delete(annotation_id: str):
        res = annotations.delete_([annotation_id])
        if not res["deleted"]:
            raise HTTPException(404, f"Annotation {annotation_id} not found")
        return res

    @app.delete("/api/annotations")
    def ann_delete_filtered(symbol: str, tag: str | None = None, author: str | None = None):
        return annotations.delete_(symbol=symbol, tag=tag, author=author)

    # --- analyses / notes / watchlists / view ---------------------------------------------

    @app.get("/api/analyses")
    def an_list(symbol: str | None = None, limit: int = 50):
        return analyses.list_(symbol, limit)

    @app.post("/api/analyses")
    def an_create(a: AnalysisIn):
        return analyses.save(a, author="user")

    @app.get("/api/analyses/{analysis_id}")
    def an_get(analysis_id: str):
        a = analyses.get(analysis_id)
        a["evaluation"] = analyses.evaluate(analysis_id)
        return a

    @app.get("/api/track-record")
    def track(symbol: str | None = None):
        return analyses.track_record(symbol)

    @app.get("/api/notes")
    def notes(symbol: str | None = None, q: str | None = None, limit: int = 50):
        return workspace.list_notes(symbol, query=q, limit=limit)

    @app.post("/api/notes")
    def note_create(n: NoteIn):
        return workspace.save_note(n, author="user")

    @app.put("/api/notes/{note_id}")
    def note_update(note_id: str, n: NoteIn):
        return workspace.save_note(n, note_id, author="user")

    @app.delete("/api/notes/{note_id}")
    def note_delete(note_id: str):
        return workspace.delete_note(note_id)

    @app.get("/api/watchlists")
    def wl_list():
        return workspace.list_watchlists()

    class WatchlistPatch(BaseModel):
        add: list[str] | None = None
        remove: list[str] | None = None
        replace: list[str] | None = None

    @app.put("/api/watchlists/{name}")
    def wl_update(name: str, p: WatchlistPatch):
        return workspace.update_watchlist(name, p.add, p.remove, p.replace)

    @app.delete("/api/watchlists/{name}")
    def wl_delete(name: str):
        return workspace.delete_watchlist(name)

    @app.get("/api/view")
    def view_get():
        return workspace.get_view()

    @app.put("/api/view")
    def view_set(v: ChartView):
        return workspace.set_view(v, author="user")

    @app.post("/api/events/relay")
    def relay(request: Request, msg: dict = Body(...)):
        """Re-broadcast events from a separate (stdio) MCP process. Local clients only."""
        if request.client and request.client.host not in ("127.0.0.1", "::1", "localhost", "testclient"):
            raise HTTPException(403, "relay is local-only")
        if not isinstance(msg.get("type"), str):
            raise HTTPException(400, "type required")
        bus.publish(msg["type"], msg.get("data"))
        return {"ok": True}

    @app.websocket("/ws")
    async def ws(socket: WebSocket):
        await socket.accept()
        q = bus.subscribe()
        try:
            await socket.send_json({"type": "hello", "data": {"view": workspace.get_view()}})
            while True:
                msg = await q.get()
                await socket.send_json(msg)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            bus.unsubscribe(q)

    # MCP streamable-HTTP endpoint at /mcp
    app.router.routes.extend(mcp_app.routes)

    dist = REPO_DIR / "web" / "dist"
    if dist.is_dir():
        app.mount("/", StaticFiles(directory=dist, html=True), name="web")
    return app
