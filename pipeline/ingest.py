"""One ingest run: prices and signals, new filings, categories, stories, cards, alerts.

    python -m pipeline.ingest --days 30      # backfill
    python -m pipeline.ingest                # normal run (last 3 days)
    python -m pipeline.ingest --reclassify   # after changing the rules
"""
import argparse
import time
from datetime import date, datetime, timedelta, timezone

import pandas as pd

from pipeline import cards, classify, db, prices, stories
from pipeline.nse import NSE
from pipeline.portfolio import HOLDINGS, INDEX, SYMBOLS, WEIGHTS

STORY_LOOKBACK_DAYS = 60  # filings loaded to rebuild stories; longer than any story window
CARD_BUDGET_S = 600       # an hourly run stops starting new cards after 10 minutes
IST = timezone(timedelta(hours=5, minutes=30))


def run(days: int = 3) -> str:
    run_id = db.client().table("ingest_runs").insert({}).execute().data[0]["id"]
    try:
        note = _run(days)
        ok = True
    except Exception as e:
        note, ok = f"{type(e).__name__}: {e}"[:500], False
    db.client().table("ingest_runs").update({
        "finished_at": datetime.now(timezone.utc).isoformat(), "ok": ok, "note": note,
    }).eq("id", run_id).execute()
    print(("ok: " if ok else "FAILED: ") + note)
    return note


def _run(days: int) -> str:
    t0 = time.perf_counter()
    db.upsert("stocks", [
        {"symbol": s, "name": n, "sector": sec, "size": size, "weight": w, "in_portfolio": True}
        for s, n, sec, size, w in HOLDINGS
    ], on_conflict="symbol")

    n_prices = _prices(days)
    n_new, n_rule, n_model = _filings(days)
    n_stories = _stories()
    done, failed, _ = cards.process_pending(limit=15, budget_s=CARD_BUDGET_S)
    try:
        from pipeline import telegram

        n_alerts = telegram.send_pending()
    except Exception as e:  # alerts must never fail the ingest
        print(f"telegram: {type(e).__name__}: {e}")
        n_alerts = 0

    secs = time.perf_counter() - t0
    return (f"{n_prices} price rows, {n_new} filings ({n_rule} by rules, {n_model} by model), "
            f"{n_stories} stories, {done} cards built, {failed} failed, {n_alerts} alerts, in {secs:.0f}s")


def unfinished_day(now: datetime | None = None) -> str | None:
    """Today's date (IST) while the trading day isn't over, else None.

    Yahoo serves today's bar while the market is still open. These are end-of-day
    alerts, so a half-finished day stays out until the 16:00 run and later."""
    now = (now or datetime.now(timezone.utc)).astimezone(IST)
    return now.date().isoformat() if now.hour < 16 else None


def _prices(days: int) -> int:
    frames = prices.download(SYMBOLS)
    index = frames.get(INDEX)
    since = pd.Timestamp(date.today() - timedelta(days=days + 5))
    all_prices, all_signals = [], []
    for sym, df in frames.items():
        sig = prices.signals(df, None if sym == INDEX else index)
        p_rows, s_rows = prices.rows(sym, df, sig, since)
        all_prices += p_rows
        if sym != INDEX:
            all_signals += s_rows
    today = unfinished_day()
    if today:
        all_prices = [r for r in all_prices if r["date"] != today]
        all_signals = [r for r in all_signals if r["date"] != today]
        for table in ("prices", "signals"):  # half-day rows from an earlier run today
            db.client().table(table).delete().eq("date", today).execute()
    db.upsert("prices", all_prices, on_conflict="symbol,date")
    db.upsert("signals", all_signals, on_conflict="symbol,date")
    return len(all_prices)


def _filings(days: int) -> tuple[int, int, int]:
    nse = NSE()
    end, start = date.today(), date.today() - timedelta(days=days)
    # NSE's dates are IST and the database's are UTC, so look a day further back
    # or filings from early on the start date get counted (and classified) twice.
    known = {r["id"] for r in db.select("filings", "id", gte_filed_at=(start - timedelta(days=1)).isoformat())}

    fresh = []
    for sym in SYMBOLS:
        try:
            rows = nse.announcements(sym, start, end)
        except Exception as e:
            print(f"{sym}: announcements failed: {e}")
            continue
        fresh += [r for r in rows if r["id"] not in known]
        time.sleep(0.4)  # be polite; 18 calls a run

    leftovers = []
    for f in fresh:
        cat = classify.by_rules(f["subject"], f["detail"])
        f["category"], f["classified_by"] = (cat, "rule") if cat else (None, "model")
        if not cat:
            leftovers.append(f)
    for f, label in zip(leftovers, classify.with_model([(f["subject"], f["detail"]) for f in leftovers])):
        f["category"] = label

    db.upsert("filings", fresh, on_conflict="id")
    return len(fresh), len(fresh) - len(leftovers), len(leftovers)


def reclassify() -> str:
    """Run the current rules over recent filings again, then rebuild the stories.

    Rules cost nothing, so every filing is checked. The model only sees filings
    a rule used to catch and no longer does; the ones it labelled before keep
    their label."""
    since = (datetime.now(timezone.utc) - timedelta(days=STORY_LOOKBACK_DAYS)).isoformat()
    rows = db.select("filings", "id,subject,detail,category,classified_by", gte_filed_at=since)
    changes: dict[tuple[str, str], list[str]] = {}
    leftovers = []
    for f in rows:
        cat = classify.by_rules(f["subject"], f["detail"])
        if cat and (cat, "rule") != (f["category"], f["classified_by"]):
            changes.setdefault((cat, "rule"), []).append(f["id"])
        elif not cat and f["classified_by"] == "rule":
            leftovers.append(f)
    for f, label in zip(leftovers, classify.with_model([(f["subject"], f["detail"]) for f in leftovers])):
        changes.setdefault((label, "model"), []).append(f["id"])

    for (cat, source), ids in changes.items():
        for i in range(0, len(ids), 200):
            db.client().table("filings").update({"category": cat, "classified_by": source}) \
                .in_("id", ids[i:i + 200]).execute()
    n = _stories()
    moved = {f"{cat} ({source})": len(ids) for (cat, source), ids in changes.items()}
    return f"{sum(moved.values())} filings relabelled {moved}, {len(leftovers)} sent to the model, {n} stories"


def _stories() -> int:
    since = (datetime.now(timezone.utc) - timedelta(days=STORY_LOOKBACK_DAYS)).isoformat()
    filings = db.select("filings", "id,symbol,filed_at,subject,detail,attachment_url,category",
                        gte_filed_at=since)
    built, assignment = stories.build(filings, WEIGHTS)

    # A story whose lead document changed (say the results PDF came after the
    # press release) needs its card rebuilt.
    existing = {s["id"]: s["lead_url"] for s in db.select("stories", "id,lead_url", gte_last_at=since)}
    for s in built:
        if s["id"] not in existing or existing[s["id"]] != s["lead_url"]:
            s["card_status"] = "pending"
        s["updated_at"] = datetime.now(timezone.utc).isoformat()
    # card_status only on some rows: PostgREST needs uniform keys per batch.
    db.upsert("stories", [s for s in built if "card_status" in s], on_conflict="id")
    db.upsert("stories", [s for s in built if "card_status" not in s], on_conflict="id")

    # Stories that no longer exist after regrouping (a filing moved to another
    # category, say) would otherwise hang around as duplicates.
    built_ids = {s["id"] for s in built}
    stale = [sid for sid in existing if sid not in built_ids]
    if stale:
        db.client().table("filings").update({"story_id": None}).in_("story_id", stale).execute()
        db.client().table("stories").delete().in_("id", stale).execute()

    by_story: dict[str, list[str]] = {}
    for fid, sid in assignment.items():
        by_story.setdefault(sid, []).append(fid)
    for sid, ids in by_story.items():
        db.client().table("filings").update({"story_id": sid}).in_("id", ids).execute()
    return len(built)


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv()
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=3)
    ap.add_argument("--reclassify", action="store_true", help="run the current rules over recent filings again")
    args = ap.parse_args()
    print(reclassify()) if args.reclassify else run(args.days)
