"""Claude page: Claude Code sessions read from a fake ~/.claude and a fake `claude` command."""

import json
import os
import sys
import time

import pytest
from fastapi.testclient import TestClient

from autobench.app import create_app
from autobench.services import claude_code
from autobench.services.events import bus

SID = "11111111-2222-4333-8444-555555555555"
OTHER = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
FORK = "99999999-8888-4777-8666-555555555555"

FAKE = r'''#!{python}
import json, sys, time, uuid
args = sys.argv[1:]
text = sys.stdin.read()
open({log!r}, "a").write(json.dumps({{"args": args, "text": text}}) + "\n")
def out(m): print(json.dumps(m), flush=True)
if "--version" in args:
    print("9.9.9 (Claude Code)"); sys.exit(0)
sid = args[args.index("--resume") + 1] if "--resume" in args else args[args.index("--session-id") + 1]
if "--fork-session" in args: sid = {fork!r}
out({{"type": "system", "subtype": "init", "session_id": sid, "cwd": "."}})
if "SLOW" in text: time.sleep(30)
if "FAIL" in text:
    print("boom", file=sys.stderr); sys.exit(3)
out({{"type": "assistant", "message": {{"content": [{{"type": "tool_use", "id": "t1", "name": "Bash", "input": {{"command": "rm -rf build"}}}}]}}}})
out({{"type": "user", "message": {{"content": [{{"type": "tool_result", "tool_use_id": "t1", "is_error": True, "content": "Permission to use Bash was denied"}}]}}}})
out({{"type": "assistant", "message": {{"content": [{{"type": "text", "text": "You said: " + text}}]}}}})
# like the real CLI, the conversation is saved to the session's transcript
import glob
for f in glob.glob({home!r} + "/projects/*/" + sid + ".jsonl"):
    with open(f, "a") as t:
        t.write(json.dumps({{"type": "user", "origin": {{"kind": "human"}}, "message": {{"role": "user", "content": text}}}}) + "\n")
        t.write(json.dumps({{"type": "assistant", "message": {{"model": "m", "content": [{{"type": "text", "text": "You said: " + text}}]}}}}) + "\n")
out({{"type": "result", "subtype": "success", "is_error": False, "total_cost_usd": 0.01, "duration_ms": 5, "num_turns": 2,
     "permission_denials": [{{"tool_name": "Bash"}}]}})
'''


def _line(**r):
    return json.dumps(r) + "\n"


def _transcript(cwd, sid, name=None, extra=""):
    rows = [
        _line(type="user", cwd=str(cwd), sessionId=sid, origin={"kind": "human"}, timestamp="2026-10-20T01:00:00Z",
              message={"role": "user", "content": "fix the chart labels"}),
        _line(type="user", cwd=str(cwd), isMeta=True, message={"role": "user", "content": "<system-reminder>x"}),
        _line(type="assistant", cwd=str(cwd), timestamp="2026-10-20T01:00:05Z",
              message={"model": "m", "content": [{"type": "text", "text": "Looking at it."},
                                                 {"type": "tool_use", "id": "u1", "name": "Read",
                                                  "input": {"file_path": "/repo/web/chart.dart"}}]}),
        _line(type="user", cwd=str(cwd), message={"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "u1", "is_error": True, "content": "File does not exist"}]}),
        _line(type="assistant", cwd=str(cwd), message={"model": "m", "content": [{"type": "text", "text": "Done."}]}),
        _line(type="ai-title", aiTitle="Chart label fixes", sessionId=sid),
    ]
    if name:
        rows.append(_line(type="custom-title", customTitle=name, sessionId=sid))
    return "".join(rows) + extra


@pytest.fixture
def claude(tmp_path, monkeypatch):
    home = tmp_path / "claude-home"
    work = tmp_path / "work"
    work.mkdir()
    proj = home / "projects" / "-work"
    proj.mkdir(parents=True)
    (proj / f"{SID}.jsonl").write_text(_transcript(work, SID, name="Autobench main"))
    (proj / f"{OTHER}.jsonl").write_text(_transcript(work, OTHER))
    (proj / "not-a-session.jsonl").write_text("{}\n")
    (home / "sessions").mkdir()
    log = tmp_path / "calls.jsonl"
    fake = tmp_path / "claude"
    fake.write_text(FAKE.format(python=sys.executable, log=str(log), fork=FORK, home=str(home)))
    fake.chmod(0o755)
    monkeypatch.setenv("AUTOBENCH_CLAUDE_BIN", str(fake))
    monkeypatch.setenv("AUTOBENCH_SECRET_THING", "do-not-leak")
    from autobench.config import get_settings

    get_settings.cache_clear()
    claude_code._cache.clear()
    return {"home": home, "work": work, "log": log}


H = {"X-Autobench": "1"}


@pytest.fixture
def client():
    with TestClient(create_app()) as c:
        yield c


def _wait(pred, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


def _calls(claude):
    return [json.loads(l) for l in claude["log"].read_text().splitlines()] if claude["log"].exists() else []


def test_lists_sessions_with_names_titles_and_folders(claude, client):
    r = client.get("/api/claude/sessions", headers=H).json()["sessions"]
    assert {s["id"] for s in r} == {SID, OTHER}
    main = next(s for s in r if s["id"] == SID)
    assert main["name"] == "Autobench main" and main["title"] == "Chart label fixes"
    assert main["cwd"] == str(claude["work"]) and main["project"] == "work" and main["live"] is None
    assert [s["id"] for s in client.get("/api/claude/sessions", params={"q": "autobench"}, headers=H).json()["sessions"]] == [SID]


def test_routes_need_the_local_ui_header(claude, client):
    assert client.get("/api/claude/sessions").status_code == 403
    assert client.post(f"/api/claude/sessions/{SID}/messages", json={"text": "hi"}).status_code == 403


def test_resolves_by_id_prefix_and_name(claude):
    assert claude_code.resolve(SID) == SID
    assert claude_code.resolve("autobench MAIN") == SID
    assert claude_code.resolve("11111111-22") == SID
    with pytest.raises(claude_code.ClaudeError) as e:
        claude_code.resolve("nope")
    assert e.value.status == 404


def test_history_keeps_what_people_typed_and_the_tools_used(claude, client):
    r = client.get("/api/claude/sessions/Autobench main/messages", headers=H).json()
    msgs = r["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[0]["text"] == "fix the chart labels"
    parts = msgs[1]["parts"]
    assert parts[0] == {"kind": "text", "text": "Looking at it."}
    assert parts[1]["name"] == "Read" and parts[1]["summary"] == "/repo/web/chart.dart"
    assert parts[1]["error"] == "File does not exist"
    assert parts[2]["text"] == "Done."


def test_sending_resumes_the_session_and_streams_the_reply(claude, client):
    bus.history.clear()
    run = client.post(f"/api/claude/sessions/{SID}/messages", json={"text": "hello there", "mode": "acceptEdits"},
                      headers=H).json()
    assert run["state"] == "running" and run["session_id"] == SID
    assert _wait(lambda: any(m["type"] == "claude.run" and m["data"]["state"] == "done" for m in bus.history))
    call = _calls(claude)[-1]
    assert call["text"] == "hello there", "the prompt goes on stdin, not the command line"
    a = call["args"]
    assert a[:3] == ["-p", "--resume", SID] and "hello there" not in a
    assert a[a.index("--permission-prompts") + 1] == "none" and a[a.index("--permission-mode") + 1] == "acceptEdits"
    events = [m["data"] for m in bus.history if m["type"] == "claude.event"]
    assert [e["kind"] for e in events] == ["tool", "tool_error", "text"]
    assert events[0]["summary"] == "rm -rf build" and events[2]["text"] == "You said: hello there"
    done = next(m["data"] for m in bus.history if m["type"] == "claude.run" and m["data"]["state"] == "done")
    assert done["result"]["denied"] == ["Bash"] and done["result"]["cost_usd"] == 0.01


def test_the_agent_does_not_get_this_apps_environment(claude, client, tmp_path):
    env_dump = tmp_path / "env.json"
    fake = tmp_path / "claude"
    fake.write_text(fake.read_text().replace("args = sys.argv[1:]",
                                            f"import os; open({str(env_dump)!r}, 'w').write(json.dumps(dict(os.environ)))\nargs = sys.argv[1:]"))
    claude_code.send(SID, "hi")
    assert _wait(lambda: not claude_code.runs.active())
    env = json.loads(env_dump.read_text())
    assert not [k for k in env if k.startswith(("AUTOBENCH_", "ALPACA_"))]


def test_rejects_unsafe_modes_and_a_second_message_while_busy(claude, client):
    r = client.post(f"/api/claude/sessions/{SID}/messages", json={"text": "x", "mode": "bypassPermissions"}, headers=H)
    assert r.status_code == 400
    claude_code.send(SID, "SLOW please")
    try:
        r = client.post(f"/api/claude/sessions/{SID}/messages", json={"text": "again"}, headers=H)
        assert r.status_code == 409 and r.json()["code"] == "busy"
        assert client.get(f"/api/claude/sessions/{SID}", headers=H).json()["running"] is True
    finally:
        assert client.post(f"/api/claude/sessions/{SID}/stop", headers=H).json()["stopped"] == 1
    assert _wait(lambda: not claude_code.runs.active())


def test_a_session_open_in_a_terminal_is_only_continued_as_a_copy(claude, client):
    (claude["home"] / "sessions" / "123.json").write_text(json.dumps({"pid": os.getpid(), "sessionId": SID, "name": "x"}))
    assert client.get(f"/api/claude/sessions/{SID}", headers=H).json()["live"]["pid"] == os.getpid()
    r = client.post(f"/api/claude/sessions/{SID}/messages", json={"text": "hi"}, headers=H)
    assert r.status_code == 409 and r.json()["code"] == "live"
    bus.history.clear()
    run = client.post(f"/api/claude/sessions/{SID}/messages", json={"text": "hi", "fork": True}, headers=H).json()
    assert run["forked_from"] == SID and run["session_id"] is None
    assert _wait(lambda: any(m["type"] == "claude.run" and m["data"]["state"] == "done" for m in bus.history))
    assert "--fork-session" in _calls(claude)[-1]["args"]
    done = next(m["data"] for m in bus.history if m["type"] == "claude.run" and m["data"]["state"] == "done")
    assert done["session_id"] == FORK


def test_new_session_in_a_folder_and_failures_are_reported(claude, client):
    bus.history.clear()
    run = client.post("/api/claude/sessions", json={"cwd": str(claude["work"]), "text": "FAIL now", "name": "Probe"},
                      headers=H).json()
    a = _calls(claude)[-1]["args"] if _wait(lambda: _calls(claude)) else []
    assert "--session-id" in a and a[a.index("--name") + 1] == "Probe"
    assert _wait(lambda: any(m["type"] == "claude.run" and m["data"]["state"] == "error" for m in bus.history))
    err = next(m["data"] for m in bus.history if m["type"] == "claude.run" and m["data"]["state"] == "error")
    assert err["run_id"] == run["run_id"] and "boom" in err["error"]
    assert client.post("/api/claude/sessions", json={"cwd": "relative/dir", "text": "x"}, headers=H).status_code == 400


def test_missing_claude_command(client, tmp_path):
    r = client.get("/api/claude/status", headers=H).json()
    assert r["available"] is False
    (tmp_path / "claude-home" / "projects" / "-w").mkdir(parents=True)
    (tmp_path / "claude-home" / "projects" / "-w" / f"{SID}.jsonl").write_text(_transcript(tmp_path, SID))
    r = client.post(f"/api/claude/sessions/{SID}/messages", json={"text": "x"}, headers=H)
    assert r.status_code == 503 and r.json()["code"] == "missing"


def test_websocket_refuses_other_sites(client):
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws", headers={"origin": "https://evil.example"}) as ws:
            ws.receive_json()
    with client.websocket_connect("/ws", headers={"origin": "http://127.0.0.1:8000"}) as ws:
        assert ws.receive_json()["type"] == "hello"
