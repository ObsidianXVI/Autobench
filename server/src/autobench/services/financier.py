"""Financier, Autobench's second mode (Mode → Financier).

Two things live here:

- **The Financier web console.** Financier is a Flutter web app (tools/Financier). Autobench serves
  its release build at `/financier/` (same origin, so the two can exchange `postMessage`s) and frames
  it when the user switches the Mode selector. `ui_dir()` finds the build; `index_html()` rewrites
  its `<base href>` so the same build also deploys to Firebase Hosting at `/`.

- **Financier's data, read-only.** Both modes use the same store: the Firebase project `vral-altos`.
  Financier's own data is its `(default)` Firestore database; Autobench's portfolio copy is in the
  `platform` database (services/cloud_sync.py). `summary()` reads the Financier side with the
  operator's Google application-default login (the same credential cloud sync uses) and returns
  what Autobench links to: the Portfolio bound to the brokerage account, the Physical and Virtual
  Accounts that hold its capital (e.g. the L-Fund backed by IBKR), and the risk plan (stop, target,
  note) Financier records for each open position. Nothing is ever written to Financier from here.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import httpx

from ..config import REPO_DIR, get_settings
from ..providers.base import ProviderError
from . import gcp
from .gcp import decode  # noqa: F401  (re-exported for callers)

log = logging.getLogger(__name__)

DEFAULT_PROJECT = "vral-altos"
CACHE_SECONDS = 120

# The collections the link needs (a subset of Financier's model; see its firestore.rules).
COLLECTIONS = ("portfolios", "physicalAccounts", "virtualAccounts", "virtualAccountBackings", "accountBalances",
               "virtualLedgerEntries", "trades", "tradeDeposits")

_lock = threading.Lock()
_cache: dict[str, Any] = {"at": 0.0, "data": None, "project": None}


# ------------------------------------------------------------------------------ web console

def ui_dir() -> Path | None:
    """The Financier web build (`flutter build web`), or None when it has not been built."""
    configured = get_settings().autobench_financier_dir.strip()
    path = Path(configured).expanduser() if configured else REPO_DIR.parent / "Financier" / "app" / "build" / "web"
    return path if (path / "index.html").is_file() else None


_BASE = re.compile(r'<base\s+href="[^"]*"\s*/?>', re.I)


def index_html(base: str = "/financier/") -> str:
    """Financier's index.html with its base href pointing at where Autobench serves it."""
    d = ui_dir()
    if d is None:
        raise KeyError("Financier is not built")
    html = (d / "index.html").read_text(encoding="utf-8")
    tag = f'<base href="{base}">'
    return _BASE.sub(tag, html, count=1) if _BASE.search(html) else html.replace("<head>", f"<head>\n  {tag}", 1)


def ui_file(path: str) -> Path | None:
    """A file of the build, refusing anything outside it."""
    d = ui_dir()
    if d is None or not path:
        return None
    root = d.resolve()
    target = (root / path).resolve()
    if root not in target.parents or not target.is_file():
        return None
    return target


def project() -> str:
    s = get_settings()
    return s.autobench_firebase_project.strip() or s.autobench_cloud_sync_project.strip() or DEFAULT_PROJECT


def status() -> dict:
    d = ui_dir()
    return {
        "ui": {"built": d is not None, "dir": str(d) if d else None,
               "build": "cd ../Financier/app && flutter build web --release --dart-define-from-file=.env"},
        "project": project(),
        "cached_at": datetime.fromtimestamp(_cache["at"], timezone.utc).isoformat() if _cache["at"] else None,
    }


# ------------------------------------------------------------------------------ Firestore REST

def _doc(raw: dict) -> dict:
    return {"id": raw["id"], **{k: decode(v) for k, v in raw["fields"].items()}}


def list_collection(client: httpx.Client, proj: str, collection: str, database: str = "(default)") -> list[dict]:
    try:
        return [_doc(d) for d in gcp.list_documents(client, proj, database, collection)]
    except gcp.GcpError as e:
        raise ProviderError(str(e)) from e


def fetch(client_factory: Callable[[], httpx.Client] | None = None) -> dict[str, list[dict]]:
    proj = project()
    with (client_factory or (lambda: httpx.Client(timeout=30)))() as client:
        try:
            gcp.headers(client, proj)  # fail early, with the sign-in message, before any read
        except (gcp.GcpError, httpx.HTTPError) as e:
            raise ProviderError(f"No Google login for Firestore: {e}") from e
        return {c: list_collection(client, proj, c) for c in COLLECTIONS}


# ------------------------------------------------------------------------------ the link

def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _r(v: float | None) -> float | None:
    return None if v is None else round(v, 2)


def build_summary(data: dict[str, list[dict]], broker_account: str | None = None) -> dict:
    """What Autobench shows from Financier, from raw collections (pure: tested without a network)."""
    physical = {d["id"]: d for d in data.get("physicalAccounts", [])}
    virtual = {d["id"]: d for d in data.get("virtualAccounts", [])}
    backings = {d["id"]: list(d.get("physicalAccountIds") or []) for d in data.get("virtualAccountBackings", [])}

    latest: dict[str, dict] = {}
    for b in data.get("accountBalances", []):
        pid = b.get("physicalAccountId")
        if pid and (pid not in latest or str(b.get("recordedAt") or "") > str(latest[pid].get("recordedAt") or "")):
            latest[pid] = b

    ledger: dict[str, float] = defaultdict(float)
    for e in data.get("virtualLedgerEntries", []):
        amount = _num(e.get("amount"))
        if e.get("virtualAccountId") and amount is not None:
            ledger[e["virtualAccountId"]] += amount

    def phys(pid: str) -> dict:
        b = latest.get(pid) or {}
        return {"id": pid, "name": (physical.get(pid) or {}).get("name", pid),
                "balance": _r(_num(b.get("amount"))), "recorded_at": b.get("recordedAt")}

    def virt(vid: str) -> dict:
        return {"id": vid, "name": (virtual.get(vid) or {}).get("name", vid),
                "balance": _r(ledger.get(vid, 0.0)) if vid in ledger else None,
                "backed_by": [(physical.get(p) or {}).get("name", p) for p in backings.get(vid, [])]}

    deposits: dict[str, float] = defaultdict(float)
    for d in data.get("tradeDeposits", []):
        amount = _num(d.get("amount"))
        if d.get("portfolioId") and amount is not None:
            deposits[d["portfolioId"]] += amount

    portfolios = []
    for p in sorted(data.get("portfolios", []), key=lambda x: x.get("name") or ""):
        pid = p.get("physicalAccountId")
        portfolios.append({
            "id": p["id"], "name": p.get("name"), "broker_account_id": p.get("brokerAccountId"),
            "base_currency": p.get("baseCurrency") or "USD", "note": p.get("note"),
            "linked": bool(broker_account and p.get("brokerAccountId") == broker_account),
            "deposits": _r(deposits.get(p["id"])),
            "physical_account": phys(pid) if pid else None,
            "virtual_accounts": [virt(v) for v, ps in sorted(backings.items()) if pid and pid in ps],
        })

    names = {p["id"]: p.get("name") for p in data.get("portfolios", [])}
    plans: dict[str, dict] = {}
    open_trades = []
    for t in data.get("trades", []):
        if t.get("closedAt") or t.get("exitPrice") is not None:
            continue
        sym = str(t.get("symbol") or "").upper()
        if not sym:
            continue
        row = {
            "id": t["id"], "symbol": sym, "portfolio": names.get(t.get("portfolioId"), t.get("portfolioId")),
            "portfolio_id": t.get("portfolioId"), "direction": t.get("direction"),
            "quantity": _num(t.get("quantity")), "entry": _num(t.get("entryPrice")),
            "stop": _num(t.get("stopLossPrice")), "target": _num(t.get("takeProfitPrice")),
            "opened_at": t.get("openedAt"), "note": t.get("note"), "imported": bool(t.get("externalId")),
        }
        open_trades.append(row)
        # one plan per symbol: prefer the linked portfolio, then the one with a stop
        cur = plans.get(sym)
        linked_ids = {p["id"] for p in portfolios if p["linked"]}
        rank = (row["portfolio_id"] in linked_ids, row["stop"] is not None)
        if cur is None or rank > (cur["portfolio_id"] in linked_ids, cur["stop"] is not None):
            plans[sym] = row
    open_trades.sort(key=lambda r: (r["portfolio"] or "", r["symbol"]))

    return {
        "project": project(),
        "broker_account": broker_account,
        "linked_portfolio": next((p for p in portfolios if p["linked"]), None),
        "portfolios": portfolios,
        "physical_accounts": [phys(pid) for pid in sorted(physical, key=lambda i: physical[i].get("name") or "")],
        "virtual_accounts": [virt(vid) for vid in sorted(virtual, key=lambda i: virtual[i].get("name") or "")],
        "open_trades": open_trades,
        "plans": plans,
    }


def _broker_account() -> str | None:
    try:
        from .trading import trading

        acct = trading.status().get("account")
        if acct:
            return acct
    except Exception:  # noqa: BLE001 — a broker problem must not hide Financier's data
        pass
    try:
        from . import portfolio

        return portfolio.default_account()
    except Exception:  # noqa: BLE001
        return None


def summary(refresh: bool = False, client_factory: Callable[[], httpx.Client] | None = None) -> dict:
    """Financier's side of the link (cached for two minutes; `refresh` re-reads)."""
    with _lock:
        fresh = _cache["data"] is not None and time.time() - _cache["at"] < CACHE_SECONDS and _cache["project"] == project()
        if refresh or not fresh:
            _cache.update(data=fetch(client_factory), at=time.time(), project=project())
        data = _cache["data"]
        at = _cache["at"]
    out = build_summary(data, _broker_account())
    out["fetched_at"] = datetime.fromtimestamp(at, timezone.utc).isoformat()
    return out


def plan_for(symbol: str) -> dict | None:
    """Financier's risk plan for an open position in `symbol`, if it records one."""
    return summary()["plans"].get(symbol.upper())
