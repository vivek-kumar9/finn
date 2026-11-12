"""Indian fiscal years: FY27 runs from April 2026 to March 2027."""
import re
from datetime import date

_PERIOD = re.compile(r"^(Q[1-4]|H[12])?\s*FY\s?'?(\d{2}|\d{4})$", re.I)


def fy_of(d: date) -> int:
    """Fiscal year (4-digit end year) a date falls in."""
    return d.year + 1 if d.month >= 4 else d.year


def quarter_end(fy: int, q: int) -> date:
    month = {1: 6, 2: 9, 3: 12, 4: 3}[q]
    year = fy - 1 if q < 4 else fy
    day = 30 if month in (6, 9) else 31
    return date(year, month, day)


def label(d: date) -> str:
    """Quarter-end date to a label like 'Q2 FY27'."""
    q = {6: 1, 9: 2, 12: 3, 3: 4}.get(d.month)
    return f"Q{q} FY{fy_of(d) % 100:02d}" if q else d.strftime("%b %Y")


def parse(period: str) -> tuple[str, int, int | None] | None:
    """'FY27' gives ('fy', 2027, None), 'Q3 FY27' gives ('q', 2027, 3), 'H2 FY27' gives ('h', 2027, 2)."""
    m = _PERIOD.match(period.strip().replace("-", " "))
    if not m:
        return None
    part, year = m.group(1), int(m.group(2))
    fy = year if year > 100 else 2000 + year
    if not part:
        return ("fy", fy, None)
    return ("q" if part[0].upper() == "Q" else "h", fy, int(part[1]))


def quarters_in(period: str) -> list[date] | None:
    """Quarter-end dates a period covers, in order."""
    p = parse(period)
    if p is None:
        return None
    kind, fy, n = p
    if kind == "fy":
        return [quarter_end(fy, q) for q in (1, 2, 3, 4)]
    if kind == "h":
        return [quarter_end(fy, q) for q in ((1, 2) if n == 1 else (3, 4))]
    return [quarter_end(fy, n)]
