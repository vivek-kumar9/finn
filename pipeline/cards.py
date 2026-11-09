"""Turn a story's lead filing into the card the Brief shows.

Models only read the PDF and return what is printed in it. Everything the
investor compares (QoQ, YoY, margins, yields) is computed here.

    python -m pipeline.cards --limit 50
"""
import argparse
import io
import os
from datetime import date

import requests
from pypdf import PdfReader, PdfWriter

from pipeline import db, llm
from pipeline.nse import UA
from pipeline.stories import WEIGHT

PROMPT_VERSION = "cards-v4"
MIN_WEIGHT = 4  # below this a story stays a one-line routine item


# ---------------------------------------------------------------- PDFs

def pdf_or_none(url: str | None, max_pages: int) -> bytes | None:
    if not url:
        return None
    try:
        return fetch_pdf(url, max_pages)
    except ValueError:
        return None


def download(url: str) -> bytes:
    r = requests.get(url, headers={"User-Agent": UA}, timeout=60)
    r.raise_for_status()
    if not r.content.startswith(b"%PDF"):
        raise ValueError("attachment is not a PDF")
    return r.content


def _subset(content: bytes, pages: list[int]) -> bytes:
    reader = PdfReader(io.BytesIO(content))
    writer = PdfWriter()
    for i in pages:
        writer.add_page(reader.pages[i])
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def fetch_pdf(url: str, max_pages: int) -> bytes:
    """The first pages only: appointment details and announcements are up front."""
    content = download(url)
    try:
        n = len(PdfReader(io.BytesIO(content)).pages)
        return content if n <= max_pages else _subset(content, list(range(max_pages)))
    except Exception:
        if len(content) > 15_000_000:
            raise
        return content


_TABLE_MARKS = ("revenue from operations", "interest earned", "profit before tax",
                "quarter ended", "three months ended")


def results_pages(content: bytes, max_pages: int = 4) -> tuple[bytes, list[int]]:
    """Find the results table instead of trusting it to be on page 1.

    Big filings open with a cover letter, the auditor's review report and
    sometimes a press release, so the table can be on page 12. Pages are
    scored on the line items a results table must carry; consolidated wins
    ties. Scanned PDFs have no text layer, so they fall back to the first pages.
    """
    reader = PdfReader(io.BytesIO(content))
    scores = []
    for i, page in enumerate(reader.pages):
        try:
            text = " ".join((page.extract_text() or "").lower().split())
        except Exception:
            text = ""
        score = sum(mark in text for mark in _TABLE_MARKS)
        if score >= 3:
            scores.append((score + 0.5 * ("consolidated" in text), i))
    if not scores:
        first = list(range(min(len(reader.pages), 8)))
        return _subset(content, first), [i + 1 for i in first]
    best = max(scores)[1]
    # Keep the best page, the page after it (tables spill over) and any
    # other qualifying pages, up to the cap, in document order.
    picked = {best, best + 1} & set(range(len(reader.pages)))
    for _, i in sorted(scores, reverse=True):
        if len(picked) >= max_pages:
            break
        picked.add(i)
    pages = sorted(picked)[:max_pages]
    return _subset(content, pages), [i + 1 for i in pages]


# ---------------------------------------------------------------- results

_N = {"type": "NUMBER", "nullable": True}
RESULTS_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "basis": {"type": "STRING", "enum": ["consolidated", "standalone"]},
        "unit": {"type": "STRING", "enum": ["crore", "lakh", "million", "thousand", "rupees"]},
        "is_bank": {"type": "BOOLEAN"},
        "page": {"type": "INTEGER"},
        "periods": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "role": {"type": "STRING", "enum": ["current", "previous_quarter", "year_ago_quarter"]},
                    "period_end": {"type": "STRING"},
                    "revenue": _N, "total_income": _N, "other_income": _N, "finance_costs": _N, "depreciation": _N,
                    "pbt_before_exceptional": _N, "pbt": _N, "pat": _N, "ebitda_reported": _N,
                    "interest_earned": _N, "interest_expended": _N, "operating_profit": _N,
                },
                "required": ["role", "period_end"],
            },
        },
    },
    "required": ["basis", "unit", "is_bank", "periods"],
}

RESULTS_MODELS = [m for m in os.environ.get(
    "RESULTS_MODELS", "gemini-3.5-flash,gemini-3.8-flash,gemini-3.7-flash,gemini-3.6-flash,gemini-flash-latest"
).split(",") if m]

RESULTS_PROMPT = """This is an Indian listed company's quarterly results filing.
Read the results table and return the three QUARTER columns: the latest quarter
(current), the quarter before it (previous_quarter) and the same quarter a year
earlier (year_ago_quarter). Ignore half-year and full-year columns.

Use the consolidated table if there is one, else standalone.
Copy numbers exactly as printed, in the table's own unit; losses are negative.
- revenue: revenue from operations (not total income). It may be labelled
  "Income from operations", "Revenue from operations (net)" or "Value of sales and
  services" followed by a GST line; use the line after GST is deducted.
- total_income: the total income line
- pbt_before_exceptional: profit before exceptional items and tax (null if no such line)
- pbt: profit before tax; pat: profit for the period (before other comprehensive income)
- ebitda_reported: only if the table prints EBITDA
- For banks set is_bank and fill interest_earned, interest_expended and operating_profit
  (operating profit before provisions and contingencies).
- period_end as YYYY-MM-DD. page: the PDF page number of the table.
Use null for anything not printed. Never estimate."""

_TO_CRORE = {"crore": 1, "lakh": 0.01, "million": 0.1, "thousand": 1e-4, "rupees": 1e-7}


def fiscal_quarter(period_end: str) -> str:
    d = date.fromisoformat(period_end)
    q = {6: 1, 9: 2, 12: 3, 3: 4}.get(d.month)
    fy = d.year + 1 if d.month >= 4 else d.year
    return f"Q{q} FY{fy % 100:02d}" if q else d.strftime("%b %Y")


def _pct(now, before):
    if now is None or before is None or before == 0 or (now < 0) != (before < 0):
        return None  # a swing from loss to profit has no meaningful %
    return round((now - before) / abs(before) * 100, 1)


def results_card(x: dict, url: str) -> dict:
    """Pure arithmetic over the extracted table. Raises if the extraction is unusable."""
    k = _TO_CRORE[x["unit"]]
    periods = {p["role"]: p for p in x["periods"]}
    if "current" not in periods:
        raise ValueError("no current-quarter column found")

    def metrics(p: dict | None) -> dict:
        if p is None:
            return {}
        v = {key: (p.get(key) * k if p.get(key) is not None else None) for key in p if key not in ("role", "period_end")}
        if v.get("revenue") is None and v.get("total_income") is not None:
            v["revenue"] = v["total_income"] - (v.get("other_income") or 0)
        if x["is_bank"]:
            nii = (v["interest_earned"] - v["interest_expended"]
                   if v.get("interest_earned") is not None and v.get("interest_expended") is not None else None)
            return {"NII": nii, "Operating profit": v.get("operating_profit"), "PBT": v.get("pbt"), "PAT": v.get("pat"),
                    "_base": v.get("interest_earned")}
        ebitda = v.get("ebitda_reported")
        if ebitda is None:
            pbt0 = v.get("pbt_before_exceptional", v.get("pbt"))
            if pbt0 is None:
                pbt0 = v.get("pbt")
            parts = [pbt0, v.get("finance_costs"), v.get("depreciation")]
            if None not in parts:
                ebitda = parts[0] + parts[1] + parts[2] - (v.get("other_income") or 0)
        return {"Revenue": v.get("revenue"), "EBITDA": ebitda, "PBT": v.get("pbt"), "PAT": v.get("pat"),
                "_base": v.get("revenue")}

    now = metrics(periods["current"])
    prev = metrics(periods.get("previous_quarter"))
    ago = metrics(periods.get("year_ago_quarter"))

    rows = [
        {"metric": m, "value": round(now[m], 1) if now[m] is not None else None,
         "qoq": _pct(now[m], prev.get(m)), "yoy": _pct(now[m], ago.get(m))}
        for m in now if not m.startswith("_")
    ]

    def margin(m, src):
        base = src.get("_base")
        return None if not base or src.get(m) is None else src[m] / base * 100

    margin_metric = "Operating profit" if x["is_bank"] else "EBITDA"
    margins = {}
    for m in (margin_metric, "PAT"):
        cur = margin(m, now)
        if cur is not None:
            margins[m] = {
                "now": round(cur, 1),
                "qoq_bps": round((cur - margin(m, prev)) * 100) if margin(m, prev) is not None else None,
                "yoy_bps": round((cur - margin(m, ago)) * 100) if margin(m, ago) is not None else None,
            }

    checks = []
    top, bottom = ("NII", "PAT") if x["is_bank"] else ("Revenue", "PAT")
    if not x["is_bank"] and (now["Revenue"] or 0) <= 0:
        raise ValueError("revenue missing or non-positive")
    if now.get(bottom) is None or not prev and not ago:
        raise ValueError("incomplete table: need PAT and at least one comparison column")
    if all(r["qoq"] is None and r["yoy"] is None for r in rows if r["metric"] == top):
        raise ValueError("incomplete table: no comparison for the top line")
    if now.get("PBT") and now.get("PAT") and now["PBT"] > 0 and now["PAT"] > now["PBT"] * 1.3:
        checks.append("PAT well above PBT: check for a tax credit or a misread line")

    return {
        "type": "results",
        "period": fiscal_quarter(periods["current"]["period_end"]),
        "basis": x["basis"],
        "unit": "₹ cr",
        "bank": x["is_bank"],
        "ebitda_derived": not x["is_bank"] and periods["current"].get("ebitda_reported") is None,
        "rows": rows,
        "margins": margins,
        "source": {"url": url, "page": x.get("page")},
        "checks": checks,
    }


# ---------------------------------------------------------------- leadership

LEADERSHIP_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "changes": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "name": {"type": "STRING"},
                    "role": {"type": "STRING"},
                    "change": {"type": "STRING", "enum": ["appointed", "resigned", "ceased", "retired", "redesignated"]},
                    "effective_date": {"type": "STRING", "nullable": True},
                    "reason": {"type": "STRING", "nullable": True},
                    "profile": {"type": "STRING", "nullable": True},
                },
                "required": ["name", "role", "change"],
            },
        },
    },
    "required": ["changes"],
}

LEADERSHIP_PROMPT = """This is an Indian listed company's disclosure about a change in
directors or key managerial personnel. List every person whose position changes.
- role: their position as written (e.g. "Chief Financial Officer", "Independent Director")
- reason: only if stated (e.g. "completion of tenure", "personal reasons")
- profile: the brief profile the filing gives for an incoming person, condensed
  to at most 60 words, using only the filing's words. null if the filing has none."""

BIO_PROMPT = """Write a 3-line background of {name}, newly appointed {role} at {company},
using ONLY the search snippets below. Cover past roles, years of experience and
education where the snippets state them. If the snippets are about someone else
or say too little, return {{"bio": null}}.

Return JSON: {{"bio": "<three short lines separated by \\n>" | null, "sources": [<urls you used>]}}

Snippets:
{snippets}"""


def _web_bio(name: str, role: str, company: str) -> dict | None:
    key = os.environ.get("TAVILY_API_KEY")
    if not key:
        return None
    r = requests.post(
        "https://api.tavily.com/search",
        headers={"Authorization": f"Bearer {key}"},
        json={"query": f'"{name}" {role} {company} experience', "max_results": 5},
        timeout=30,
    )
    r.raise_for_status()
    results = r.json().get("results", [])
    if not results:
        return None
    snippets = "\n\n".join(f"[{x['url']}]\n{x.get('content', '')[:800]}" for x in results)
    out = llm.gemini_json(BIO_PROMPT.format(name=name, role=role, company=company, snippets=snippets),
                          purpose="bio", model=llm.GEMINI_LITE)
    return out if out.get("bio") else None


def leadership_card(x: dict, company: str, url: str) -> dict:
    incoming = [c for c in x["changes"] if c["change"] in ("appointed", "redesignated")]
    outgoing = [c for c in x["changes"] if c["change"] in ("resigned", "ceased", "retired")]
    for person in incoming:
        if person.get("profile"):
            person["bio"], person["bio_source"] = person["profile"], "filing"
        else:
            web = _web_bio(person["name"], person["role"], company)
            if web:
                person["bio"], person["bio_source"], person["bio_links"] = web["bio"], "web", web.get("sources", [])
    return {"type": "leadership", "incoming": incoming, "outgoing": outgoing, "source": {"url": url}}


# ---------------------------------------------------------------- everything else

FACTS = {
    "M&A": {"target": "STRING", "deal_value_cr": "NUMBER", "stake_pct": "NUMBER", "consideration": "STRING"},
    "fund_raise": {"instrument": "STRING", "amount_cr": "NUMBER", "issue_price": "NUMBER"},
    "litigation": {"authority": "STRING", "amount_cr": "NUMBER", "company_view": "STRING"},
    "pledging": {"promoter": "STRING", "pledged_pct_before": "NUMBER", "pledged_pct_after": "NUMBER"},
    "capex": {"amount_cr": "NUMBER", "capacity": "STRING", "timeline": "STRING"},
    "insider_trading": {"person": "STRING", "action": "STRING", "value_cr": "NUMBER"},
    "credit_rating": {"agency": "STRING", "instrument": "STRING", "rating": "STRING",
                      "action": "STRING"},
    "press_release": {"order_value_cr": "NUMBER", "counterparty": "STRING"},
    "bonus_split": {"ratio": "STRING", "record_date": "STRING"},
    "dividend": {"per_share": "NUMBER", "kind": "STRING", "record_date": "STRING"},
}

GENERIC_PROMPT = """This is an Indian listed company's stock-exchange filing, category: {category}.
Return:
- summary: one sentence, at most 25 words, stating what happened and the key number.
  Plain words, no adjectives like "significant" or "strategic".
- facts: the listed fields, only where the filing states them; amounts in ₹ crore.
  For credit_rating, action is one of assigned / upgraded / downgraded / reaffirmed.
Use null for anything the filing does not state."""


def generic_card(category: str, pdf: bytes | None, text: str, symbol: str, url: str | None) -> dict:
    fields = FACTS.get(category, {})
    schema = {
        "type": "OBJECT",
        "properties": {
            "summary": {"type": "STRING"},
            "facts": {"type": "OBJECT", "nullable": True,
                      "properties": {k: {"type": t, "nullable": True} for k, t in fields.items()}}
            if fields else {"type": "OBJECT", "nullable": True, "properties": {"note": {"type": "STRING", "nullable": True}}},
        },
        "required": ["summary"],
    }
    prompt = GENERIC_PROMPT.format(category=category)
    if pdf is None:
        prompt += f"\n\nFiling text:\n{text}"
    out = llm.gemini_json(prompt, purpose=f"card:{category}", model=llm.GEMINI_LITE, pdf=pdf, schema=schema)
    facts = {k: v for k, v in (out.get("facts") or {}).items() if v not in (None, "")}

    card = {"type": "summary", "category": category, "summary": out["summary"], "facts": facts,
            "source": {"url": url}, "routine": False}
    if category == "credit_rating" and str(facts.get("action", "")).lower().startswith("reaffirm"):
        card["routine"] = True  # a reaffirmation is not news
    if category == "litigation" and isinstance(facts.get("amount_cr"), (int, float)) and facts["amount_cr"] < 1:
        card["routine"] = True  # a sub-crore penalty doesn't move a listed company
    if category == "dividend" and facts.get("per_share"):
        last = db.client().table("prices").select("close").eq("symbol", symbol) \
            .order("date", desc=True).limit(1).execute().data
        if last:
            card["facts"]["yield_pct"] = round(facts["per_share"] / float(last[0]["close"]) * 100, 2)
    return card


# ---------------------------------------------------------------- driver

def build(story: dict, company: str) -> dict:
    cat, url = story["category"], story.get("lead_url")
    if cat == "results":
        if not url:
            raise ValueError("results story without a PDF")
        def extract():
            pdf, pages = results_pages(download(url))
            x = None
            for i, model in enumerate(RESULTS_MODELS):
                try:
                    x = llm.gemini_json(RESULTS_PROMPT, purpose="results", model=model, fallback=False,
                                        pdf=pdf, schema=RESULTS_SCHEMA)
                    break
                except llm.QuotaExhausted:
                    if i == len(RESULTS_MODELS) - 1:
                        raise
            # The model numbers pages within the excerpt; map back to the filing.
            if x.get("page") and 1 <= x["page"] <= len(pages):
                x["page"] = pages[x["page"] - 1]
            return x

        x = llm.cached([url, PROMPT_VERSION, "results"], extract)
        return results_card(x, url)
    if cat == "management_change":
        if not url:
            raise ValueError("management change without a PDF")
        def extract():
            pdf = pdf_or_none(url, 6)
            prompt = LEADERSHIP_PROMPT if pdf else f"{LEADERSHIP_PROMPT}\n\nFiling text:\n{story['headline']}"
            return llm.gemini_json(prompt, purpose="leadership", model=llm.GEMINI_LITE,
                                   pdf=pdf, schema=LEADERSHIP_SCHEMA)

        x = llm.cached([url, PROMPT_VERSION, "leadership"], extract)
        return leadership_card(x, company, url)
    return llm.cached([url or story["id"], PROMPT_VERSION, cat], lambda: generic_card(
        cat, pdf_or_none(url, 4), story["headline"], story["symbol"], url))


def process_pending(limit: int = 15) -> tuple[int, int, int]:
    names = {s["symbol"]: s["name"] for s in db.select("stocks", "symbol,name")}
    pending = db.client().table("stories").select("*").eq("card_status", "pending") \
        .order("base_score", desc=True).limit(limit).execute().data
    done = failed = skipped = 0
    for s in pending:
        if WEIGHT[s["category"]] < MIN_WEIGHT:
            update, skipped = {"card_status": "skipped"}, skipped + 1
        else:
            try:
                card = build(s, names.get(s["symbol"], s["symbol"]))
                update, done = {"card": card, "card_status": "done", "summary": card.get("summary")}, done + 1
                print(f"{s['id']}: {card['type']} card ok")
            except llm.QuotaExhausted:
                print(f"{s['id']}: model quota used up for today, left pending")
                continue
            except Exception as e:
                print(f"{s['id']}: card failed: {type(e).__name__}: {e}")
                update, failed = {"card": {"error": str(e)[:300]}, "card_status": "failed"}, failed + 1
        db.client().table("stories").update(update).eq("id", s["id"]).execute()
    return done, failed, skipped


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv()
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--retry-failed", action="store_true")
    ap.add_argument("--redo-results", action="store_true", help="rebuild every results card")
    ap.add_argument("--show", metavar="STORY_ID", help="print the stored extraction for one story")
    args = ap.parse_args()
    if args.show:
        import hashlib
        import json

        story = db.client().table("stories").select("*").eq("id", args.show).execute().data[0]
        print("lead:", story["lead_url"])
        key = hashlib.sha256("|".join([story["lead_url"], PROMPT_VERSION, "results"]).encode()).hexdigest()
        hit = db.client().table("research_cache").select("value").eq("key", key).execute().data
        print(json.dumps(hit[0]["value"] if hit else "no cached extraction", indent=1))
        raise SystemExit
    if args.redo_results:
        db.client().table("stories").update({"card_status": "pending"}).eq("category", "results").execute()
    if args.retry_failed:
        db.client().table("stories").update({"card_status": "pending"}).eq("card_status", "failed").execute()
    print("done %d, failed %d, skipped %d" % process_pending(args.limit))
