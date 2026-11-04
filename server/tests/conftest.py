import os

import numpy as np
import pandas as pd
import pytest

os.environ["AUTOBENCH_BAR_PROVIDER"] = "fake"
os.environ["AUTOBENCH_BACKGROUND"] = "0"  # no scheduler thread in unit tests
os.environ["AUTOBENCH_BROKER"] = "sim"  # never reach a real IB Gateway from tests
os.environ["AUTOBENCH_ALLOW_LIVE_TRADING"] = "0"  # tests that want it on set it themselves; ignore .env
os.environ["AUTOBENCH_IBKR_FLEX_TOKEN"] = ""  # an empty variable also hides a value in .env
os.environ["AUTOBENCH_IBKR_FLEX_QUERY"] = ""
os.environ.pop("ALPACA_API_KEY_ID", None)
os.environ.pop("ALPACA_API_KEY", None)


@pytest.fixture(autouse=True)
def fresh_env(tmp_path, monkeypatch, request):
    """Every test gets an empty SQLite DB and fresh settings/providers (fake data unless marked live)."""
    from autobench import db
    from autobench.config import get_settings
    from autobench.providers import reset_providers

    if "live" not in request.keywords:
        monkeypatch.setenv("AUTOBENCH_BAR_PROVIDER", "fake")
        # keep a developer's .env (e.g. real Alpaca keys) out of unit tests
        monkeypatch.setenv("ALPACA_API_KEY_ID", "")
        monkeypatch.setenv("ALPACA_API_SECRET_KEY", "")
    else:
        monkeypatch.delenv("AUTOBENCH_BAR_PROVIDER", raising=False)
    monkeypatch.setenv("AUTOBENCH_DB_PATH", str(tmp_path / "test.db"))
    # never read the developer's Claude Code sessions or run the real `claude`
    monkeypatch.setenv("AUTOBENCH_CLAUDE_HOME", str(tmp_path / "claude-home"))
    monkeypatch.setenv("AUTOBENCH_CLAUDE_BIN", str(tmp_path / "no-claude"))
    get_settings.cache_clear()
    reset_providers()
    db.init_db(f"sqlite:///{tmp_path / 'test.db'}")
    yield
    from autobench.services.trading import trading

    trading.stop()
    get_settings.cache_clear()
    reset_providers()


def make_bars(closes, start="2024-01-02", spread=0.01, volume=1_000_000, opens=None, highs=None, lows=None,
              volumes=None) -> pd.DataFrame:
    """Daily bar frame from a close series (business days, 00:00 UTC stamps)."""
    closes = np.asarray(closes, dtype=float)
    n = len(closes)
    days = pd.bdate_range(start, periods=n)
    o = np.asarray(opens, float) if opens is not None else np.r_[closes[0], closes[:-1]]
    h = np.asarray(highs, float) if highs is not None else np.maximum(o, closes) * (1 + spread)
    lo = np.asarray(lows, float) if lows is not None else np.minimum(o, closes) * (1 - spread)
    v = np.asarray(volumes, float) if volumes is not None else np.full(n, float(volume))
    idx = pd.Index([int(d.tz_localize("UTC").timestamp()) for d in days], name="t")
    return pd.DataFrame({"o": o, "h": h, "l": lo, "c": closes, "v": v}, index=idx)


@pytest.fixture
def patch_bars(monkeypatch):
    """Make market.get_bars return a given frame (for exact, hand-built scenarios)."""
    from autobench.services import market
    from autobench.timeutil import get_interval

    def apply(df: pd.DataFrame, symbol_frames: dict | None = None):
        def fake_get_bars(symbol, interval="1d", start=None, end=None, lookback=None, use_cache=True):
            frame = (symbol_frames or {}).get(symbol.upper(), df)
            from autobench.timeutil import parse_time

            s = parse_time(start)
            out = frame if s is None else frame[frame.index >= s]
            return market.Bars(symbol.upper(), get_interval(interval), out, "test")

        for mod in ("autobench.services.market", "autobench.services.backtest", "autobench.services.analyses",
                    "autobench.services.risk", "autobench.services.technicals"):
            monkeypatch.setattr(f"{mod}.get_bars", fake_get_bars)
        return fake_get_bars

    return apply
