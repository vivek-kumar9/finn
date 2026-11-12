"""Build the card the Brief shows for each story.

Models only read PDFs and copy out what's printed. Anything you'd compare
(QoQ, YoY, margins, yields) is worked out here in Python.

    python -m pipeline.cards --limit 50
    python -m pipeline.cards --debug CDSL      # what the results reader sees
"""
import argparse
import copy
import io
import os
import time
from datetime import date, datetime, timedelta

import requests
from pypdf import PdfReader, PdfWriter

from pipeline import db, llm
from pipeline.nse import UA
from pipeline.stories import WEIGHT

# Bumping this re-reads everything. I didn't bump it when the tax and
# exceptional-items fields were added: readings that already pass don't need
# them, and failed readings are never stored, so only those get read again.
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
    """Just the first pages. Appointment details and announcements are at the front."""
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
    """Find the pages with the results table; it isn't always on page 1.

    Big filings start with a cover letter, the auditor's report and sometimes a
    press release, so the table can be on page 12. Pages are scored on line items
    every results table has, and consolidated wins a tie. Scanned PDFs have no
    text to score, so they just send the first pages.
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
    # The best page, the one after it (tables run over) and any other pages that
    # scored, up to the cap, in their original order.
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
    """Turn a reading into the card: units, derived lines, QoQ, YoY, margins.
    Raises ValueError if the reading isn't good enough to show.

    x["_fill"], if there, has screener's figures for lines the reader couldn't
    find. They're only used where the filing's own number is missing, and the
    card marks which lines they are."""
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
    """Screener's PBT and operating profit (sales minus expenses, the same thing
    as EBITDA here) for each column, keyed by role. Only used once the reading's
    revenue and PAT have matched screener, so both are the same table."""
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
    """A short background from a web search, for people the filing doesn't profile.
    Stored, so rebuilding a card doesn't search again. A failed search just means no bio."""
    key = os.environ.get("TAVILY_API_KEY")
    if not key:
        return None

    def search() -> dict:
        r = requests.post(
            "https://api.tavily.com/search",
            headers={"Authorization": f"Bearer {key}"},
            json={"query": f'"{name}" {role} {company} experience', "max_results": 5},
            timeout=30,
        )
        r.raise_for_status()
        results = r.json().get("results", [])
        if not results:
            return {"bio": None}
        snippets = "\n\n".join(f"[{x['url']}]\n{x.get('content', '')[:800]}" for x in results)
        out = llm.gemini_json(BIO_PROMPT.format(name=name, role=role, company=company, snippets=snippets),
                              purpose="bio", model=llm.GEMINI_LITE)
        return out if out.get("bio") else {"bio": None}

    try:
        out = llm.cached([name.lower(), role.lower(), company.lower(), "bio-v1"], search)
    except (llm.QuotaExhausted, llm.ModelUnavailable):
        raise  # out of model quota: the card waits for the next run and gets its bio then
    except Exception as e:
        print(f"bio search for {name} failed: {type(e).__name__}: {e}")
        return None
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


# ---------------------------------------------------------------- checking a results reading

class CheckFailed(ValueError):
    def __init__(self, message: str, card: dict | None = None):
        super().__init__(message)
        self.card = card


def _compare(card: dict, ref: dict | None) -> str | None:
    """None if the card agrees with screener's quarter, else what differs."""
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


def cross_check(card: dict, symbol: str, period_end: str) -> dict:
    """Compare the reading with screener's quarterly table. Revenue and PAT must
    agree (PAT only for banks; screener defines bank revenue differently).

    Filings have both a standalone and a consolidated table and readers mix them
    up, so a reading that matches the other basis is kept and relabelled."""
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


def _without_other_income(x: dict) -> bool:
    """If revenue equals total income, other income is inside it; take it out.
    Returns False if the current quarter didn't need it."""
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


def _current(x: dict) -> dict:
    p = next((p for p in x.get("periods", []) if p.get("role") == "current"), None)
    if p is None:
        raise ValueError("no current-quarter column found")
    return p


def _finish(x: dict, model: str, url: str, symbol: str) -> dict:
    """Check one reading and return it ready to store, or raise.

    It has to be complete and match screener on revenue and PAT. Two common
    slips get fixed from the filing's own lines first: the wrong unit (the stated
    one is tried first, then the others) and total income read as revenue. Only
    a reading that matches can take screener's PBT or EBITDA for a line it
    couldn't read, and it still needs a PBT at the end."""
    stated = _normalise(x)["unit"]
    units = [stated] * (stated in _TO_CRORE) + [u for u in _TO_CRORE if u != stated]
    period_end = _current(x)["period_end"]
    failure = None
    for unit in units:
        for repair in (False, True):
            y = copy.deepcopy(x)
            y["unit"] = unit
            if repair and not _without_other_income(y):
                continue
            card = results_card(y, url, need_pbt=False)
            try:
                y["_check"] = cross_check(card, symbol, period_end)
            except CheckFailed as e:
                failure = failure or CheckFailed(str(e), card)
                continue
            y["basis"] = card["basis"]
            notes = []
            if unit != stated:
                notes.append(f"the reader took the table to be in {stated}; {unit} is the scale that matches"
                             if stated in _TO_CRORE else
                             f"unit not stated by the reader; {unit} is the scale that matches")
            if repair:
                notes.append("the reader took total income for revenue, so other income was taken out")
            if notes:
                y["_note"] = "; ".join(notes)
            if not _has_pbt(card) or any(r["value"] is None for r in card["rows"]):
                y["_fill"] = screener_fill(y, card, symbol)
            results_card(y, url)  # raises if there's still no PBT
            y["_model"] = model
            return y
    raise failure


def _agreed(disputed: list) -> dict | None:
    """Two different models reading the same numbers is good evidence of what the
    filing says, even if screener's table differs. Returns the first such reading."""
    for i, (m1, x1, c1, why) in enumerate(disputed):
        a = {r["metric"]: r["value"] for r in c1["rows"]}
        for m2, _, c2, _ in disputed[i + 1:]:
            b = {r["metric"]: r["value"] for r in c2["rows"]}
            keys = [m for m in ("Revenue", "NII", "PAT") if a.get(m) is not None]
            if keys and all(b.get(m) is not None and abs(a[m] - b[m]) <= 0.005 * abs(b[m]) + 0.5 for m in keys):
                x1["_check"] = {"against": "screener", "metrics": [], "disputed": why, "readers": [m1, m2]}
                x1["_model"] = m1
                return x1
    return None


def read_results(url: str, content: bytes, symbol: str) -> dict:
    """Try the models in turn until one reading passes _finish: full Flash first,
    then Lite, then Groq on the page text. A reading that fails is never stored,
    so a better model gets another go on a later run."""
    pdf, pages = results_pages(content)
    problems, disputed = [], []

    def note_failure(model, e, x):
        problems.append(f"{model}: {e}")
        if isinstance(e, CheckFailed) and e.card and _has_pbt(e.card):
            disputed.append((model, x, e.card, str(e)))
        return _agreed(disputed)

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
            return _finish(x, model, url, symbol)
        except (ValueError, KeyError, StopIteration, TypeError) as e:
            if (hit := note_failure(model, e, x)) is not None:
                return hit

    # Last go without Gemini: the table pages as text, to Groq's biggest model.
    # Text loses the column layout, so it gets exactly the same checks.
    text = _pages_text(pdf)
    if text:
        try:
            x = llm.groq_json(RESULTS_PROMPT + RESULTS_TEXT_SUFFIX + text, purpose="results", model=llm.GROQ_LARGE)
        except Exception as e:
            raise llm.ModelUnavailable(f"groq: {e}") from e
        x["page"] = pages[0] if pages else None
        try:
            return _finish(x, llm.GROQ_LARGE, url, symbol)
        except (ValueError, KeyError, StopIteration, TypeError) as e:
            if (hit := note_failure(llm.GROQ_LARGE, e, x)) is not None:
                return hit
    if problems:
        raise ValueError("; ".join(problems)[:400])
    raise llm.QuotaExhausted("no model free to read results")


# ---------------------------------------------------------------- which PDF to read

_NOT_THE_TABLE = ("presentation", "press release", "media release", "transcript", "audio",
                  "recording", "conference call", "earnings call", "newspaper", "investor meet")


def results_candidates(story: dict) -> list[str]:
    """PDFs the company filed around this story, the likeliest statement first.

    The story's lead is sometimes the press release or the deck, and the
    statement can sit in a filing that was grouped somewhere else (Eternal's
    was), so this looks at all of the company's filings in that date range."""
    first = datetime.fromisoformat(story["first_at"]) - timedelta(days=2)
    last = datetime.fromisoformat(story.get("last_at") or story["first_at"]) + timedelta(days=2)
    rows = (db.client().table("filings").select("subject,detail,attachment_url,story_id")
            .eq("symbol", story["symbol"]).gte("filed_at", first.isoformat())
            .lte("filed_at", last.isoformat()).execute().data)

    def rank(f):
        text = f"{f['subject']} {f.get('detail') or ''} {f['attachment_url']}".lower()
        not_table = any(w in text for w in _NOT_THE_TABLE)
        return ("result" in text and not not_table) * 4 + (f.get("story_id") == story["id"]) * 2 \
            + ("outcome" in text) - not_table * 4

    urls = [f["attachment_url"] for f in sorted(rows, key=rank, reverse=True)
            if (f.get("attachment_url") or "").lower().endswith(".pdf")]
    if story.get("lead_url"):
        urls.append(story["lead_url"])
    return list(dict.fromkeys(urls))


def pick_results_pdf(story: dict, enough: int = 7, tries: int = 6) -> tuple[str, bytes]:
    """Download candidates until one clearly looks like a results statement, and
    return the best one seen. No model call."""
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


def _results_key(url: str) -> list[str]:
    return [url, PROMPT_VERSION, "results"]


def _stored_reading(story: dict) -> tuple[str, dict] | None:
    """A reading already stored for one of the story's PDFs that still passes
    today's checks. Looking here first means a change of lead filing, or a
    regrouped story, doesn't cost a model call."""
    urls = [story.get("lead_url")] + results_candidates(story)
    for url in dict.fromkeys(u for u in urls if u):
        x = llm.lookup(_results_key(url))
        if x is None:
            continue
        try:
            card = results_card(copy.deepcopy(x), url)
            if "_check" not in x:  # stored before the screener check existed
                x["_check"] = cross_check(card, story["symbol"], _current(x)["period_end"])
                x["basis"] = card["basis"]
                llm.store(_results_key(url), x)
            return url, x
        except ValueError:
            llm.forget(_results_key(url))  # fails today's checks, so it gets read again
    return None


def results_story_card(story: dict) -> dict:
    hit = _stored_reading(story)
    if hit:
        url, x = hit
    else:
        url, content = pick_results_pdf(story)
        x = llm.cached(_results_key(url), lambda: read_results(url, content, story["symbol"]))
    card = results_card(x, url)
    if x["_check"].get("metrics") and "_fill" not in x and any(r["value"] is None for r in card["rows"]):
        # Matched screener on revenue and PAT, but a line wasn't read: use screener's figure, marked "s".
        x["_fill"] = screener_fill(x, card, story["symbol"])
        card = results_card(x, url)
        llm.store(_results_key(url), x)
    if (x.get("_check") or {}).get("relabelled"):
        card["basis"] = x["_check"]["relabelled"].split(" → ")[1]
    if x.get("_note"):
        card["checks"].append(x["_note"][0].upper() + x["_note"][1:] + ".")
    card["model"] = x.get("_model")
    card["check"] = x.get("_check")
    if card["check"] and card["check"].get("disputed"):
        card["checks"].append(f"Two separate readings of the filing agree, but screener differs: "
                              f"{card['check']['disputed']}.")
        card["check"] = None
    return card


# ---------------------------------------------------------------- building cards

def build(story: dict, company: str) -> dict:
    cat, url = story["category"], story.get("lead_url")
    if cat == "results":
        if not url:
            raise ValueError("results story without a PDF")
        return results_story_card(story)
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


def _try_later(e: Exception) -> bool:
    """Quota, overload or a network blip: leave the story pending for the next run.
    A 404 or a bad PDF won't fix itself, so those fail."""
    if isinstance(e, (llm.QuotaExhausted, llm.ModelUnavailable, requests.ConnectionError, requests.Timeout)):
        return True
    response = getattr(e, "response", None)
    return isinstance(e, requests.HTTPError) and response is not None and \
        (response.status_code == 429 or response.status_code >= 500)


def process_pending(limit: int = 15, budget_s: float | None = None) -> tuple[int, int, int]:
    """Build cards for pending stories, most important first. With budget_s, no new
    card is started after that many seconds, so an hourly run can't drag into the next."""
    t0 = time.monotonic()
    names = {s["symbol"]: s["name"] for s in db.select("stocks", "symbol,name")}
    pending = db.client().table("stories").select("*").eq("card_status", "pending") \
        .order("base_score", desc=True).limit(limit).execute().data
    done = failed = skipped = 0
    for s in pending:
        if budget_s is not None and time.monotonic() - t0 > budget_s:
            print(f"card budget of {budget_s:.0f}s used up; the rest wait for the next run")
            break
        if WEIGHT[s["category"]] < MIN_WEIGHT:
            update, skipped = {"card_status": "skipped"}, skipped + 1
        else:
            try:
                card = build(s, names.get(s["symbol"], s["symbol"]))
                update, done = {"card": card, "card_status": "done", "summary": card.get("summary")}, done + 1
                print(f"{s['id']}: {card['type']} card ok")
            except Exception as e:
                if _try_later(e):
                    print(f"{s['id']}: {type(e).__name__}: {e}; left pending for the next run")
                    continue
                print(f"{s['id']}: card failed: {type(e).__name__}: {e}")
                update, failed = {"card": {"error": str(e)[:300]}, "card_status": "failed"}, failed + 1
        db.client().table("stories").update(update).eq("id", s["id"]).execute()
    return done, failed, skipped


def _debug(symbol: str):
    """What the reader is given for a symbol's latest results story, and what it returns."""
    import json
    import re

    story = (db.client().table("stories").select("*").eq("symbol", symbol).eq("category", "results")
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


def _show(story_id: str):
    """Print the stored reading behind a results card."""
    import json

    story = db.client().table("stories").select("*").eq("id", story_id).execute().data[0]
    url = ((story.get("card") or {}).get("source") or {}).get("url") or story["lead_url"]
    print("read from:", url)
    stored = llm.lookup(_results_key(url))
    print(json.dumps(stored if stored is not None else "nothing stored", indent=1))


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv()
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--retry-failed", action="store_true")
    ap.add_argument("--redo-results", action="store_true", help="rebuild every results card")
    ap.add_argument("--show", metavar="STORY_ID", help="print the stored reading behind a results card")
    ap.add_argument("--debug", metavar="SYMBOL", help="show what a reader sees and returns for a results story")
    args = ap.parse_args()
    if args.debug:
        _debug(args.debug)
    elif args.show:
        _show(args.show)
    else:
        if args.redo_results:
            db.client().table("stories").update({"card_status": "pending"}).eq("category", "results").execute()
        if args.retry_failed:
            db.client().table("stories").update({"card_status": "pending"}).eq("card_status", "failed").execute()
        print("done %d, failed %d, skipped %d" % process_pending(args.limit))
