"""SQLite persistence: bar cache, JSON cache, annotations, analyses, notes, watchlists, app state."""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any, Iterator

from sqlalchemy import (
    JSON,
    Boolean,
    Float,
    Integer,
    String,
    Text,
    create_engine,
    event,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from .config import get_settings


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class BarRow(Base):
    __tablename__ = "bars"
    symbol: Mapped[str] = mapped_column(String, primary_key=True)
    interval: Mapped[str] = mapped_column(String, primary_key=True)
    t: Mapped[int] = mapped_column(Integer, primary_key=True)  # epoch seconds, UTC
    o: Mapped[float] = mapped_column(Float)
    h: Mapped[float] = mapped_column(Float)
    l: Mapped[float] = mapped_column(Float)  # noqa: E741
    c: Mapped[float] = mapped_column(Float)
    v: Mapped[float] = mapped_column(Float)


class BarCoverage(Base):
    """Which [start, end] range of bars is cached for a symbol/interval, and when it was fetched."""

    __tablename__ = "bar_coverage"
    symbol: Mapped[str] = mapped_column(String, primary_key=True)
    interval: Mapped[str] = mapped_column(String, primary_key=True)
    start: Mapped[int] = mapped_column(Integer)
    end: Mapped[int] = mapped_column(Integer)
    fetched_at: Mapped[int] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(String)


class CacheRow(Base):
    __tablename__ = "kv_cache"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[int] = mapped_column(Integer)


class AnnotationRow(Base):
    __tablename__ = "annotations"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    symbol: Mapped[str] = mapped_column(String, index=True)
    interval: Mapped[str | None] = mapped_column(String, nullable=True)
    type: Mapped[str] = mapped_column(String)
    points: Mapped[list] = mapped_column(JSON)
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    style: Mapped[dict] = mapped_column(JSON)
    author: Mapped[str] = mapped_column(String)
    tag: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    layer: Mapped[str | None] = mapped_column(String, nullable=True)  # chart layer id (None = default layer)
    visible: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow)


class AnalysisRow(Base):
    __tablename__ = "analyses"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    symbol: Mapped[str] = mapped_column(String, index=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    action: Mapped[str] = mapped_column(String)
    horizon: Mapped[str] = mapped_column(String)
    confidence: Mapped[float] = mapped_column(Float)
    price_at_creation: Mapped[float | None] = mapped_column(Float, nullable=True)
    entry_low: Mapped[float | None] = mapped_column(Float, nullable=True)
    entry_high: Mapped[float | None] = mapped_column(Float, nullable=True)
    stop: Mapped[float | None] = mapped_column(Float, nullable=True)
    targets: Mapped[list] = mapped_column(JSON, default=list)
    summary: Mapped[str] = mapped_column(Text)
    rationale: Mapped[str] = mapped_column(Text)
    scores: Mapped[dict] = mapped_column(JSON, default=dict)
    risks: Mapped[list] = mapped_column(JSON, default=list)
    catalysts: Mapped[list] = mapped_column(JSON, default=list)
    invalidation: Mapped[str | None] = mapped_column(Text, nullable=True)
    annotation_tag: Mapped[str | None] = mapped_column(String, nullable=True)
    author: Mapped[str] = mapped_column(String, default="agent")


class NoteRow(Base):
    __tablename__ = "notes"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    symbol: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    title: Mapped[str] = mapped_column(String)
    content: Mapped[str] = mapped_column(Text)
    tags: Mapped[list] = mapped_column(JSON, default=list)
    author: Mapped[str] = mapped_column(String, default="agent")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow)


class WatchlistRow(Base):
    __tablename__ = "watchlists"
    name: Mapped[str] = mapped_column(String, primary_key=True)
    symbols: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow)


class AssetRow(Base):
    """Symbol directory (from Alpaca's asset list) used for local symbol search."""

    __tablename__ = "assets"
    symbol: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String)
    exchange: Mapped[str] = mapped_column(String)
    asset_class: Mapped[str] = mapped_column(String)
    tradable: Mapped[bool] = mapped_column(Boolean)


class AlertRow(Base):
    """A price / indicator alert, evaluated by the background scheduler."""

    __tablename__ = "alerts"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    symbol: Mapped[str] = mapped_column(String, index=True)
    kind: Mapped[str] = mapped_column(String)  # above | below | crosses | change_above | change_below | expression
    value: Mapped[float | None] = mapped_column(Float, nullable=True)
    expression: Mapped[str | None] = mapped_column(Text, nullable=True)
    interval: Mapped[str] = mapped_column(String, default="1d")
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String, default="active", index=True)  # active | triggered | paused
    repeat: Mapped[bool] = mapped_column(Boolean, default=False)
    annotation_id: Mapped[str | None] = mapped_column(String, nullable=True)
    author: Mapped[str] = mapped_column(String, default="user")
    last_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_checked: Mapped[datetime | None] = mapped_column(nullable=True)
    triggered_at: Mapped[datetime | None] = mapped_column(nullable=True)
    trigger_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    trigger_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class AlertEventRow(Base):
    """One firing of an alert, kept for the inbox (survives the alert being edited or deleted)."""

    __tablename__ = "alert_events"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    alert_id: Mapped[str] = mapped_column(String, index=True)
    symbol: Mapped[str] = mapped_column(String)
    description: Mapped[str] = mapped_column(Text)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    author: Mapped[str] = mapped_column(String, default="user")
    read: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    at: Mapped[datetime] = mapped_column(default=utcnow, index=True)


class OrderTicketRow(Base):
    """An order request: proposed by the agent (awaiting approval) or entered by the user, and the
    broker orders it became (entry plus optional stop-loss / take-profit children)."""

    __tablename__ = "order_tickets"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    symbol: Mapped[str] = mapped_column(String, index=True)
    spec: Mapped[dict] = mapped_column(JSON)  # OrderSpec as submitted
    source: Mapped[str] = mapped_column(String)  # user | agent
    status: Mapped[str] = mapped_column(String, index=True)  # pending|rejected|expired|submitted|working|filled|cancelled|error
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    analysis_id: Mapped[str | None] = mapped_column(String, nullable=True)
    check: Mapped[dict] = mapped_column(JSON, default=dict)  # risk check at proposal/submission time
    order_ids: Mapped[list] = mapped_column(JSON, default=list)  # broker order ids
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    broker: Mapped[str | None] = mapped_column(String, nullable=True)
    account: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow)
    decided_at: Mapped[datetime | None] = mapped_column(nullable=True)


class FillRow(Base):
    """An execution reported by the broker (kept locally so trade history outlives the broker's window)."""

    __tablename__ = "fills"
    exec_id: Mapped[str] = mapped_column(String, primary_key=True)
    account: Mapped[str] = mapped_column(String, index=True)
    broker: Mapped[str] = mapped_column(String)
    order_id: Mapped[str | None] = mapped_column(String, nullable=True)
    ref: Mapped[str | None] = mapped_column(String, nullable=True)  # our order ref (ticket:role)
    symbol: Mapped[str] = mapped_column(String, index=True)
    side: Mapped[str] = mapped_column(String)  # buy | sell
    qty: Mapped[float] = mapped_column(Float)
    price: Mapped[float] = mapped_column(Float)
    time: Mapped[int] = mapped_column(Integer, index=True)  # epoch seconds
    commission: Mapped[float | None] = mapped_column(Float, nullable=True)
    realized_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)


class EquityRow(Base):
    """Periodic account snapshots for the equity curve."""

    __tablename__ = "equity"
    account: Mapped[str] = mapped_column(String, primary_key=True)
    t: Mapped[int] = mapped_column(Integer, primary_key=True)
    net_liquidation: Mapped[float] = mapped_column(Float)
    cash: Mapped[float] = mapped_column(Float)
    gross_position_value: Mapped[float] = mapped_column(Float)
    daily_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    currency: Mapped[str | None] = mapped_column(String, nullable=True)


class TradingAuditRow(Base):
    """Every trading decision and action (who, what, outcome)."""

    __tablename__ = "trading_audit"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    time: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    actor: Mapped[str] = mapped_column(String)  # user | agent | system
    action: Mapped[str] = mapped_column(String)
    symbol: Mapped[str | None] = mapped_column(String, nullable=True)
    ticket_id: Mapped[str | None] = mapped_column(String, nullable=True)
    details: Mapped[dict] = mapped_column(JSON, default=dict)


class StateRow(Base):
    __tablename__ = "app_state"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[dict] = mapped_column(JSON)


_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None
_lock = threading.Lock()


def _make_engine(url: str) -> Engine:
    engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 30})

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):  # pragma: no cover - trivial
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()

    return engine


def init_db(url: str | None = None) -> Engine:
    """Create (or re-create, when a url is given, e.g. in tests) the engine and tables."""
    global _engine, _session_factory
    with _lock:
        if url is None:
            path = get_settings().db_path
            path.parent.mkdir(parents=True, exist_ok=True)
            url = f"sqlite:///{path}"
        if _engine is not None:
            _engine.dispose()
        _engine = _make_engine(url)
        Base.metadata.create_all(_engine)
        _migrate(_engine)
        _session_factory = sessionmaker(_engine, expire_on_commit=False)
        return _engine


def _migrate(engine: Engine) -> None:
    """Add columns introduced after a table was first created (create_all never alters tables).
    Only additive, nullable columns, so existing data stays valid."""
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if not insp.has_table(table.name):
                continue
            existing = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name not in existing and col.nullable:
                    conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {col.type.compile(engine.dialect)}'))


@contextmanager
def session_scope() -> Iterator[Session]:
    if _session_factory is None:
        init_db()
    assert _session_factory is not None
    session = _session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# --- small JSON cache with TTL -------------------------------------------------------------


def cache_get(key: str) -> Any | None:
    now = int(utcnow().timestamp())
    with session_scope() as s:
        row = s.get(CacheRow, key)
        if row is None or row.expires_at < now:
            return None
        return json.loads(row.value)


def cache_set(key: str, value: Any, ttl_seconds: int) -> None:
    expires = int(utcnow().timestamp()) + ttl_seconds
    payload = json.dumps(value, default=str)
    with session_scope() as s:
        s.merge(CacheRow(key=key, value=payload, expires_at=expires))


def cached(key: str, ttl_seconds: int, fn):
    """Return the cached value for key, computing and storing it with fn() on a miss."""
    hit = cache_get(key)
    if hit is not None:
        return hit
    value = fn()
    if value is not None:
        cache_set(key, value, ttl_seconds)
    return value


def state_get(key: str, default: dict | None = None) -> dict | None:
    with session_scope() as s:
        row = s.get(StateRow, key)
        return dict(row.value) if row else default


def state_set(key: str, value: dict) -> None:
    with session_scope() as s:
        s.merge(StateRow(key=key, value=value))
