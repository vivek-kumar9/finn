"""Group filings into stories and score them.

One results event comes as five to seven filings (outcome, results PDF, press
release, deck, newspaper ad, call notice, transcript). You care about the
event, so the Brief shows one card per story.

A filing joins a story if it has the same symbol and category and was filed
within WINDOW days of the story's first filing. Counting from the first filing
and not the last stops stories from chaining forever, and keeps ids stable
between runs.
"""
import re
from datetime import datetime, timedelta

from pipeline.classify import is_senior

WEIGHT = {
    "results": 10, "M&A": 9, "management_change": 9, "fund_raise": 8,
    "litigation": 8, "pledging": 8, "capex": 7, "insider_trading": 6,
    "credit_rating": 6, "press_release": 5, "bonus_split": 5, "dividend": 4,
    "earnings_call": 3, "agm_egm": 2, "postal_ballot": 2,
    "investor_meetings": 1, "unclassified": 0,
}
JUNIOR_MANAGEMENT_WEIGHT = 5
WINDOW = {"results": timedelta(days=21)}
DEFAULT_WINDOW = timedelta(days=7)

_BOILERPLATE = re.compile(
    r"^.{0,120}?\b(has|have) (informed|submitted to|intimated) the exchange\b"
    r"( (about|regarding|that|of))?\s*",
    re.I,
)


def build(filings: list[dict], weights: dict[str, float]) -> tuple[list[dict], dict[str, str]]:
    """Return (stories, {filing_id: story_id})."""
    filings = sorted(filings, key=lambda f: _ts(f["filed_at"]))
    groups: list[dict] = []
    open_by_key: dict[tuple[str, str], dict] = {}

    for f in filings:
        cat = f["category"]
        group = None
        # Call notices and transcripts belong with the results they discuss.
        if cat == "earnings_call":
            group = _fits(open_by_key.get((f["symbol"], "results")), f)
        if group is None:
            group = _fits(open_by_key.get((f["symbol"], cat)), f)
        if group is None:
            group = {"category": cat, "symbol": f["symbol"], "filings": []}
            groups.append(group)
            open_by_key[(f["symbol"], cat)] = group
        group["filings"].append(f)

    stories, assignment = [], {}
    for g in groups:
        story = _story(g, weights.get(g["symbol"], 0))
        stories.append(story)
        for f in g["filings"]:
            assignment[f["id"]] = story["id"]
    return stories, assignment


def _fits(group: dict | None, f: dict) -> dict | None:
    if group is None:
        return None
    first = _ts(group["filings"][0]["filed_at"])
    window = WINDOW.get(group["category"], DEFAULT_WINDOW)
    return group if _ts(f["filed_at"]) - first <= window else None


def _story(g: dict, position_weight: float) -> dict:
    fs = g["filings"]
    lead = max(fs, key=lambda f: (_lead_rank(g["category"], f), _ts(f["filed_at"])))
    weight = WEIGHT[g["category"]]
    if g["category"] == "management_change":
        text = " ".join(f"{f['subject']} {f.get('detail') or ''}" for f in fs)
        if not is_senior(text):
            weight = JUNIOR_MANAGEMENT_WEIGHT
    return {
        "id": f"{g['symbol']}:{g['category']}:{fs[0]['id']}",
        "symbol": g["symbol"],
        "category": g["category"],
        "headline": headline(lead),
        "first_at": _iso(fs[0]["filed_at"]),
        "last_at": _iso(fs[-1]["filed_at"]),
        "filing_count": len(fs),
        "lead_url": lead.get("attachment_url"),
        "base_score": round(weight + position_weight / 2, 2),
    }


# These carry headline numbers but not the full statement.
_NOT_THE_STATEMENT = ("press release", "media release", "presentation", "newspaper", "advertisement")


def _lead_rank(category: str, f: dict) -> int:
    """Which filing in a story is worth opening. Higher is better."""
    text = f"{f['subject']} {f.get('detail') or ''}".lower()
    rank = 2 if f.get("attachment_url") else 0
    if "newspaper" in text or "advertisement" in text:
        rank -= 3
    if "intimation" in text and category == "results":
        rank -= 2
    if category == "results":
        if f.get("category") == "earnings_call":
            rank -= 6  # a call transcript is never the results document
        elif any(w in text for w in _NOT_THE_STATEMENT):
            rank += 1
        elif ("financial result" in text or "outcome" in text) and "intimation" not in text:
            rank += 5
    return rank


def headline(f: dict) -> str:
    detail = (f.get("detail") or "").strip()
    text = _BOILERPLATE.sub("", detail) if detail else f["subject"]
    text = text.strip(" .,:;-") or f["subject"]
    text = text[0].upper() + text[1:]
    return text if len(text) <= 140 else text[:137].rsplit(" ", 1)[0] + "…"


def _ts(value) -> datetime:
    return value if isinstance(value, datetime) else datetime.fromisoformat(value)


def _iso(value) -> str:
    return _ts(value).isoformat()
