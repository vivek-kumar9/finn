"""Telegram alerts.

Connecting: the Alert rules sheet in the app links to t.me/<bot>?start=<rules>,
so the chat starts with the same thresholds you set in the app.
Sending: every ingest run calls send_pending(), which alerts each chat once
per story or move.

    python -m pipeline.telegram --set-webhook https://finn-api-enea.onrender.com
"""
import hashlib
import html
import os
import re
from datetime import datetime, timedelta, timezone

import requests

from pipeline import db
from pipeline.portfolio import WEIGHTS

APP_URL = os.environ.get("APP_URL", "https://finn-pi-six.vercel.app")
DEFAULT_CATEGORIES = ["results", "management_change", "M&A", "fund_raise", "pledging"]
FRESH = timedelta(hours=36)  # never alert on something older than this


def _api(method: str, **payload):
    r = requests.post(f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}/{method}",
                      json=payload, timeout=20)
    r.raise_for_status()
    return r.json()


def webhook_secret() -> str:
    # Derived from PROBE_TOKEN so there's nothing extra to configure. Telegram
    # sends it back with every update, which is how the webhook knows it's real.
    return hashlib.sha256(f"tg:{os.environ['PROBE_TOKEN']}".encode()).hexdigest()[:32]


def send(chat_id: int, text: str):
    _api("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML", disable_web_page_preview=True)


# ---------------------------------------------------------------- rules

_START = re.compile(r"^(f|i|v)_(\d{1,3})_(\d{1,3})_(\d{1,3})$")
MODE = {"f": "fixed", "i": "vs_index", "v": "volatility"}


def rules_from_start(param: str | None) -> dict:
    """'v_25_50_20' means volatility mode, 2.5x the usual move, 5% move, 2x volume."""
    m = _START.match(param or "")
    if not m:
        return {"mode": "volatility", "z_mult": 2.5, "move_pct": 5, "vol_mult": 2}
    return {"mode": MODE[m.group(1)], "z_mult": int(m.group(2)) / 10,
            "move_pct": int(m.group(3)) / 10, "vol_mult": int(m.group(4)) / 10}


def rules_text(r: dict) -> str:
    price = {"volatility": f"moves bigger than {r['z_mult']}x a stock's usual day",
             "vs_index": f"moves of {r['move_pct']}% or more against Nifty",
             "fixed": f"moves of {r['move_pct']}% or more"}[r["mode"]]
    return (f"{price}, volume {r['vol_mult']}x its 20-day average, and new results, "
            f"leadership changes, deals, fund raises and pledges")


def trips(sig: dict, r: dict) -> bool:
    ret, z, rel, vm = (sig.get(k) for k in ("ret", "z", "ret_vs_index", "vol_mult"))
    price = (abs(float(ret)) >= r["move_pct"] if r["mode"] == "fixed"
             else rel is not None and abs(float(rel)) >= r["move_pct"] if r["mode"] == "vs_index"
             else z is not None and abs(float(z)) >= r["z_mult"])
    return price or (vm is not None and float(vm) >= r["vol_mult"])


# ---------------------------------------------------------------- messages

def _pct(v) -> str:
    return "n/a" if v is None else f"{v:+.1f}%"


def _cr(v) -> str:
    return "n/a" if v is None else f"₹{v:,.0f} cr"


def _esc(text) -> str:
    # Messages go out as HTML, and a bare "&" in a headline (Larsen & Toubro)
    # makes Telegram reject the whole message.
    return html.escape(str(text), quote=False)


def story_text(s: dict) -> str:
    c = s.get("card") or {}
    head = f"<b>{_esc(s['symbol'])}</b>"
    if c.get("type") == "results":
        rows = {r["metric"]: r for r in c["rows"]}
        top = rows.get("Revenue") or rows.get("NII") or {}
        pat = rows.get("PAT") or {}
        body = (f"{c['period']} results. {top.get('metric', 'Revenue')} {_cr(top.get('value'))} "
                f"({_pct(top.get('qoq'))} QoQ, {_pct(top.get('yoy'))} YoY); "
                f"PAT {_cr(pat.get('value'))} ({_pct(pat.get('yoy'))} YoY).")
    elif c.get("type") == "leadership":
        parts = [f"{p['name']} joins as {p['role']}" for p in c["incoming"]] + \
                [f"{p['name']} leaves as {p['role']}" for p in c["outgoing"]]
        body = "; ".join(parts) + "." if parts else s["headline"]
    elif c.get("type") == "summary":
        body = c["summary"]
    else:
        body = s["headline"]
    return f"{head} {_esc(body)}\n<a href=\"{APP_URL}\">Open the Brief</a>"


def move_text(sig: dict, driver: dict | None) -> str:
    bits = [f"<b>{_esc(sig['symbol'])}</b> {_pct(float(sig['ret']))} on {sig['date']}"]
    if sig.get("z") is not None:
        bits.append(f"{abs(float(sig['z'])):.1f}x its usual daily move")
    if sig.get("vol_mult") is not None:
        bits.append(f"volume {float(sig['vol_mult']):.1f}x average")
    line = ", ".join(bits) + "."
    line += f"\nLikely driver: {_esc(driver['headline'])}" if driver else "\nNo filing found around this move."
    return line + f"\n<a href=\"{APP_URL}\">Open the Brief</a>"


# ---------------------------------------------------------------- what's worth sending

def material(s: dict) -> bool:
    """Same test as isMaterial in web/lib/brief.ts."""
    card = s.get("card") or {}
    if card.get("routine"):
        return False
    facts = card.get("facts") or {}
    amount = facts.get("amount_cr")
    if s["category"] == "litigation" and isinstance(amount, (int, float)) and amount < 1:
        return False
    if s["category"] == "credit_rating" and str(facts.get("action", "")).lower().startswith("reaffirm"):
        return False
    return float(s["base_score"]) - WEIGHTS.get(s["symbol"], 0) / 2 >= 4


def _close(day: str) -> datetime:
    return datetime.fromisoformat(f"{day}T15:30:00+05:30")


def driver(sig: dict, stories: list[dict], prev_day: str | None) -> dict | None:
    """The most important material story that began between the previous close
    and this one, like explainers() in web/lib/brief.ts."""
    end = _close(sig["date"])
    start = _close(prev_day) if prev_day else end - timedelta(hours=30)
    same = [s for s in stories if s["symbol"] == sig["symbol"] and material(s)
            and start < datetime.fromisoformat(s["first_at"]) <= end]
    return max(same, key=lambda s: float(s["base_score"])) if same else None


def _previous_days(signals: list[dict]) -> dict[tuple[str, str], str]:
    by_symbol: dict[str, list[str]] = {}
    for sig in signals:
        by_symbol.setdefault(sig["symbol"], []).append(sig["date"])
    prev = {}
    for sym, days in by_symbol.items():
        days = sorted(set(days))
        for before, day in zip(days, days[1:]):
            prev[(sym, day)] = before
    return prev


def recent(days: int = 10) -> tuple[list[dict], list[dict]]:
    """Stories and signals from the last few days. Older ones are only needed to
    find a move's driver and the previous trading day."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    stories = db.client().table("stories").select("*").gte("last_at", since.isoformat()).execute().data
    signals = db.client().table("signals").select("*").gte("date", since.date().isoformat()).execute().data
    return stories, signals


# ---------------------------------------------------------------- sending

def _already(chat_id: int) -> set[str]:
    rows = db.client().table("sent_alerts").select("key").eq("chat_id", chat_id).execute().data
    return {r["key"] for r in rows}


def _mark(chat_id: int, keys: list[str]):
    if keys:
        db.upsert("sent_alerts", [{"chat_id": chat_id, "key": k} for k in keys], on_conflict="chat_id,key")


def _alert(rule: dict, stories: list[dict], signals: list[dict], since: datetime) -> int:
    chat, done, sent = rule["chat_id"], _already(rule["chat_id"]), []
    prev = _previous_days(signals)
    try:
        for s in stories:
            key = f"s:{s['id']}"
            if (key in done or s["card_status"] != "done" or datetime.fromisoformat(s["last_at"]) < since
                    or s["category"] not in rule["categories"] or not material(s)):
                continue
            send(chat, story_text(s))
            sent.append(key)
        for sig in signals:
            key = f"m:{sig['symbol']}:{sig['date']}"
            if key in done or sig["date"] < since.date().isoformat() or not trips(sig, rule):
                continue
            send(chat, move_text(sig, driver(sig, stories, prev.get((sig["symbol"], sig["date"])))))
            sent.append(key)
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 403:
            # They blocked the bot. Stop trying instead of failing every run.
            db.client().table("watch_rules").delete().eq("chat_id", chat).execute()
        else:
            raise
    finally:
        _mark(chat, sent)  # whatever went out is never sent twice
    return len(sent)


def send_pending() -> int:
    if not os.environ.get("TELEGRAM_BOT_TOKEN"):
        return 0
    since = datetime.now(timezone.utc) - FRESH
    stories, signals = recent()
    total = 0
    for rule in db.client().table("watch_rules").select("*").execute().data:
        try:
            total += _alert(rule, stories, signals, since)
        except Exception as e:  # one broken chat shouldn't stop the others
            print(f"telegram: chat {rule['chat_id']}: {type(e).__name__}: {e}")
    return total


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
        # Show what an alert looks like straight away, without marking it as sent.
        stories, _ = recent(7)
        worth = [s for s in stories if s["card_status"] == "done" and s["category"] in DEFAULT_CATEGORIES
                 and material(s)]
        if worth:
            top = max(worth, key=lambda s: float(s["base_score"]))
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
