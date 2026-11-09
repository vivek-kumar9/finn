"""Promised vs delivered.

1. A model reads each concall transcript and lists management's quantified,
   forward-looking statements, each with a verbatim quote and page number.
2. Code checks every quote against the PDF's own text and drops the ones it
   can't find, so the ledger never cites something the call didn't say.
3. Code, not the model, compares each promise with reported numbers.
"""
import io
import re
from dataclasses import dataclass, field
from datetime import date

from pypdf import PdfReader
from rapidfuzz import fuzz

from pipeline import fiscal

METRICS = ["revenue_growth", "revenue", "ebitda_margin", "pat_growth", "capex", "order_book",
           "order_inflow", "other"]

GUIDANCE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "call_period": {"type": "STRING"},
        "items": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "metric": {"type": "STRING", "enum": METRICS},
                    "scope": {"type": "STRING", "enum": ["company", "segment"]},
                    "segment": {"type": "STRING", "nullable": True},
                    "promise": {"type": "STRING"},
                    "period": {"type": "STRING"},
                    "low": {"type": "NUMBER", "nullable": True},
                    "high": {"type": "NUMBER", "nullable": True},
                    "unit": {"type": "STRING", "enum": ["pct", "crore", "other"]},
                    "quote": {"type": "STRING"},
                    "page": {"type": "INTEGER"},
                    "speaker": {"type": "STRING", "nullable": True},
                },
                "required": ["metric", "scope", "promise", "period", "unit", "quote", "page"],
            },
        },
    },
    "required": ["call_period", "items"],
}

GUIDANCE_PROMPT = """This is the transcript of an Indian listed company's earnings call.
call_period: the quarter the call discusses, as "Q2 FY27" (Indian fiscal year, April to March).

List every QUANTIFIED, FORWARD-LOOKING statement made by MANAGEMENT (not analysts)
about the company's own future numbers: revenue or growth, margins, profit growth,
capex, order book or order inflow. Skip past results, vague hopes without a number,
and industry forecasts.

For each:
- metric: revenue_growth (% growth), revenue (an absolute ₹ figure), ebitda_margin
  (operating or EBITDA margin %), pat_growth, capex, order_book, order_inflow, or other
- scope: "company" only when it is about the whole company's consolidated numbers;
  "segment" when it is about one business, product line, plant, subsidiary or geography
- segment: that business's name when scope is "segment", else null
- promise: what was committed, at most 15 words, e.g. "FY27 revenue growth of 15-20%"
- period: "FY27", "H2 FY27" or "Q3 FY27". If they say "next year" on a Q2 FY27 call, that is FY28.
- low / high: the range. A single number sets both. "At least 20%" sets low 20, high null.
- unit: pct, crore (convert lakh or billion to crore), or other
- quote: the exact words from the transcript, at most 30 words, copied verbatim
- page: the PDF page the quote is on
Return an empty list if there is no such statement."""


# ---------------------------------------------------------------- grounding

def page_texts(pdf: bytes) -> list[str]:
    out = []
    for page in PdfReader(io.BytesIO(pdf)).pages:
        try:
            out.append(_norm(page.extract_text() or ""))
        except Exception:
            out.append("")
    return out


def _norm(s: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9%.\s]", " ", s.lower()).split())


def verify(quote: str, page: int, pages: list[str], threshold: float = 88) -> int | None:
    """Return the 1-based page where the quote really is, or None.

    Checks the claimed page first, then its neighbours (models are often off
    by one), then the whole document."""
    q = _norm(quote)
    if len(q) < 12:
        return None
    order = [page, page - 1, page + 1] + list(range(1, len(pages) + 1))
    for p in dict.fromkeys(order):
        if 1 <= p <= len(pages) and pages[p - 1] and fuzz.partial_ratio(q, pages[p - 1]) >= threshold:
            return p
    return None


# ---------------------------------------------------------------- verdicts

@dataclass
class Quarter:
    end: date
    sales: float | None
    op_profit: float | None
    pat: float | None


@dataclass
class Verdict:
    status: str           # met / missed / on_track / off_track / open / unverifiable
    actual: str | None    # what the numbers show, in words
    basis: str | None = None


@dataclass
class Promise:
    metric: str
    scope: str
    segment: str | None
    promise: str
    period: str
    low: float | None
    high: float | None
    unit: str
    quote: str
    page: int
    call: str             # call_period it was made on
    url: str
    revisions: list[dict] = field(default_factory=list)


def _sum(qs: list[Quarter], attr: str) -> float | None:
    vals = [getattr(q, attr) for q in qs]
    return None if not vals or any(v is None for v in vals) else sum(vals)


def _growth(now: float | None, before: float | None) -> float | None:
    if now is None or before is None or before <= 0:
        return None
    return (now / before - 1) * 100


def judge(p: Promise, quarters: list[Quarter]) -> Verdict:
    """Compare a promise with reported quarterly numbers.

    A finished period is met or missed. A period under way is on or off track
    against the quarters reported so far (YTD). Margins and growth carry half a
    point of tolerance, since management rounds when they speak."""
    if p.scope != "company":
        name = f" ({p.segment})" if p.segment else ""
        return Verdict("unverifiable", None, f"Segment guidance{name}; reported numbers are company-wide")
    if p.metric not in ("revenue_growth", "revenue", "ebitda_margin", "pat_growth"):
        return Verdict("unverifiable", None, "Not in the standard reported financials")
    span = fiscal.quarters_in(p.period)
    if span is None:
        return Verdict("unverifiable", None, f"Can't place period '{p.period}'")
    if p.low is None and p.high is None:
        return Verdict("unverifiable", None, "No number to check against")

    by_end = {q.end: q for q in quarters}
    have = [by_end[e] for e in span if e in by_end]
    if not have:
        return Verdict("open", None, "No quarter of this period is reported yet")
    done = len(have) == len(span)
    prior = [by_end.get(e.replace(year=e.year - 1)) for e in span[:len(have)]]
    k = len(have)
    scope = "full period" if done else f"{k} of {len(span)} quarters"

    if p.metric in ("revenue_growth", "pat_growth"):
        attr = "sales" if p.metric == "revenue_growth" else "pat"
        if any(x is None for x in prior):
            return Verdict("open", None, "Prior-year quarters missing for comparison")
        actual = _growth(_sum(have, attr), _sum(prior, attr))
        shown = None if actual is None else f"{actual:+.1f}% YoY ({scope})"
        tol = 0.5
    elif p.metric == "ebitda_margin":
        sales, op = _sum(have, "sales"), _sum(have, "op_profit")
        actual = None if not sales or op is None else op / sales * 100
        shown = None if actual is None else f"{actual:.1f}% ({scope})"
        tol = 0.5
    else:  # absolute revenue in crore
        total = _sum(have, "sales")
        actual = None if total is None else total * len(span) / k  # run-rate when under way
        shown = None if total is None else (
            f"₹{total:,.0f} cr" if done else f"₹{total:,.0f} cr in {k} quarters, run-rate ₹{actual:,.0f} cr")
        tol = 0.0

    if actual is None:
        return Verdict("open", None, "Reported numbers incomplete")
    # "At least X" and ranges are judged on the floor; "up to X" on the ceiling.
    hit = actual >= p.low - tol if p.low is not None else actual <= p.high + tol
    if done:
        return Verdict("met" if hit else "missed", shown)
    return Verdict("on_track" if hit else "off_track", shown)


# ---------------------------------------------------------------- ledger

def _key(item: dict) -> tuple[str, ...]:
    period = item["period"].upper().replace(" ", "")
    if item["metric"] == "other":  # unrelated promises; never merge them
        return "other", period, item["promise"].lower()[:60]
    segment = (item.get("segment") or "").lower() if item.get("scope") == "segment" else ""
    return item["metric"], period, segment


def ledger(calls: list[dict], quarters: list[Quarter]) -> list[dict]:
    """calls: oldest first, each {"call_period", "url", "items": [verified items]}.

    One row per (metric, period). The latest statement is the promise of
    record; earlier different statements become its revision history."""
    rows: dict[tuple[str, str], Promise] = {}
    for call in calls:
        for it in call["items"]:
            k = _key(it)
            p = Promise(it["metric"], it.get("scope", "company"), it.get("segment"), it["promise"], it["period"],
                        it.get("low"), it.get("high"), it["unit"], it["quote"], it["page"], call["call_period"], call["url"])
            if k in rows:
                old = rows[k]
                if (old.low, old.high) != (p.low, p.high):
                    p.revisions = old.revisions + [{
                        "call": old.call, "low": old.low, "high": old.high,
                        "direction": _direction(old, p),
                    }]
                else:
                    p.revisions = old.revisions
                    p.call, p.quote, p.page, p.url = old.call, old.quote, old.page, old.url  # first said, kept
            rows[k] = p

    out = []
    for p in rows.values():
        v = judge(p, quarters)
        out.append({**p.__dict__, "status": v.status, "actual": v.actual, "basis": v.basis})
    order = {"missed": 0, "off_track": 1, "met": 2, "on_track": 3, "open": 4, "unverifiable": 5}
    return sorted(out, key=lambda r: (order[r["status"]], r["period"]))


def _direction(old: Promise, new: Promise) -> str:
    def mid(p: Promise):
        vals = [v for v in (p.low, p.high) if v is not None]
        return sum(vals) / len(vals) if vals else None

    a, b = mid(old), mid(new)
    if a is None or b is None or a == b:
        return "narrowed" if a == b else "changed"
    return "raised" if b > a else "cut"
