"""NSE corporate announcements.

The JSON API wants the cookies NSE sets on its homepage, and starts returning
401/403 once they go stale, so the session visits the homepage again when that happens.
"""
import time
from datetime import date, datetime
from zoneinfo import ZoneInfo

import requests

IST = ZoneInfo("Asia/Kolkata")
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)


class NSE:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": UA,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.nseindia.com/",
        })
        self._warm()

    def _warm(self):
        self.s.get("https://www.nseindia.com/", timeout=20)

    def announcements(self, symbol: str, start: date, end: date) -> list[dict]:
        params = {
            "index": "equities",
            "symbol": symbol,
            "from_date": start.strftime("%d-%m-%Y"),
            "to_date": end.strftime("%d-%m-%Y"),
        }
        for attempt in range(3):
            r = self.s.get("https://www.nseindia.com/api/corporate-announcements",
                           params=params, timeout=30)
            if r.status_code in (401, 403) and attempt < 2:
                time.sleep(2)
                self._warm()
                continue
            r.raise_for_status()
            rows = [parse(row) for row in r.json() if row.get("seq_id")]
            # NSE sometimes ignores the dates and sends years of history, so filter again here.
            return [f for f in rows if f["filed_at"][:10] >= start.isoformat()]
        return []


def parse(row: dict) -> dict:
    return {
        "id": str(row["seq_id"]),
        "symbol": row["symbol"],
        "filed_at": _when(row).isoformat(),
        "subject": (row.get("desc") or "").strip(),
        "detail": (row.get("attchmntText") or "").strip() or None,
        "attachment_url": row.get("attchmntFile") or None,
        "raw": row,
    }


def _when(row: dict) -> datetime:
    for key, fmt in (("sort_date", "%Y-%m-%d %H:%M:%S"), ("an_dt", "%d-%b-%Y %H:%M:%S")):
        value = row.get(key)
        if value:
            try:
                return datetime.strptime(value, fmt).replace(tzinfo=IST)
            except ValueError:
                pass
    raise ValueError(f"no usable timestamp in {row.get('seq_id')}")
