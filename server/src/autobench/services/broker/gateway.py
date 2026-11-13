"""IB Gateway managed inside the container (Cloud Run), started on demand.

The image carries IB Gateway (standalone installer, /opt/Jts/ibgateway/stable), IBC (/opt/ibc, which logs in
and answers Gateway's dialogs) and Xvfb (Gateway is a Swing app). Nothing runs until the broker is needed:
`ensure_running()` is called when a trading call actually needs IBKR (the Trading page's data, an order, an
agent proposal) or from the Trading page's Start button. After `idle_minutes` without such a call it is
stopped again; Cloud Run scaling the instance to zero stops it too.

Login: the IBKR username/password come from the environment (Secret Manager `autobench-credentials` on Cloud Run)
and are written only to a 0600 IBC config under /tmp. A live login needs the IB Key (IBKR Mobile) approval
on each start: the status says so while IBC waits for it. The session is `primaryoverride`: starting it takes
the session over from a phone/TWS login; logging in there later takes it back.

Off Cloud Run (AUTOBENCH_GATEWAY != managed) this module does nothing: the operator runs IB Gateway/TWS.
"""

from __future__ import annotations

import logging
import os
import shutil
import signal
import socket
import subprocess
import threading
import time
from pathlib import Path

from ...config import get_settings

log = logging.getLogger(__name__)

IBC_DIR = Path(os.environ.get("AUTOBENCH_IBC_DIR", "/opt/ibc"))
JTS_DIR = Path(os.environ.get("AUTOBENCH_JTS_DIR", "/opt/Jts"))
GATEWAY_VERSION = "stable"  # the install directory under /opt/Jts/ibgateway
WORK = Path(os.environ.get("AUTOBENCH_GATEWAY_WORK", "/tmp/ibgateway"))
DISPLAY = ":99"
COMMAND_PORT = 7462
POLL_SECONDS = 5


def _config(user: str, password: str, mode: str, api_port: int, device: str = "") -> str:
    """IBC settings (names and values as documented in IBC 3.24's config.ini)."""
    return "\n".join([
        f"IbLoginId={user}",
        f"IbPassword={password}",
        f"TradingMode={mode}",
        "ExistingSessionDetectedAction=primaryoverride",
        "ReloginAfterSecondFactorAuthenticationTimeout=yes",
        "SecondFactorAuthenticationTimeout=180",
        f"SecondFactorDevice={device}",
        "AcceptIncomingConnectionAction=accept",
        "AcceptNonBrokerageAccountWarning=yes",
        "ReadOnlyApi=no",
        f"OverrideTwsApiPort={api_port}",
        "TrustedTwsApiClientIPs=127.0.0.1",
        f"CommandServerPort={COMMAND_PORT}",
        "BindAddress=127.0.0.1",
        "ControlFrom=127.0.0.1",
        "AllowBlindTrading=no",
        "MinimizeMainWindow=yes",
        "",
    ])


def _redact(line: str, secrets: tuple[str, ...]) -> str:
    for secret in secrets:
        if secret:
            line = line.replace(secret, "***")
    return line


def _forward(stream, log_path: Path, secrets: tuple[str, ...], on_line=None) -> None:
    """IBC's output into ibc.log and the app log (Cloud Logging), with the login redacted."""
    with log_path.open("a", errors="replace") as out:
        for raw in iter(stream.readline, b""):
            line = _redact(raw.decode(errors="replace").rstrip(), secrets)
            if not line:
                continue
            out.write(line + "\n")
            out.flush()
            log.info("IBC: %s", line[:500])
            if on_line is not None:
                on_line(line)


class ManagedGateway:
    def __init__(self) -> None:
        self._lock = threading.RLock()  # re-entrant: a status read from inside a locked section cannot deadlock
        self._xvfb: subprocess.Popen | None = None
        self._ibc: subprocess.Popen | None = None
        self._started_at = 0.0
        self._last_used = 0.0
        self._state = "stopped"
        self._message = "Not started"
        self._monitor: threading.Thread | None = None
        self._awaiting_2fa = False

    # --- configuration ---------------------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return get_settings().autobench_gateway == "managed"

    def _port(self) -> int:
        return int(get_settings().ibkr_port)

    # --- lifecycle -------------------------------------------------------------------------------------

    def touch(self) -> None:
        """A trading call needed IBKR: keep it running (and start it if it is not)."""
        if not self.enabled:
            return
        self._last_used = time.time()
        self.ensure_running()

    def ensure_running(self) -> dict:
        if not self.enabled:
            return self.status()
        with self._lock:
            if self._ibc is not None and self._ibc.poll() is None:
                return self._status_locked()
            user, password = os.environ.get("IBKR_USERNAME", ""), os.environ.get("IBKR_PASSWORD", "")
            if not user or not password:
                self._state, self._message = "failed", ("No IBKR login: run tool/set_autobench_credentials.sh, then restart "
                                                        "(Secret Manager autobench-credentials)")
                return self._status_locked()
            mode = "live" if get_settings().autobench_gateway_mode == "live" else "paper"
            WORK.mkdir(parents=True, exist_ok=True)
            ini = WORK / "config.ini"
            ini.write_text(_config(user, password, mode, self._port(), get_settings().autobench_gateway_2fa_device.strip()))
            ini.chmod(0o600)
            settings_dir = WORK / "jts"
            settings_dir.mkdir(exist_ok=True)
            env = {k: v for k, v in os.environ.items() if not k.startswith(("IBKR_", "AUTOBENCH_", "ALPACA_", "CLAUDE"))}
            env["DISPLAY"] = DISPLAY
            if self._xvfb is None or self._xvfb.poll() is not None:
                self._xvfb = subprocess.Popen(["Xvfb", DISPLAY, "-screen", "0", "1024x768x16", "-nolisten", "tcp"],
                                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                              start_new_session=True)
            self._ibc = subprocess.Popen(
                [str(IBC_DIR / "scripts" / "ibcstart.sh"), GATEWAY_VERSION, "-g", f"--tws-path={JTS_DIR}",
                 f"--tws-settings-path={settings_dir}", f"--ibc-path={IBC_DIR}", f"--ibc-ini={ini}", f"--mode={mode}",
                 "--on2fatimeout=restart"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, start_new_session=True)
            threading.Thread(target=_forward, args=(self._ibc.stdout, WORK / "ibc.log", (user, password), self._on_ibc_line),
                             name="ibc-log", daemon=True).start()
            self._started_at = self._last_used = time.time()
            self._awaiting_2fa = False
            self._state = "starting"
            self._message = ("Logging in to IBKR. Approve the IBKR Mobile (IB Key) notification on your phone."
                             if mode == "live" else "Logging in to the IBKR paper account.")
            log.info("IB Gateway starting (%s)", mode)
            if self._monitor is None or not self._monitor.is_alive():
                self._monitor = threading.Thread(target=self._watch, name="ibgateway", daemon=True)
                self._monitor.start()
            return self._status_locked()

    def stop(self, reason: str = "Stopped") -> dict:
        with self._lock:
            ibc, xvfb = self._ibc, self._xvfb
            self._ibc = self._xvfb = None
            self._state, self._message = "stopped", reason
            self._awaiting_2fa = False
        for p in (ibc, xvfb):
            if p is not None and p.poll() is None:
                try:
                    os.killpg(p.pid, signal.SIGTERM)
                    p.wait(timeout=20)
                except (ProcessLookupError, subprocess.TimeoutExpired, PermissionError):
                    try:
                        os.killpg(p.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
        log.info("IB Gateway stopped: %s", reason)
        self._changed()
        return self.status()

    def _api_open(self) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", self._port()), timeout=1):
                return True
        except OSError:
            return False

    def _watch(self) -> None:
        # _changed() (which reads status() under the lock) is only ever called after the lock is released:
        # calling it inside deadlocked every trading request once the Gateway came up.
        while True:
            time.sleep(POLL_SECONDS)
            changed = finished = False
            with self._lock:
                ibc = self._ibc
                if ibc is None:
                    return
                if ibc.poll() is not None:
                    self._state = "failed"
                    self._message = f"IB Gateway exited (code {ibc.returncode}). {self._log_tail()}"
                    self._ibc = None
                    changed = finished = True
                elif self._api_open() and self._state != "running":
                    log.info("IB Gateway API is up")
                    self._state, self._message = "running", "Logged in"
                    self._awaiting_2fa = False
                    changed = True
            if changed:
                self._changed()
            if finished:
                return
            idle = get_settings().autobench_gateway_idle_minutes * 60
            if idle and self._state == "running" and time.time() - self._last_used > idle:
                self.stop(f"Stopped after {idle // 60} idle minutes")
                return

    def _on_ibc_line(self, line: str) -> None:
        """IBC reports the Second Factor Authentication dialog opening/closing: the UI then offers a code box
        (accounts that use an authenticator app get no phone notification: the code must be typed in)."""
        if "Second Factor Authentication initiated" in line:
            waiting = True
        elif "Second Factor Authentication" in line and "event=Closed" in line:
            waiting = False
        else:
            return
        with self._lock:
            self._awaiting_2fa = waiting
            if waiting and self._state == "starting":
                self._message = ("IBKR asks for the second factor: approve the IBKR Mobile notification, or enter the "
                                 "code from your authenticator app.")
        self._changed()

    def submit_2fa_code(self, code: str) -> dict:
        """Type the owner's one-time code into the Gateway's Second Factor Authentication dialog."""
        code = (code or "").strip().replace(" ", "")
        if not code.isdigit() or not 4 <= len(code) <= 10:
            raise ValueError("Enter the digits shown in your authenticator app")
        with self._lock:
            if self._ibc is None or self._ibc.poll() is not None or self._state != "starting":
                raise ValueError("IB Gateway is not logging in")
            if not self._awaiting_2fa:
                raise ValueError("IB Gateway is not asking for a code right now")
        if shutil.which("xdotool") is None:
            raise ValueError("No xdotool in this image")
        env = {"PATH": os.environ.get("PATH", ""), "DISPLAY": DISPLAY}
        r = subprocess.run(["xdotool", "search", "--name", "Second Factor Authentication"],
                           capture_output=True, text=True, timeout=10, env=env)
        windows = r.stdout.split()
        if not windows:
            raise ValueError("The Second Factor Authentication dialog is not open")
        win = windows[-1]
        # No window manager under Xvfb: no windowactivate (_NET_ACTIVE_WINDOW). Focus the dialog, click into its
        # code field (upper middle of the dialog), then type with XTEST (Java ignores synthetic --window events).
        geo = subprocess.run(["xdotool", "getwindowgeometry", "--shell", win],
                             capture_output=True, text=True, timeout=10, env=env).stdout
        dims = dict(line.split("=", 1) for line in geo.split() if "=" in line)
        width, height = int(dims.get("WIDTH", 300)), int(dims.get("HEIGHT", 125))
        r = subprocess.run(["xdotool", "windowfocus", win,
                            "mousemove", "--window", win, str(width // 2), str(int(height * 0.46)), "click", "1",
                            "sleep", "0.2", "key", "ctrl+a", "BackSpace",
                            "type", "--delay", "60", code],
                           capture_output=True, text=True, timeout=20, env=env)
        if r.returncode != 0:
            raise ValueError(f"Could not type the code: {r.stderr[:200]}")
        subprocess.run(["xdotool", "key", "Return"], capture_output=True, timeout=10, env=env)
        log.info("IB Gateway: second factor code entered")
        with self._lock:
            self._message = "Code entered: logging in…"
        self._changed()
        return self.status()

    def screenshot(self) -> bytes:
        """A PNG of the Gateway's virtual display (to see a login dialog IBC cannot answer). Read only."""
        if not self.enabled:
            raise ValueError("IB Gateway is not managed here")
        with self._lock:
            if self._xvfb is None or self._xvfb.poll() is not None:
                raise ValueError("IB Gateway is not running")
        if shutil.which("import") is None:
            raise ValueError("No screenshot tool in this image (ImageMagick's import)")
        r = subprocess.run(["import", "-display", DISPLAY, "-window", "root", "png:-"], capture_output=True,
                           timeout=20, env={"PATH": os.environ.get("PATH", ""), "DISPLAY": DISPLAY})
        if r.returncode != 0 or not r.stdout:
            raise ValueError(f"Screenshot failed: {r.stderr.decode(errors='replace')[:200]}")
        return r.stdout

    def _log_tail(self) -> str:
        try:
            lines = (WORK / "ibc.log").read_text(errors="replace").strip().splitlines()
        except OSError:
            return ""
        return lines[-1][-200:] if lines else ""

    def _changed(self) -> None:
        """Tell the UI (the Trading page follows `trading.status`)."""
        try:
            from ..events import bus
            from ..trading import trading

            bus.publish("trading.status", trading.status())
        except Exception:  # noqa: BLE001 — never let a UI notification break the watcher
            pass

    # --- status ----------------------------------------------------------------------------------------

    def _status_locked(self) -> dict:
        return {"managed": True, "state": self._state, "message": self._message, "awaiting_2fa": self._awaiting_2fa,
                "started_at": self._started_at or None, "last_used": self._last_used or None,
                "idle_minutes": get_settings().autobench_gateway_idle_minutes,
                "mode": get_settings().autobench_gateway_mode,
                "installed": (JTS_DIR / "ibgateway" / GATEWAY_VERSION).is_dir() and shutil.which("Xvfb") is not None}

    def status(self) -> dict:
        if not self.enabled:
            return {"managed": False}
        with self._lock:
            return self._status_locked()


gateway = ManagedGateway()
