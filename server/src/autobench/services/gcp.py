"""Google Cloud access shared by every Autobench service that talks to the Vral Altos project:
credentials, Firestore (REST) and Cloud Storage (JSON API). No Google client libraries: plain httpx.

Credentials, in order:
- `FIRESTORE_EMULATOR_HOST` set: the Firestore emulator, no token (development and tests).
- `GOOGLE_APPLICATION_CREDENTIALS` / the gcloud application-default login (an `authorized_user` file):
  the operator on the Mac.
- the metadata server: the service account on Cloud Run.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

log = logging.getLogger(__name__)

TOKEN_URL = "https://oauth2.googleapis.com/token"
METADATA_TOKEN = "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"
FIRESTORE = "https://firestore.googleapis.com/v1"
STORAGE = "https://storage.googleapis.com"

_lock = threading.Lock()
_token: dict[str, Any] = {"value": None, "expiry": 0.0, "user": False}


class GcpError(RuntimeError):
    pass


# ------------------------------------------------------------------------------ credentials

def adc_path() -> Path:
    env = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if env:
        return Path(env)
    return Path.home() / ".config" / "gcloud" / "application_default_credentials.json"


def emulator_host() -> str | None:
    return os.environ.get("FIRESTORE_EMULATOR_HOST") or None


def access_token(client: httpx.Client) -> str:
    """An OAuth access token (cached until a minute before it expires)."""
    with _lock:
        if _token["value"] and time.time() < _token["expiry"] - 60:
            return _token["value"]
        path = adc_path()
        if path.exists():
            creds = json.loads(path.read_text())
            if creds.get("type") != "authorized_user":
                raise GcpError(f"Unsupported credential type {creds.get('type')!r}; expected a user login.")
            r = client.post(TOKEN_URL, data={
                "grant_type": "refresh_token", "refresh_token": creds["refresh_token"],
                "client_id": creds["client_id"], "client_secret": creds["client_secret"],
            })
        else:
            try:
                r = client.get(METADATA_TOKEN, headers={"Metadata-Flavor": "Google"}, timeout=5)
            except httpx.HTTPError as e:
                raise GcpError("No Google login: run `gcloud auth application-default login` "
                               "(or run on Cloud Run with a service account).") from e
        if r.status_code >= 400:
            raise GcpError(f"Google sign-in failed ({r.status_code}): {r.text[:200]}")
        body = r.json()
        _token["user"] = path.exists()  # a user login bills a quota project; a service account does not need one
        _token["value"] = body["access_token"]
        _token["expiry"] = time.time() + int(body.get("expires_in", 3600))
        return _token["value"]


def reset_token() -> None:
    with _lock:
        _token.update(value=None, expiry=0.0, user=False)


def headers(client: httpx.Client, project: str) -> dict[str, str]:
    if emulator_host():
        return {"Authorization": "Bearer owner"}
    token = access_token(client)
    out = {"Authorization": f"Bearer {token}"}
    if _token.get("user", True):
        out["x-goog-user-project"] = project
    return out


# ------------------------------------------------------------------------------ Firestore values

def encode(value: Any) -> dict:
    """A JSON-like value as a Firestore REST `Value`."""
    if value is None:
        return {"nullValue": None}
    if isinstance(value, bool):
        return {"booleanValue": value}
    if isinstance(value, int):
        return {"integerValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    if isinstance(value, str):
        return {"stringValue": value}
    if isinstance(value, datetime):
        v = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return {"timestampValue": v.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")}
    if isinstance(value, dict):
        return {"mapValue": {"fields": {str(k): encode(v) for k, v in value.items()}}}
    if isinstance(value, (list, tuple)):
        return {"arrayValue": {"values": [encode(v) for v in value]}}
    return {"stringValue": str(value)}


def decode(value: dict) -> Any:
    """A Firestore REST `Value` as plain Python (timestamps stay ISO strings)."""
    if "nullValue" in value:
        return None
    if "booleanValue" in value:
        return bool(value["booleanValue"])
    if "integerValue" in value:
        return int(value["integerValue"])
    if "doubleValue" in value:
        return float(value["doubleValue"])
    for k in ("stringValue", "timestampValue", "referenceValue", "bytesValue"):
        if k in value:
            return value[k]
    if "mapValue" in value:
        return {k: decode(v) for k, v in (value["mapValue"].get("fields") or {}).items()}
    if "arrayValue" in value:
        return [decode(v) for v in (value["arrayValue"].get("values") or [])]
    if "geoPointValue" in value:
        return value["geoPointValue"]
    return None


def document(fields: dict) -> dict:
    return {"fields": {k: encode(v) for k, v in fields.items()}}


# ------------------------------------------------------------------------------ Firestore calls

def firestore_base(project: str, database: str) -> str:
    host = emulator_host()
    root = f"http://{host}/v1" if host else FIRESTORE
    return f"{root}/projects/{project}/databases/{database}/documents"


def doc_name(project: str, database: str, path: str) -> str:
    return f"projects/{project}/databases/{database}/documents/{path}"


def list_documents(client: httpx.Client, project: str, database: str, collection: str,
                   limit: int = 100_000) -> list[dict]:
    """Every document of a collection as {'id', 'fields' (raw Values)}; [] when it does not exist."""
    url = f"{firestore_base(project, database)}/{collection}"
    out: list[dict] = []
    page: str | None = None
    while len(out) < limit:
        r = client.get(url, params={"pageSize": 300, **({"pageToken": page} if page else {})},
                       headers=headers(client, project))
        if r.status_code == 404:
            return out
        if r.status_code >= 400:
            raise GcpError(f"Firestore read of {collection} failed ({r.status_code}): {_message(r)}")
        body = r.json()
        for d in body.get("documents") or []:
            out.append({"id": d["name"].rsplit("/", 1)[-1], "fields": d.get("fields") or {}})
        page = body.get("nextPageToken")
        if not page:
            break
    return out


def get_document(client: httpx.Client, project: str, database: str, path: str) -> dict | None:
    """One document's raw fields, or None when it does not exist."""
    r = client.get(f"{firestore_base(project, database)}/{path}", headers=headers(client, project))
    if r.status_code == 404:
        return None
    if r.status_code >= 400:
        raise GcpError(f"Firestore read of {path} failed ({r.status_code}): {_message(r)}")
    return r.json().get("fields") or {}


def commit(client: httpx.Client, project: str, database: str, writes: list[dict]) -> None:
    """Atomic commits of at most 500 writes each."""
    url = f"{firestore_base(project, database)}:commit"
    for i in range(0, len(writes), 500):
        r = client.post(url, json={"writes": writes[i:i + 500]}, headers=headers(client, project))
        if r.status_code >= 400:
            raise GcpError(f"Firestore commit failed ({r.status_code}): {_message(r)}")


def _message(r: httpx.Response) -> str:
    try:
        return r.json()["error"]["message"]
    except Exception:  # noqa: BLE001
        return r.text[:200]


# ------------------------------------------------------------------------------ Cloud Storage

def gcs_get(client: httpx.Client, project: str, bucket: str, name: str) -> bytes | None:
    r = client.get(f"{STORAGE}/storage/v1/b/{bucket}/o/{quote(name, safe='')}", params={"alt": "media"},
                   headers=headers(client, project))
    if r.status_code == 404:
        return None
    if r.status_code >= 400:
        raise GcpError(f"Storage read of {name} failed ({r.status_code}): {_message(r)}")
    return r.content


def gcs_put(client: httpx.Client, project: str, bucket: str, name: str, data: bytes,
            content_type: str = "application/octet-stream") -> None:
    r = client.post(f"{STORAGE}/upload/storage/v1/b/{bucket}/o", params={"uploadType": "media", "name": name},
                    content=data, headers={**headers(client, project), "Content-Type": content_type})
    if r.status_code >= 400:
        raise GcpError(f"Storage write of {name} failed ({r.status_code}): {_message(r)}")
