"""Recommendation storage, validation and outcome grading on controlled price paths."""

from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
from pydantic import ValidationError

from autobench.db import AnalysisRow, session_scope
from autobench.schemas import AnalysisIn
from autobench.services import analyses
from conftest import make_bars


def _save(monkeypatch, created: datetime, price: float, **kw):
    monkeypatch.setattr("autobench.services.analyses.get_quote", lambda s: {"price": price})
    a = analyses.save(AnalysisIn(symbol="TEST", horizon=kw.pop("horizon", "swing"), summary="s", rationale="r",
                                 confidence=kw.pop("confidence", 0.6), action=kw.pop("action", "buy"), **kw))
    with session_scope() as s:
        s.get(AnalysisRow, a["id"]).created_at = created.replace(tzinfo=None)
    return a["id"]


def test_validation_rules():
    with pytest.raises(ValidationError, match="stop must be below"):
        AnalysisIn(symbol="X", action="buy", horizon="swing", confidence=0.5, summary="s", rationale="r",
                   entry_low=100, entry_high=102, stop=105)
    with pytest.raises(ValidationError, match="targets must be below"):
        AnalysisIn(symbol="X", action="sell", horizon="swing", confidence=0.5, summary="s", rationale="r",
                   entry_low=100, stop=105, targets=[110])
    with pytest.raises(ValidationError):
        AnalysisIn(symbol="X", action="buy", horizon="swing", confidence=1.5, summary="s", rationale="r")


def test_target_hit(monkeypatch, patch_bars):
    df = make_bars(np.linspace(100, 120, 21), start="2025-01-02", spread=0.0)
    patch_bars(df)
    created = datetime(2025, 1, 2, 15, tzinfo=UTC)
    aid = _save(monkeypatch, created, 100.0, stop=95, targets=[110], entry_low=99, entry_high=101)
    ev = analyses.evaluate(aid, now=created + timedelta(days=40))
    assert ev["status"] == "target_hit" and ev["success"] is True
    assert ev["r_multiple"] == pytest.approx(2.0)
    assert ev["max_adverse_pct"] >= -0.01


def test_stop_wins_same_bar_tie(monkeypatch, patch_bars):
    c = np.array([100, 100, 100.0])
    df = make_bars(c, start="2025-01-02", highs=[100, 112, 100], lows=[100, 90, 100], spread=0.0)
    patch_bars(df)
    created = datetime(2025, 1, 2, 15, tzinfo=UTC)
    aid = _save(monkeypatch, created, 100.0, stop=95, targets=[110])
    ev = analyses.evaluate(aid, now=created + timedelta(days=40))
    assert ev["status"] == "stopped_out" and ev["success"] is False and ev["r_multiple"] == pytest.approx(-1.0)


def test_short_and_expiry(monkeypatch, patch_bars):
    df = make_bars(np.linspace(100, 97, 40), start="2025-01-02", spread=0.0)
    patch_bars(df)
    created = datetime(2025, 1, 2, 15, tzinfo=UTC)
    aid = _save(monkeypatch, created, 100.0, action="sell", stop=110, targets=[80])
    ev = analyses.evaluate(aid, now=created + timedelta(days=60))
    assert ev["status"] == "expired" and ev["success"] is True
    assert ev["directional_return_pct"] > 0
    open_ev = analyses.evaluate(aid, now=created + timedelta(days=5))
    assert open_ev["status"] == "open" and open_ev["success"] is None


def test_creation_day_bar_excluded(monkeypatch, patch_bars):
    # the creation-day bar dips below the stop, but that happened before/around the call: ignore it
    df = make_bars([100, 101, 102], start="2025-01-02", lows=[90, 100.5, 101.5], highs=[101, 101.5, 102.5], spread=0)
    patch_bars(df)
    created = datetime(2025, 1, 2, 18, tzinfo=UTC)
    aid = _save(monkeypatch, created, 100.0, stop=95, targets=[120])
    assert analyses.evaluate(aid, now=created + timedelta(days=3))["status"] == "open"


def test_track_record_calibration(monkeypatch, patch_bars):
    df = make_bars(np.linspace(100, 120, 30), start="2025-01-02", spread=0.0)
    patch_bars(df)
    created = datetime(2025, 1, 2, 15, tzinfo=UTC)
    _save(monkeypatch, created, 100.0, stop=95, targets=[110], confidence=0.9)
    _save(monkeypatch, created, 100.0, action="sell", stop=105, targets=[90], confidence=0.7)
    _save(monkeypatch, created, 100.0, action="hold", confidence=0.5)
    tr = analyses.track_record(now=created + timedelta(days=90))
    s = tr["summary"]
    assert s["total"] == 3 and s["directional"] == 2 and s["resolved"] == 2
    assert s["hit_rate_pct"] == 50.0
    assert s["brier_score"] == pytest.approx(((0.9 - 1) ** 2 + (0.7 - 0) ** 2) / 2, abs=1e-3)
    assert s["by_action"] == {"buy": {"n": 1, "wins": 1}, "sell": {"n": 1, "wins": 0}}

