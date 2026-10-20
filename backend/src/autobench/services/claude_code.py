"""Chat with Claude Code sessions on this Mac from the web UI.

Sessions are read from Claude Code's own files (~/.claude/projects/<project>/<session id>.jsonl, and
~/.claude/sessions/<pid>.json for the ones open in a terminal). Sending a message runs
`claude -p --resume <id> --output-format stream-json` in the session's directory; its output is
published on the event bus as `claude.event` / `claude.run` so every open tab follows along.

Permission prompts cannot be answered from the app: anything that would ask is denied (the session's
permission mode still allows what it allows). A session open in a terminal is not written to by a second
process; the app offers to continue a copy (--fork-session) instead."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from ..config import get_settings
from .events import bus

log = logging.getLogger(__name__)

MODES = ("default", "acceptEdits", "plan")  # the ones the app may choose; never bypassPermissions
_HEAD = 64 * 1024
_TAIL = 512 * 1024
_HISTORY_TAIL = 6 * 1024 * 1024
_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
# variables of this app (keys, account settings) are not handed to the agent's shell
_SCRUB = ("AUTOBENCH_", "ALPACA_", "APCA_", "IBKR_", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")


class ClaudeError(Exception):
    def __init__(self, message: str, status: int = 400, code: str | None = None):
        super().__init__(message)
        self.status = status
        self.code = code


def home() -> Path:
    s = get_settings().autobench_claude_home
    return Path(s).expanduser() if s else Path.home() / ".claude"


def binary() -> str | None:
    s = get_settings().autobench_claude_bin
    if s:
        return s if Path(s).expanduser().is_file() else None
    found = shutil.which("claude")
    if found:
        return found
    # a server started from a launch agent or app may not have the shell's PATH
    for p in (Path.home() / ".local/bin/claude", Path("/opt/homebrew/bin/claude"), Path("/usr/local/bin/claude")):
        if p.is_file():
            return str(p)
    return None


_version: tuple[str, str | None] | None = None


def status() -> dict[str, Any]:
    global _version
    enabled = get_settings().autobench_claude_chat
    path = binary() if enabled else None
    version = None
    if path:
        if _version is None or _version[0] != path:
            try:
                out = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=15)
                _version = (path, out.stdout.strip() or None)
            except (OSError, subprocess.SubprocessError):
                _version = (path, None)
        version = _version[1]
    return {"enabled": enabled, "available": bool(path), "binary": path, "version": version, "modes": list(MODES),
            "running": [r.public() for r in runs.active()]}


# ------------------------------------------------------------------------------------------- sessions


def _read_edges(path: Path, size: int) -> tuple[bytes, bytes]:
    with path.open("rb") as f:
        head = f.read(_HEAD)
        if size <= _HEAD + _TAIL:
            return head + f.read(), b""
        f.seek(size - _TAIL)
        return head, f.read()


def _records(chunk: bytes, partial_start: bool = False):
    lines = chunk.split(b"\n")
    if partial_start:
        lines = lines[1:]
    for line in lines:
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if isinstance(r, dict):
            yield r


def _prompt_text(r: dict) -> str | None:
    """The text a person typed, from a transcript `user` record (None for tool results, reminders...)."""
    if r.get("isMeta") or r.get("isSidechain"):
        return None
    content = (r.get("message") or {}).get("content")
    if isinstance(content, list):
        content = "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    if not isinstance(content, str) or not content.strip():
        return None
    origin = r.get("origin") or {}
    if origin and origin.get("kind") != "human":
        return None
    return content


_cache: dict[str, tuple[float, int, dict]] = {}


def _summarize(path: Path) -> dict | None:
    st = path.stat()
    hit = _cache.get(str(path))
    if hit and hit[0] == st.st_mtime and hit[1] == st.st_size:
        return hit[2]
    head, tail = _read_edges(path, st.st_size)
    info: dict[str, Any] = {"id": path.stem, "cwd": None, "first_prompt": None, "name": None, "title": None,
                            "last_prompt": None, "updated": st.st_mtime, "size": st.st_size}
    for r in _records(head):
        if info["cwd"] is None and r.get("cwd"):
            info["cwd"] = r["cwd"]
        if info["first_prompt"] is None and r.get("type") == "user":
            text = _prompt_text(r)
            if text and not text.startswith("<"):
                info["first_prompt"] = text
        _titles(info, r)
    for r in _records(tail, partial_start=True):
        if info["cwd"] is None and r.get("cwd"):
            info["cwd"] = r["cwd"]
        _titles(info, r)
    _cache[str(path)] = (st.st_mtime, st.st_size, info)
    return info


def _titles(info: dict, r: dict) -> None:
    t = r.get("type")
    if t in ("custom-title", "agent-name"):
        info["name"] = r.get("customTitle") or r.get("agentName") or info["name"]
    elif t == "ai-title":
        info["title"] = r.get("aiTitle") or info["title"]
    elif t == "last-prompt":
        info["last_prompt"] = r.get("lastPrompt") or info["last_prompt"]


def _live() -> dict[str, dict]:
    """Sessions open in a Claude Code process right now (other than the app's own runs), by session id."""
    out: dict[str, dict] = {}
    ours = runs.pids()
    for f in (home() / "sessions").glob("*.json"):
        try:
            d = json.loads(f.read_text())
            pid = int(d["pid"])
            os.kill(pid, 0)
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if pid in ours or not d.get("sessionId"):
            continue
        out[d["sessionId"]] = {"pid": pid, "name": d.get("name"), "status": d.get("status"), "kind": d.get("kind")}
    return out


def _files() -> list[Path]:
    root = home() / "projects"
    return [p for p in root.glob("*/*.jsonl") if _ID.match(p.stem)] if root.is_dir() else []


def _public(info: dict, live: dict[str, dict]) -> dict:
    lv = live.get(info["id"])
    name = info["name"] or (lv or {}).get("name")
    return {
        "id": info["id"],
        "name": name,
        "title": info["title"] or _clip(info["first_prompt"] or info["last_prompt"] or "", 80) or None,
        "cwd": info["cwd"],
        "project": Path(info["cwd"]).name if info["cwd"] else None,
        "updated": datetime.fromtimestamp(info["updated"]).astimezone().isoformat(timespec="seconds"),
        "size": info["size"],
        "live": lv,
        "running": runs.running(info["id"]),
    }


def sessions(q: str | None = None, limit: int = 100) -> list[dict]:
    live = _live()
    infos = []
    for p in _files():
        try:
            info = _summarize(p)
        except OSError:
            continue
        if info:
            infos.append(info)
    infos.sort(key=lambda i: i["updated"], reverse=True)
    out = [_public(i, live) for i in infos]
    if q:
        ql = q.lower()
        out = [s for s in out if any(ql in (s.get(k) or "").lower() for k in ("id", "name", "title", "cwd"))]
    return out[:limit]


def _path(session_id: str) -> Path:
    if not _ID.match(session_id):
        raise ClaudeError("Not a session id", 400)
    for p in (home() / "projects").glob(f"*/{session_id}.jsonl"):
        return p
    raise ClaudeError(f"No Claude Code session {session_id}", 404)


def resolve(ref: str) -> str:
    """A session id from an id, an id prefix (6+ characters) or a session name."""
    ref = ref.strip()
    if not ref:
        raise ClaudeError("Name or id required")
    if _ID.match(ref.lower()):
        _path(ref.lower())
        return ref.lower()
    all_ = sessions(limit=100_000)
    rl = ref.lower()
    for test in (lambda s: (s["name"] or "").lower() == rl,
                 lambda s: len(rl) >= 6 and s["id"].startswith(rl),
                 lambda s: rl in (s["name"] or "").lower()):
        hits = [s for s in all_ if test(s)]
        if len(hits) == 1:
            return hits[0]["id"]
        if len(hits) > 1:
            names = ", ".join(f"{s['name'] or s['title']} ({s['id'][:8]})" for s in hits[:5])
            raise ClaudeError(f"'{ref}' matches {len(hits)} sessions: {names}", 409, "ambiguous")
    raise ClaudeError(f"No Claude Code session named '{ref}'", 404)


def session(ref: str) -> dict:
    sid = resolve(ref)
    return _public(_summarize(_path(sid)), _live())


# ------------------------------------------------------------------------------------------- history


def _clip(s: str, n: int) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


def tool_summary(name: str, args: Any) -> str:
    if not isinstance(args, dict):
        return ""
    for key in ("command", "file_path", "path", "pattern", "url", "query", "description", "prompt", "skill"):
        v = args.get(key)
        if isinstance(v, str) and v:
            return _clip(v, 160)
    for v in args.values():
        if isinstance(v, str) and v:
            return _clip(v, 160)
    return ""


def _result_text(block: dict) -> str:
    c = block.get("content")
    if isinstance(c, list):
        c = " ".join(b.get("text", "") for b in c if isinstance(b, dict))
    return _clip(c if isinstance(c, str) else "", 300)


def messages(ref: str, limit: int = 60) -> dict:
    """The recent conversation: person's messages and Claude's replies with the tools it used."""
    sid = resolve(ref)
    path = _path(sid)
    size = path.stat().st_size
    with path.open("rb") as f:
        if size > _HISTORY_TAIL:
            f.seek(size - _HISTORY_TAIL)
        chunk = f.read()
    out: list[dict] = []
    tools: dict[str, dict] = {}
    for r in _records(chunk, partial_start=size > _HISTORY_TAIL):
        t = r.get("type")
        if r.get("isSidechain"):
            continue
        if t == "user":
            content = (r.get("message") or {}).get("content")
            if isinstance(content, list):
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("is_error"):
                        tool = tools.get(b.get("tool_use_id"))
                        if tool is not None:
                            tool["error"] = _result_text(b)
            text = _prompt_text(r)
            if text is None:
                continue
            if text.startswith("This session is being continued from a previous conversation"):
                out.append({"role": "note", "text": "Earlier conversation was summarised", "at": r.get("timestamp")})
            else:
                out.append({"role": "user", "text": text, "at": r.get("timestamp")})
        elif t == "assistant":
            msg = r.get("message") or {}
            if msg.get("model") == "<synthetic>":
                continue
            for b in msg.get("content") or []:
                if not isinstance(b, dict):
                    continue
                last = out[-1] if out and out[-1]["role"] == "assistant" else None
                if last is None:
                    last = {"role": "assistant", "parts": [], "at": r.get("timestamp")}
                    out.append(last)
                if b.get("type") == "text" and b.get("text", "").strip():
                    last["parts"].append({"kind": "text", "text": b["text"]})
                elif b.get("type") == "tool_use":
                    tool = {"kind": "tool", "name": b.get("name"), "summary": tool_summary(b.get("name", ""), b.get("input"))}
                    tools[b.get("id")] = tool
                    last["parts"].append(tool)
            if out and out[-1]["role"] == "assistant" and not out[-1]["parts"]:
                out.pop()
    return {"session": _public(_summarize(path), _live()), "messages": out[-limit:], "truncated": len(out) > limit}


# ------------------------------------------------------------------------------------------- runs


@dataclass
class Run:
    id: str
    session_id: str | None  # None until a new session reports its id
    cwd: str
    text: str
    started: float = field(default_factory=time.time)
    proc: subprocess.Popen | None = None
    state: str = "running"  # running | done | error | stopped
    error: str | None = None
    result: dict | None = None
    forked_from: str | None = None

    def public(self) -> dict:
        return {"run_id": self.id, "session_id": self.session_id, "state": self.state, "error": self.error,
                "started": self.started, "result": self.result, "forked_from": self.forked_from}


class Runs:
    def __init__(self):
        self._runs: dict[str, Run] = {}
        self._lock = threading.Lock()

    def active(self) -> list[Run]:
        with self._lock:
            return [r for r in self._runs.values() if r.state == "running"]

    def running(self, session_id: str) -> bool:
        return any(r.session_id == session_id or r.forked_from == session_id and r.session_id is None
                   for r in self.active())

    def pids(self) -> set[int]:
        return {r.proc.pid for r in self.active() if r.proc}

    def add(self, run: Run) -> None:
        with self._lock:
            self._runs[run.id] = run
            done = [k for k, r in self._runs.items() if r.state != "running"]
            for k in done[:-20]:
                del self._runs[k]

    def get(self, run_id: str) -> Run | None:
        with self._lock:
            return self._runs.get(run_id)


runs = Runs()


def _env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if not k.startswith(_SCRUB)}


def _check(text: str, mode: str | None) -> str:
    if not get_settings().autobench_claude_chat:
        raise ClaudeError("Claude Code chat is turned off (AUTOBENCH_CLAUDE_CHAT=0)", 403, "disabled")
    path = binary()
    if not path:
        raise ClaudeError("Claude Code (the `claude` command) was not found; set AUTOBENCH_CLAUDE_BIN", 503, "missing")
    if not text.strip():
        raise ClaudeError("Message is empty")
    if mode is not None and mode not in MODES:
        raise ClaudeError(f"Permission mode must be one of {', '.join(MODES)}")
    return path


def send(ref: str, text: str, mode: str | None = None, fork: bool = False) -> dict:
    """Sends [text] to an existing session. A session open in a terminal needs fork=True (continue a copy)."""
    path = _check(text, mode)
    sid = resolve(ref)
    info = _summarize(_path(sid))
    if runs.running(sid):
        raise ClaudeError("Claude is still answering in this session", 409, "busy")
    live = _live().get(sid)
    if live and not fork:
        raise ClaudeError(f"This session is open in another Claude Code window (pid {live['pid']}). Close it there, "
                          "or continue a copy here.", 409, "live")
    cwd = info["cwd"]
    if not cwd or not Path(cwd).is_dir():
        raise ClaudeError(f"The session's folder no longer exists: {cwd}", 409, "cwd")
    args = [path, "-p", "--resume", sid, *(["--fork-session"] if fork else [])]
    run = Run(uuid.uuid4().hex[:12], None if fork else sid, cwd, text, forked_from=sid if fork else None)
    return _start(run, args, mode, text)


def create(cwd: str, text: str, name: str | None = None, mode: str | None = None) -> dict:
    """Starts a new session in folder [cwd]."""
    path = _check(text, mode)
    folder = Path(cwd).expanduser()
    if not folder.is_absolute() or not folder.is_dir():
        raise ClaudeError(f"Not a folder: {cwd}")
    sid = str(uuid.uuid4())
    args = [path, "-p", "--session-id", sid, *(["--name", name.strip()] if name and name.strip() else [])]
    return _start(Run(uuid.uuid4().hex[:12], sid, str(folder), text), args, mode, text)


def _start(run: Run, args: list[str], mode: str | None, text: str) -> dict:
    args += ["--output-format", "stream-json", "--verbose", "--permission-prompts", "none"]
    if mode:
        args += ["--permission-mode", mode]
    try:
        run.proc = subprocess.Popen(args, cwd=run.cwd, env=_env(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True, bufsize=1, start_new_session=True)
    except OSError as e:
        raise ClaudeError(f"Could not start Claude Code: {e}", 503) from e
    runs.add(run)
    # the prompt goes on stdin so it never shows in the process list
    threading.Thread(target=_feed, args=(run, text), daemon=True).start()
    threading.Thread(target=_pump, args=(run,), daemon=True).start()
    bus.publish("claude.run", {**run.public(), "text": text})
    return run.public()


def _feed(run: Run, text: str) -> None:
    try:
        assert run.proc and run.proc.stdin
        run.proc.stdin.write(text)
        run.proc.stdin.close()
    except (OSError, ValueError):
        pass


def _emit(run: Run, part: dict) -> None:
    bus.publish("claude.event", {"run_id": run.id, "session_id": run.session_id, **part})


def handle_line(run: Run, msg: dict) -> None:
    """One stream-json message from the CLI."""
    t = msg.get("type")
    if t == "system" and msg.get("subtype") == "init" and msg.get("session_id"):
        if run.session_id != msg["session_id"]:
            run.session_id = msg["session_id"]
            bus.publish("claude.run", run.public())
    elif t == "assistant":
        for b in (msg.get("message") or {}).get("content") or []:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text" and b.get("text", "").strip():
                _emit(run, {"kind": "text", "text": b["text"]})
            elif b.get("type") == "tool_use":
                _emit(run, {"kind": "tool", "id": b.get("id"), "name": b.get("name"),
                            "summary": tool_summary(b.get("name", ""), b.get("input"))})
    elif t == "user":
        for b in (msg.get("message") or {}).get("content") or []:
            if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("is_error"):
                _emit(run, {"kind": "tool_error", "id": b.get("tool_use_id"), "text": _result_text(b)})
    elif t == "result":
        run.result = {"is_error": bool(msg.get("is_error")), "cost_usd": msg.get("total_cost_usd"),
                      "duration_ms": msg.get("duration_ms"), "turns": msg.get("num_turns"),
                      "denied": [d.get("tool_name") for d in msg.get("permission_denials") or [] if isinstance(d, dict)]}
        if msg.get("is_error"):
            run.error = _clip(str(msg.get("result") or msg.get("subtype") or "Claude Code reported an error"), 500)


def _pump(run: Run) -> None:
    proc = run.proc
    assert proc and proc.stdout and proc.stderr
    for line in proc.stdout:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        try:
            handle_line(run, msg)
        except Exception:  # never let one odd message stop the stream
            log.exception("claude stream message")
    err = proc.stderr.read()
    code = proc.wait()
    if run.state == "running":
        if code != 0 and not run.error:
            run.error = _clip(err.strip() or f"Claude Code exited with code {code}", 500)
        run.state = "error" if run.error else "done"
    bus.publish("claude.run", run.public())


def stop(ref: str) -> dict:
    sid = resolve(ref)
    hit = [r for r in runs.active() if r.session_id == sid or r.forked_from == sid]
    if not hit:
        raise ClaudeError("Nothing is running in this session", 409)
    for r in hit:
        r.state = "stopped"
        if r.proc and r.proc.poll() is None:
            try:
                os.killpg(r.proc.pid, 15)
            except OSError:
                r.proc.terminate()
    return {"stopped": len(hit)}
