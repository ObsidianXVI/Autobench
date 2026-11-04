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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import httpx

from ..config import get_settings

log = logging.getLogger(__name__)

TOKEN_URL = "https://oauth2.googleapis.com/token"
FIRESTORE = "https://firestore.googleapis.com/v1"

_lock = threading.Lock()
_state: dict[str, Any] = {
    "last_push": 0.0, "last_connected": False, "last_hash": None, "last_error": None, "last_source": None,
    "token": None, "token_expiry": 0.0,
}


# ------------------------------------------------------------------------------ Firestore encoding

def encode(value: Any) -> dict:
    """A JSON-like value as a Firestore REST `Value`."""
    if value is None:
        return {"nullValue": None}
    if isinstance(value, bool):
        return {"booleanValue": value}
    if isinstance(value, int):
        return {"integerValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    if isinstance(value, str):
        return {"stringValue": value}
    if isinstance(value, datetime):
        return {"timestampValue": value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")}
    if isinstance(value, dict):
        return {"mapValue": {"fields": {str(k): encode(v) for k, v in value.items()}}}
    if isinstance(value, (list, tuple)):
        return {"arrayValue": {"values": [encode(v) for v in value]}}
    return {"stringValue": str(value)}


def document(fields: dict) -> dict:
    return {"fields": {k: encode(v) for k, v in fields.items()}}


# ------------------------------------------------------------------------------ credentials

def _adc_path() -> Path:
    env = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if env:
        return Path(env)
    return Path.home() / ".config" / "gcloud" / "application_default_credentials.json"


def access_token(client: httpx.Client) -> str:
    """OAuth access token from the application-default user login, cached until expiry."""
    if _state["token"] and time.time() < _state["token_expiry"] - 60:
        return _state["token"]
    path = _adc_path()
    if not path.exists():
        raise RuntimeError("No Google application-default login; run `gcloud auth application-default login`.")
    creds = json.loads(path.read_text())
    if creds.get("type") != "authorized_user":
        raise RuntimeError(f"Unsupported credential type {creds.get('type')!r}; expected a user login.")
    r = client.post(TOKEN_URL, data={
        "grant_type": "refresh_token", "refresh_token": creds["refresh_token"],
        "client_id": creds["client_id"], "client_secret": creds["client_secret"],
    })
    r.raise_for_status()
    body = r.json()
    _state["token"] = body["access_token"]
    _state["token_expiry"] = time.time() + int(body.get("expires_in", 3600))
    return _state["token"]


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
    token = access_token(client)
    headers = {"Authorization": f"Bearer {token}", "x-goog-user-project": project}
    body = document({**snap, "synced_at": now})
    base = f"{FIRESTORE}/projects/{project}/databases/(default)/documents/autobench/latest"
    day = now.astimezone().strftime("%Y-%m-%d")
    for url in (base, f"{base}/days/{day}"):
        r = client.patch(url, json=body, headers=headers)
        if r.status_code >= 400:
            raise RuntimeError(f"Firestore write failed ({r.status_code}): {r.text[:300]}")


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
        with factory() as client:
            push(snap, project, client)
        _state.update(last_push=now, last_hash=fingerprint, last_error=None, last_source=snap["source"])
        log.info("cloud sync: pushed %s snapshot to %s", snap["source"], project)
        return snap["source"]
    except Exception as e:
        _state["last_error"] = str(e)
        log.warning("cloud sync failed: %s", e)
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
