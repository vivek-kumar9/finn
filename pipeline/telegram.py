"""Telegram push: the part of FINN that reaches out.

Connecting: the app's Alert rules sheet links to t.me/<bot>?start=<rules>,
so the chat starts with the same thresholds the person set in the app.
Sending: every ingest run calls send_pending(), which alerts each chat once
per story or move.

    python -m pipeline.telegram --set-webhook https://finn-api-enea.onrender.com
"""
import hashlib
import os
import re
from datetime import datetime, timedelta, timezone

import requests

from pipeline import db
from pipeline.stories import WEIGHT

APP_URL = os.environ.get("APP_URL", "https://finn-pi-six.vercel.app")
DEFAULT_CATEGORIES = ["results", "management_change", "M&A", "fund_raise", "pledging"]
FRESH = timedelta(hours=36)  # never alert on something older than this


def _api(method: str, **payload):
    r = requests.post(f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}/{method}",
                      json=payload, timeout=20)
    r.raise_for_status()
    return r.json()


def webhook_secret() -> str:
    # Derived, so nothing new to configure; Telegram echoes it back on every update.
    return hashlib.sha256(f"tg:{os.environ['PROBE_TOKEN']}".encode()).hexdigest()[:32]


def send(chat_id: int, text: str):
    _api("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML", disable_web_page_preview=True)


# ---------------------------------------------------------------- rules

_START = re.compile(r"^(f|i|v)_(\d{1,3})_(\d{1,3})_(\d{1,3})$")
MODE = {"f": "fixed", "i": "vs_index", "v": "volatility"}


def rules_from_start(param: str | None) -> dict:
    """'v_25_50_20' → volatility mode, 2.5× usual move, 5% move, 2× volume."""
    m = _START.match(param or "")
    if not m:
        return {"mode": "volatility", "z_mult": 2.5, "move_pct": 5, "vol_mult": 2}
    return {"mode": MODE[m.group(1)], "z_mult": int(m.group(2)) / 10,
            "move_pct": int(m.group(3)) / 10, "vol_mult": int(m.group(4)) / 10}


def rules_text(r: dict) -> str:
    price = {"volatility": f"moves bigger than {r['z_mult']}× a stock's usual day",
             "vs_index": f"moves of {r['move_pct']}% or more against Nifty",
             "fixed": f"moves of {r['move_pct']}% or more"}[r["mode"]]
    return f"{price}, volume {r['vol_mult']}× its 20-day average, and new results, leadership changes, deals, fund raises and pledges"


def trips(sig: dict, r: dict) -> bool:
    ret, z, rel, vm = (sig.get(k) for k in ("ret", "z", "ret_vs_index", "vol_mult"))
    price = (abs(float(ret)) >= r["move_pct"] if r["mode"] == "fixed"
             else rel is not None and abs(float(rel)) >= r["move_pct"] if r["mode"] == "vs_index"
             else z is not None and abs(float(z)) >= r["z_mult"])
    return price or (vm is not None and float(vm) >= r["vol_mult"])


# ---------------------------------------------------------------- messages

def _pct(v):
    return "–" if v is None else f"{'+' if v > 0 else '−' if v < 0 else ''}{abs(v):.1f}%"


def story_text(s: dict) -> str:
    c = s.get("card") or {}
    head = f"<b>{s['symbol']}</b>"
    if c.get("type") == "results":
        rows = {r["metric"]: r for r in c["rows"]}
        top = rows.get("Revenue") or rows.get("NII") or {}
        pat = rows.get("PAT") or {}
        body = (f"{c['period']} results. {top.get('metric', 'Revenue')} ₹{top.get('value', 0):,.0f} cr "
                f"({_pct(top.get('qoq'))} QoQ, {_pct(top.get('yoy'))} YoY); PAT ₹{pat.get('value') or 0:,.0f} cr "
                f"({_pct(pat.get('yoy'))} YoY).")
    elif c.get("type") == "leadership":
        parts = [f"{p['name']} joins as {p['role']}" for p in c["incoming"]] + \
                [f"{p['name']} leaves as {p['role']}" for p in c["outgoing"]]
        body = "; ".join(parts) + "." if parts else s["headline"]
    elif c.get("type") == "summary":
        body = c["summary"]
    else:
        body = s["headline"]
    return f"{head} {body}\n<a href=\"{APP_URL}\">Open the Brief</a>"


def move_text(sig: dict, driver: dict | None) -> str:
    bits = [f"<b>{sig['symbol']}</b> {_pct(float(sig['ret']))} on {sig['date']}"]
    if sig.get("z") is not None:
        bits.append(f"{abs(float(sig['z'])):.1f}× its usual daily move")
    if sig.get("vol_mult") is not None:
        bits.append(f"volume {float(sig['vol_mult']):.1f}× average")
    line = ", ".join(bits) + "."
    line += f"\nLikely driver: {driver['headline']}" if driver else "\nNo filing found around this move."
    return line + f"\n<a href=\"{APP_URL}\">Open the Brief</a>"


# ---------------------------------------------------------------- sending

def _already(chat_id: int) -> set[str]:
    rows = db.client().table("sent_alerts").select("key").eq("chat_id", chat_id).execute().data
    return {r["key"] for r in rows}


def _mark(chat_id: int, keys: list[str]):
    if keys:
        db.upsert("sent_alerts", [{"chat_id": chat_id, "key": k} for k in keys], on_conflict="chat_id,key")


def candidates(since: datetime) -> tuple[list[dict], list[dict]]:
    stories = db.client().table("stories").select("*").eq("card_status", "done") \
        .gte("last_at", since.isoformat()).execute().data
    signals = db.client().table("signals").select("*").gte("date", since.date().isoformat()).execute().data
    return stories, signals


def _driver(sig: dict, stories: list[dict]) -> dict | None:
    same = [s for s in stories if s["symbol"] == sig["symbol"] and s["last_at"][:10] <= sig["date"]
            and WEIGHT[s["category"]] >= 3]
    return max(same, key=lambda s: s["last_at"]) if same else None


def send_pending() -> int:
    if not os.environ.get("TELEGRAM_BOT_TOKEN"):
        return 0
    since = datetime.now(timezone.utc) - FRESH
    stories, signals = candidates(since)
    sent = 0
    for r in db.client().table("watch_rules").select("*").execute().data:
        done, new_keys = _already(r["chat_id"]), []
        for s in stories:
            card = s.get("card") or {}
            if s["category"] not in r["categories"] or card.get("routine") or f"s:{s['id']}" in done:
                continue
            send(r["chat_id"], story_text(s))
            new_keys.append(f"s:{s['id']}")
        for sig in signals:
            key = f"m:{sig['symbol']}:{sig['date']}"
            if key in done or not trips(sig, r):
                continue
            send(r["chat_id"], move_text(sig, _driver(sig, stories)))
            new_keys.append(key)
        _mark(r["chat_id"], new_keys)
        sent += len(new_keys)
    return sent


def handle_update(update: dict):
    msg = update.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    text = (msg.get("text") or "").strip()
    if not chat_id or not text.startswith("/"):
        return
    cmd, _, arg = text.partition(" ")
    if cmd == "/start":
        r = rules_from_start(arg)
        db.upsert("watch_rules", [{"chat_id": chat_id, "mode": r["mode"], "move_pct": r["move_pct"],
                                   "z_mult": r["z_mult"], "vol_mult": r["vol_mult"],
                                   "categories": DEFAULT_CATEGORIES}], on_conflict="chat_id")
        send(chat_id, f"Connected. FINN will message you about {rules_text(r)} across the portfolio.\n"
                      f"Send /stop to turn alerts off.")
        # Show what an alert looks like right away, without marking it sent.
        stories, _ = candidates(datetime.now(timezone.utc) - timedelta(days=7))
        material = [s for s in stories if s["category"] in DEFAULT_CATEGORIES]
        if material:
            top = max(material, key=lambda s: s["base_score"])
            send(chat_id, "The most important item from the last week looks like this:\n\n" + story_text(top))
    elif cmd == "/stop":
        db.client().table("watch_rules").delete().eq("chat_id", chat_id).execute()
        send(chat_id, "Alerts are off. Send /start to turn them back on.")
    elif cmd == "/rules":
        rows = db.client().table("watch_rules").select("*").eq("chat_id", chat_id).execute().data
        send(chat_id, f"You get {rules_text(rows[0])}." if rows else "Alerts are off. Send /start to turn them on.")


if __name__ == "__main__":
    import sys

    from dotenv import load_dotenv

    load_dotenv()
    if sys.argv[1:2] == ["--set-webhook"]:
        base = sys.argv[2].rstrip("/")
        print(_api("setWebhook", url=f"{base}/telegram", secret_token=webhook_secret(),
                   allowed_updates=["message"]))
        print(_api("getMe")["result"]["username"])
    elif sys.argv[1:2] == ["--send"]:
        print(f"{send_pending()} alerts sent")
