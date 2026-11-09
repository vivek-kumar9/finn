"""On-demand research for any NSE symbol, streamed section by section.

Sources: screener's multi-year tables (compiled from filings), the company's
latest annual report and its last four concall transcripts. Nothing else:
no news, no broker notes, and no model background knowledge. Every model
output is cached by document, so a second run of the same company is free.

    python -m pipeline.research KAYNES     # prints each section as it lands
"""
import io
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone
from typing import Iterator

import requests
from pypdf import PdfReader

from pipeline import db, fiscal, guidance, llm, screener
from pipeline.cards import _subset
from pipeline.nse import UA

VERSION = "research-v2"
DOC_HEADERS = {"User-Agent": UA, "Referer": "https://www.bseindia.com/", "Accept": "application/pdf,*/*"}
STRONG_MODELS = ["gemini-3.5-flash", "gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash"]


def _download(url: str, max_mb: int = 40) -> bytes:
    r = requests.get(url, headers=DOC_HEADERS, timeout=90)
    r.raise_for_status()
    if not r.content.startswith(b"%PDF"):
        raise ValueError("not a PDF")
    if len(r.content) > max_mb * 1e6:
        raise ValueError("document too large")
    return r.content


def _strong(prompt: str, purpose: str, lite_ok: bool = False, **kw) -> dict:
    """Full Flash models in turn. Documents can fall back to the Lite models
    when lite_ok (prose, with every quote verified afterwards); text-only
    prompts fall back to Groq's largest open model. The model that answered
    is recorded on the result."""
    chain = STRONG_MODELS + (llm.GEMINI_FALLBACKS if lite_ok else [])
    for model in chain:
        try:
            out = llm.gemini_json(prompt, purpose=purpose, model=model, fallback=False, **kw)
            out["_model"] = model
            return out
        except (llm.QuotaExhausted, llm.ModelUnavailable) as e:
            print(f"{model}: {e}")
            continue
    if kw.get("pdf") is not None:
        raise llm.QuotaExhausted("no Gemini model is free to read documents right now")
    out = llm.groq_json(prompt + "\n\nReturn JSON only.", purpose=purpose, model=llm.GROQ_LARGE)
    out["_model"] = llm.GROQ_LARGE
    return out


# ---------------------------------------------------------------- numbers (no model)

def _series(table: dict | None, *names: str) -> list[tuple[str, float | None]]:
    if not table:
        return []
    row = screener._pick(table["rows"], *names)
    if not row:
        return []
    return list(zip(table["columns"], row))


def _years(series):
    return [(c, v) for c, v in series if re.match(r"[A-Z][a-z]{2} \d{4}$", c)]


def _cagr(series, years: int) -> float | None:
    s = [v for _, v in _years(series)]
    if len(s) <= years or not s[-1] or not s[-1 - years] or s[-1] <= 0 or s[-1 - years] <= 0:
        return None
    return round(((s[-1] / s[-1 - years]) ** (1 / years) - 1) * 100, 1)


def numbers(page: dict) -> dict:
    a, b, c, r = page["annual"], page["balance"], page["cash"], page["ratios"]
    bank = "Financing Profit" in (a or {}).get("rows", {})
    sales = _series(a, "Sales", "Revenue")
    op = _series(a, "Operating Profit", "Financing Profit")
    pat = _series(a, "Net Profit")
    opm = _series(a, "OPM %", "Financing Margin %")

    # A bank's "financing profit" on screener is after interest and provisions
    # and often negative, so operating margin and D/E are left out for banks.
    quarters = [{**q, "label": fiscal.label(date.fromisoformat(q["end"])),
                 "opm": round(q["op_profit"] / q["sales"] * 100, 1)
                 if not bank and q["sales"] and q["op_profit"] is not None else None}
                for q in page["quarters"][-8:]]

    years = [col for col, _ in _years(sales)][-6:]
    def pick(series, col):
        return dict(series).get(col)

    annual = [{"label": f"FY{int(col[-2:]):02d}" if col.startswith("Mar") else col,
               "sales": pick(sales, col), "op_profit": pick(op, col), "pat": pick(pat, col),
               "opm": None if bank else pick(opm, col)}
              for col in years]

    equity = [(x + y if x is not None and y is not None else None) for x, y in zip(
        [v for _, v in _series(b, "Equity Capital")], [v for _, v in _series(b, "Reserves")])]
    debt = [v for _, v in _series(b, "Borrowings", "Borrowing")]
    bcols = (b or {}).get("columns", [])
    balance = []
    for i in range(max(0, len(bcols) - 4), len(bcols)):
        e = equity[i] if i < len(equity) else None
        d = debt[i] if i < len(debt) else None
        balance.append({"label": bcols[i], "equity": e, "debt": d,
                        "de": round(d / e, 2) if d is not None and e and not bank else None})
    wc = {}
    for name in ("Debtor Days", "Inventory Days", "Days Payable", "Cash Conversion Cycle", "ROCE %", "ROE %"):
        s = [(col, v) for col, v in _series(r, name)][-4:]
        if s:
            wc[name] = s

    cfo = dict(_series(c, "Cash from Operating Activity"))
    fcf = dict(_series(c, "Free Cash Flow"))
    cash = []
    for col in years[-5:]:
        p_, o = pick(pat, col), cfo.get(col)
        cash.append({"label": f"FY{int(col[-2:]):02d}", "pat": p_, "cfo": o,
                     "ratio": round(o / p_, 2) if o is not None and p_ and p_ > 0 else None,
                     "fcf": fcf.get(col)})
    tot_cfo = sum(x["cfo"] for x in cash if x["cfo"] is not None)
    tot_pat = sum(x["pat"] for x in cash if x["pat"] is not None)

    return {
        "bank": bank,
        "basis": page.get("basis"),
        "quarters": quarters,
        "annual": annual,
        "growth": {"sales_3y": _cagr(sales, 3), "sales_5y": _cagr(sales, 5),
                   "pat_3y": _cagr(pat, 3), "pat_5y": _cagr(pat, 5)},
        "balance": balance,
        "working_capital": wc,
        "cash": cash,
        "cash_5y_ratio": round(tot_cfo / tot_pat, 2) if tot_pat > 0 else None,
        "top": {k: page["top"].get(k) for k in ("Market Cap", "Current Price", "Stock P/E", "ROCE", "ROE")},
    }


def facts(n: dict) -> list[dict]:
    """Numbered facts the bull/bear writer may cite. Built in code."""
    out = []

    def add(text):
        out.append({"id": f"N{len(out) + 1}", "text": text})

    g = n["growth"]
    if g["sales_3y"] is not None:
        add(f"Revenue grew {g['sales_3y']}% a year over 3 years" + (f" and {g['sales_5y']}% over 5" if g["sales_5y"] is not None else ""))
    if g["pat_3y"] is not None:
        add(f"Net profit grew {g['pat_3y']}% a year over 3 years")
    a = [x for x in n["annual"] if x["opm"] is not None]
    if len(a) >= 3:
        add(f"Operating margin {a[-1]['opm']}% in {a[-1]['label']} vs {a[-3]['opm']}% in {a[-3]['label']}")
    q = n["quarters"]
    if len(q) >= 5 and q[-1]["sales"] and q[-5]["sales"]:
        add(f"Latest quarter ({q[-1]['label']}) revenue {((q[-1]['sales'] / q[-5]['sales']) - 1) * 100:+.1f}% YoY")
    bal = [x for x in n["balance"] if x["de"] is not None]
    if bal:
        add(f"Debt to equity {bal[-1]['de']} at {bal[-1]['label']}" + (f", {bal[0]['de']} at {bal[0]['label']}" if len(bal) > 1 else ""))
    ccc = n["working_capital"].get("Cash Conversion Cycle")
    if ccc and len(ccc) >= 2 and ccc[0][1] is not None and ccc[-1][1] is not None:
        add(f"Cash conversion cycle {ccc[-1][1]:.0f} days in {ccc[-1][0]} vs {ccc[0][1]:.0f} in {ccc[0][0]}")
    if n["cash_5y_ratio"] is not None:
        add(f"Cash from operations was {n['cash_5y_ratio']}× net profit over 5 years")
    roce = n["working_capital"].get("ROCE %")
    if roce and roce[-1][1] is not None:
        add(f"ROCE {roce[-1][1]:.0f}% in {roce[-1][0]}")
    return out


# ---------------------------------------------------------------- annual report snapshot

SNAPSHOT_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "what_it_does": {"type": "STRING"},
        "segments": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "name": {"type": "STRING"}, "share_pct": {"type": "NUMBER", "nullable": True},
            "detail": {"type": "STRING", "nullable": True}}, "required": ["name"]}},
        "customers_and_markets": {"type": "STRING", "nullable": True},
        "quote": {"type": "STRING"},
        "page": {"type": "INTEGER"},
    },
    "required": ["what_it_does", "segments", "quote", "page"],
}
SNAPSHOT_PROMPT = """These are pages from an Indian listed company's annual report.
Using ONLY these pages:
- what_it_does: what the company makes or sells and to whom, at most 50 words, plain words
- segments: its business segments, with each one's share of revenue if the pages state it
- customers_and_markets: key customer types and geographies, if stated (one sentence)
- quote: one sentence copied verbatim from the pages that supports what_it_does
- page: the page of the quote within these pages"""
_AR_MARKS = {"management discussion": 3, "business overview": 3, "our business": 2, "segment": 1,
             "revenue mix": 2, "product portfolio": 1, "customers": 1, "manufacturing facilities": 1}


def snapshot(ar_url: str, stats: dict) -> dict:
    def extract():
        pdf = _download(ar_url, max_mb=60)
        reader = PdfReader(io.BytesIO(pdf))
        scored = []
        for i, page in enumerate(reader.pages[:160]):
            try:
                t = (page.extract_text() or "").lower()
            except Exception:
                continue
            score = sum(w for k, w in _AR_MARKS.items() if k in t)
            if score:
                scored.append((score, i))
        picked = sorted(i for _, i in sorted(scored, reverse=True)[:12]) or list(range(min(12, len(reader.pages))))
        excerpt = _subset(pdf, picked)
        out = _strong(SNAPSHOT_PROMPT, "snapshot", lite_ok=True, pdf=excerpt, schema=SNAPSHOT_SCHEMA)
        texts = guidance.page_texts(excerpt)
        found = guidance.verify(out.get("quote", ""), out.get("page", 1), texts)
        out["page"] = picked[found - 1] + 1 if found else None
        out["verified"] = found is not None
        out["pages_read"] = len(picked)
        return out

    return llm.cached([ar_url, VERSION, "snapshot"], extract, stats)


# ---------------------------------------------------------------- transcripts

def read_call(url: str, stats: dict) -> dict:
    def extract():
        pdf = _download(url)
        n_pages = len(PdfReader(io.BytesIO(pdf)).pages)
        if n_pages > 45:
            pdf = _subset(pdf, list(range(45)))
        out = _strong(guidance.GUIDANCE_PROMPT, "guidance", lite_ok=True, pdf=pdf, schema=guidance.GUIDANCE_SCHEMA)
        texts = guidance.page_texts(pdf)
        has_text = any(texts)
        kept, dropped = [], 0
        for it in out.get("items", []):
            page = guidance.verify(it["quote"], it["page"], texts) if has_text else it["page"]
            if page is None:
                dropped += 1
                continue
            kept.append({**it, "page": page})
        return {"call_period": out.get("call_period"), "items": kept, "dropped": dropped,
                "pages": n_pages, "text_layer": has_text, "model": out.get("_model")}

    return llm.cached([url, VERSION, "guidance"], extract, stats)


# ---------------------------------------------------------------- bull vs bear

VIEWS_PROMPT = """You are writing for a portfolio manager. Using ONLY the numbered facts below
(no outside knowledge, no news, nothing the facts don't say), write the three strongest
points for the bull case and the three strongest for the bear case on {name}.
Each point: one sentence, at most 25 words, plain words, and cite the fact ids it rests on.

Return JSON: {{"bull": [{{"point": "...", "refs": ["N1", "G2"]}}], "bear": [...]}}

Facts:
{facts}"""


def views(name: str, all_facts: list[dict], stats: dict) -> dict:
    text = "\n".join(f"{f['id']}: {f['text']}" for f in all_facts)
    prompt = VIEWS_PROMPT.format(name=name, facts=text)
    out = llm.cached([prompt, VERSION, "views"], lambda: _strong(prompt, "views", think="high"), stats)
    ids = {f["id"] for f in all_facts}
    clean = {}
    for side in ("bull", "bear"):
        pts = []
        for p in out.get(side, [])[:3]:
            refs = [r for r in p.get("refs", []) if r in ids]
            if refs:  # a point with nothing behind it doesn't ship
                pts.append({"point": p["point"], "refs": refs})
        clean[side] = pts
    return clean


# ---------------------------------------------------------------- the run

def run(symbol: str) -> Iterator[tuple[str, dict]]:
    started = datetime.now(timezone.utc)
    t0 = time.perf_counter()
    stats: dict = {}

    page = screener.fetch(symbol)
    yield "company", {"symbol": page["symbol"], "name": page["name"], "basis": page["basis"]}

    n = numbers(page)
    yield "numbers", n
    all_facts = facts(n)

    # Promised vs delivered over the last four calls, oldest first.
    calls_meta = [c for c in page["concalls"] if c.get("transcript")][:4][::-1]
    quarters = [guidance.Quarter(date.fromisoformat(q["end"]), q["sales"], q["op_profit"], q["pat"])
                            for q in page["quarters"]]
    if not calls_meta:
        decks = sum(1 for c in page["concalls"] if c.get("ppt"))
        note = (f"{page['name'] or symbol} doesn't publish earnings-call transcripts; only investor presentations "
                f"are filed ({decks} quarters). With nothing management said on record, there are no promises to check."
                if decks else "No earnings-call transcripts are filed for this company, so there are no promises to check.")
        yield "ledger", {"rows": [], "calls": [], "note": note}
    if calls_meta:
        yield "progress", {"text": f"Reading the last {len(calls_meta)} earnings calls"}
    # Transcripts are independent, so they're read side by side; each one is
    # cached by URL, so only new calls ever reach a model.
    results: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(read_call, c["transcript"], stats): c for c in calls_meta}
        for fut in as_completed(futures):
            c = futures[fut]
            month = datetime.fromisoformat(c["month"]).strftime("%b %Y")
            try:
                got = fut.result()
                results[c["month"]] = {**got, "url": c["transcript"], "month": c["month"],
                                       "call_period": got.get("call_period") or month}
                yield "progress", {"text": f"Read the {month} call"}
            except Exception as e:
                results[c["month"]] = {"url": c["transcript"], "month": c["month"], "call_period": month,
                                       "items": [], "error": f"{type(e).__name__}: {e}"[:200]}
                yield "progress", {"text": f"Couldn't read the {month} call"}
    calls = [results[c["month"]] for c in calls_meta]  # oldest first, for revision tracking
    if calls_meta:
        rows = guidance.ledger(calls, quarters)
        yield "ledger", {
            "rows": rows,
            "calls": [{k: c.get(k) for k in ("call_period", "month", "url", "pages", "dropped", "error", "text_layer", "model")}
                      | {"items": len(c.get("items", []))} for c in calls],
        }
        for i, r in enumerate(x for x in rows if x["status"] != "unverifiable"):
            all_facts.append({"id": f"G{i + 1}", "text": f"Guided {r['promise']} ({r['period']}, said on the "
                              f"{r['call']} call): {r['status'].replace('_', ' ')}" + (f", actual {r['actual']}" if r["actual"] else "")})

    if page["annual_reports"]:
        ar = page["annual_reports"][0]
        yield "progress", {"text": f"Reading the FY{ar['year'] % 100:02d} annual report"}
        try:
            snap = snapshot(ar["url"], stats)
            yield "snapshot", {**snap, "url": ar["url"], "year": ar["year"]}
            all_facts.append({"id": "S1", "text": snap["what_it_does"]})
        except Exception as e:
            yield "snapshot", {"error": f"The annual report couldn't be read ({type(e).__name__})."}
    else:
        yield "snapshot", {"error": "No annual report is linked for this company."}

    yield "progress", {"text": "Weighing the bull and bear case"}
    try:
        yield "views", {**views(page["name"] or symbol, all_facts, stats), "facts": all_facts}
    except Exception as e:
        yield "views", {"error": f"{type(e).__name__}: {e}"[:200], "facts": all_facts}

    try:
        used = db.client().table("usage").select("tokens_in,tokens_out").gte("at", started.isoformat()).execute().data
    except Exception:
        used = []
    yield "cost", {
        "model_calls": len(used),
        "tokens": sum((u["tokens_in"] or 0) + (u["tokens_out"] or 0) for u in used),
        "cache_hits": stats.get("hits", 0),
        "seconds": round(time.perf_counter() - t0, 1),
        "rupees": 0,
    }


def warm(symbols: list[str]):
    """Run each company once so later views come from the cache."""
    for sym in symbols:
        t0, read, failed, rows = time.perf_counter(), 0, 0, 0
        try:
            for event, payload in run(sym):
                if event == "ledger":
                    read = sum(1 for c in payload["calls"] if not c.get("error"))
                    failed = sum(1 for c in payload["calls"] if c.get("error"))
                    rows = len(payload["rows"])
            print(f"{sym}: {read} calls read, {failed} failed, {rows} promises, {time.perf_counter() - t0:.0f}s")
        except Exception as e:
            print(f"{sym}: failed: {type(e).__name__}: {e}")


if __name__ == "__main__":
    import sys

    from dotenv import load_dotenv

    load_dotenv()
    if sys.argv[1] == "--warm":
        warm(sys.argv[2:])
        raise SystemExit
    t0 = time.perf_counter()
    for event, payload in run(sys.argv[1]):
        print(f"\n== {event} at {time.perf_counter() - t0:.0f}s\n{json.dumps(payload, indent=1, default=str)[:1500]}", flush=True)
