"""Copies the portfolio to the Vral Altos cloud store, so agents running in the cloud
have recent account data while this Mac (and IB Gateway) is off.

- IB Gateway connected: the live portfolio is pushed every `autobench_cloud_sync_interval`
  seconds (and at once when the connection comes up). `source` = "live".
- Not connected: the latest imported IBKR statement is pushed, but only when it changed.
  `source` = "statement".

Target: Firestore in `autobench_cloud_sync_project` (off when empty), documents
`autobench/latest` and `autobench/latest/days/<YYYY-MM-DD>` (last snapshot of each day).
Credentials: the operator's Google application-default login
(`gcloud auth application-default login`). The data is private to that project; nothing
is public.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import httpx

from ..config import get_settings

log = logging.getLogger(__name__)

from .gcp import TOKEN_URL, access_token, document, encode  # noqa: F401  (one mechanism: services/gcp.py)
from .gcp import headers as gcp_headers

FIRESTORE = "https://firestore.googleapis.com/v1"

_lock = threading.Lock()
_state: dict[str, Any] = {
    "last_push": 0.0, "last_connected": False, "last_hash": None, "last_error": None, "last_source": None,
}


# ------------------------------------------------------------------------------ snapshot

def build_snapshot() -> dict:
    """What the cloud copy holds: account state, risk limits, trading record."""
    from . import portfolio
    from .trading import trading

    status = trading.status()
    connected = bool(status.get("connected"))
    ov = portfolio.overview()
    trades = trading.trades(limit=500)
    snap: dict[str, Any] = {
        "source": "live" if connected and ov.get("live") else "statement",
        "account": ov.get("account"),
        "broker": {"connected": connected, "paper": status.get("paper"), "mode": status.get("mode"),
                   "halted": status.get("halted")},
        "live": ov.get("live"),
        "statement_positions": ov.get("statement_positions"),
        "nav": ov.get("nav"),
        "lifetime": ov.get("lifetime"),
        "totals": ov.get("totals"),
        "limits": trading.settings().model_dump(),
        "trade_stats": trades.get("stats"),
        "open_trades": [
            {k: t.get(k) for k in ("symbol", "side", "qty", "entry_avg", "opened_at", "planned_stop",
                                   "unrealized_pnl", "market_price")}
            for t in trades.get("trades", []) if t.get("status") == "open"
        ],
    }
    if connected:
        live = trading.overview()
        snap["orders"] = live.get("orders")
        snap["capital"] = live.get("capital")
    return snap


def _fingerprint(snap: dict) -> str:
    return hashlib.sha256(json.dumps(snap, sort_keys=True, default=str).encode()).hexdigest()


# ------------------------------------------------------------------------------ push

def push(snap: dict, project: str, client: httpx.Client, now: datetime | None = None) -> None:
    now = now or datetime.now(timezone.utc)
    headers = gcp_headers(client, project)
    body = document({**snap, "synced_at": now})
    # The Vral Altos platform database ((default) is Financier's).
    base = f"{FIRESTORE}/projects/{project}/databases/platform/documents/autobench/latest"
    day = now.astimezone().strftime("%Y-%m-%d")
    for url in (base, f"{base}/days/{day}"):
        r = client.patch(url, json=body, headers=headers)
        if r.status_code >= 400:
            raise RuntimeError(f"Firestore write failed ({r.status_code}): {r.text[:300]}")


# ------------------------------------------------------------------------------ telemetry

def telemetry(project: str, client: httpx.Client, severity: str, message: str,
              fields: dict | None = None, counts: dict | None = None,
              now: datetime | None = None) -> None:
    """One log entry plus metric counters in the Vral Altos platform database, in the
    shared telemetry shape (meshes/service_mesh `Telemetry`): `logs/{id}` and
    `metrics/autobench~<yyyyMMddHH>`. Best effort: never raises."""
    try:
        now = now or datetime.now(timezone.utc)
        root = f"projects/{project}/databases/platform/documents"
        headers = gcp_headers(client, project)
        hour = now.strftime("%Y%m%d%H")
        minute = f"m{now.minute:02d}"
        start = now.replace(minute=0, second=0, microsecond=0)
        counts = {**(counts or {}), f"log_{severity}": 1}
        writes = [
            {"update": {"name": f"{root}/logs/{os.urandom(10).hex()}", **document({
                "at": now, "source": "autobench", "host": os.uname().nodename, "component": "cloud_sync",
                "severity": severity, "message": message, "fields": fields or {},
                "expireAt": now + timedelta(days=14)})}},
            {"update": {"name": f"{root}/metrics/autobench~{hour}", **document({
                "source": "autobench", "hour": start, "expireAt": start + timedelta(days=30)})},
             "updateMask": {"fieldPaths": ["source", "hour", "expireAt"]},
             "updateTransforms": [t for k, v in counts.items() for t in (
                 {"fieldPath": f"h.{k}", "increment": encode(v)},
                 {"fieldPath": f"m.{minute}.{k}", "increment": encode(v)})]},
        ]
        r = client.post(f"{FIRESTORE}/{root}:commit", json={"writes": writes},
                        headers=headers)
        if r.status_code >= 400:
            log.warning("telemetry not written (%s): %s", r.status_code, r.text[:200])
    except Exception as e:  # telemetry never breaks the sync
        log.warning("telemetry not written: %s", e)


def maybe_sync(client_factory: Callable[[], httpx.Client] | None = None,
               snapshot: Callable[[], dict] = build_snapshot) -> str | None:
    """Push if due. Returns the source pushed ("live" / "statement"), or None if nothing was sent."""
    settings = get_settings()
    project = settings.autobench_cloud_sync_project
    if not project:
        return None
    if not _lock.acquire(blocking=False):
        return None  # a push is already running
    try:
        snap = snapshot()
        connected = snap["source"] == "live"
        now = time.time()
        due = (connected and (not _state["last_connected"]
                              or now - _state["last_push"] >= settings.autobench_cloud_sync_interval))
        fingerprint = _fingerprint({k: v for k, v in snap.items() if k != "live"}) if not connected else None
        if not connected and fingerprint != _state["last_hash"]:
            due = True
        _state["last_connected"] = connected
        if not due:
            return None
        factory = client_factory or (lambda: httpx.Client(timeout=30))
        started = time.time()
        with factory() as client:
            push(snap, project, client)
            telemetry(project, client, "info", f"Pushed the {snap['source']} portfolio snapshot",
                      {"source": snap["source"], "durationMs": int((time.time() - started) * 1000)},
                      {"syncs": 1})
        _state.update(last_push=now, last_hash=fingerprint, last_error=None, last_source=snap["source"])
        log.info("cloud sync: pushed %s snapshot to %s", snap["source"], project)
        return snap["source"]
    except Exception as e:
        _state["last_error"] = str(e)
        log.warning("cloud sync failed: %s", e)
        try:
            with (client_factory or (lambda: httpx.Client(timeout=30)))() as client:
                telemetry(project, client, "error", "Portfolio sync failed", {"error": str(e)[:500]}, {"sync_failed": 1})
        except Exception:
            pass
        return None
    finally:
        _lock.release()


def status() -> dict:
    settings = get_settings()
    return {
        "project": settings.autobench_cloud_sync_project or None,
        "interval_seconds": settings.autobench_cloud_sync_interval,
        "last_push": datetime.fromtimestamp(_state["last_push"], timezone.utc).isoformat()
        if _state["last_push"] else None,
        "last_source": _state["last_source"],
        "last_error": _state["last_error"],
    }
