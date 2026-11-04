"""Cloud sync (services/cloud_sync.py): encoding, when it pushes, and what it sends.
HTTP is intercepted with respx; nothing reaches Google."""

import json
from datetime import datetime, timezone

import httpx
import pytest
import respx

from autobench.config import get_settings
from autobench.services import cloud_sync

DOC = "https://firestore.googleapis.com/v1/projects/test-proj/databases/platform/documents/autobench/latest"


@pytest.fixture(autouse=True)
def _reset(monkeypatch, tmp_path):
    adc = tmp_path / "adc.json"
    adc.write_text(json.dumps({"type": "authorized_user", "client_id": "cid", "client_secret": "cs",
                               "refresh_token": "rt"}))
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(adc))
    cloud_sync._state.update(last_push=0.0, last_connected=False, last_hash=None, last_error=None,
                             last_source=None, token=None, token_expiry=0.0)


def _enable(monkeypatch, interval=300):
    monkeypatch.setenv("AUTOBENCH_CLOUD_SYNC_PROJECT", "test-proj")
    monkeypatch.setenv("AUTOBENCH_CLOUD_SYNC_INTERVAL", str(interval))
    get_settings.cache_clear()


def _routes(mock):
    mock.post(cloud_sync.TOKEN_URL).respond(200, json={"access_token": "tok", "expires_in": 3600})
    latest = mock.patch(DOC).respond(200, json={})
    day = mock.patch(url__regex=r".*/autobench/latest/days/\d{4}-\d{2}-\d{2}$").respond(200, json={})
    mock.post(url__regex=r".*/databases/platform/documents:commit$").respond(200, json={})
    return latest, day


def snap(source="statement", nav=100.0):
    return {"source": source, "account": "U1", "nav": {"value": nav}, "live": None}


def test_encode_covers_json_types():
    v = cloud_sync.encode({"a": 1, "b": 1.5, "c": "x", "d": None, "e": True, "f": [1, "y"],
                           "t": datetime(2026, 11, 4, 1, 2, 3, tzinfo=timezone.utc)})
    f = v["mapValue"]["fields"]
    assert f["a"] == {"integerValue": "1"}
    assert f["b"] == {"doubleValue": 1.5}
    assert f["c"] == {"stringValue": "x"}
    assert f["d"] == {"nullValue": None}
    assert f["e"] == {"booleanValue": True}
    assert f["f"]["arrayValue"]["values"][1] == {"stringValue": "y"}
    assert f["t"] == {"timestampValue": "2026-11-04T01:02:03Z"}


def test_off_without_a_project():
    assert cloud_sync.maybe_sync(snapshot=lambda: snap()) is None


@respx.mock
def test_statement_snapshot_is_pushed_once_until_it_changes(monkeypatch):
    _enable(monkeypatch)
    latest, day = _routes(respx)
    assert cloud_sync.maybe_sync(snapshot=lambda: snap()) == "statement"
    assert latest.call_count == 1 and day.call_count == 1
    req = latest.calls[0].request
    assert req.headers["authorization"] == "Bearer tok"
    assert req.headers["x-goog-user-project"] == "test-proj"
    body = json.loads(req.content)
    assert body["fields"]["account"] == {"stringValue": "U1"}
    assert "timestampValue" in body["fields"]["synced_at"]
    # Unchanged: nothing sent. Changed: sent again.
    assert cloud_sync.maybe_sync(snapshot=lambda: snap()) is None
    assert cloud_sync.maybe_sync(snapshot=lambda: snap(nav=101.0)) == "statement"
    assert latest.call_count == 2


@respx.mock
def test_live_snapshot_pushes_on_connect_then_on_interval(monkeypatch):
    _enable(monkeypatch, interval=300)
    latest, _ = _routes(respx)
    clock = [1_000_000.0]
    monkeypatch.setattr(cloud_sync.time, "time", lambda: clock[0])
    assert cloud_sync.maybe_sync(snapshot=lambda: snap("live")) == "live"  # connection came up
    clock[0] += 100
    assert cloud_sync.maybe_sync(snapshot=lambda: snap("live")) is None  # not due
    clock[0] += 250
    assert cloud_sync.maybe_sync(snapshot=lambda: snap("live")) == "live"  # interval passed
    assert latest.call_count == 2


@respx.mock
def test_a_failed_write_is_reported_not_raised(monkeypatch):
    _enable(monkeypatch)
    respx.post(cloud_sync.TOKEN_URL).respond(200, json={"access_token": "tok", "expires_in": 3600})
    respx.patch(DOC).respond(403, json={"error": {"message": "denied"}})
    assert cloud_sync.maybe_sync(snapshot=lambda: snap()) is None
    assert "403" in cloud_sync.status()["last_error"]


def test_missing_login_is_reported(monkeypatch, tmp_path):
    _enable(monkeypatch)
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(tmp_path / "none.json"))
    assert cloud_sync.maybe_sync(snapshot=lambda: snap()) is None
    assert "application-default" in cloud_sync.status()["last_error"]


def test_build_snapshot_uses_real_services_with_no_broker():
    s = cloud_sync.build_snapshot()
    assert s["source"] == "statement"
    assert s["broker"]["connected"] is False
    assert "max_position_pct" in s["limits"]["limits"]
    assert s["open_trades"] == []


def test_a_push_reports_to_vral_altos_telemetry(monkeypatch):
    _enable(monkeypatch)
    with respx.mock(assert_all_called=False) as mock:
        _routes(mock)
        commit = mock.routes[-1]
        assert cloud_sync.maybe_sync(snapshot=lambda: snap()) == "statement"
        writes = json.loads(commit.calls.last.request.content)["writes"]
    entry = writes[0]["update"]
    assert "/logs/" in entry["name"]
    assert entry["fields"]["source"]["stringValue"] == "autobench"
    assert entry["fields"]["severity"]["stringValue"] == "info"
    metric = writes[1]
    assert metric["update"]["name"].split("/")[-1].startswith("autobench~")
    paths = {t["fieldPath"] for t in metric["updateTransforms"]}
    assert {"h.syncs", "h.log_info"} <= paths
