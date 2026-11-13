"""Autobench's state lives in Firestore (Vral Altos: all state outside the container, in Firestore and
GCS). The `platform` database of the configured project holds one collection per table,
`autobench_<table>`, one document per row.

Inside the process the services keep using SQLAlchemy: the SQLite database is a working copy that is
loaded from Firestore when the process starts, and every committed change to a mirrored table is written
through to Firestore (ORM inserts/updates/deletes after each flush, plus bulk UPDATE/DELETE statements).
Autobench runs as a single instance (Cloud Run max 1). During a deploy Cloud Run briefly runs the old and the
new revision side by side, so writers hold a lease (`autobench_meta/lease`): a starting instance claims it and
waits two check intervals before loading; an instance that finds the lease taken stops writing (every commit
raises Superseded) and stops its IB Gateway, so nothing it does after the new copy was loaded is lost silently.

Tables that are caches of upstream data (bars, bar_coverage, kv_cache, assets) are not state and are not
mirrored; they refill on demand. With no state project configured (unit tests), nothing is mirrored.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

import httpx
from sqlalchemy import JSON, DateTime, Table, delete, event, insert, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from .services import gcp

log = logging.getLogger(__name__)

CACHE_TABLES = {"bars", "bar_coverage", "kv_cache", "assets"}
PREFIX = "autobench_"
LEASE = "autobench_meta/lease"


class Superseded(RuntimeError):
    """A newer Autobench instance holds the state lease; this one must not write."""


def _refuse(m: "Mirror") -> None:
    if m.superseded:  # raised inside the flush, so the transaction rolls back
        raise Superseded("Autobench restarted in a newer instance: reload the page and try again")


def collection(table: str) -> str:
    return PREFIX + table


def doc_id(table: Table, row: dict) -> str:
    raw = "~".join(str(row[c.name]) for c in table.primary_key.columns)
    safe = raw.replace("%", "%25").replace("/", "%2F")
    return "k" + safe if safe.startswith("__") or safe in (".", "..") else safe


def to_fields(table: Table, row: dict) -> dict:
    out: dict[str, Any] = {}
    for c in table.columns:
        v = row.get(c.name)
        if isinstance(c.type, JSON):
            v = None if v is None else json.dumps(v, default=str)
        elif isinstance(v, datetime) and v.tzinfo is None:
            v = v.replace(tzinfo=timezone.utc)
        out[c.name] = v
    return out


def from_fields(table: Table, fields: dict) -> dict:
    row: dict[str, Any] = {}
    for c in table.columns:
        if c.name not in fields:
            continue
        v = gcp.decode(fields[c.name])
        if v is not None:
            if isinstance(c.type, JSON):
                v = json.loads(v) if isinstance(v, str) else v
            elif _is_datetime(c) and isinstance(v, str):
                v = datetime.fromisoformat(v.replace("Z", "+00:00")).astimezone(timezone.utc).replace(tzinfo=None)
        row[c.name] = v
    return row


def _is_datetime(c) -> bool:
    try:
        return isinstance(c.type, DateTime) or c.type.python_type is datetime
    except NotImplementedError:
        return False


class Mirror:
    """Write-through of mirrored tables to Firestore."""

    def __init__(self, project: str, database: str, tables: dict[str, Table],
                 client_factory: Callable[[], httpx.Client] | None = None):
        self.project, self.database = project, database
        self.tables = tables  # name -> Table, mirrored ones only
        self._client_factory = client_factory or (lambda: client_factory_default())
        self._lock = threading.Lock()
        self.backlog: list[dict] = []
        self.last_error: str | None = None
        self.writes = 0
        self.instance = uuid.uuid4().hex
        self.superseded = False
        self._watcher: threading.Thread | None = None

    # --- single-writer lease -------------------------------------------------------------------

    def claim(self, wait: float) -> None:
        """Take the lease (an older instance sees it within one check interval), then give it time to stop."""
        name = gcp.doc_name(self.project, self.database, LEASE)
        with self._client_factory() as client:
            gcp.commit(client, self.project, self.database, [{"update": {"name": name, **gcp.document(
                {"instance": self.instance, "at": datetime.now(timezone.utc)})}}])
        if wait > 0:
            time.sleep(wait)

    def check_lease(self) -> bool:
        """True while this instance holds the lease; the first time it does not, stop writing."""
        if self.superseded:
            return False
        try:
            with self._client_factory() as client:
                fields = gcp.get_document(client, self.project, self.database, LEASE) or {}
        except (gcp.GcpError, httpx.HTTPError) as e:
            log.warning("state lease check failed: %s", e)
            return True
        holder = gcp.decode(fields["instance"]) if "instance" in fields else None
        if holder is not None and holder != self.instance:
            self.superseded = True
            log.error("a newer Autobench instance took over the state (lease %s); this one stops writing", holder)
            try:
                from .services.broker.gateway import gateway
                gateway.stop("A newer Autobench instance took over")
            except Exception:  # noqa: BLE001 — stopping is best effort; the instance is going away
                pass
        return not self.superseded

    def watch_lease(self, interval: float) -> None:
        def loop() -> None:
            while self.check_lease():
                time.sleep(interval)
        self._watcher = threading.Thread(target=loop, name="state-lease", daemon=True)
        self._watcher.start()

    # --- startup -------------------------------------------------------------------------------

    def load(self, engine: Engine) -> dict[str, int]:
        """Replace the working copy of every mirrored table with what Firestore holds."""
        counts = {}
        with self._client_factory() as client, engine.begin() as conn:
            for name, table in self.tables.items():
                docs = gcp.list_documents(client, self.project, self.database, collection(name))
                conn.execute(delete(table))
                rows = [from_fields(table, d["fields"]) for d in docs]
                for i in range(0, len(rows), 500):
                    conn.execute(insert(table), rows[i:i + 500])
                counts[name] = len(rows)
        return counts

    def upload_all(self, engine: Engine) -> dict[str, int]:
        """Copy every mirrored row of a local database to Firestore (one-off migration)."""
        counts, writes = {}, []
        with engine.connect() as conn:
            for name, table in self.tables.items():
                rows = [{c.name: r._mapping[c] for c in table.columns} for r in conn.execute(select(table))]
                counts[name] = len(rows)
                writes += [self._upsert(table, r) for r in rows]
        self._push(writes)
        return counts

    # --- write-through -------------------------------------------------------------------------

    def _upsert(self, table: Table, row: dict) -> dict:
        name = gcp.doc_name(self.project, self.database, f"{collection(table.name)}/{doc_id(table, row)}")
        return {"update": {"name": name, **gcp.document(to_fields(table, row))}}

    def _delete(self, table: Table, row: dict) -> dict:
        return {"delete": gcp.doc_name(self.project, self.database, f"{collection(table.name)}/{doc_id(table, row)}")}

    def _push(self, writes: list[dict]) -> None:
        if not writes and not self.backlog:
            return
        with self._lock:
            batch = self.backlog + writes
            self.backlog = []
            for attempt in range(3):
                try:
                    with self._client_factory() as client:
                        gcp.commit(client, self.project, self.database, batch)
                    self.writes += len(batch)
                    self.last_error = None
                    return
                except (gcp.GcpError, httpx.HTTPError) as e:
                    self.last_error = str(e)
                    time.sleep(0.3 * (attempt + 1))
            # kept, in order, for the next commit (and reported by /api/health)
            self.backlog = batch
            log.error("state write-through failed; %d writes kept for retry: %s", len(batch), self.last_error)

    def flush(self) -> None:
        self._push([])

    def status(self) -> dict:
        return {"store": "firestore", "project": self.project, "database": self.database,
                "backlog": len(self.backlog), "writes": self.writes, "last_error": self.last_error,
                "superseded": self.superseded}

    # --- SQLAlchemy hooks ----------------------------------------------------------------------



# --- SQLAlchemy hooks: registered once, acting for whichever mirror is active ---------------------

def _pending(session: Session) -> list[dict]:
    return session.info.setdefault("mirror_writes", [])


def _row_of(obj) -> dict:
    return {c.name: getattr(obj, c.key) for c in obj.__table__.columns}


@event.listens_for(Session, "after_flush")
def _after_flush(session: Session, _ctx) -> None:
    m = _active
    if m is None:
        return
    out = _pending(session)
    for obj in list(session.new) + list(session.dirty):
        t = getattr(obj, "__table__", None)
        if t is not None and t.name in m.tables:
            out.append(m._upsert(t, _row_of(obj)))
    for obj in session.deleted:
        t = getattr(obj, "__table__", None)
        if t is not None and t.name in m.tables:
            out.append(m._delete(t, _row_of(obj)))
    if out:
        _refuse(m)


@event.listens_for(Session, "do_orm_execute")
def _bulk(state) -> Any:
    m = _active
    if m is None or not (state.is_update or state.is_delete):
        return None
    table = state.statement.table
    if not isinstance(table, Table) or table.name not in m.tables:
        return None
    _refuse(m)
    pks = list(table.primary_key.columns)
    where = state.statement.whereclause
    q = select(*pks) if where is None else select(*pks).where(where)
    conn = state.session.connection()  # Core rows, not ORM entities
    keys = [tuple(r) for r in conn.execute(q)]
    result = state.invoke_statement()
    out = _pending(state.session)
    if state.is_delete:
        out += [m._delete(table, dict(zip([c.name for c in pks], k))) for k in keys]
    else:
        for k in keys:
            r = conn.execute(select(table).where(*[c == v for c, v in zip(pks, k)])).first()
            if r is not None:
                out.append(m._upsert(table, {c.name: r._mapping[c] for c in table.columns}))
    return result


@event.listens_for(Session, "after_commit")
def _after_commit(session: Session) -> None:
    writes = session.info.pop("mirror_writes", None)
    m = _active
    if m is not None and writes:
        m._push(writes)


@event.listens_for(Session, "after_rollback")
def _after_rollback(session: Session) -> None:
    session.info.pop("mirror_writes", None)


_active: Mirror | None = None


def client_factory_default() -> httpx.Client:
    """The HTTP client for Firestore (tests replace it with a fake Firestore)."""
    return httpx.Client(timeout=30)


def activate(m: Mirror | None) -> None:
    global _active
    _active = m


def active() -> Mirror | None:
    return _active
