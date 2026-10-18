"""SEC EDGAR: filing lists, filing text (with 10-K/10-Q section extraction) and long-run
XBRL financial history. Only sec.gov URLs are ever fetched."""

from __future__ import annotations

import re
import threading
import time
from datetime import date
from html.parser import HTMLParser
from urllib.parse import urlparse

import httpx

from .. import db
from ..config import get_settings
from ..providers.base import ProviderError
from .market import normalize_symbol

DEFAULT_UA = "Autobench research tool admin@autobench.local"
_client: httpx.Client | None = None
_rate_lock = threading.Lock()
_last_request = 0.0


def _http() -> httpx.Client:
    global _client
    if _client is None:
        ua = get_settings().sec_user_agent or DEFAULT_UA
        _client = httpx.Client(timeout=30, headers={"User-Agent": ua, "Accept-Encoding": "gzip, deflate"},
                               follow_redirects=True)
    return _client


def _get(url: str) -> httpx.Response:
    host = urlparse(url).hostname or ""
    if not (host == "sec.gov" or host.endswith(".sec.gov")):
        raise ValueError("Only sec.gov URLs can be fetched")
    global _last_request
    with _rate_lock:  # SEC fair-access policy: max 10 requests/second
        wait = 0.12 - (time.monotonic() - _last_request)
        if wait > 0:
            time.sleep(wait)
        _last_request = time.monotonic()
    try:
        r = _http().get(url)
    except httpx.HTTPError as e:
        raise ProviderError(f"SEC request failed: {e}") from e
    if r.status_code != 200:
        raise ProviderError(f"SEC returned HTTP {r.status_code} for {url}")
    return r


def cik_for(symbol: str) -> int:
    symbol = normalize_symbol(symbol).replace(".", "-")

    def fetch():
        data = _get("https://www.sec.gov/files/company_tickers.json").json()
        return {v["ticker"].upper(): int(v["cik_str"]) for v in data.values()}

    mapping = db.cached("sec_tickers", 7 * 86400, fetch)
    if symbol not in mapping:
        raise ProviderError(f"No SEC CIK found for {symbol} (ETFs, funds and foreign issuers may not file 10-K/10-Q)")
    return mapping[symbol]


def list_filings(symbol: str, forms: list[str] | None = None, limit: int = 20) -> dict:
    """Recent filings. forms e.g. ["10-K","10-Q","8-K","4","DEF 14A","S-1","SC 13D"]."""
    symbol = normalize_symbol(symbol)
    cik = cik_for(symbol)

    def fetch():
        return _get(f"https://data.sec.gov/submissions/CIK{cik:010d}.json").json()

    sub = db.cached(f"sec_sub:{cik}", 3600, fetch)
    recent = sub.get("filings", {}).get("recent", {})
    wanted = {f.upper() for f in forms} if forms else None
    out = []
    for i, form in enumerate(recent.get("form", [])):
        if wanted and form.upper() not in wanted:
            continue
        acc = recent["accessionNumber"][i]
        doc = recent["primaryDocument"][i]
        out.append({
            "form": form,
            "filing_date": recent["filingDate"][i],
            "report_date": recent.get("reportDate", [None] * (i + 1))[i] or None,
            "accession": acc,
            "description": recent.get("primaryDocDescription", [""] * (i + 1))[i],
            "items": recent.get("items", [""] * (i + 1))[i] or None,
            "url": f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc.replace('-', '')}/{doc}",
        })
        if len(out) >= limit:
            break
    return {"symbol": symbol, "cik": cik, "company": sub.get("name"), "filings": out,
            "note": "8-K item codes: 2.02 results of operations (earnings), 5.02 officer changes, 1.01 material agreement, "
                    "8.01 other events. Form 4 = insider transaction."}


def filing_documents(url: str) -> list[dict]:
    """All documents in a filing (e.g. exhibit 99.1 press release of an 8-K)."""
    base = url.rsplit("/", 1)[0]
    idx = _get(base + "/index.json").json()
    return [{"name": it["name"], "type": it.get("type"), "size": it.get("size"), "url": f"{base}/{it['name']}"}
            for it in idx.get("directory", {}).get("item", [])
            if it["name"].lower().endswith((".htm", ".html", ".txt")) and not it["name"].endswith("-index.htm")]


class _TextExtractor(HTMLParser):
    BLOCK = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table", "section", "title"}
    SKIP = {"script", "style", "ix:header", "head"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")
        elif tag in ("td", "th"):
            self.parts.append(" | ")

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    p = _TextExtractor()
    p.feed(html)
    text = "".join(p.parts).replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\| *(\| *)+", " | ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


_ITEM_RE = re.compile(r"(?im)^\s*(?:part\s+i{1,2}\s*[,.\-–—]?\s*)?item\s+(\d{1,2}[a-c]?)\s*[.:\-–—]?\s*([^\n]{0,120})")

SECTION_ITEMS = {
    "10-K": {"business": "1", "risk_factors": "1A", "cybersecurity": "1C", "properties": "2", "legal_proceedings": "3",
             "market_for_equity": "5", "mdna": "7", "market_risk": "7A", "financial_statements": "8", "controls": "9A",
             "executive_compensation": "11"},
    "10-Q": {"financial_statements": "1", "mdna": "2", "market_risk": "3", "controls": "4", "risk_factors": "1A",
             "legal_proceedings": "1", "unregistered_sales": "2"},
}


def _sections(text: str) -> list[dict]:
    """Locate item headings, keeping for each item code the occurrence with the longest body
    (skips table-of-contents entries)."""
    hits = [(m.start(), m.group(1).upper(), m.group(2).strip()) for m in _ITEM_RE.finditer(text)]
    spans = []
    for k, (pos, code, title) in enumerate(hits):
        end = hits[k + 1][0] if k + 1 < len(hits) else len(text)
        spans.append({"item": code, "title": title[:80], "start": pos, "end": end, "chars": end - pos})
    best: dict[str, dict] = {}
    for s in spans:
        if s["item"] not in best or s["chars"] > best[s["item"]]["chars"]:
            best[s["item"]] = s
    return sorted(best.values(), key=lambda s: s["start"])


def read_filing(url: str, section: str | None = None, offset: int = 0, max_chars: int = 15000) -> dict:
    """Plain text of a filing document. For 10-K/10-Q pass section (e.g. 'risk_factors', 'mdna',
    'business', or an item code like '7A'). Long text is paginated: call again with next_offset."""
    max_chars = max(1000, min(max_chars, 40000))
    text = db.cached(f"sec_doc:{url}", 7 * 86400, lambda: html_to_text(_get(url).text))
    secs = _sections(text)
    chosen = None
    if section:
        key = section.strip().lower().replace(" ", "_").replace("&", "").replace("'", "")
        form = "10-Q" if "10q" in url.lower() or "10-q" in url.lower() else "10-K"
        code = SECTION_ITEMS.get(form, {}).get(key) or SECTION_ITEMS["10-K"].get(key) or section.upper().replace("ITEM", "").strip()
        chosen = next((s for s in secs if s["item"] == code), None)
        if chosen is None:
            raise ValueError(f"Section '{section}' not found. Items found: {', '.join(s['item'] + ' ' + s['title'] for s in secs) or 'none'}")
        body = text[chosen["start"] : chosen["end"]]
    else:
        body = text
    chunk = body[offset : offset + max_chars]
    nxt = offset + max_chars if offset + max_chars < len(body) else None
    return {
        "url": url,
        "section": chosen and {"item": chosen["item"], "title": chosen["title"]},
        "total_chars": len(body), "offset": offset, "next_offset": nxt,
        "sections_available": [{"item": s["item"], "title": s["title"], "chars": s["chars"]} for s in secs
                               if s["chars"] > 500 and s["title"][:1].isalpha()],
        "text": chunk,
    }


# --- XBRL company facts --------------------------------------------------------------------

METRICS: dict[str, tuple[list[str], str]] = {
    # name: (us-gaap concepts in priority order, unit)
    "revenue": (["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet",
                 "RevenueFromContractWithCustomerIncludingAssessedTax", "SalesRevenueGoodsNet"], "USD"),
    "gross_profit": (["GrossProfit"], "USD"),
    "operating_income": (["OperatingIncomeLoss"], "USD"),
    "net_income": (["NetIncomeLoss", "ProfitLoss"], "USD"),
    "eps_diluted": (["EarningsPerShareDiluted"], "USD/shares"),
    "diluted_shares": (["WeightedAverageNumberOfDilutedSharesOutstanding"], "shares"),
    "operating_cash_flow": (["NetCashProvidedByUsedInOperatingActivities"], "USD"),
    "capex": (["PaymentsToAcquirePropertyPlantAndEquipment"], "USD"),
    "rnd": (["ResearchAndDevelopmentExpense"], "USD"),
    "sbc": (["ShareBasedCompensation", "AllocatedShareBasedCompensationExpense"], "USD"),
    "dividends_paid": (["PaymentsOfDividends", "PaymentsOfDividendsCommonStock"], "USD"),
    "buybacks": (["PaymentsForRepurchaseOfCommonStock"], "USD"),
    "cash": (["CashAndCashEquivalentsAtCarryingValue"], "USD"),
    "total_assets": (["Assets"], "USD"),
    "total_liabilities": (["Liabilities"], "USD"),
    "equity": (["StockholdersEquity"], "USD"),
    "long_term_debt": (["LongTermDebtNoncurrent", "LongTermDebt"], "USD"),
}
_INSTANT = {"cash", "total_assets", "total_liabilities", "equity", "long_term_debt"}


def _days(a: str, b: str) -> int:
    return (date.fromisoformat(b) - date.fromisoformat(a)).days


def financial_history(symbol: str, metrics: list[str] | None = None, period: str = "annual", years: int = 10) -> dict:
    """Long-run reported financials from XBRL (10-K/10-Q). period: annual | quarterly.
    Fiscal Q4 is derived as FY minus Q1-Q3 for flow metrics (marked in 'derived_q4')."""
    symbol = normalize_symbol(symbol)
    if period not in ("annual", "quarterly"):
        raise ValueError("period must be annual or quarterly")
    metrics = metrics or ["revenue", "operating_income", "net_income", "eps_diluted", "operating_cash_flow", "capex", "diluted_shares"]
    bad = [m for m in metrics if m not in METRICS]
    if bad:
        raise ValueError(f"Unknown metrics {bad}. Available: {', '.join(METRICS)}")
    cik = cik_for(symbol)

    def fetch():
        facts = _get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json").json()
        gaap = facts.get("facts", {}).get("us-gaap", {})
        extracted = {}
        for name, (concepts, unit) in METRICS.items():
            entries = []
            for pri, concept in enumerate(concepts):
                for e in gaap.get(concept, {}).get("units", {}).get(unit, []):
                    if e.get("form") not in ("10-K", "10-Q", "10-K/A", "10-Q/A"):
                        continue
                    entries.append({"start": e.get("start"), "end": e["end"], "val": e["val"], "fy": e.get("fy"),
                                    "fp": e.get("fp"), "filed": e.get("filed"), "pri": pri})
            extracted[name] = entries
        return {"name": facts.get("entityName"), "metrics": extracted}

    data = db.cached(f"sec_facts:{cik}", 86400, fetch)
    out: dict[str, dict] = {}
    derived: dict[str, list[str]] = {}
    for m in metrics:
        entries = data["metrics"].get(m, [])
        annual: dict[str, dict] = {}
        quarterly: dict[str, dict] = {}
        for e in entries:
            if m in _INSTANT:
                target = annual if e["fp"] == "FY" else quarterly
            else:
                if not e["start"]:
                    continue
                d = _days(e["start"], e["end"])
                target = annual if 350 <= d <= 380 else quarterly if 80 <= d <= 100 else None
                if target is None:
                    continue
            prev = target.get(e["end"])
            # prefer the higher-priority concept, then the most recent filing (restatements)
            if prev is None or (e["pri"], -(int(e["filed"].replace("-", "")) if e["filed"] else 0)) < (
                    prev["pri"], -(int(prev["filed"].replace("-", "")) if prev["filed"] else 0)):
                target[e["end"]] = e
        if period == "annual":
            series = {k: v["val"] for k, v in sorted(annual.items())}
        else:
            series = {k: v["val"] for k, v in sorted(quarterly.items())}
            if m not in _INSTANT:
                for fy_end, fy in annual.items():
                    if fy_end in series or not fy.get("start"):
                        continue
                    qs = [v for k, v in quarterly.items() if fy["start"] <= quarterly[k]["start"] and k < fy_end]
                    if len(qs) == 3:
                        series[fy_end] = fy["val"] - sum(q["val"] for q in qs)
                        derived.setdefault(m, []).append(fy_end)
                series = dict(sorted(series.items()))
        keep = years if period == "annual" else years * 4
        out[m] = dict(list(series.items())[-keep:])
    return {"symbol": symbol, "company": data["name"], "period": period, "metrics": out,
            "derived_q4": derived or None, "source": "SEC XBRL companyfacts (as reported, period end dates as keys)"}
