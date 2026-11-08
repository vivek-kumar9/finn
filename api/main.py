import os

from fastapi import FastAPI, Header, HTTPException

from pipeline import probe

app = FastAPI(title="FINN API")


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/probe")
def run_probe(x_probe_token: str | None = Header(default=None)):
    # Hits third-party sites, so it shouldn't be open to anyone with the URL.
    if x_probe_token != os.environ.get("PROBE_TOKEN"):
        raise HTTPException(status_code=403)
    return {"host": "render", "results": probe.run()}
