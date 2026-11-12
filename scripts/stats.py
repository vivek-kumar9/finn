"""Numbers for the README, straight from the database, plus a quick check that
the hourly ingest is still running.

    python scripts/stats.py
"""
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()
from pipeline import db  # noqa: E402

filings = db.select("filings", "classified_by,category")
by = Counter(f["classified_by"] for f in filings)
print(f"filings: {len(filings)}, by rules {by['rule']} ({by['rule'] / len(filings):.0%}), by model {by['model']}")

stories = db.select("stories", "category,card_status")
print(f"stories: {len(stories)}; cards {dict(Counter(s['card_status'] for s in stories))}")
res = Counter(s["card_status"] for s in stories if s["category"] == "results")
print(f"results cards: {dict(res)}")

cache = db.select("research_cache", "value")
kinds = Counter()
kept = dropped = 0
models = Counter()
for row in cache:
    v = row["value"]
    if not isinstance(v, dict):
        continue
    if "dropped" in v and "items" in v:
        kinds["transcript"] += 1
        kept += len(v["items"])
        dropped += v["dropped"]
        models[v.get("model") or "?"] += 1
    elif "what_it_does" in v:
        kinds["annual report"] += 1
    elif "periods" in v:
        kinds["results table"] += 1
    elif "changes" in v:
        kinds["leadership"] += 1
    elif "bull" in v:
        kinds["bull/bear"] += 1
    elif "summary" in v:
        kinds["other filing"] += 1
print(f"cached model outputs: {dict(kinds)}")
if kept + dropped:
    print(f"guidance quotes: {kept} kept, {dropped} dropped as not found in the transcript ({dropped / (kept + dropped):.0%})")
print(f"transcripts read by: {dict(models)}")

usage = db.select("usage", "provider,model,purpose,tokens_in,tokens_out")
tok = Counter()
calls = Counter()
for u in usage:
    k = f"{u['provider']}:{u['purpose'].split(':')[0]}"
    calls[k] += 1
    tok[k] += (u["tokens_in"] or 0) + (u["tokens_out"] or 0)
print(f"model calls: {len(usage)}, tokens: {sum(tok.values()):,}")
for k, n in calls.most_common():
    print(f"  {k}: {n} calls, {tok[k]:,} tokens")

print("\nlast ingest runs:")
for r in db.client().table("ingest_runs").select("*").order("started_at", desc=True).limit(5).execute().data:
    took = ""
    if r["finished_at"]:
        secs = (datetime.fromisoformat(r["finished_at"]) - datetime.fromisoformat(r["started_at"])).total_seconds()
        took = f" in {secs / 60:.1f} min"
    state = "ok" if r["ok"] else "FAILED" if r["ok"] is False else "still running?"
    print(f"  {r['started_at'][:16]} {state}{took}: {(r['note'] or '')[:110]}")
latest = db.client().table("signals").select("date").order("date", desc=True).limit(1).execute().data
print(f"latest prices: {latest[0]['date'] if latest else 'none'}")
waiting = Counter(s["category"] for s in stories if s["card_status"] in ("pending", "failed"))
print(f"cards pending or failed: {dict(waiting) or 'none'}")
