"""Background jobs for `autobench serve`: alert checks, universe refreshes (listings and
fundamentals daily, bars every few minutes in market hours, live prices while someone is
looking at the screener/heatmap) and macro cache warm-up."""

from __future__ import annotations

import logging
import threading
import time

log = logging.getLogger(__name__)

_thread: threading.Thread | None = None
_stop = threading.Event()
last_universe_access = 0.0  # set by API handlers that read the universe


def touch_universe() -> None:
    global last_universe_access
    last_universe_access = time.time()


def start() -> None:
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_run, name="autobench-scheduler", daemon=True)
    _thread.start()


def stop() -> None:
    _stop.set()


def _every(seconds: float, last: dict, key: str) -> bool:
    now = time.time()
    if now - last.get(key, 0) >= seconds:
        last[key] = now
        return True
    return False


def _run() -> None:
    from . import alerts, macro
    from .universe import universe

    last: dict[str, float] = {}
    _stop.wait(5)  # let the server finish starting
    while not _stop.is_set():
        try:
            if _every(20, last, "alerts"):
                fired = alerts.check()
                if fired:
                    log.info("alerts fired: %s", ", ".join(a["description"] for a in fired))
            if _every(60, last, "universe"):
                stale = universe.stale()
                if time.time() - last_universe_access > 600:
                    stale = [p for p in stale if p != "quotes"]  # nobody is looking: skip live prices
                if stale:
                    universe.ensure(stale)
            if _every(3600, last, "macro"):
                threading.Thread(target=_warm_macro, args=(macro,), daemon=True).start()
        except Exception:
            log.exception("scheduler iteration failed")
        _stop.wait(5)


def _warm_macro(macro) -> None:
    try:
        macro.macro_dashboard()
        macro.markets_board()
    except Exception as e:
        log.info("macro warm-up failed: %s", e)
