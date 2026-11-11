"""Standardised financials and document links from screener.in.

Screener is used for two things only: its multi-year tables (which are
compiled from the company's filings) and links to the company's own annual
reports and concall transcripts. Its commentary and summaries are not used.
"""
import re
import time
from datetime import date

import requests
from bs4 import BeautifulSoup

from pipeline.nse import UA

BASE = "https://www.screener.in"
_cache: dict[str, tuple[float, dict]] = {}
TTL = 6 * 3600

MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], start=1)}


def _month_end(label: str) -> date | None:
    m = re.match(r"([A-Z][a-z]{2})\s+(\d{4})", label.strip())
    if not m or m.group(1) not in MONTHS:
        return None
    month, year = MONTHS[m.group(1)], int(m.group(2))
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    return date.fromordinal(nxt.toordinal() - 1)


def _num(text: str) -> float | None:
    t = text.replace(",", "").replace("%", "").replace("₹", "").strip()
    try:
        return float(t)
    except ValueError:
        return None


def _label(cell) -> str:
    # Row labels carry an expand button ("Sales +"); keep the words only.
    text = cell.get_text(" ", strip=True).replace("\xa0", " ")
    return re.sub(r"\s*\+\s*$", "", text).strip()


def _table(soup, section_id: str) -> dict | None:
    sec = soup.find("section", id=section_id)
    table = sec.find("table") if sec else None
    if table is None:
        return None
    heads = [th.get_text(" ", strip=True) for th in table.find("thead").find_all("th")][1:]
    rows = {}
    for tr in table.find("tbody").find_all("tr"):
        cells = tr.find_all("td")
        if len(cells) < 2:
            continue
        rows[_label(cells[0])] = [_num(td.get_text(" ", strip=True)) for td in cells[1:]]
    return {"columns": heads, "rows": rows}


def _pick(rows: dict, *names: str) -> list | None:
    for n in names:
        if n in rows:
            return rows[n]
    return None


def search(q: str) -> list[dict]:
    r = requests.get(f"{BASE}/api/company/search/", params={"q": q, "v": 3, "fts": 1},
                     headers={"User-Agent": UA}, timeout=15)
    r.raise_for_status()
    out = []
    for hit in r.json():
        m = re.match(r"/company/([^/]+)/", hit.get("url", ""))
        if m and hit.get("name"):
            out.append({"symbol": m.group(1), "name": hit["name"]})
    return out[:8]


def fetch(symbol: str) -> dict:
    symbol = symbol.upper()
    hit = _cache.get(symbol)
    if hit and time.time() - hit[0] < TTL:
        return hit[1]
    page = None
    for variant in ("consolidated/", ""):
        r = requests.get(f"{BASE}/company/{symbol}/{variant}", headers={"User-Agent": UA}, timeout=20)
        if r.status_code == 404:
            continue
        r.raise_for_status()
        candidate = parse(r.text)
        # Companies without subsidiaries have an empty consolidated page.
        if candidate["quarters"]:
            page = candidate
            page["basis"] = "consolidated" if variant else "standalone"
            break
    if page is None:
        raise LookupError(f"screener has no financials for {symbol}")
    page["symbol"] = symbol
    _cache[symbol] = (time.time(), page)
    return page


def quarters(symbol: str, basis: str) -> dict[str, dict]:
    """Quarter-end date → {sales, pat} from the consolidated or standalone page."""
    key = f"{symbol.upper()}:{basis}"
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < TTL:
        return hit[1]
    variant = "consolidated/" if basis == "consolidated" else ""
    r = requests.get(f"{BASE}/company/{symbol.upper()}/{variant}", headers={"User-Agent": UA}, timeout=20)
    r.raise_for_status()
    out = {q["end"]: q for q in parse(r.text)["quarters"]}
    _cache[key] = (time.time(), out)
    return out


def parse(html: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    h1 = soup.find("h1")
    top = {}
    for li in soup.select("#top-ratios li"):
        name = li.select_one(".name")
        value = li.select_one(".number")
        if name and value:
            top[name.get_text(strip=True)] = _num(value.get_text())

    q = _table(soup, "quarters")
    quarters = []
    if q:
        sales = _pick(q["rows"], "Sales", "Revenue")
        op = _pick(q["rows"], "Operating Profit", "Financing Profit")
        pat = _pick(q["rows"], "Net Profit")
        dep = _pick(q["rows"], "Depreciation")
        pbt = _pick(q["rows"], "Profit before tax")
        for i, col in enumerate(q["columns"]):
            end = _month_end(col)
            if end and sales:
                quarters.append({
                    "end": end.isoformat(), "sales": sales[i],
                    "op_profit": op[i] if op else None, "pat": pat[i] if pat else None,
                    "depreciation": dep[i] if dep else None, "pbt": pbt[i] if pbt else None,
                })

    annual = _table(soup, "profit-loss")
    balance = _table(soup, "balance-sheet")
    cash = _table(soup, "cash-flow")
    ratios = _table(soup, "ratios")

    reports = []
    for a in soup.select(".annual-reports a"):
        m = re.search(r"(\d{4})", a.get_text(" ", strip=True))
        if m and a.get("href"):
            reports.append({"year": int(m.group(1)), "url": a["href"]})

    concalls = []
    for li in soup.select(".concalls li"):
        when = li.find("div")
        links = {a.get_text(strip=True).lower(): a.get("href") for a in li.find_all("a") if a.get("href")}
        month = _month_end(when.get_text(" ", strip=True)) if when else None
        if month:
            concalls.append({"month": month.isoformat(), "transcript": links.get("transcript"),
                             "ppt": links.get("ppt")})

    return {
        "name": h1.get_text(" ", strip=True) if h1 else None,
        "top": top,
        "quarters": quarters,
        "annual": annual,
        "balance": balance,
        "cash": cash,
        "ratios": ratios,
        "annual_reports": sorted(reports, key=lambda r: -r["year"]),
        "concalls": sorted(concalls, key=lambda c: c["month"], reverse=True),
    }
