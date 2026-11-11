"""Turn a story's lead filing into the card the Brief shows.

Models only read the PDF and return what is printed in it. Everything the
investor compares (QoQ, YoY, margins, yields) is computed here.

    python -m pipeline.cards --limit 50
"""
import argparse
import copy
import io
import os
from datetime import date

import requests
from pypdf import PdfReader, PdfWriter

from pipeline import db, llm
from pipeline.nse import UA
from pipeline.stories import WEIGHT

# Bump to re-read everything. Not bumped for the tax and exceptional-items
# fields: readings that already pass need neither, and failed readings are never
# cached, so only the failures are read again with the new prompt.
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
# Lines a statutory results statement prints and a press release or investor
# deck usually doesn't. Used to choose which of a story's PDFs to read.
_STATEMENT_MARKS = ("revenue from operations", "interest earned", "other income", "total expenses",
                    "finance cost", "depreciation", "profit before tax", "tax expense",
                    "earnings per share", "quarter ended", "three months ended")


def _page_texts(content: bytes) -> list[str]:
    try:
        reader = PdfReader(io.BytesIO(content))
    except Exception:
        return []
    texts = []
    for page in reader.pages:
        try:
            texts.append(" ".join((page.extract_text() or "").lower().split()))
        except Exception:
            texts.append("")
    return texts


def statement_strength(content: bytes) -> int:
    """How many statement line items the best page carries (0 for a scan)."""
    return max((sum(m in t for m in _STATEMENT_MARKS) for t in _page_texts(content)), default=0)


def results_pages(content: bytes, max_pages: int = 4) -> tuple[bytes, list[int]]:
    """Find the results table instead of trusting it to be on page 1.

    Big filings open with a cover letter, the auditor's review report and
    sometimes a press release, so the table can be on page 12. Pages are
    scored on the line items a results table must carry; consolidated wins
    ties. Scanned PDFs have no text layer, so they fall back to the first pages.
    """
    reader = PdfReader(io.BytesIO(content))
    scores = []
    for i, text in enumerate(_page_texts(content)):
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
                    "pbt_before_exceptional": _N, "exceptional_items": _N, "pbt": _N,
                    "tax_expense": _N, "pat": _N, "ebitda_reported": _N,
                    "interest_earned": _N, "interest_expended": _N, "operating_profit": _N,
                },
                "required": ["role", "period_end"],
            },
        },
    },
    "required": ["basis", "unit", "is_bank", "periods"],
}

CHECK_TOLERANCE = 0.02  # 2%, or ₹1 cr for small numbers

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
- exceptional_items: the exceptional items line, negative if it is a charge (null if none)
- pbt: profit before tax. Older formats call it "profit from ordinary activities
  before tax"; a table may print it as "profit before share of associates and tax"
  followed by the share, in which case use the line after the share is added.
- tax_expense: total tax expense (current + deferred + earlier years); a credit is negative
- pat: profit for the period (before other comprehensive income), the total
  before it is split between owners and non-controlling interests
- ebitda_reported: only if the table prints EBITDA
- For banks set is_bank and fill interest_earned, interest_expended and operating_profit
  (operating profit before provisions and contingencies).
- period_end as YYYY-MM-DD. page: the PDF page number of the table.
Use null for anything not printed. Never estimate."""

RESULTS_TEXT_SUFFIX = """

You get the TEXT of the results pages, extracted from the PDF with its layout
kept as spacing, not the PDF itself. Return JSON only, in this shape:
{"basis": "consolidated|standalone", "unit": "crore|lakh|million|thousand|rupees",
 "is_bank": false, "page": 1, "periods": [{"role": "current|previous_quarter|year_ago_quarter",
 "period_end": "YYYY-MM-DD", "revenue": 0, "total_income": 0, "other_income": 0, "finance_costs": 0,
 "depreciation": 0, "pbt_before_exceptional": 0, "exceptional_items": null, "pbt": 0,
 "tax_expense": 0, "pat": 0, "ebitda_reported": null,
 "interest_earned": null, "interest_expended": null, "operating_profit": null}]}

Page text:
"""


def _pages_text(pdf: bytes, limit: int = 14000) -> str:
    try:
        reader = PdfReader(io.BytesIO(pdf))
        parts = []
        for page in reader.pages:
            try:
                parts.append(page.extract_text(extraction_mode="layout") or "")
            except TypeError:  # older pypdf
                parts.append(page.extract_text() or "")
        text = "\n\n".join(parts)
        return text[:limit] if text.strip() else ""
    except Exception:
        return ""


_TO_CRORE = {"crore": 1, "lakh": 0.01, "million": 0.1, "thousand": 1e-4, "rupees": 1e-7}
# Compared after lower-casing and stripping a trailing "s", so "Crores" is "crore".
_UNIT_NAMES = {"crore": "crore", "cr": "crore", "lakh": "lakh", "lac": "lakh", "lacs": "lakh",
               "million": "million", "mn": "million", "thousand": "thousand", "000": "thousand",
               "rupee": "rupees", "rupees": "rupees", "r": "rupees", "inr": "rupees", "rs": "rupees"}


def fiscal_quarter(period_end: str) -> str:
    d = date.fromisoformat(period_end)
    q = {6: 1, 9: 2, 12: 3, 3: 4}.get(d.month)
    fy = d.year + 1 if d.month >= 4 else d.year
    return f"Q{q} FY{fy % 100:02d}" if q else d.strftime("%b %Y")


def _pct(now, before):
    if now is None or before is None or before == 0 or (now < 0) != (before < 0):
        return None  # a swing from loss to profit has no meaningful %
    return round((now - before) / abs(before) * 100, 1)


def _number(v):
    """Models sometimes return '1,234.5', '(12.3)' for a loss, or '-' for blank."""
    if v is None or isinstance(v, (int, float)):
        return v
    t = str(v).strip().replace(",", "").replace("₹", "")
    if t in ("", "-", "–", "—", "NA", "N/A", "nil"):
        return None
    neg = t.startswith("(") and t.endswith(")")
    try:
        n = float(t.strip("()"))
    except ValueError:
        return None
    return -n if neg else n


def _normalise(x: dict) -> dict:
    x["is_bank"] = x.get("is_bank") in (True, "true", "True", 1)
    x["unit"] = _UNIT_NAMES.get(str(x.get("unit") or "").lower().strip(" .s"), "unknown")
    for p in x.get("periods", []):
        for key in list(p):
            if key not in ("role", "period_end"):
                p[key] = _number(p[key])
    return x


def results_card(x: dict, url: str, need_pbt: bool = True) -> dict:
    """Pure arithmetic over the extracted table. Raises if the extraction is unusable.

    x["_fill"], when present, holds screener's figures for lines the reader
    couldn't find; they are used only where the filing's own value is missing,
    and the card says which ones they are."""
    x = _normalise(x)
    if x["unit"] not in _TO_CRORE:
        raise ValueError(f"unknown unit {x['unit']!r}")
    k = _TO_CRORE[x["unit"]]
    periods = {p["role"]: p for p in x["periods"]}
    if "current" not in periods:
        raise ValueError("no current-quarter column found")
    sourced = {}  # metric -> "derived" | "screener", for the current quarter

    def metrics(p: dict | None, role: str) -> dict:
        if p is None:
            return {}
        v = {key: (p.get(key) * k if p.get(key) is not None else None) for key in p if key not in ("role", "period_end")}
        if v.get("revenue") is None and v.get("total_income") is not None:
            v["revenue"] = v["total_income"] - (v.get("other_income") or 0)
        if v.get("pat") is None and v.get("pbt") is not None and v.get("tax_expense") is not None:
            v["pat"] = v["pbt"] - v["tax_expense"]
            if role == "current":
                sourced["PAT"] = "derived"
        pbt = v.get("pbt")
        if pbt is None and v.get("pat") is not None and v.get("tax_expense") is not None:
            pbt = v["pat"] + v["tax_expense"]
            if role == "current":
                sourced["PBT"] = "derived"
        if pbt is None and v.get("pbt_before_exceptional") is not None and v.get("exceptional_items") is not None:
            pbt = v["pbt_before_exceptional"] + v["exceptional_items"]
            if role == "current":
                sourced["PBT"] = "derived"
        if x["is_bank"]:
            nii = (v["interest_earned"] - v["interest_expended"]
                   if v.get("interest_earned") is not None and v.get("interest_expended") is not None else None)
            out = {"NII": nii, "Operating profit": v.get("operating_profit"), "PBT": pbt, "PAT": v.get("pat"),
                   "_base": v.get("interest_earned")}
        else:
            ebitda = v.get("ebitda_reported")
            if ebitda is None:
                pbt0 = v.get("pbt_before_exceptional")
                if pbt0 is None:
                    pbt0 = pbt
                parts = [pbt0, v.get("finance_costs"), v.get("depreciation")]
                if None not in parts:
                    ebitda = parts[0] + parts[1] + parts[2] - (v.get("other_income") or 0)
            out = {"Revenue": v.get("revenue"), "EBITDA": ebitda, "PBT": pbt, "PAT": v.get("pat"),
                   "_base": v.get("revenue")}
        for metric, value in ((x.get("_fill") or {}).get(role) or {}).items():
            if out.get(metric) is None and value is not None:
                out[metric] = value
                if role == "current":
                    sourced[metric] = "screener"
        return out

    now = metrics(periods["current"], "current")
    prev = metrics(periods.get("previous_quarter"), "previous_quarter")
    ago = metrics(periods.get("year_ago_quarter"), "year_ago_quarter")

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
    if now.get(bottom) is None:
        raise ValueError(f"incomplete table: no {bottom} for the current quarter")
    if not prev and not ago:
        raise ValueError(f"incomplete table: no comparison column (read {', '.join(sorted(periods))})")
    if need_pbt and now.get("PBT") is None:
        raise ValueError("incomplete table: PBT missing")
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
        "ebitda_derived": not x["is_bank"] and periods["current"].get("ebitda_reported") is None
                          and sourced.get("EBITDA") != "screener",
        "sourced": sourced,
        "rows": rows,
        "margins": margins,
        "source": {"url": url, "page": x.get("page")},
        "checks": checks,
    }


def screener_fill(x: dict, card: dict, symbol: str) -> dict:
    """Screener's PBT and operating profit (sales less expenses, i.e. EBITDA as
    derived here) for each column, keyed by role. Only called after the reading's
    revenue and PAT have matched screener, so both describe the same table."""
    from pipeline import screener

    table = screener.quarters(symbol, card["basis"])
    fill = {}
    for p in x["periods"]:
        q = table.get(p.get("period_end") or "")
        if not q:
            continue
        values = {"PBT": q.get("pbt")}
        if not card["bank"]:
            values["EBITDA"] = q.get("op_profit")
        fill[p["role"]] = {m: v for m, v in values.items() if v is not None}
    return fill


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

class CheckFailed(ValueError):
    def __init__(self, message: str, card: dict | None = None):
        super().__init__(message)
        self.card = card


def _compare(card: dict, ref: dict | None) -> str | None:
    """None when the card agrees with screener's quarter, else what differs."""
    if not ref:
        return "screener has no figures for that quarter"
    rows = {r["metric"]: r["value"] for r in card["rows"]}
    pairs = [("PAT", rows.get("PAT"), ref.get("pat"))]
    if not card["bank"]:
        pairs.insert(0, ("Revenue", rows.get("Revenue"), ref.get("sales")))
    for name, ours, theirs in pairs:
        if ours is None or theirs is None:
            return f"{name} missing on one side"
        if abs(ours - theirs) > max(1.0, CHECK_TOLERANCE * abs(theirs)):
            return f"{name} read as {ours:,.0f} but screener has {theirs:,.0f}"
    return None


def _without_other_income(x: dict) -> bool:
    """Revenue that equals total income has other income in it. Take it out of
    every column where that is the case; False if the current column isn't."""
    changed = False
    for p in x["periods"]:
        rev, total, other = p.get("revenue"), p.get("total_income"), p.get("other_income")
        if rev is None or not other or (total is not None and abs(rev - total) > 0.005 * abs(total)):
            continue
        p["revenue"] = rev - other
        changed = changed or p["role"] == "current"
    return changed


def _has_pbt(card: dict) -> bool:
    return any(r["metric"] == "PBT" and r["value"] is not None for r in card["rows"])


def cross_check(card: dict, symbol: str, period_end: str) -> dict:
    """Compare the extracted quarter with screener's table, compiled independently
    from the same filings. Revenue and PAT must agree; for banks, PAT only,
    since screener's bank revenue line is defined differently.

    Filings carry both standalone and consolidated tables and readers sometimes
    take one for the other, so a reading that matches the other basis is kept
    and relabelled rather than thrown away."""
    from pipeline import screener

    metrics = ["PAT"] if card["bank"] else ["Revenue", "PAT"]
    claimed = card["basis"]
    problem = _compare(card, screener.quarters(symbol, claimed).get(period_end))
    if problem is None:
        return {"against": "screener", "metrics": metrics}
    other = "standalone" if claimed == "consolidated" else "consolidated"
    try:
        if _compare(card, screener.quarters(symbol, other).get(period_end)) is None:
            card["basis"] = other
            return {"against": "screener", "metrics": metrics, "relabelled": f"{claimed} → {other}"}
    except Exception:
        pass
    raise CheckFailed(problem)


_NOT_THE_TABLE = ("presentation", "press release", "media release", "transcript", "audio",
                  "recording", "conference call", "earnings call", "newspaper", "investor meet")


def results_candidates(story: dict) -> list[str]:
    """Every PDF the company filed around this story, most likely table first.

    A results event is several filings, and the one stories.py leads with is
    sometimes the press release or the deck, which carry headline numbers but
    not the statement. The statement can also arrive under a subject that was
    grouped elsewhere (Eternal's did), so this looks at the company's filings
    in the story's date range, not only the story's own."""
    from datetime import datetime, timedelta

    first = datetime.fromisoformat(story["first_at"]) - timedelta(days=2)
    last = datetime.fromisoformat(story.get("last_at") or story["first_at"]) + timedelta(days=2)
    rows = (db.client().table("filings").select("subject,detail,attachment_url,story_id")
            .eq("symbol", story["symbol"]).gte("filed_at", first.isoformat())
            .lte("filed_at", last.isoformat()).execute().data)

    def rank(f):
        text = f"{f['subject']} {f.get('detail') or ''} {f['attachment_url']}".lower()
        return (("result" in text and not any(w in text for w in _NOT_THE_TABLE)) * 4
                + (f.get("story_id") == story["id"]) * 2
                + ("outcome" in text) - any(w in text for w in _NOT_THE_TABLE) * 4)

    urls = [f["attachment_url"] for f in sorted(rows, key=rank, reverse=True)
            if (f.get("attachment_url") or "").lower().endswith(".pdf")]
    if story.get("lead_url"):
        urls.append(story["lead_url"])
    return list(dict.fromkeys(urls))


def pick_results_pdf(story: dict, enough: int = 7, tries: int = 6) -> tuple[str, bytes]:
    """The candidate whose best page looks most like a results statement.
    Stops at the first one that clearly is; no model call."""
    best = None
    for url in results_candidates(story)[:tries]:
        try:
            content = download(url)
        except Exception:
            continue
        strength = statement_strength(content)
        if best is None or strength > best[0]:
            best = (strength, url, content)
        if strength >= enough:
            break
    if best is None:
        raise ValueError("no results PDF could be downloaded")
    return best[1], best[2]


def build(story: dict, company: str) -> dict:
    cat, url = story["category"], story.get("lead_url")
    if cat == "results":
        if not url:
            raise ValueError("results story without a PDF")
        content = None
        known = llm.lookup([url, PROMPT_VERSION, "results"])
        try:
            if known is None:
                raise ValueError
            results_card(copy.deepcopy(known), url)  # the lead's stored reading still passes: keep it
        except ValueError:
            url, content = pick_results_pdf(story)

        def extract():
            """Full Flash models first, then Lite. Every reading must agree with
            screener before it is kept; a failed reading is never cached, so a
            better model can try again later."""
            pdf, pages = results_pages(content if content is not None else download(url))
            problems = []
            disputed = []  # (model, x, card, why): complete readings screener disagrees with

            def agreed():
                """Two different models reading the same numbers is strong evidence of
                what the filing prints, even when screener's table differs."""
                for i, (m1, x1, c1, why) in enumerate(disputed):
                    for m2, x2, c2, _ in disputed[i + 1:]:
                        a = {r["metric"]: r["value"] for r in c1["rows"]}
                        b = {r["metric"]: r["value"] for r in c2["rows"]}
                        keys = [m for m in ("Revenue", "NII", "PAT") if a.get(m) is not None]
                        if keys and all(b.get(m) is not None and abs(a[m] - b[m]) <= 0.005 * abs(b[m]) + 0.5
                                        for m in keys):
                            x1["_check"] = {"against": "screener", "metrics": [], "disputed": why,
                                            "readers": [m1, m2]}
                            x1["_model"] = m1
                            return x1
                return None
            def finish(x, model):
                """Completeness, then screener's cross-check on revenue and PAT. Only a
                reading that passes both may borrow screener's PBT and EBITDA for lines
                it couldn't find; it still has to end up with a PBT.

                Two reader slips are repaired from the filing's own lines, and the
                repaired reading still has to match screener on both revenue and PAT:
                a unit the reader didn't name (each scale is tried), and total income
                taken for revenue (other income is taken back out)."""
                stated = _normalise(x)["unit"]
                # The stated unit first; readers also misread "₹ in million" as crore,
                # so the other scales are tried when it fails. Both revenue and PAT
                # have to match at the same scale, which a wrong scale won't do.
                units = [stated] * (stated in _TO_CRORE) + [u for u in _TO_CRORE if u != stated]
                current = next((p for p in x["periods"] if p["role"] == "current"), None)
                if current is None:
                    raise ValueError("no current-quarter column found")
                failure = None
                for unit in units:
                    for repair in (False, True):
                        y = copy.deepcopy(x)
                        y["unit"] = unit
                        if repair and not _without_other_income(y):
                            continue
                        card = results_card(y, url, need_pbt=False)
                        try:
                            y["_check"] = cross_check(card, story["symbol"], current["period_end"])
                        except CheckFailed as e:
                            failure = failure or CheckFailed(str(e), card)
                            continue
                        y["basis"] = card["basis"]
                        notes = []
                        if unit != stated:
                            notes.append(f"unit not stated by the reader; {unit} is the scale that matches"
                                         if stated not in _TO_CRORE else
                                         f"the reader took the table to be in {stated}; {unit} is the scale that matches")
                        if repair:
                            notes.append("the reader took total income for revenue, so other income was taken out")
                        if notes:
                            y["_note"] = "; ".join(notes)
                        if not _has_pbt(card) or any(r["value"] is None for r in card["rows"]):
                            y["_fill"] = screener_fill(y, card, story["symbol"])
                        results_card(y, url)  # raises if PBT is still missing
                        y["_model"] = model
                        return y
                raise failure

            for model in RESULTS_MODELS + llm.GEMINI_FALLBACKS:
                try:
                    x = llm.gemini_json(RESULTS_PROMPT, purpose="results", model=model, fallback=False,
                                        pdf=pdf, schema=RESULTS_SCHEMA)
                except (llm.QuotaExhausted, llm.ModelUnavailable):
                    continue
                # The model numbers pages within the excerpt; map back to the filing.
                if x.get("page") and 1 <= x["page"] <= len(pages):
                    x["page"] = pages[x["page"] - 1]
                try:
                    return finish(x, model)
                except CheckFailed as e:
                    problems.append(f"{model}: {e}")
                    if e.card and _has_pbt(e.card):
                        disputed.append((model, x, e.card, str(e)))
                    if (hit := agreed()) is not None:
                        return hit
                except (ValueError, KeyError, StopIteration) as e:  # incomplete or malformed table
                    problems.append(f"{model}: {e}")
            # Last resort, no Gemini: the table pages' own text to Groq's largest
            # model. Text loses the column layout, so this reading has to pass
            # exactly the same completeness check and screener cross-check.
            text = _pages_text(pdf)
            if text:
                try:
                    x = llm.groq_json(RESULTS_PROMPT + RESULTS_TEXT_SUFFIX + text, purpose="results",
                                      model=llm.GROQ_LARGE)
                    x["page"] = pages[0] if pages else None
                    return finish(x, llm.GROQ_LARGE)
                except CheckFailed as e:
                    problems.append(f"{llm.GROQ_LARGE}: {e}")
                    if e.card and _has_pbt(e.card):
                        disputed.append((llm.GROQ_LARGE, x, e.card, str(e)))
                    if (hit := agreed()) is not None:
                        return hit
                except (ValueError, KeyError, StopIteration, TypeError) as e:
                    problems.append(f"{llm.GROQ_LARGE}: {e}")
                except Exception as e:  # rate limits and the like: try again on a later run
                    raise llm.ModelUnavailable(f"groq: {e}") from e
            if problems:
                raise ValueError("; ".join(problems)[:400])
            raise llm.QuotaExhausted("no model free to read results")

        key = [url, PROMPT_VERSION, "results"]
        x = llm.cached(key, extract)
        try:
            card = results_card(x, url)
            if "_check" not in x:  # stored before the cross-check existed: check it now
                current = next(p for p in x["periods"] if p["role"] == "current")
                x["_check"] = cross_check(card, story["symbol"], current["period_end"])
                x["basis"] = card["basis"]
                llm.store(key, x)
        except ValueError:
            # A stored reading that fails today's checks is read again, from the
            # PDF that looks most like a results statement.
            llm.forget(key)
            url, content = pick_results_pdf(story)
            key = [url, PROMPT_VERSION, "results"]
            x = llm.cached(key, extract)
            card = results_card(x, url)
        if (x["_check"].get("metrics") and "_fill" not in x
                and any(r["value"] is None for r in card["rows"])):
            # Passed on revenue and PAT but a line wasn't read: screener's figure, marked.
            x["_fill"] = screener_fill(x, card, story["symbol"])
            card = results_card(x, url)
            llm.store(key, x)
        if (x.get("_check") or {}).get("relabelled"):
            card["basis"] = x["_check"]["relabelled"].split(" → ")[1]
        if x.get("_note"):
            card["checks"].append(x["_note"][0].upper() + x["_note"][1:] + ".")
        card["model"] = x.get("_model")
        card["check"] = x.get("_check")
        if card["check"] and card["check"].get("disputed"):
            card["checks"].append(f"Two independent readings of the filing agree, but screener differs: "
                                  f"{card['check']['disputed']}.")
            card["check"] = None
        return card
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
            except (llm.QuotaExhausted, llm.ModelUnavailable) as e:
                print(f"{s['id']}: {e}; left pending for the next run")
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
    ap.add_argument("--debug", metavar="SYMBOL", help="show what a reader sees and returns for a results story")
    args = ap.parse_args()
    if args.debug:
        import json
        import re

        story = (db.client().table("stories").select("*").eq("symbol", args.debug).eq("category", "results")
                 .order("first_at", desc=True).limit(1).execute().data[0])
        print("lead:", story["lead_url"], "|", story.get("headline", "")[:80])
        url, content = pick_results_pdf(story)
        print("chosen:", url, "| statement strength", statement_strength(content))
        pdf, pages = results_pages(content)
        print("pages sent:", pages)
        lines = [ln.strip() for ln in _pages_text(pdf, limit=60000).splitlines()]
        wanted = re.compile(r"revenue|total income|other income|before tax|tax expense|profit for|net profit|"
                            r"profit after|quarter ended|three months|crore|lakh|million", re.I)
        for ln in [ln for ln in lines if wanted.search(ln)][:30]:
            print("  |", re.sub(r"\s{2,}", "  ", ln)[:170])
        x = llm.gemini_json(RESULTS_PROMPT, purpose="results", model=llm.GEMINI_LITE, pdf=pdf, schema=RESULTS_SCHEMA)
        print("reader:", json.dumps({k: v for k, v in x.items() if k != "periods"}))
        for per in x.get("periods", []):
            print(" ", json.dumps({k: v for k, v in per.items() if v is not None}))
        raise SystemExit
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
