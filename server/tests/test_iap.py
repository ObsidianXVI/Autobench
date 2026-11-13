"""Cloud Run behind IAP: every request needs a valid IAP assertion of an owner (signature, issuer, audience,
expiry, email); trading writes and the WebSocket accept the service's own host only for a verified owner."""

import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

from autobench import iap
from autobench.app import create_app
from autobench.config import get_settings

AUD = "/projects/123/locations/us-central1/services/autobench"


@pytest.fixture
def keys(monkeypatch):
    priv = ec.generate_private_key(ec.SECP256R1())
    other = ec.generate_private_key(ec.SECP256R1())
    jwk = jwt.algorithms.ECAlgorithm.to_jwk(priv.public_key(), as_dict=True) | {"kid": "k1", "alg": "ES256"}
    iap._keys.update(at=time.time(), set=jwt.PyJWKSet.from_dict({"keys": [jwk]}))
    monkeypatch.setenv("AUTOBENCH_IAP_AUDIENCE", AUD)
    monkeypatch.setenv("AUTOBENCH_OWNER_EMAILS", "owner@example.com")
    get_settings.cache_clear()
    yield priv, other
    iap._keys.update(at=0.0, set=None)


def token(key, email="owner@example.com", aud=AUD, iss=iap.ISSUER, exp=600, kid="k1"):
    now = int(time.time())
    return jwt.encode({"email": email, "aud": aud, "iss": iss, "iat": now, "exp": now + exp}, key,
                      algorithm="ES256", headers={"kid": kid})


def test_only_a_verified_owner_gets_in(keys):
    priv, other = keys
    host = {"host": "autobench-xyz.a.run.app"}
    with TestClient(create_app(), base_url="https://autobench-xyz.a.run.app") as c:
        assert c.get("/api/health").status_code == 200  # the container's probe
        assert c.get("/api/watchlists").status_code == 401
        for bad in (token(other), token(priv, email="someone@else.com"), token(priv, aud="/projects/9/x"),
                    token(priv, iss="https://evil"), token(priv, exp=-10), "garbage"):
            r = c.get("/api/watchlists", headers={iap.HEADER: bad})
            assert r.status_code == 401, bad
        ok = {iap.HEADER: token(priv)}
        assert c.get("/api/watchlists", headers=ok).status_code == 200
        # trading writes: the service's own host is fine for a verified owner, the header is still required
        r = c.post("/api/trading/halt", headers={**ok, **host, "x-autobench": "1"}, json={"reason": "t"})
        assert r.status_code != 403, r.text
        assert c.post("/api/trading/halt", headers={iap.HEADER: token(priv)}, json={}).status_code == 403
        c.post("/api/trading/resume", headers={**ok, "x-autobench": "1"})
        # the WebSocket: refused without an identity, accepted with one (same-origin)
        with pytest.raises(Exception):
            with c.websocket_connect("/ws") as ws:
                ws.receive_json()
        same_origin = {**ok, **host, "origin": "https://autobench-xyz.a.run.app"}
        with c.websocket_connect("/ws", headers=same_origin) as ws:
            assert ws.receive_json()["type"] == "hello"
        # another site's page, even with the owner's identity, cannot listen
        with pytest.raises(Exception):
            with c.websocket_connect("/ws", headers={**ok, **host, "origin": "https://evil.example"}) as ws:
                ws.receive_json()


def test_off_cloud_nothing_changes(monkeypatch):
    monkeypatch.setenv("AUTOBENCH_IAP_AUDIENCE", "")
    get_settings.cache_clear()
    with TestClient(create_app()) as c:
        assert c.get("/api/watchlists").status_code == 200
        assert c.post("/api/trading/halt", json={}, headers={"x-autobench": "1"}).status_code != 403
        c.post("/api/trading/resume", headers={"x-autobench": "1"})


def test_claude_in_the_container_uses_the_internal_token(keys, tmp_path):
    import json as _json

    path = iap.write_workspace_mcp(tmp_path, 8080)
    cfg = _json.loads(path.read_text())["mcpServers"]["autobench"]
    assert cfg["url"] == "http://127.0.0.1:8080/mcp" and oct(path.stat().st_mode & 0o777) == "0o600"
    with TestClient(create_app()) as c:
        assert c.get("/api/watchlists", headers=cfg["headers"]).status_code == 200
        assert c.get("/api/watchlists", headers={iap.INTERNAL_HEADER: "guess"}).status_code == 401
        assert c.get("/api/watchlists", headers={iap.INTERNAL_HEADER: ""}).status_code == 401


def test_credentials_file_fills_the_environment(tmp_path, monkeypatch):
    import json as _json

    from autobench.config import load_credentials_file

    f = tmp_path / "c.json"
    f.write_text(_json.dumps({"IBKR_USERNAME": "me", "ALPACA_API_KEY_ID": "kid", "OTHER": "x"}))
    monkeypatch.delenv("IBKR_USERNAME", raising=False)
    monkeypatch.setenv("ALPACA_API_KEY_ID", "already")
    loaded = load_credentials_file(str(f))
    import os

    assert loaded == ["IBKR_USERNAME"] and os.environ["IBKR_USERNAME"] == "me"
    assert os.environ["ALPACA_API_KEY_ID"] == "already" and "OTHER" not in os.environ
    monkeypatch.delenv("IBKR_USERNAME")
    assert load_credentials_file(str(tmp_path / "missing.json")) == []
