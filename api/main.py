import os

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException

from pipeline import ingest, probe

app = FastAPI(title="FINN API")


def _admin(token: str | None):
    # These endpoints hit third-party sites and write to the database.
    if not token or token != os.environ.get("PROBE_TOKEN"):
        raise HTTPException(status_code=403)


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/probe")
def run_probe(x_token: str | None = Header(default=None)):
    _admin(x_token)
    return {"host": "render", "results": probe.run()}


@app.post("/ingest", status_code=202)
def run_ingest(background: BackgroundTasks, days: int = 3, x_token: str | None = Header(default=None)):
    _admin(x_token)
    background.add_task(ingest.run, min(days, 60))
    return {"started": True, "days": days}
