import json
import os
import re
import threading
import time
from collections import defaultdict, deque

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

# The pipeline imports pandas, yfinance, pypdf and the Supabase client. On a free
# instance that's slow enough to miss Render's port check, so the API starts
# first and only imports the pipeline when a job actually runs.
STARTED = time.time()
app = FastAPI(title="FINN API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET"], allow_headers=["*"])
print("finn-api: app created", flush=True)

# Anyone with the link can start a research run, and each new company uses free
# model quota. Cached runs cost nothing, so the limit can be generous.
RESEARCH_PER_HOUR = int(os.environ.get("RESEARCH_PER_HOUR", 12))
_recent: dict[str, deque] = defaultdict(deque)


def _limited(ip: str) -> bool:
    q, now = _recent[ip], time.time()
    while q and now - q[0] > 3600:
        q.popleft()
    if len(q) >= RESEARCH_PER_HOUR:
        return True
    q.append(now)
    return False


def _admin(token: str | None):
    # These endpoints hit third-party sites and write to the database.
    if not token or token != os.environ.get("PROBE_TOKEN"):
        raise HTTPException(status_code=403)


@app.get("/health")
def health():
    return {"ok": True, "up_seconds": round(time.time() - STARTED)}


@app.get("/probe")
def run_probe(x_token: str | None = Header(default=None)):
    _admin(x_token)
    from pipeline import probe

    return {"host": "render", "results": probe.run()}


# A slow run (Gemini timing out, say) can outlast the hour. Two runs at once would
# do the same work twice and race on the stories table, so the later one is skipped.
_ingest_running = threading.Lock()


def _ingest(days: int):
    if not _ingest_running.acquire(blocking=False):
        print("finn-api: previous ingest still running, skipping this one", flush=True)
        return
    try:
        t0 = time.time()
        from pipeline import ingest

        print(f"finn-api: pipeline imported in {time.time() - t0:.1f}s", flush=True)
        ingest.run(days)
    finally:
        _ingest_running.release()


@app.post("/ingest", status_code=202)
def run_ingest(background: BackgroundTasks, days: int = 3, x_token: str | None = Header(default=None)):
    _admin(x_token)
    if _ingest_running.locked():
        return {"started": False, "reason": "previous run still going"}
    days = max(1, min(days, 60))
    background.add_task(_ingest, days)
    return {"started": True, "days": days}


@app.post("/telegram")
async def telegram_webhook(request: Request, background: BackgroundTasks,
                           x_telegram_bot_api_secret_token: str | None = Header(default=None)):
    from pipeline import telegram

    if x_telegram_bot_api_secret_token != telegram.webhook_secret():
        raise HTTPException(status_code=403)
    # Answer Telegram at once; a slow reply makes it resend the update.
    background.add_task(telegram.handle_update, await request.json())
    return {"ok": True}


@app.get("/search")
def search(q: str):
    from pipeline import screener

    if len(q.strip()) < 2:
        return []
    return screener.search(q.strip())


# NSE symbols: letters, digits and a few marks (M&M, BAJAJ-AUTO).
_SYMBOL = re.compile(r"[A-Za-z0-9&_.-]{1,20}")


@app.get("/research/{symbol}")
def research(symbol: str, request: Request):
    if not _SYMBOL.fullmatch(symbol):
        raise HTTPException(status_code=400, detail="That doesn't look like an NSE symbol.")
    ip = request.headers.get("x-forwarded-for", request.client.host if request.client else "?").split(",")[0]
    if _limited(ip):
        raise HTTPException(status_code=429, detail="Too many research runs from this address; try again in an hour.")

    def stream():
        from pipeline import research as r

        yield ": start\n\n"  # gets the headers through proxies straight away
        try:
            for event, payload in r.run(symbol):
                yield f"event: {event}\ndata: {json.dumps(payload, default=str)}\n\n"
        except LookupError as e:
            yield f"event: fail\ndata: {json.dumps({'error': str(e)})}\n\n"
        except Exception as e:
            yield f"event: fail\ndata: {json.dumps({'error': f'{type(e).__name__}: {e}'[:300]})}\n\n"
        yield "event: end\ndata: {}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
