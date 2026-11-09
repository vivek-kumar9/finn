import json
import os
import time
from collections import defaultdict, deque

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

# The pipeline pulls in pandas, yfinance, pypdf and the Supabase client. On a
# free instance that takes long enough to miss Render's port check, so the API
# binds first and imports the pipeline only when a job actually runs.
STARTED = time.time()
app = FastAPI(title="FINN API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET"], allow_headers=["*"])
print("finn-api: app created", flush=True)

# A public link can burn the free model quota. Research runs are cheap when
# cached, so the limit is generous but finite.
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


def _ingest(days: int):
    t0 = time.time()
    from pipeline import ingest

    print(f"finn-api: pipeline imported in {time.time() - t0:.1f}s", flush=True)
    ingest.run(days)


@app.post("/ingest", status_code=202)
def run_ingest(background: BackgroundTasks, days: int = 3, x_token: str | None = Header(default=None)):
    _admin(x_token)
    background.add_task(_ingest, min(days, 60))
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


@app.get("/research/{symbol}")
def research(symbol: str, request: Request):
    ip = request.headers.get("x-forwarded-for", request.client.host if request.client else "?").split(",")[0]
    if _limited(ip):
        raise HTTPException(status_code=429, detail="Too many research runs from this address; try again in an hour.")

    def stream():
        from pipeline import research as r

        yield ": start\n\n"  # flushes headers through proxies right away
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
