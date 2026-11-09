import os
import time

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException

# The pipeline pulls in pandas, yfinance, pypdf and the Supabase client. On a
# free instance that takes long enough to miss Render's port check, so the API
# binds first and imports the pipeline only when a job actually runs.
STARTED = time.time()
app = FastAPI(title="FINN API")
print("finn-api: app created", flush=True)


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
