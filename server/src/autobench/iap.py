"""Identity on Cloud Run: Identity-Aware Proxy in front of the service admits only the operator, and every
request it forwards carries a signed `x-goog-iap-jwt-assertion`. Autobench verifies that header itself
(ES256 against Google's published keys; issuer, audience, expiry) and checks the email against the owners,
so a request that somehow reached the container without passing IAP gets nothing. Configured by
AUTOBENCH_IAP_AUDIENCE (empty on the Mac: local servers bind to 127.0.0.1 and keep their loopback checks).
"""

from __future__ import annotations

import hmac
import json
import secrets
import threading
import time
from pathlib import Path

import httpx
import jwt

from .config import get_settings

KEYS_URL = "https://www.gstatic.com/iap/verify/public_key-jwk"
ISSUER = "https://cloud.google.com/iap"
HEADER = "x-goog-iap-jwt-assertion"

_lock = threading.Lock()
_keys: dict = {"at": 0.0, "set": None}


INTERNAL_HEADER = "x-autobench-internal"
# A random token for processes inside the container (Claude Code → Autobench's MCP), made at start and written
# only into the workspace's .mcp.json (write_workspace_mcp). Never leaves the container.
_internal_token = secrets.token_urlsafe(32)


def internal_ok(value: str | None) -> bool:
    return bool(value) and hmac.compare_digest(value, _internal_token)


def write_workspace_mcp(workspace: Path, port: int) -> Path:
    """Claude Code sessions started in the workspace use Autobench's MCP with the internal token."""
    path = workspace / ".mcp.json"
    path.write_text(json.dumps({"mcpServers": {"autobench": {
        "type": "http", "url": f"http://127.0.0.1:{port}/mcp", "headers": {INTERNAL_HEADER: _internal_token}}}}, indent=2))
    path.chmod(0o600)
    return path


class Denied(Exception):
    pass


def enabled() -> bool:
    return bool(get_settings().autobench_iap_audience)


def owners() -> set[str]:
    return {e.strip().lower() for e in get_settings().autobench_owner_emails.split(",") if e.strip()}


def _jwks(fetch=None) -> jwt.PyJWKSet:
    with _lock:
        if _keys["set"] is None or time.time() - _keys["at"] > 3600:
            data = (fetch or (lambda: httpx.get(KEYS_URL, timeout=10).json()))()
            _keys.update(set=jwt.PyJWKSet.from_dict(data), at=time.time())
        return _keys["set"]


def verify(assertion: str | None, fetch=None) -> str:
    """The verified owner email, or Denied."""
    if not assertion:
        raise Denied("No IAP identity")
    try:
        kid = jwt.get_unverified_header(assertion).get("kid")
        key = next((k for k in _jwks(fetch).keys if k.key_id == kid), None)
        if key is None:
            raise Denied("Unknown IAP signing key")
        claims = jwt.decode(assertion, key.key, algorithms=["ES256"], audience=get_settings().autobench_iap_audience,
                            issuer=ISSUER, options={"require": ["exp", "iat", "aud", "iss", "email"]})
    except jwt.PyJWTError as e:
        raise Denied(f"Invalid IAP identity: {e}") from e
    email = str(claims.get("email", "")).lower()
    if email not in owners():
        raise Denied("Not an owner of this Autobench")
    return email
