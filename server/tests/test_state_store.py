"""State in Firestore (state_store.py): the working copy loads from Firestore and every committed change
reaches it — ORM writes, bulk UPDATE/DELETE statements, JSON and datetime columns — against a fake
Firestore REST API (an in-memory dict behind httpx.MockTransport)."""

import json
from datetime import datetime

import httpx
import pytest

from autobench import db, state_store
from autobench.config import get_settings
from autobench.services import gcp


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setattr(gcp, "access_token", lambda client: "tok")


class FakeFirestore:
    def __init__(self):
        self.docs: dict[str, dict] = {}  # "collection/id" -> raw fields
        self.fail = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        root = "/v1/projects/test-proj/databases/platform/documents"
        assert path.startswith(root), path
        rest = path[len(root):]
        if rest == ":commit":
            if self.fail:
                self.fail -= 1
                return httpx.Response(503, json={"error": {"message": "unavailable"}})
            for w in json.loads(request.content)["writes"]:
                if "update" in w:
                    key = w["update"]["name"].split("/documents/", 1)[1]
                    self.docs[key] = w["update"]["fields"]
                else:
                    self.docs.pop(w["delete"].split("/documents/", 1)[1], None)
            return httpx.Response(200, json={})
        coll = rest.strip("/")
        if request.method == "GET" and coll.count("/") == 1:  # one document
            if coll not in self.docs:
                return httpx.Response(404, json={"error": {"message": "not found"}})
            return httpx.Response(200, json={"name": f"{root}/{coll}", "fields": self.docs[coll]})
        docs = [{"name": f"{root}/{k}", "fields": v} for k, v in self.docs.items() if k.split("/")[0] == coll]
        return httpx.Response(200, json={"documents": docs} if docs else {})

    def collection(self, table: str) -> dict[str, dict]:
        pre = f"autobench_{table}/"
        return {k[len(pre):]: v for k, v in self.docs.items() if k.startswith(pre)}


@pytest.fixture
def fake(monkeypatch, tmp_path):
    f = FakeFirestore()
    monkeypatch.setattr(state_store, "client_factory_default",
                        lambda: httpx.Client(transport=httpx.MockTransport(f.handler)))
    monkeypatch.setenv("AUTOBENCH_STATE_PROJECT", "test-proj")
    monkeypatch.setenv("AUTOBENCH_DB_PATH", str(tmp_path / "copy.db"))
    get_settings.cache_clear()
    db.init_db()
    yield f
    state_store.activate(None)


def _restart(tmp_path, monkeypatch, name="fresh.db"):
    """A new process: an empty working copy, filled from Firestore."""
    monkeypatch.setenv("AUTOBENCH_DB_PATH", str(tmp_path / name))
    get_settings.cache_clear()
    db.init_db()


def test_writes_reach_firestore_and_reload(fake, tmp_path, monkeypatch):
    from autobench.schemas import AnnotationIn, NoteIn
    from autobench.services import annotations, workspace

    a = annotations.create("AAPL", [AnnotationIn(type="horizontal_line", points=[{"time": 1700000000, "price": 190.5}],
                                                 text="support", tag="t1")], author="agent")[0]
    note = workspace.save_note(NoteIn(title="Thesis", content="AI capex", symbol="NVDA", tags=["ai"]))
    workspace.update_watchlist("swing", add=["MSFT", "AMD"])
    db.state_set("chartcfg:AAPL", {"layers": [{"id": "main", "items": [[1, 2]]}]})  # nested arrays survive

    assert a["id"] in fake.collection("annotations")
    assert note["id"] in fake.collection("notes")
    assert "chartcfg:AAPL" in fake.collection("app_state")
    stored = fake.collection("app_state")["chartcfg:AAPL"]["value"]["stringValue"]
    assert json.loads(stored)["layers"][0]["items"] == [[1, 2]]

    # update and delete go through too
    workspace.save_note(NoteIn(title="Thesis v2", content="AI capex", symbol="NVDA"), note_id=note["id"])
    assert fake.collection("notes")[note["id"]]["title"]["stringValue"] == "Thesis v2"
    workspace.delete_note(note["id"])
    assert note["id"] not in fake.collection("notes")

    _restart(tmp_path, monkeypatch)
    got = annotations.list_(symbol="AAPL")
    assert [g["id"] for g in got] == [a["id"]] and got[0]["points"][0]["price"] == 190.5
    assert isinstance(got[0]["created_at"], str) and got[0]["text"] == "support"
    assert db.state_get("chartcfg:AAPL")["layers"][0]["items"] == [[1, 2]]
    assert set(workspace.get_watchlist("swing")["symbols"]) >= {"MSFT", "AMD"}
    assert workspace.list_notes() == [] or all(n["id"] != note["id"] for n in workspace.list_notes())


def test_bulk_statements_are_mirrored(fake):
    from sqlalchemy import update

    from autobench.services import alerts

    with db.session_scope() as s:
        for i in range(3):
            s.add(db.AlertEventRow(id=f"e{i}", alert_id="a", symbol="AAPL", description="x", read=False,
                                   at=datetime(2026, 11, i + 1)))
    assert len(fake.collection("alert_events")) == 3
    alerts.mark_read(["e0", "e1"]) if hasattr(alerts, "mark_read") else None
    with db.session_scope() as s:
        s.execute(update(db.AlertEventRow).where(db.AlertEventRow.id == "e2").values(read=True))
    assert fake.collection("alert_events")["e2"]["read"]["booleanValue"] is True
    alerts.clear_inbox(["e0"])
    assert set(fake.collection("alert_events")) == {"e1", "e2"}
    alerts.clear_inbox(None)
    assert fake.collection("alert_events") == {}


def test_caches_are_not_state(fake):
    db.cache_set("k", {"v": 1}, 60)
    assert not any(k.startswith("autobench_kv_cache") for k in fake.docs)
    assert db.cache_get("k") == {"v": 1}


def test_failed_writes_are_kept_and_retried(fake, monkeypatch):
    monkeypatch.setattr(state_store.time, "sleep", lambda s: None)
    fake.fail = 3  # all three attempts fail
    db.state_set("a", {"x": 1})
    m = state_store.active()
    assert m.backlog and "unavailable" in m.status()["last_error"]
    db.state_set("b", {"x": 2})  # the next commit carries the backlog first
    assert set(fake.collection("app_state")) >= {"a", "b"} and not m.backlog


def test_rollback_writes_nothing(fake):
    with pytest.raises(RuntimeError):
        with db.session_scope() as s:
            s.add(db.StateRow(key="never", value={}))
            s.flush()
            raise RuntimeError("boom")
    assert "never" not in fake.collection("app_state")


def test_upload_all_migrates_a_local_database(tmp_path, monkeypatch):
    f = FakeFirestore()
    monkeypatch.setattr(state_store, "client_factory_default",
                        lambda: httpx.Client(transport=httpx.MockTransport(f.handler)))
    db.init_db(f"sqlite:///{tmp_path / 'old.db'}")
    db.state_set("ui", {"theme": "dark"})
    with db.session_scope() as s:
        s.add(db.FillRow(exec_id="x1", account="U1", symbol="AAPL", side="buy", qty=1.0, price=1.0,
                         time=1767225600, broker="ibkr"))
    m = state_store.Mirror("test-proj", "platform", db.mirrored_tables())
    counts = m.upload_all(db._engine)
    assert counts["app_state"] == 1 and counts["fills"] == 1
    assert "ui" in f.collection("app_state") and len(f.collection("fills")) == 1


def test_universe_snapshots_go_through_cloud_storage(monkeypatch, tmp_path):
    from autobench.services import universe

    store: dict[str, bytes] = {}
    monkeypatch.setattr(gcp, "gcs_put", lambda c, p, b, name, data, **k: store.__setitem__(f"{b}/{name}", data))
    monkeypatch.setattr(gcp, "gcs_get", lambda c, p, b, name: store.get(f"{b}/{name}"))
    monkeypatch.setenv("AUTOBENCH_STATE_BUCKET", "bkt")
    get_settings.cache_clear()
    layer = universe._Layer("listings")
    layer.store({"rows": 3})
    assert "bkt/universe/listings.pkl" in store
    universe._Layer("quotes").store({"q": 1})
    assert "bkt/universe/quotes.pkl" not in store  # live quotes are not kept
    # a cold start (empty data dir) fetches the snapshot
    monkeypatch.setenv("AUTOBENCH_DB_PATH", str(tmp_path / "cold" / "x.db"))
    get_settings.cache_clear()
    cold = universe._Layer("listings")
    cold.load()
    assert cold.value == {"rows": 3} and cold.updated > 0


def test_a_newer_instance_takes_the_lease_and_the_old_one_stops_writing(fake, tmp_path, monkeypatch):
    old = state_store.active()
    old.claim(wait=0)
    assert old.check_lease() and fake.docs["autobench_meta/lease"]
    db.state_set("before", {"v": 1})
    new = state_store.Mirror("test-proj", "platform", db.mirrored_tables())
    new.claim(wait=0)  # the new revision starts
    assert old.check_lease() is False and old.status()["superseded"]
    with pytest.raises(state_store.Superseded):
        db.state_set("after", {"v": 2})
    assert new.check_lease() is True
    keys = set(fake.collection("app_state"))
    assert any("before" in k for k in keys) and not any("after" in k for k in keys)
    assert db.state_get("before") == {"v": 1}  # reads still work on the old instance


def test_superseded_writes_answer_503(fake, monkeypatch):
    from fastapi.testclient import TestClient

    from autobench.app import create_app

    app = create_app()  # loads the state (a new mirror)
    state_store.active().superseded = True
    with TestClient(app) as c:
        r = c.put("/api/ui-state", json={"v": 3, "savedAt": 1, "prefs": {}}, headers={"X-Autobench": "1"})
    assert r.status_code == 503 and "newer instance" in r.json()["detail"]
