"""End-to-end tests of the compiled web UI (web/build) in headless Chrome, against a real
`autobench serve` on deterministic fake data, with an MCP client playing the agent.

Needs: `pip install -e ".[ui-test]"` (Playwright), Chrome or `playwright install chromium`, and a UI
build (`cd web && dart run tool/build.dart`). Skipped when any of these is missing."""

import asyncio
import json
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

MOD = "Meta" if sys.platform == "darwin" else "Control"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    port = _free_port()
    env = {**os.environ, "AUTOBENCH_BAR_PROVIDER": "fake", "ALPACA_API_KEY_ID": "", "ALPACA_API_SECRET_KEY": "",
           "AUTOBENCH_BACKGROUND": "1", "AUTOBENCH_BROKER": "sim", "AUTOBENCH_DB_PATH": str(tmp_path_factory.mktemp("ui") / "ui.db")}
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
    # build the (fake) market universe up front so screener/heatmap tests do not wait on it
    httpx.post(f"{base}/api/universe/refresh", timeout=10)
    for _ in range(150):
        st = httpx.get(f"{base}/api/universe/status").json()
        if all(st[k]["rows"] for k in ("listings", "fundamentals", "bars")) and not any(v["refreshing"] for v in st.values()):
            break
        time.sleep(0.2)
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


def _reset(server):
    # every test starts from the same state (tests share one server)
    httpx.put(f"{server}/api/ui-state", json={})
    httpx.put(f"{server}/api/view", json={"symbol": "SPY", "interval": "1d", "indicators": [],
                                          "from_time": None, "to_time": None})
    for a in httpx.get(f"{server}/api/alerts").json():
        httpx.delete(f"{server}/api/alerts/{a['id']}")
    for sym in ("SPY", "NVDA", "AMD", "AAPL", "MSFT"):
        httpx.delete(f"{server}/api/annotations", params={"symbol": sym})
    for cfg in httpx.get(f"{server}/api/chart-config").json():
        httpx.delete(f"{server}/api/chart-config/{cfg}")
    th = {"X-Autobench": "1"}
    httpx.post(f"{server}/api/trading/resume", headers=th)
    httpx.put(f"{server}/api/trading/settings", json={"mode": "confirm"}, headers=th)
    httpx.post(f"{server}/api/trading/account", json={"account": "SIM-PAPER"}, headers=th)
    httpx.post(f"{server}/api/trading/sim/reset", params={"cash": 100000}, headers=th)


@pytest.fixture
def page(browser, server):
    _reset(server)
    pg = browser.new_page(viewport={"width": 1440, "height": 860}, color_scheme="dark")
    errors: list[str] = []
    pg.on("pageerror", lambda e: errors.append(f"{e}\n{e.stack}"))
    pg.on("console", lambda m: m.type == "error" and errors.append(m.text))
    pg.goto(f"{server}/#chart")
    pg.wait_for_function("window.__autobench && __autobench.bars() > 0", timeout=20000)
    # the default indicators load asynchronously and add the RSI pane, which resizes the main pane;
    # wait for it so pixel positions used by a test do not shift underneath it
    pg.wait_for_function("__autobench.panes() === 2", timeout=20000)
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
    return page.locator(".chart canvas").first.bounding_box()


def legend_symbol(page) -> str:
    return page.locator(".legend-symbol").first.inner_text()


# ------------------------------------------------------------------------------- chart


def test_loads_chart_with_default_indicators_and_paints(page):
    assert legend_symbol(page) == "SPY"
    page.wait_for_function("__autobench.panes() === 2 && __autobench.series() >= 3")  # SMA50, SMA200, RSI pane
    assert "sma(50)" in page.inner_text(".legend") and "rsi(14)" in page.inner_text(".pane-legends")
    lo, hi = page.evaluate("__autobench.visibleRange()")
    assert hi - lo > 50
    colors = page.evaluate("""(() => { const c = document.querySelector('.chart canvas');
        const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data; const s = new Set();
        for (let i = 0; i < d.length; i += 4 * 13) s.add(d[i] + ',' + d[i+1] + ',' + d[i+2]); return s.size; })()""")
    assert colors > 20
    assert "New York" in page.inner_text("#market-clock")


def test_agent_controls_view_draws_and_navigates(page, server):
    mcp_call(server, "set_chart_view", {"symbol": "NVDA", "interval": "1d",
                                        "indicators": [{"name": "sma", "params": {"length": 20}}, {"name": "rsi"},
                                                       {"name": "macd"}]})
    page.wait_for_function("document.querySelector('.legend-symbol').textContent === 'NVDA'", timeout=10000)
    page.wait_for_function("__autobench.panes() === 3", timeout=10000)
    bars = httpx.get(f"{server}/api/bars/NVDA", params={"interval": "1d", "lookback": "1y"}).json()["bars"]
    last = bars[-1]
    mcp_call(server, "draw_annotations", {"symbol": "NVDA", "items": [
        {"type": "horizontal_line", "points": [{"price": last["close"] * 1.05}], "text": "resistance"},
        {"type": "long_position", "points": [{"time": last["time"], "price": last["close"]},
                                             {"time": last["time"] + 20 * 86400, "price": last["close"] * 0.95},
                                             {"time": last["time"] + 20 * 86400, "price": last["close"] * 1.1}]},
    ]})
    page.wait_for_function("__autobench.drawn() === 2", timeout=10000)
    x = page.evaluate(f"__autobench.xOfTime({last['time'] + 20 * 86400})")
    assert 0 < x < chart_box(page)["width"]  # the future-dated plan is on screen
    page.click("button[aria-label='Agent activity & analyses']")
    page.wait_for_selector(".activity li.agent")
    assert "Drew 2 objects on NVDA" in page.inner_text(".activity")
    # the agent opens the screener for the user
    mcp_call(server, "show_in_ui", {"page": "screener", "screen": {"signal": "top_gainers"}})
    page.wait_for_selector(".screener-page:not([hidden]) .chip.sig", timeout=10000)
    assert "Top Gainers" in page.inner_text(".scr-chips")


def test_user_draws_drags_undoes_and_deletes(page, server):
    box = chart_box(page)
    page.mouse.click(box["x"] + 300, box["y"] + 300)
    page.keyboard.press("Alt+KeyH")
    page.mouse.click(box["x"] + box["width"] * 0.4, box["y"] + box["height"] * 0.3)
    page.wait_for_function("__autobench.drawn() === 1")
    price0 = annotations(server, "SPY")[0]["points"][0]["price"]
    assert page.is_visible(".draw-toolbar")  # the new drawing is selected
    hx, hy = page.evaluate("__autobench.chart.active().handles()[0]")
    page.mouse.move(box["x"] + hx, box["y"] + hy)
    page.mouse.down()
    page.mouse.move(box["x"] + hx, box["y"] + hy - 60, steps=6)
    page.mouse.up()
    check = "fetch('/api/annotations?symbol=SPY').then(r => r.json()).then(a => a.length && a[0].points[0].price {})"
    page.wait_for_function(check.format(f"> {price0}"))
    moved = annotations(server, "SPY")[0]["points"][0]["price"]
    page.keyboard.press(f"{MOD}+KeyZ")
    page.wait_for_function(check.format(f"=== {price0}"))
    page.keyboard.press(f"{MOD}+Shift+KeyZ")
    page.wait_for_function(check.format(f"=== {moved}"))
    page.keyboard.press("Delete")
    page.wait_for_function("__autobench.drawn() === 0")
    assert annotations(server, "SPY") == []
    page.keyboard.press(f"{MOD}+KeyZ")  # undoing the delete re-creates the drawing
    page.wait_for_function("__autobench.drawn() === 1")


def test_trade_plan_text_and_styles(page, server):
    box = chart_box(page)
    page.mouse.click(box["x"] + 300, box["y"] + 300)
    page.keyboard.press("Alt+KeyL")
    page.mouse.click(box["x"] + box["width"] * 0.5, box["y"] + box["height"] * 0.45)
    page.mouse.move(box["x"] + box["width"] * 0.6, box["y"] + box["height"] * 0.25)
    page.mouse.click(box["x"] + box["width"] * 0.6, box["y"] + box["height"] * 0.25)
    page.wait_for_function("__autobench.drawn() === 1")
    plan = annotations(server, "SPY")[0]
    entry, stop, target = (p["price"] for p in plan["points"])
    assert target > entry > stop and abs((target - entry) - 2 * (entry - stop)) < 0.01  # default 2R stop
    page.click(".draw-toolbar button[aria-label='Lock']")
    page.wait_for_function("fetch('/api/annotations?symbol=SPY').then(r => r.json()).then(a => a[0].style.locked === true)")

    page.click("button[aria-label='Text']")
    page.mouse.click(box["x"] + box["width"] * 0.3, box["y"] + box["height"] * 0.35)
    page.fill(".text-prompt input", "breakout watch")
    page.keyboard.press("Enter")
    page.wait_for_function("__autobench.drawn() === 2")
    assert any(a["text"] == "breakout watch" for a in annotations(server, "SPY"))


def test_zoom_pan_interval_and_chart_type(page):
    box = chart_box(page)
    lo, hi = page.evaluate("__autobench.visibleRange()")
    page.mouse.move(box["x"] + box["width"] * 0.5, box["y"] + box["height"] * 0.5)
    page.mouse.wheel(0, -400)
    page.wait_for_timeout(100)
    lo2, hi2 = page.evaluate("__autobench.visibleRange()")
    assert hi2 - lo2 < (hi - lo) * 0.8
    page.mouse.down()
    page.mouse.move(box["x"] + box["width"] * 0.5 + 200, box["y"] + box["height"] * 0.5, steps=5)
    page.mouse.up()
    lo3, hi3 = page.evaluate("__autobench.visibleRange()")
    assert lo3 < lo2 - 5 and abs((hi3 - lo3) - (hi2 - lo2)) < 0.5
    page.wait_for_selector(".to-latest:not([hidden])")  # scrolled away from the latest bar
    page.keyboard.press("End")
    page.wait_for_selector(".to-latest[hidden]", state="attached")
    page.click(".tb.iv:text-is('1H')")
    page.wait_for_function("document.querySelector('.legend-interval').textContent === '1H'")
    page.wait_for_function("__autobench.bars() > 100")
    page.click("button[aria-label='Chart type']")
    page.click(".menu-item:has-text('Heikin Ashi')")
    page.wait_for_function("__autobench.chart.active().chartType() === 'heikin'")
    page.click(".range-btn:text-is('log')")
    page.wait_for_function("__autobench.chart.active().scale() === 'log'")


def test_indicator_picker_settings_and_remove(page):
    page.mouse.click(400, 400)
    page.keyboard.press("/")
    page.fill(".ind-dialog input", "macd")
    page.click(".ind-item >> nth=0")
    page.keyboard.press("Escape")
    page.wait_for_function("__autobench.panes() === 3")
    page.hover(".pane-legend:has-text('macd') .ind-name")
    page.click("button[aria-label^='Settings for macd']")
    page.fill(".dialog input >> nth=0", "8")  # fast length
    page.click(".dialog button:has-text('Apply')")
    page.wait_for_function("document.querySelector('.pane-legends').textContent.includes('macd(8')")
    page.hover(".pane-legend:has-text('macd') .ind-name")
    page.click("button[aria-label^='Remove macd']")
    page.wait_for_function("__autobench.panes() === 2")


def test_symbol_search_by_typing_and_compare(page):
    page.mouse.click(400, 400)
    page.keyboard.type("AMD", delay=40)
    page.wait_for_selector(".search-row")
    page.keyboard.press("Enter")
    page.wait_for_function("document.querySelector('.legend-symbol').textContent === 'AMD'")
    page.click("button[aria-label='Compare or add symbol']")
    page.keyboard.type("MSFT", delay=40)
    page.wait_for_selector(".search-row")
    page.keyboard.press("Enter")
    page.wait_for_function("__autobench.chart.active().compares() === 1")
    assert page.evaluate("__autobench.chart.active().scale()") == "percent"


def test_watchlist_keyboard_and_multi_chart(page, server):
    httpx.put(f"{server}/api/watchlists/default", json={"replace": ["###TECH", "AAPL", "MSFT", "NVDA"]})
    page.reload()
    page.wait_for_function("window.__autobench && __autobench.bars() > 0")
    page.wait_for_selector(".wl-row[data-symbol='NVDA']")
    assert page.inner_text(".wl-section").strip() == "TECH"
    page.click(".wl-row[data-symbol='AAPL']")
    page.wait_for_function("document.querySelector('.legend-symbol').textContent === 'AAPL'")
    page.keyboard.press("ArrowDown")
    page.wait_for_function("document.querySelector('.legend-symbol').textContent === 'MSFT'")
    page.wait_for_function("document.querySelector('.wl-row[data-symbol=\"MSFT\"] .wl-num').textContent !== '…'")
    page.click("button[aria-label='Layout: charts and sync']")
    page.click(".menu-item:has-text('4 charts')")
    page.wait_for_function("__autobench.chart.count() === 4")
    page.locator(".chart-cell").nth(3).click(position={"x": 200, "y": 150})
    page.click(".wl-row[data-symbol='NVDA']")
    page.wait_for_function("document.querySelectorAll('.legend-symbol')[3].textContent === 'NVDA'")
    assert page.locator(".legend-symbol").nth(0).inner_text() == "MSFT"  # the other charts are unchanged


def test_alert_from_context_menu_triggers_toast(page, server):
    box = chart_box(page)
    page.mouse.click(box["x"] + 300, box["y"] + 300, button="right")
    page.click(".menu-item:has-text('Add alert at')")
    page.fill(".dialog input[type=number]", "1")
    page.select_option(".dialog select", "above")
    page.click(".dialog button:has-text('Create')")
    page.wait_for_function("fetch('/api/alerts').then(r => r.json()).then(a => a.length === 1)")
    httpx.post(f"{server}/api/alerts/check")  # SPY is far above 1: fires now
    page.wait_for_selector(".toast.alert")
    assert "SPY" in page.inner_text(".toast.alert")
    page.click("button[aria-label='Alerts']")
    page.wait_for_selector(".alert-row.triggered")


# ------------------------------------------------------------------------------- pages


def test_screener_filters_sort_views(page):
    page.click("a.page-tab[href='#screener']")
    page.wait_for_selector(".data-table tbody tr")
    page.wait_for_function("document.querySelectorAll('.data-table tbody tr').length === 10")  # the fake universe
    page.fill(".scr-row input.mono", 'market_cap > 0 and sector == "Information Technology"')
    page.keyboard.press("Enter")
    page.wait_for_function("document.querySelectorAll('.data-table tbody tr').length === 4")
    page.click(".data-table th:has-text('Price')")
    page.wait_for_selector(".data-table th.sorted:has-text('Price')")
    page.wait_for_timeout(300)
    col = [h.strip() for h in page.locator(".data-table thead th").all_inner_texts()].index("Price") + 1
    prices = [float(t.replace(",", "")) for t in page.locator(f".data-table tbody tr td:nth-child({col})").all_inner_texts()]
    assert prices == sorted(prices, reverse=True)
    page.click(".view-tabs .tab:text-is('Charts')")
    page.wait_for_selector(".tile")
    page.click(".tile >> nth=0")
    page.wait_for_selector(".chart-page:not([hidden])")


def test_heatmap_markets_calendar_pages(page):
    page.click("a.page-tab[href='#heatmap']")
    page.wait_for_function("""(() => { const c = document.querySelector('.hm-canvas'); if (!c || !c.width) return false;
        const d = c.getContext('2d').getImageData(c.width / 2, c.height / 2, 1, 1).data; return d[1] > 40 || d[0] > 40; })()""")
    box = page.locator(".hm-canvas").bounding_box()
    page.mouse.move(box["x"] + box["width"] * 0.3, box["y"] + box["height"] * 0.5)
    page.wait_for_selector(".hm-tip:not([hidden])")
    page.click("a.page-tab[href='#markets']")
    page.wait_for_selector(".mk-breadth .split")
    page.wait_for_selector(".macro-card")
    assert "Advancing" in page.inner_text(".mk-breadth")
    page.click("a.page-tab[href='#calendar']")
    page.wait_for_selector(".earn-item")
    page.click(".seg-btn:text-is('Economic')")
    page.wait_for_selector(".econ-table")


def test_mobile_and_light_theme(browser, server):
    _reset(server)
    pg = browser.new_page(viewport={"width": 390, "height": 844}, color_scheme="light")
    pg.goto(f"{server}/#chart")
    pg.wait_for_function("window.__autobench && __autobench.bars() > 0", timeout=20000)
    assert pg.evaluate("document.documentElement.scrollWidth") <= 390
    bg = pg.evaluate("""(() => { const c = document.querySelector('.chart canvas');
        return Array.from(c.getContext('2d').getImageData(c.width / 3, 5, 1, 1).data); })()""")
    assert min(bg[:3]) > 230  # light chart background
    for tab in ("screener", "heatmap", "markets", "calendar"):
        pg.click(f"a.page-tab[href='#{tab}']")
        pg.wait_for_timeout(800)
        assert pg.evaluate("document.documentElement.scrollWidth") <= 392, tab
    pg.close()


def _layers(page) -> list[dict]:
    import json
    return json.loads(page.evaluate("__autobench.chart.active().layers()"))["layers"]


def _goto_symbol(page, sym: str):
    page.mouse.click(400, 400)
    page.keyboard.type(sym, delay=40)
    page.wait_for_selector(".search-row")
    page.keyboard.press("Enter")
    page.wait_for_function(f"document.querySelector('.legend-symbol').textContent === '{sym}'")
    page.wait_for_function("__autobench.chart.active().layers() !== null && __autobench.bars() > 0")


def _open_layers(page):
    if not page.is_visible(".layers-panel"):
        page.click("button[aria-label='Layers & objects']")
    page.wait_for_selector(".layers-panel .layer-head")


def test_each_symbol_remembers_its_layers(page, server):
    _goto_symbol(page, "AAPL")
    page.keyboard.press("/")
    page.fill(".ind-dialog input", "macd")
    page.click(".ind-item >> nth=0")
    page.keyboard.press("Escape")
    page.wait_for_function("__autobench.panes() === 3")
    page.wait_for_function("fetch('/api/chart-config/AAPL').then(r => r.json()).then(c => JSON.stringify(c).includes('macd'))")
    _goto_symbol(page, "MSFT")
    page.wait_for_function("__autobench.panes() === 2")  # MSFT keeps the default template
    assert "macd" not in page.evaluate("__autobench.chart.active().layers()")
    _goto_symbol(page, "AAPL")
    page.wait_for_function("__autobench.panes() === 3")
    assert "macd" in page.inner_text(".pane-legends")


def test_layer_create_hide_lock_and_delete(page, server):
    _open_layers(page)
    page.click(".layers-panel button[aria-label='New layer']")
    page.fill(".dialog input", "Setups")
    page.keyboard.press("Enter")
    page.wait_for_selector(".layer-head:has-text('Setups').active")
    assert _layers(page)[0]["name"] == "Setups"  # new layers go on top and become active
    # an indicator added now lands on the active layer
    page.mouse.click(400, 400)
    page.keyboard.press("/")
    page.fill(".ind-dialog input", "macd")
    page.click(".ind-item >> nth=0")
    page.keyboard.press("Escape")
    page.wait_for_function("__autobench.panes() === 3")
    assert any(i.get("name") == "macd" for i in _layers(page)[0]["items"])
    # so does a drawing
    box = chart_box(page)
    page.mouse.click(box["x"] + 300, box["y"] + 300)
    page.keyboard.press("Alt+KeyH")
    page.mouse.click(box["x"] + box["width"] * 0.4, box["y"] + box["height"] * 0.3)
    page.wait_for_function("__autobench.drawn() === 1")
    lid = _layers(page)[0]["id"]
    assert annotations(server, "SPY")[0]["layer"] == lid
    page.keyboard.press("Escape")
    # hiding the layer hides its indicator and drawing
    page.click(".layers-panel button[aria-label='Hide layer Setups']")
    page.wait_for_function("__autobench.panes() === 2 && __autobench.drawn() === 0")
    page.click(".layers-panel button[aria-label='Show layer Setups']")
    page.wait_for_function("__autobench.panes() === 3 && __autobench.drawn() === 1")
    page.click(".layers-panel button[aria-label='Lock layer Setups']")
    page.wait_for_function(f"__autobench.chart.active().layers().includes('\"locked\":true')")
    # deleting the layer removes its items and drawings (after confirming)
    page.click(".layers-panel button[aria-label='Layer Setups menu']")
    page.click(".menu-item:has-text('Delete layer')")
    page.click(".dialog button:has-text('Delete')")
    page.wait_for_function("__autobench.panes() === 2 && __autobench.drawn() === 0")
    page.wait_for_function("fetch('/api/annotations?symbol=SPY').then(r => r.json()).then(a => a.length === 0)")
    assert [l["name"] for l in _layers(page)] == ["Main"]


def test_layer_with_its_own_chart_per_interval(page, server):
    _open_layers(page)
    page.click(".layers-panel button[aria-label='New layer']")
    page.fill(".dialog input", "Entry")
    page.keyboard.press("Enter")
    page.wait_for_selector(".layer-head:has-text('Entry').active")
    page.click(".layers-panel button[aria-label='Layer Entry menu']")
    page.hover(".menu-item:has-text('Add price chart')")
    page.click(".menu-item:has-text('at 30m')")
    # the chart switches to 30m and shows the Entry layer's chart (price + volume, no Main indicators)
    page.wait_for_function("document.querySelector('.legend-interval').textContent === '30m'")
    page.wait_for_selector(".legend-layer:text-is('Entry')")
    page.wait_for_function("__autobench.bars() > 100 && __autobench.panes() === 1")
    page.mouse.click(400, 400)
    page.keyboard.press("/")
    page.fill(".ind-dialog input", "macd")
    page.click(".ind-item >> nth=0")
    page.keyboard.press("Escape")
    page.wait_for_function("__autobench.panes() === 2")
    entry = next(l for l in _layers(page) if l["name"] == "Entry")
    assert entry["interval"] == "30m" and [i["kind"] for i in entry["items"]] == ["price", "volume", "indicator"]
    # back on 1D: Main's setup (RSI pane, SMAs), Entry's MACD gone, Main becomes the active layer
    page.click(".tb.iv:text-is('1D')")
    page.wait_for_function("document.querySelector('.legend-interval').textContent === '1D'")
    page.wait_for_selector(".legend-layer:text-is('Main')")
    page.wait_for_selector(".layer-head:has-text('Main').active")
    page.wait_for_function("__autobench.panes() === 2 && !document.querySelector('.pane-legends').textContent.includes('macd')")
    assert page.is_visible(".layer-head.off-tf:has-text('Entry')")
    # clicking the Entry layer switches the chart back to 30m
    page.click(".layer-head:has-text('Entry') .layer-name")
    page.wait_for_function("document.querySelector('.legend-interval').textContent === '30m'")
    page.wait_for_function("__autobench.panes() === 2 && document.querySelector('.pane-legends').textContent.includes('macd')")
    page.click(".tb.iv:text-is('1D')")  # leave the shared server on 1D


def test_price_and_volume_hide_and_volume_in_own_pane(page):
    _open_layers(page)
    assert page.evaluate("__autobench.chart.active().showPrice()") is True
    page.click(".layers-panel button[aria-label^='Hide Price']")
    page.wait_for_function("__autobench.chart.active().showPrice() === false")
    page.click(".layers-panel button[aria-label^='Show Price']")
    page.wait_for_function("__autobench.chart.active().showPrice() === true")
    page.click(".layers-panel button[aria-label='Hide Volume']")
    page.wait_for_function("__autobench.chart.active().showVolume() === false")
    page.click(".layers-panel button[aria-label='Show Volume']")
    page.wait_for_function("__autobench.chart.active().showVolume() === true")
    page.click(".layers-panel button[aria-label='Settings for Volume']")
    page.select_option(".dialog label:has-text('Pane') select", "new")
    page.click(".dialog button:has-text('Apply')")
    page.wait_for_function("__autobench.panes() === 3")  # main, volume, rsi


def test_compare_symbol_as_candles_in_own_pane(page):
    _open_layers(page)
    page.click(".layers-panel button[aria-label='Layer Main menu']")
    page.click(".menu-item:has-text('Add symbol')")
    page.keyboard.type("NVDA", delay=40)
    page.wait_for_selector(".search-row")
    page.keyboard.press("Enter")
    page.wait_for_function("__autobench.panes() === 3")  # added in its own pane
    page.click(".layers-panel button[aria-label^='Settings for NVDA']")
    page.select_option(".dialog label:has-text('Display') select", "candles")
    page.click(".dialog button:has-text('Apply')")
    page.wait_for_function("__autobench.chart.active().layers().includes('\"display\":\"candles\"')")
    page.wait_for_function("__autobench.panes() === 3")


def test_drawing_moves_between_layers(page, server):
    _open_layers(page)
    box = chart_box(page)
    page.mouse.click(box["x"] + 300, box["y"] + 300)
    page.keyboard.press("Alt+KeyH")
    page.mouse.click(box["x"] + box["width"] * 0.4, box["y"] + box["height"] * 0.3)
    page.wait_for_function("__autobench.drawn() === 1")
    assert annotations(server, "SPY")[0].get("layer") in (None, "main")
    page.keyboard.press("Escape")
    page.click(".layers-panel button[aria-label='New layer']")
    page.fill(".dialog input", "Levels")
    page.keyboard.press("Enter")
    page.wait_for_selector(".layer-head:has-text('Levels')")
    page.drag_and_drop(".layer-row.drawing", ".layer-head:has-text('Levels')")
    page.wait_for_function("fetch('/api/annotations?symbol=SPY').then(r => r.json()).then(a => a[0].layer && a[0].layer !== 'main')")
    page.click(".layers-panel button[aria-label='Hide layer Levels']")
    page.wait_for_function("__autobench.drawn() === 0")
    page.click(".layers-panel button[aria-label='Hide layer Main']")
    page.wait_for_function("__autobench.series() === 0 || !__autobench.chart.active().showPrice()")


def test_wheel_over_an_axis_scales_only_that_axis(page):
    box = chart_box(page)
    page.wait_for_function("__autobench.bars() > 100")
    pw = page.evaluate("__autobench.chart.active().plotWidth()")
    spacing = page.evaluate("__autobench.chart.active().spacing()")
    lo, hi = page.evaluate("__autobench.chart.active().priceRange()")
    page.mouse.move(box["x"] + pw + 20, box["y"] + box["height"] * 0.4)  # price axis
    page.mouse.wheel(0, -300)
    page.wait_for_timeout(100)
    lo2, hi2 = page.evaluate("__autobench.chart.active().priceRange()")
    assert hi2 - lo2 < (hi - lo) * 0.8
    assert page.evaluate("__autobench.chart.active().spacing()") == spacing  # time scale untouched
    page.mouse.move(box["x"] + pw * 0.5, box["y"] + box["height"] - 8)  # time axis
    page.mouse.wheel(0, -300)
    page.wait_for_timeout(100)
    assert page.evaluate("__autobench.chart.active().spacing()") > spacing * 1.3
    assert page.evaluate("__autobench.chart.active().priceRange()") == [lo2, hi2]  # price scale untouched


def test_reload_restores_zoom_prefs_and_panels(page, server):
    _open_layers(page)
    page.click(".tb.iv:text-is('1H')")
    page.wait_for_function("document.querySelector('.legend-interval').textContent === '1H' && __autobench.bars() > 100")
    box = chart_box(page)
    page.mouse.move(box["x"] + box["width"] * 0.5, box["y"] + box["height"] * 0.5)
    page.mouse.wheel(0, -400)
    page.wait_for_timeout(200)
    spacing = page.evaluate("__autobench.chart.active().spacing()")
    page.click(".layers-panel button[aria-label='Hide Volume']")
    page.wait_for_function("__autobench.chart.active().showVolume() === false")
    page.wait_for_timeout(1200)  # debounced saves
    page.reload()
    page.wait_for_function("window.__autobench && __autobench.bars() > 0 && __autobench.chart.active().layers() !== null")
    assert page.inner_text(".legend-interval") == "1H"
    assert page.is_visible(".layers-panel")
    page.wait_for_function("__autobench.chart.active().showVolume() === false")
    page.wait_for_function(f"Math.abs(__autobench.chart.active().spacing() - {spacing}) / {spacing} < 0.03")


# ------------------------------------------------------------------------------------- trading (simulator)

TH = {"X-Autobench": "1"}


def sim_price(server, sym, price):
    httpx.post(f"{server}/api/trading/sim/price", json={"symbol": sym, "price": price}, headers=TH)


def test_order_ticket_bracket_fill_and_drag_stop(page, server):
    sim_price(server, "SPY", 100)
    page.click("button[aria-label='Trade (order ticket)']")
    page.wait_for_selector(".trade-panel .side-btn.buy.on")
    page.fill("input[aria-label='Limit price']", "99")
    page.fill("input[aria-label='Shares']", "20")
    page.wait_for_function("document.querySelector('.trade-check').textContent.includes('Risk to stop')")
    page.click(".submit-btn")
    page.wait_for_selector(".toast:has-text('Order sent')")
    page.wait_for_function("__autobench.chart.active().priceLines() === 3")  # entry + held stop & target
    orders = httpx.get(f"{server}/api/trading/orders").json()
    assert sorted(o["role"] for o in orders) == ["entry", "stop_loss", "take_profit"]
    sim_price(server, "SPY", 98.5)  # fills the entry
    page.wait_for_selector(".toast:has-text('Bought SPY')")
    page.wait_for_selector(".pos-card:has-text('LONG 20')")
    page.wait_for_function("__autobench.chart.active().priceLines() === 3")  # position + stop + target
    sl = next(o for o in httpx.get(f"{server}/api/trading/orders").json() if o["role"] == "stop_loss")
    box = chart_box(page)
    y = page.evaluate(f"__autobench.chart.active().yOfPrice({sl['stop_price']})")
    page.mouse.move(box["x"] + box["width"] * 0.4, box["y"] + y)
    page.mouse.down()
    page.mouse.move(box["x"] + box["width"] * 0.4, box["y"] + y - 25, steps=6)
    page.mouse.up()
    page.wait_for_function(f"fetch('/api/trading/orders').then(r => r.json()).then(a => a.find(o => o.role === 'stop_loss').stop_price > {sl['stop_price']})")


def test_agent_proposal_approval_and_trading_page(page, server):
    sim_price(server, "AAPL", 100)
    mcp_call(server, "propose_order", {"symbol": "AAPL", "side": "buy", "qty": 10, "limit_price": 101, "stop_loss": 96,
                                       "take_profit": 110, "rationale": "Test proposal: pullback entry with a defined stop."})
    page.wait_for_selector(".toast:has-text('Agent proposes')")
    page.wait_for_selector(".trade-nav .nav-badge:text-is('1')")
    page.click(".toast:has-text('Agent proposes') button:has-text('Review')")
    page.wait_for_selector(".ticket-review .check-rows")
    page.click(".dialog button:has-text('Approve & send')")
    page.wait_for_selector(".toast:has-text('Bought AAPL')")
    page.click("a.page-tab:has-text('Trading')")
    page.wait_for_selector(".tr-body tr[data-symbol='AAPL']")
    assert "PAPER" in page.inner_text(".tr-head")
    page.click(".tr-tabs .tab:has-text('History')")
    page.wait_for_selector(".tr-body tr[data-trade]")
    page.click(".tr-tabs .tab:has-text('Activity log')")
    page.wait_for_selector(".tr-body td:has-text('approve')")
    # kill switch: halt, banner and nav state, then resume
    page.click("button[aria-label='Kill switch']")
    page.click(".menu-item:has-text('Halt new orders')")
    page.click(".dialog button:has-text('Halt')")
    page.wait_for_selector(".tr-banner:has-text('Trading halted')")
    page.wait_for_selector(".trade-nav.halted")
    page.click("button[aria-label='Resume trading']")
    page.wait_for_selector(".tr-banner:empty", state="attached")


def test_switch_account_from_trading_page(page, server):
    sim_price(server, "AAPL", 100)
    httpx.post(f"{server}/api/trading/orders", headers={"X-Autobench": "1"},
               json={"symbol": "AAPL", "side": "buy", "qty": 5, "order_type": "limit", "limit_price": 101})
    page.click("a.page-tab:has-text('Trading')")
    page.wait_for_selector(".tr-body tr[data-symbol='AAPL']")
    page.select_option(".tr-head select[aria-label='Account']", "SIM-PAPER-2")
    page.wait_for_selector(".toast:has-text('Using account SIM-PAPER-2')")
    page.wait_for_selector(".tr-body tr[data-symbol='AAPL']", state="detached")
    assert httpx.get(f"{server}/api/trading/status").json()["account"] == "SIM-PAPER-2"
    page.click("button[aria-label='Broker connection']")
    assert page.input_value(".dialog select[aria-label='Account']") == "SIM-PAPER-2"
    page.click(".dialog button:has-text('Cancel')")
    page.select_option(".tr-head select[aria-label='Account']", "SIM-PAPER")
    page.wait_for_selector(".tr-body tr[data-symbol='AAPL']")


def test_chart_context_menu_and_close_position(page, server):
    px = round(httpx.get(f"{server}/api/quote/SPY").json()["price"], 2)  # keep the position line on screen
    sim_price(server, "SPY", px)
    r = httpx.post(f"{server}/api/trading/orders", json={"symbol": "SPY", "side": "buy", "qty": 2, "limit_price": round(px * 1.01, 2)},
                   headers=TH)
    assert r.status_code == 200, r.text
    page.wait_for_function("__autobench.chart.active().priceLines() >= 1")  # the position line
    box = chart_box(page)
    page.mouse.click(box["x"] + 300, box["y"] + 150, button="right")
    page.hover(".menu-item:has-text('Trade at')")
    page.click(".menu-item:has-text('Sell limit')")
    page.wait_for_selector(".trade-panel .side-btn.sell.on")
    page.wait_for_selector(".trade-form:has-text('Reduces your long position')")
    # close the position from the position line's × button
    page.wait_for_function("JSON.parse(__autobench.chart.active().tradeHits()).some(h => h.kind === 'position-x')")
    x = next(h for h in json.loads(page.evaluate("__autobench.chart.active().tradeHits()")) if h["kind"] == "position-x")
    page.mouse.click(box["x"] + x["x"], box["y"] + x["y"])  # the × on the position label
    page.wait_for_selector(".dialog:has-text('Close SPY long')")
    page.click(".dialog button:has-text('Sell to close')")
    page.wait_for_selector(".toast:has-text('Sold SPY')")
    page.wait_for_function("fetch('/api/trading/overview').then(r => r.json()).then(o => o.positions.length === 0)")
