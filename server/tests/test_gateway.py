"""The managed IB Gateway (Cloud Run): started on demand by a trading call, credentials only in a 0600
IBC config (never in the child environment), running once the API port opens, stopped when idle.
IBC and Xvfb are stand-ins: a script that records its arguments/environment and opens the API port."""

import json
import os
import socket
import stat
import sys
import time

import pytest

from autobench.config import get_settings
from autobench.services.broker import gateway as gw


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def managed(tmp_path, monkeypatch):
    port = _free_port()
    ibc = tmp_path / "ibc" / "scripts"
    ibc.mkdir(parents=True)
    record = tmp_path / "record.json"
    start = ibc / "ibcstart.sh"
    start.write_text(f"""#!{sys.executable}
import json, os, socket, sys, time
json.dump({{"args": sys.argv[1:], "env": dict(os.environ)}}, open({str(record)!r}, "w"))
print("IBC: Login dialog: entering password s3cret for me", flush=True)
time.sleep(0.3)
s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); s.bind(("127.0.0.1", {port})); s.listen()
while True:
    c, _ = s.accept(); c.close()
""")
    start.chmod(0o755)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "Xvfb").write_text("#!/bin/sh\nsleep 600\n")
    (bin_dir / "Xvfb").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    (tmp_path / "jts" / "ibgateway" / "stable").mkdir(parents=True)
    monkeypatch.setattr(gw, "IBC_DIR", tmp_path / "ibc")
    monkeypatch.setattr(gw, "JTS_DIR", tmp_path / "jts")
    monkeypatch.setattr(gw, "WORK", tmp_path / "work")
    monkeypatch.setattr(gw, "POLL_SECONDS", 0.2)
    monkeypatch.setenv("AUTOBENCH_GATEWAY", "managed")
    monkeypatch.setenv("IBKR_PORT", str(port))
    monkeypatch.setenv("IBKR_USERNAME", "me")
    monkeypatch.setenv("IBKR_PASSWORD", "s3cret")
    get_settings.cache_clear()
    g = gw.ManagedGateway()
    yield g, record, tmp_path
    g.stop("test over")


def _wait(pred, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.1)
    return False


def test_not_managed_does_nothing(monkeypatch):
    monkeypatch.setenv("AUTOBENCH_GATEWAY", "")
    get_settings.cache_clear()
    g = gw.ManagedGateway()
    g.touch()
    assert g.status() == {"managed": False}


def test_starts_on_demand_and_keeps_the_password_out_of_the_environment(managed):
    g, record, tmp = managed
    assert g.status()["state"] == "stopped" and g.status()["installed"]
    g.touch()
    assert g.status()["state"] == "starting" and "paper" in g.status()["message"]
    assert _wait(lambda: g.status()["state"] == "running")
    rec = json.loads(record.read_text())
    assert rec["args"][:2] == ["stable", "-g"] and "--mode=paper" in rec["args"]
    assert not any(k.startswith(("IBKR_", "AUTOBENCH_")) for k in rec["env"]) and rec["env"]["DISPLAY"] == ":99"
    assert "s3cret" not in json.dumps(rec)
    ini = tmp / "work" / "config.ini"
    assert stat.S_IMODE(ini.stat().st_mode) == 0o600
    text = ini.read_text()
    assert "IbLoginId=me" in text and "IbPassword=s3cret" in text and "ExistingSessionDetectedAction=primaryoverride" in text
    # a second call does not start another one
    g.touch()
    assert g.status()["state"] == "running"
    g.stop("done")
    assert g.status()["state"] == "stopped"


def test_ibc_output_reaches_the_log_without_the_login(managed, caplog):
    g, _, tmp = managed
    with caplog.at_level("INFO", logger=gw.log.name):
        g.touch()
        assert _wait(lambda: g.status()["state"] == "running")
        assert _wait(lambda: "Login dialog" in caplog.text)
    assert "s3cret" not in caplog.text and "s3cret" not in (tmp / "work" / "ibc.log").read_text()
    assert "entering password *** for ***" in caplog.text


def test_2fa_device_goes_into_the_ibc_config(managed, monkeypatch):
    g, _, tmp = managed
    monkeypatch.setenv("AUTOBENCH_GATEWAY_2FA_DEVICE", "IB Key")
    get_settings.cache_clear()
    g.touch()
    assert "SecondFactorDevice=IB Key" in (tmp / "work" / "config.ini").read_text()


def test_screenshot_needs_a_running_gateway(managed):
    g, _, _ = managed
    with pytest.raises(ValueError, match="not running"):
        g.screenshot()


def test_second_factor_prompt_offers_a_code_box_and_types_the_code(managed, monkeypatch):
    g, _, _ = managed
    g.touch()
    with pytest.raises(ValueError, match="not asking"):
        g.submit_2fa_code("123456")
    g._state = "starting"  # the stand-in opens the API port at once; pin the login phase
    g._on_ibc_line("2026-11-13 07:28:02:391 IBC: Second Factor Authentication initiated")
    assert g.status()["awaiting_2fa"] and "authenticator app" in g.status()["message"]
    with pytest.raises(ValueError, match="digits"):
        g.submit_2fa_code("12ab56")
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return gw.subprocess.CompletedProcess(cmd, 0, stdout="4194305\n" if "search" in cmd else "", stderr="")

    monkeypatch.setattr(gw.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(gw.subprocess, "run", fake_run)
    g.submit_2fa_code(" 123 456 ")
    typed = calls[2]
    assert typed[0] == "xdotool" and "4194305" in typed and typed[-1] == "123456"
    assert "windowactivate" not in typed and "click" in typed
    assert calls[3] == ["xdotool", "key", "Return"]
    g._on_ibc_line("IBC: detected dialog entitled: Second Factor Authentication; event=Closed")
    assert not g.status()["awaiting_2fa"]


def test_status_updates_on_start_do_not_deadlock(managed, monkeypatch):
    """The 'running' notification reads status() (as trading.status does); it must not hang the watcher."""
    g, _, _ = managed
    seen = []
    monkeypatch.setattr(g, "_changed", lambda: seen.append(g.status()["state"]))
    g.touch()
    assert _wait(lambda: "running" in seen)
    assert g.status()["state"] == "running"


def test_live_mode_asks_for_the_phone_approval(managed, monkeypatch):
    g, record, _ = managed
    monkeypatch.setenv("AUTOBENCH_GATEWAY_MODE", "live")
    get_settings.cache_clear()
    g.ensure_running()
    assert "IBKR Mobile" in g.status()["message"]
    assert _wait(lambda: record.exists() and "--mode=live" in json.loads(record.read_text())["args"])


def test_without_credentials_it_says_how_to_add_them(managed, monkeypatch):
    g, _, _ = managed
    monkeypatch.delenv("IBKR_PASSWORD")
    st = g.ensure_running()
    assert st["state"] == "failed" and "set_autobench_credentials.sh" in st["message"]


def test_stops_when_idle(managed, monkeypatch):
    g, _, _ = managed
    g.touch()
    assert _wait(lambda: g.status()["state"] == "running")
    monkeypatch.setattr(gw, "get_settings", lambda: type("S", (), {**get_settings().__dict__,
                                                                    "autobench_gateway_idle_minutes": 1})())
    g._last_used = time.time() - 120
    assert _wait(lambda: g.status()["state"] == "stopped")
    assert "idle" in g.status()["message"]


def test_trading_call_starts_it_and_status_reports_it(managed, monkeypatch):
    g, _, _ = managed
    monkeypatch.setattr(gw, "gateway", g)
    from autobench.services.trading import trading

    monkeypatch.setenv("AUTOBENCH_BROKER", "ibkr")
    get_settings.cache_clear()
    trading.stop()
    try:
        trading._broker()
        assert trading.status()["gateway"]["state"] in ("starting", "running")
    finally:
        trading.stop()
