"""How reference.py talks to Nasdaq and FRED from cloud networks (no network: the client and curl are faked)."""

import subprocess

import httpx
import pytest

from autobench.providers import reference
from autobench.providers.base import ProviderError


def _client(monkeypatch, handler):
    monkeypatch.setattr(reference, "_client", httpx.Client(transport=httpx.MockTransport(handler)))


def test_nasdaq_403_is_retried_with_curl(monkeypatch):
    _client(monkeypatch, lambda req: httpx.Response(403, text="denied"))
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=b'{"data": {"rows": []}}\n200', stderr=b"")

    monkeypatch.setattr(reference.shutil, "which", lambda name: "/usr/bin/curl")
    monkeypatch.setattr(reference.subprocess, "run", fake_run)
    r = reference._get("https://api.nasdaq.com/api/calendar/earnings", {"date": "2026-11-12"})
    assert r.json() == {"data": {"rows": []}}
    assert "https://api.nasdaq.com/api/calendar/earnings?date=2026-11-12" in calls[0]
    assert any(a.startswith("User-Agent: Mozilla") for a in calls[0])


def test_curl_refused_too_is_a_provider_error(monkeypatch):
    _client(monkeypatch, lambda req: httpx.Response(403))
    monkeypatch.setattr(reference.shutil, "which", lambda name: "/usr/bin/curl")
    monkeypatch.setattr(reference.subprocess, "run",
                        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout=b"denied\n403", stderr=b""))
    with pytest.raises(ProviderError, match="403"):
        reference._get("https://api.nasdaq.com/api/screener/stocks")


def test_other_hosts_do_not_fall_back_to_curl(monkeypatch):
    _client(monkeypatch, lambda req: httpx.Response(403))
    monkeypatch.setattr(reference.subprocess, "run", lambda *a, **k: pytest.fail("curl used"))
    with pytest.raises(ProviderError):
        reference._get("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies")


def test_fred_sends_a_plain_user_agent(monkeypatch):
    seen = {}

    def handler(req):
        seen["ua"] = req.headers["user-agent"]
        return httpx.Response(200, text="observation_date,DGS10\n2026-11-12,4.1\n")

    _client(monkeypatch, handler)
    df = reference.fred_series(["DGS10"])
    assert seen["ua"] == "Autobench/1.0" and float(df["DGS10"].iloc[0]) == 4.1


def test_fred_mixed_frequencies_arrive_as_a_zip(monkeypatch):
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("README.txt", "notes")
        z.writestr("Daily.csv", "observation_date,DFF\n2026-10-30,4.0\n2026-11-02,4.1\n")
        z.writestr("Monthly.csv", "observation_date,CPIAUCSL\n2026-09-01,320.5\n2026-10-01,321.0\n")
    _client(monkeypatch, lambda req: httpx.Response(200, content=buf.getvalue(),
                                                    headers={"content-type": "application/zip"}))
    df = reference.fred_series(["DFF", "CPIAUCSL"], start="2026-10-01")
    assert list(df.columns) == ["DFF", "CPIAUCSL"]
    assert df["CPIAUCSL"].dropna().tolist() == [321.0] and df["DFF"].dropna().tolist() == [4.0, 4.1]


def test_fred_garbage_is_a_provider_error(monkeypatch):
    _client(monkeypatch, lambda req: httpx.Response(200, content=b"<html>\n<p>a, b</p>\n<p>c</p>\nx,y,z\n"))
    with pytest.raises(ProviderError, match="unreadable"):
        reference.fred_series(["DFF"])
