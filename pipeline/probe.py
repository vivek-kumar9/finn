"""Check which data sources answer from the machine this runs on.

NSE is known to block cloud IPs, so I ran this from my laptop, from Render and
from GitHub Actions before deciding where the ingest should run.

    python -m pipeline.probe
"""
import re
import sys
import time
from datetime import date, timedelta

import requests

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)
TIMEOUT = 20
BSE_HEADERS = {
    "User-Agent": UA,
    "Referer": "https://www.bseindia.com/",
    "Origin": "https://www.bseindia.com",
    "Accept": "application/json, text/plain, */*",
}

# Later checks reuse URLs that earlier ones found (a PDF link from the API, say).
state: dict[str, str] = {}


def check_yfinance() -> str:
    import yfinance as yf

    df = yf.Ticker("RELIANCE.NS").history(period="5d")
    if df.empty:
        raise RuntimeError("empty frame")
    return f"{len(df)} rows, last close {df['Close'].iloc[-1]:.1f}"


def check_nse_api() -> str:
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.nseindia.com/",
    })
    s.get("https://www.nseindia.com/", timeout=TIMEOUT)  # sets the cookies the API wants
    r = s.get(
        "https://www.nseindia.com/api/corporate-announcements",
        params={"index": "equities", "symbol": "RELIANCE"},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    rows = r.json()
    if not rows:
        raise RuntimeError("no rows")
    if rows[0].get("attchmntFile"):
        state["nse_pdf"] = rows[0]["attchmntFile"]
    return f"{len(rows)} filings, latest: {rows[0].get('desc', '')[:40]}"


def check_nse_pdf() -> str:
    return _fetch_pdf(state.get("nse_pdf"), {"User-Agent": UA})


def check_bse_api() -> str:
    today = date.today()
    params = {
        "pageno": 1,
        "strCat": -1,
        "strPrevDate": (today - timedelta(days=14)).strftime("%Y%m%d"),
        "strScrip": "500325",  # RELIANCE
        "strSearch": "P",
        "strToDate": today.strftime("%Y%m%d"),
        "strType": "C",
        "subcategory": -1,
    }
    r = requests.get(
        "https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w",
        params=params,
        headers=BSE_HEADERS,
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    rows = r.json().get("Table", [])
    if not rows:
        raise RuntimeError("no rows")
    if rows[0].get("ATTACHMENTNAME"):
        state["bse_pdf"] = (
            "https://www.bseindia.com/xml-data/corpfiling/AttachLive/"
            + rows[0]["ATTACHMENTNAME"]
        )
    return f"{len(rows)} filings, latest: {rows[0].get('NEWSSUB', '')[:40]}"


def check_bse_pdf() -> str:
    return _fetch_pdf(state.get("bse_pdf"), BSE_HEADERS)


def check_screener_page() -> str:
    r = requests.get(
        "https://www.screener.in/company/RELIANCE/consolidated/",
        headers={"User-Agent": UA},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    if 'id="quarters"' not in r.text:
        raise RuntimeError("quarterly results section missing")
    links = re.findall(r'href="([^"]+)"[^>]*>\s*Transcript', r.text)
    if links:
        state["transcript"] = links[0]
    return f"{len(r.text) // 1024} KB, {len(links)} transcript links"


def check_screener_search() -> str:
    r = requests.get(
        "https://www.screener.in/api/company/search/",
        params={"q": "kaynes", "v": 3, "fts": 1},
        headers={"User-Agent": UA},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    hits = r.json()
    if not hits:
        raise RuntimeError("no hits")
    return f"{len(hits)} hits, first: {hits[0].get('name', '')}"


def check_transcript_pdf() -> str:
    return _fetch_pdf(state.get("transcript"), BSE_HEADERS)


def _fetch_pdf(url: str | None, headers: dict) -> str:
    if not url:
        raise RuntimeError("no URL from the earlier check")
    r = requests.get(url, headers=headers, timeout=TIMEOUT)
    r.raise_for_status()
    if not r.content.startswith(b"%PDF"):
        raise RuntimeError(f"not a PDF ({r.headers.get('content-type', '?')})")
    return f"{len(r.content) // 1024} KB"


CHECKS = [
    ("yfinance", check_yfinance),
    ("nse_api", check_nse_api),
    ("nse_pdf", check_nse_pdf),
    ("bse_api", check_bse_api),
    ("bse_pdf", check_bse_pdf),
    ("screener_page", check_screener_page),
    ("screener_search", check_screener_search),
    ("transcript_pdf", check_transcript_pdf),
]


def run() -> list[dict]:
    state.clear()
    results = []
    for name, check in CHECKS:
        t0 = time.perf_counter()
        try:
            detail, ok = check(), True
        except Exception as e:  # report every failure instead of stopping at the first
            detail, ok = f"{type(e).__name__}: {e}"[:160], False
        ms = round((time.perf_counter() - t0) * 1000)
        results.append({"source": name, "ok": ok, "ms": ms, "detail": detail})
    return results


if __name__ == "__main__":
    results = run()
    for r in results:
        status = "PASS" if r["ok"] else "FAIL"
        print(f"{status}  {r['source']:<16} {r['ms']:>6} ms  {r['detail']}")
    sys.exit(0 if all(r["ok"] for r in results) else 1)
