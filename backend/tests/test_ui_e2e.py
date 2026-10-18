"""End-to-end tests of the compiled web UI (web/build) in headless Chrome, against a real
`autobench serve` on deterministic fake data, with an MCP client playing the agent.

Needs: `pip install -e ".[ui-test]"` (Playwright), Chrome or `playwright install chromium`, and a UI
build (`cd web && dart run tool/build.dart`). Skipped when any of these is missing."""

import asyncio
import concurrent.futures
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.ui

REPO = Path(__file__).resolve().parents[2]
BUILD = REPO / "web" / "build" / "main.dart.js"

playwright_api = pytest.importorskip("playwright.sync_api")
if not BUILD.exists():
    pytest.skip("web UI not built (cd web && dart run tool/build.dart)", allow_module_level=True)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    port = _free_port()
    env = {**os.environ, "AUTOBENCH_BAR_PROVIDER": "fake", "ALPACA_API_KEY_ID": "", "ALPACA_API_SECRET_KEY": "",
           "AUTOBENCH_DB_PATH": str(tmp_path_factory.mktemp("ui") / "ui.db")}
    log = open(tmp_path_factory.mktemp("ui-log") / "server.log", "w")
    proc = subprocess.Popen([sys.executable, "-m", "autobench", "serve", "--port", str(port)], env=env,
                            stdout=log, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            if httpx.get(f"{base}/api/health", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.1)
    else:
        proc.kill()
        pytest.fail("server did not start")
    yield base
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:  # uvicorn waits for open WebSockets
        proc.kill()


@pytest.fixture(scope="module")
def browser():
    with playwright_api.sync_playwright() as p:
        try:
            b = p.chromium.launch(channel="chrome", headless=True)
        except Exception:
            try:
                b = p.chromium.launch(headless=True)
            except Exception as e:  # no browser available
                pytest.skip(f"no Chromium available: {e}")
        yield b
        b.close()


@pytest.fixture
def page(browser, server):
    pg = browser.new_page(viewport={"width": 1400, "height": 860}, color_scheme="dark")
    errors: list[str] = []
    pg.on("pageerror", lambda e: errors.append(f"{e}\n{e.stack}"))
    pg.on("console", lambda m: m.type == "error" and errors.append(m.text))
    # every test starts from the same shared view (tests share one server)
    httpx.put(f"{server}/api/view", json={"symbol": "SPY", "interval": "1d", "indicators": [],
                                          "from_time": None, "to_time": None})
    pg.goto(server)
    pg.wait_for_function("window.__autobench && __autobench.bars() > 0", timeout=20000)
    yield pg
    pg.close()
    errors = [e for e in errors if "favicon" not in e]
    assert not errors, errors


def mcp_call(base: str, tool: str, args: dict):
    from mcp import Client

    async def run():
        async with Client(f"{base}/mcp") as c:
            r = await c.call_tool(tool, args)
            assert not r.is_error, r
            return r

    # Playwright's sync API runs its own event loop in this thread
    with concurrent.futures.ThreadPoolExecutor(1) as pool:
        return pool.submit(asyncio.run, run()).result()


def annotations(base: str, symbol: str) -> list[dict]:
    return httpx.get(f"{base}/api/annotations", params={"symbol": symbol}).json()


def chart_box(page):
    return page.locator(".chart canvas").bounding_box()


def test_loads_chart_with_legend_and_axes(page):
    legend = page.inner_text(".legend")
    assert "SPY" in legend and " O " in legend and " C " in legend
    lo, hi = page.evaluate("__autobench.visibleRange()")
    assert hi - lo > 50  # a sensible initial window, not the whole history
    # the canvas actually paints: background and candle colors are present
    colors = page.evaluate("""(() => { const c = document.querySelector('.chart canvas');
        const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data; const s = new Set();
        for (let i = 0; i < d.length; i += 4 * 13) s.add(d[i] + ',' + d[i+1] + ',' + d[i+2]); return s.size; })()""")
    assert colors > 20


def test_agent_controls_view_and_draws_live(page, server):
    mcp_call(server, "set_chart_view", {"symbol": "NVDA", "interval": "1d",
                                        "indicators": [{"name": "sma", "params": {"length": 20}}, {"name": "rsi"},
                                                       {"name": "macd"}]})
    page.wait_for_function("document.querySelector('.legend-symbol').textContent === 'NVDA'", timeout=10000)
    page.wait_for_function("__autobench.panes() === 3 && __autobench.series() >= 5", timeout=10000)
    assert "sma(20)" in page.inner_text(".legend")

    bars = httpx.get(f"{server}/api/bars/NVDA", params={"interval": "1d", "lookback": "1y"}).json()["bars"]
    last = bars[-1]
    mcp_call(server, "draw_annotations", {"symbol": "NVDA", "items": [
        {"type": "horizontal_line", "points": [{"price": last["close"] * 1.05}], "text": "resistance"},
        {"type": "long_position", "points": [{"time": last["time"], "price": last["close"]},
                                             {"time": last["time"] + 20 * 86400, "price": last["close"] * 0.95},
                                             {"time": last["time"] + 20 * 86400, "price": last["close"] * 1.1}],
         "tag": "plan"},
    ]})
    page.wait_for_function("__autobench.drawn() === 2", timeout=10000)
    # the future-dated trade plan is on screen (right space was added)
    x = page.evaluate(f"__autobench.xOfTime({last['time'] + 20 * 86400})")
    assert 0 < x < chart_box(page)["width"]
    page.click(".tabs button:has-text('Agent')")
    page.wait_for_selector(".activity li.agent")
    assert "Drew 2 objects on NVDA" in page.inner_text(".activity")


def test_user_draws_selects_and_deletes(page, server):
    box = chart_box(page)
    before = len(annotations(server, "SPY"))
    page.click("button[aria-label='Horizontal line']")
    page.mouse.click(box["x"] + box["width"] * 0.4, box["y"] + box["height"] * 0.3)
    page.wait_for_function(f"__autobench.drawn() === {before + 1}")
    created = [a for a in annotations(server, "SPY") if a["author"] == "user"][-1]
    assert created["type"] == "horizontal_line"
    # tool returns to select; click empty space deselects, clicking the line selects it again
    page.mouse.click(box["x"] + 30, box["y"] + box["height"] * 0.6)
    y = page.evaluate(f"__autobench.yOfPrice({created['points'][0]['price']})")
    page.mouse.click(box["x"] + box["width"] * 0.2, box["y"] + y)
    page.click(".tabs button:has-text('Drawings')")
    assert page.locator(".drawings li.selected").count() == 1
    page.locator(".chart canvas").hover()
    page.keyboard.press("Delete")
    page.wait_for_function(f"__autobench.drawn() === {before}")
    assert len(annotations(server, "SPY")) == before


def test_trade_plan_and_text_tools(page, server):
    box = chart_box(page)
    page.click("button[aria-label^='Long position']")
    page.mouse.click(box["x"] + box["width"] * 0.5, box["y"] + box["height"] * 0.5)
    page.mouse.move(box["x"] + box["width"] * 0.6, box["y"] + box["height"] * 0.3)
    page.mouse.click(box["x"] + box["width"] * 0.6, box["y"] + box["height"] * 0.3)
    page.wait_for_function("document.querySelectorAll('.drawings li, .tabs').length > 0")
    plan = next(a for a in annotations(server, "SPY") if a["type"] == "long_position")
    entry, stop, target = (p["price"] for p in plan["points"])
    assert target > entry > stop
    assert abs((target - entry) - 2 * (entry - stop)) < 0.01  # default 2R stop

    page.click("button[aria-label='Text label']")
    page.mouse.click(box["x"] + box["width"] * 0.3, box["y"] + box["height"] * 0.4)
    page.fill(".text-prompt input", "breakout watch")
    page.keyboard.press("Enter")
    page.wait_for_function("!document.querySelector('.text-prompt')")
    texts = [a for a in annotations(server, "SPY") if a["type"] == "text"]
    assert texts and texts[-1]["text"] == "breakout watch"


def test_zoom_pan_and_interval_switch(page):
    box = chart_box(page)
    lo, hi = page.evaluate("__autobench.visibleRange()")
    page.mouse.move(box["x"] + box["width"] * 0.5, box["y"] + box["height"] * 0.5)
    page.mouse.wheel(0, -400)  # zoom in
    page.wait_for_timeout(100)
    lo2, hi2 = page.evaluate("__autobench.visibleRange()")
    assert hi2 - lo2 < (hi - lo) * 0.8
    page.mouse.down()
    page.mouse.move(box["x"] + box["width"] * 0.5 + 200, box["y"] + box["height"] * 0.5, steps=5)
    page.mouse.up()
    lo3, hi3 = page.evaluate("__autobench.visibleRange()")
    assert lo3 < lo2 - 5 and abs((hi3 - lo3) - (hi2 - lo2)) < 0.5  # panned back in time, same zoom

    page.click(".intervals button:text-is('1h')")
    page.wait_for_function("document.querySelector('.legend').textContent.includes('1h')")
    page.wait_for_function("__autobench.bars() > 100")
    assert ":" in page.inner_text(".legend .ohlc")  # intraday timestamps


def test_indicator_menu_adds_and_removes(page):
    page.click(".menu > button")
    page.fill(".menu-filter", "macd")
    page.click(".menu-list li >> nth=0")
    page.click(".ind-edit button:has-text('Add')")
    page.wait_for_function("__autobench.panes() >= 2")
    assert "macd" in page.inner_text(".legend")
    page.click(".menu .chip button")
    page.wait_for_function("__autobench.panes() === 1")


def test_search_notes_and_watchlist(page, server):
    page.fill(".search input", "AMD")
    page.wait_for_selector(".search-results li")
    page.keyboard.press("Enter")
    page.wait_for_function("document.querySelector('.legend-symbol').textContent === 'AMD'")

    page.click(".tabs button:has-text('Notes')")
    page.fill(".note-form input", "Thesis")
    page.fill(".note-form textarea", "**Bold** idea\n- point one")
    page.click(".note-form button")
    page.wait_for_selector(".card header:has-text('Thesis')")
    page.click(".card header:has-text('Thesis')")
    assert page.locator(".md b").inner_text() == "Bold"

    page.click(".tabs button:has-text('Watchlist')")
    page.fill("input[aria-label='Add tickers']", "MSFT, NVDA")
    page.keyboard.press("Enter")
    page.wait_for_selector(".watch li:has-text('NVDA')")
    page.click(".watch li:has-text('MSFT')")
    page.wait_for_function("document.querySelector('.legend-symbol').textContent === 'MSFT'")


def test_mobile_and_light_theme(browser, server):
    pg = browser.new_page(viewport={"width": 390, "height": 844}, color_scheme="light")
    pg.goto(server)
    pg.wait_for_function("window.__autobench && __autobench.bars() > 0", timeout=20000)
    assert pg.evaluate("document.documentElement.scrollWidth") <= 390
    bg = pg.evaluate("""(() => { const c = document.querySelector('.chart canvas');
        return Array.from(c.getContext('2d').getImageData(c.width / 3, 5, 1, 1).data); })()""")
    assert min(bg[:3]) > 230  # light chart background
    pg.close()
