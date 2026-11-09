"""Map an NSE announcement to one of the 17 categories in the brief's glossary.

Most NSE subjects are formulaic ("Outcome of Board Meeting", "Credit Rating"),
so ordered regex rules handle the bulk for free. Whatever falls through goes
to a small model in one batched call (see classify_with_model).
"""
import re

from pipeline import llm

CATEGORIES = [
    "results", "M&A", "fund_raise", "management_change", "litigation",
    "insider_trading", "earnings_call", "investor_meetings", "credit_rating",
    "dividend", "bonus_split", "capex", "press_release", "postal_ballot",
    "agm_egm", "pledging", "unclassified",
]

# Order matters: first match wins. Routine paperwork goes first because it
# often mentions results ("trading window closed for financial results").
_RULES = [
    ("unclassified", r"trading window|loss of share cert|duplicate share|"
                     r"regulation 74\s*\(5\)|depositories and participants|compliance certificate|"
                     r"\besops?\b|\besos\b|\besps\b|employee stock|secretarial compliance|"
                     r"business responsibility|\bbrsr\b|\besg\b|sustainability report|investor complaint|"
                     r"statement of deviation|registrar and share transfer|"
                     r"(statutory|secretarial|internal|cost) auditor|"
                     r"(payment|servicing) of interest|interest payment|record date for (interest|redemption)|"
                     r"committee meeting|change in auditor|certificate pursuant|"
                     r"change in (registered|corporate) office|production volume|sales volume|"
                     r"(about|regarding) '?\s*(general )?updates?'?\.?$"),
    ("pledging", r"pledg|encumbrance"),
    ("earnings_call", r"earnings call|transcript of the (earnings|conference) call"),
    ("investor_meetings", r"analyst\(s\)|investor\(s\) meet|investor conference|investors? meet"),
    ("insider_trading", r"insider trading|prohibition of insider|regulation 7\s*\(2\)|\bpit\b"),
    ("results", r"financial results?|quarterly results?|(un)?audited (standalone|consolidated|financial)|"
                r"results? for the (quarter|period|half|year)|investor presentation|earnings presentation|"
                r"shareholders? letter|integrated filing.{0,3}financial|interim condensed|"
                r"\bq[1-4]\b.{0,25}results|announce .{0,40}results"),
    ("management_change", r"change in (directorate|director|management|key managerial|kmp|senior management)|"
                          r"appointment|resignation|cessation|retire|re-designat|redesignat"),
    ("M&A", r"acqui|amalgamat|merger|demerger|scheme of arrangement|joint venture|\bjv\b|"
            r"incorporation of .{0,40}subsidiary|investment in .{0,40}subsidiar|divest|"
            r"sale of (stake|shares|business|undertaking)|slump sale|stake (sale|purchase)|"
            r"business transfer|restructuring"),
    ("fund_raise", r"qualified institution|\bqip\b|rights issue|preferential (issue|allotment)|buy-?back|"
                   r"commercial paper|non-convertible debenture|\bncds?\b|raising of funds|fund rais|"
                   r"issue of (securities|bonds|debentures|warrants)|allotment of (debenture|bond|warrant)"),
    ("litigation", r"litigation|dispute|regulatory action|orders? passed|penalt|fine imposed|show cause|"
                   r"demand (notice|order)|tax demand|insolvency|\bnclt\b|\bcirp\b|warning letter|"
                   r"enforcement|search and seizure|summons|court"),
    ("credit_rating", r"credit rating|\brating\b|crisil|\bicra\b|india ratings"),
    ("bonus_split", r"\bbonus\b|sub-?division|stock split"),
    ("dividend", r"dividend"),
    ("capex", r"capex|capital expenditure|capacity expansion|expansion of (capacity|plant)|"
              r"new (plant|facility|unit|manufacturing)|greenfield|brownfield|commercial production|"
              r"commission(ing|ed) of"),
    ("postal_ballot", r"postal ballot"),
    ("agm_egm", r"annual general meeting|\bagm\b|extra.?ordinary general meeting|\begm\b|"
                r"shareholders'? meeting|scrutini[sz]er"),
    ("press_release", r"press release|media release|bagging|receiving of orders|order (win|received)|"
                      r"\bcontracts?\b|launch|partnership|collaborat|clarification|spurt in volume|"
                      r"memorandum of understanding|\bmou\b|media statement|\bannounces\b"),
    ("unclassified", r"newspaper|copy of advertisement|allotment of [\d,]+ (equity )?(shares|securities)"),
]
_RULES = [(cat, re.compile(rx)) for cat, rx in _RULES]

# NSE files both investor meetings and earnings calls under one subject.
_ANALYST_SUBJECT = re.compile(r"investor meet|con\. call|analyst")
_CALL = re.compile(r"transcript|audio|recording|earnings call|conference call|concall|con call|"
                   r"results? call|investor call")

# Seniority decides how loud a management change is.
_SENIOR = re.compile(r"\bceo\b|\bcfo\b|\bcoo\b|\bmd\b|chief|managing director|whole.?time director|"
                     r"chairman|chairperson|executive director")


def by_rules(subject: str, detail: str | None) -> str | None:
    s, d = subject.lower(), (detail or "").lower()
    if _ANALYST_SUBJECT.search(s):
        return "earnings_call" if _CALL.search(d) else "investor_meetings"
    text = f"{s} | {d}"
    for category, rx in _RULES:
        if rx.search(text):
            return category
    return None


def is_senior(text: str) -> bool:
    return bool(_SENIOR.search(text.lower()))


_PROMPT = """You label Indian stock-exchange announcements.
Categories: {categories}
Use "unclassified" for routine compliance paperwork that would not move the stock.

Return JSON: {{"labels": ["<category>", ...]}} with one label per item, in order.

Items:
{items}"""


def with_model(items: list[tuple[str, str | None]]) -> list[str]:
    """Label the leftovers in one call. Never raises: falls back to unclassified."""
    if not items:
        return []
    lines = "\n".join(
        f"{i + 1}. {subject} :: {(detail or '')[:300]}" for i, (subject, detail) in enumerate(items)
    )
    prompt = _PROMPT.format(categories=", ".join(CATEGORIES), items=lines)
    try:
        out = llm.groq_json(prompt, purpose="classify")
        labels = out.get("labels", [])
    except Exception as e:
        print(f"model classify failed, marking {len(items)} unclassified: {e}")
        labels = []
    return [lab if lab in CATEGORIES else "unclassified" for lab in labels] + \
        ["unclassified"] * (len(items) - len(labels))
