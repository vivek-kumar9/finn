"""Thin model clients. Every call is logged to the usage table.

Model IDs come from the environment so they can follow whatever the free
tiers offer this week without a code change.
"""
import json
import os
import threading
import time

import requests

GROQ_SMALL = os.environ.get("GROQ_SMALL_MODEL", "openai/gpt-oss-20b")
GROQ_LARGE = os.environ.get("GROQ_LARGE_MODEL", "openai/gpt-oss-120b")


class _Bucket:
    """Spaces calls out to stay under a requests-per-minute limit."""

    def __init__(self, rpm: int):
        self.gap = 60.0 / rpm
        self.next_at = 0.0
        self.lock = threading.Lock()

    def wait(self):
        with self.lock:
            now = time.monotonic()
            delay = self.next_at - now
            self.next_at = max(now, self.next_at) + self.gap
        if delay > 0:
            time.sleep(delay)


_groq_bucket = _Bucket(int(os.environ.get("GROQ_RPM", 25)))


def _raise_for(r: requests.Response, provider: str, model: str):
    if r.ok:
        return
    try:
        msg = r.json().get("error", {}).get("message") or r.text
    except ValueError:
        msg = r.text
    raise RuntimeError(f"{provider} {model} HTTP {r.status_code}: {msg[:300]}")


def _log(provider: str, model: str, purpose: str, tokens_in: int | None, tokens_out: int | None):
    from pipeline import db  # local import: llm is usable without a database in tests

    try:
        db.insert("usage", [{
            "provider": provider, "model": model, "purpose": purpose,
            "tokens_in": tokens_in, "tokens_out": tokens_out,
        }])
    except Exception as e:
        print(f"usage log failed: {e}")


def groq_json(prompt: str, purpose: str, model: str = GROQ_SMALL, retries: int = 3) -> dict:
    for attempt in range(retries):
        _groq_bucket.wait()
        r = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {os.environ['GROQ_API_KEY']}"},
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "response_format": {"type": "json_object"},
                "temperature": 0,
            },
            timeout=60,
        )
        if r.status_code == 429 and attempt < retries - 1:
            time.sleep(float(r.headers.get("retry-after", 5 * (attempt + 1))))
            continue
        _raise_for(r, "groq", model)
        body = r.json()
        usage = body.get("usage", {})
        _log("groq", model, purpose, usage.get("prompt_tokens"), usage.get("completion_tokens"))
        return json.loads(body["choices"][0]["message"]["content"])
    raise RuntimeError("groq: out of retries")


GEMINI_FLASH = os.environ.get("GEMINI_FLASH_MODEL", "gemini-3.5-flash")
GEMINI_LITE = os.environ.get("GEMINI_LITE_MODEL", "gemini-3.5-flash-lite")
GEMINI_PRO = os.environ.get("GEMINI_PRO_MODEL", "gemini-pro-latest")
_gemini_bucket = _Bucket(int(os.environ.get("GEMINI_RPM", 8)))


# Free-tier quotas are per model and per day, so when one model's daily
# allowance runs out the call moves down this list instead of failing.
GEMINI_FALLBACKS = [m for m in os.environ.get(
    "GEMINI_FALLBACKS", "gemini-3.5-flash-lite,gemini-3.1-flash-lite,gemini-flash-lite-latest").split(",") if m]


class QuotaExhausted(RuntimeError):
    pass


class ModelUnavailable(RuntimeError):
    """Overloaded (503) or too slow. Another model will usually answer."""


def gemini_json(prompt: str, purpose: str, model: str = GEMINI_FLASH, fallback: bool = True, **kw) -> dict:
    chain = [model] + ([m for m in GEMINI_FALLBACKS if m != model] if fallback else [])
    for i, m in enumerate(chain):
        try:
            return _gemini(prompt, purpose, m, **kw)
        except (QuotaExhausted, ModelUnavailable) as e:
            if i == len(chain) - 1:
                raise
            print(f"{m}: {e}; trying {chain[i + 1]}")
    raise AssertionError("unreachable")


def _gemini(prompt: str, purpose: str, model: str,
            pdf: bytes | None = None, schema: dict | None = None, retries: int = 2,
            think: str = "low") -> dict:
    """think: "low" for copying facts out of a document, "high" for judgment calls.
    Reading a results table doesn't need a long chain of thought, and Gemini 3.x
    thinks hard by default, which made 8-page extractions run past three minutes."""
    parts = [{"text": prompt}]
    if pdf is not None:
        import base64

        parts.insert(0, {"inline_data": {"mime_type": "application/pdf",
                                         "data": base64.b64encode(pdf).decode()}})
    config = {"responseMimeType": "application/json", "temperature": 0}
    if schema:
        config["responseSchema"] = schema
    if model.startswith("gemini-3"):
        config["thinkingConfig"] = {"thinkingLevel": think}

    for attempt in range(retries):
        _gemini_bucket.wait()
        try:
            r = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                headers={"x-goog-api-key": os.environ["GEMINI_API_KEY"]},
                json={"contents": [{"parts": parts}], "generationConfig": config},
                timeout=90,
            )
        except requests.Timeout:
            raise ModelUnavailable(f"gemini {model}: no answer in 90 s")
        if r.status_code == 400 and "thinking" in r.text.lower() and "thinkingConfig" in config:
            config.pop("thinkingConfig")  # model doesn't take the setting; run without it
            continue
        if r.status_code == 429 and "PerDay" in r.text:
            raise QuotaExhausted(f"gemini {model}: daily free quota used up")
        if r.status_code in (500, 503):
            # Overload is per model; waiting rarely helps, switching does.
            raise ModelUnavailable(f"gemini {model}: HTTP {r.status_code}, overloaded")
        if r.status_code == 429 and attempt < retries - 1:
            time.sleep(10)
            continue
        _raise_for(r, "gemini", model)
        body = r.json()
        usage = body.get("usageMetadata", {})
        _log("gemini", model, purpose, usage.get("promptTokenCount"), usage.get("candidatesTokenCount"))
        text = body["candidates"][0]["content"]["parts"][-1]["text"]
        return json.loads(text)
    raise RuntimeError(f"gemini {model}: out of retries")


def cached(key_parts: list[str], fn, stats: dict | None = None):
    """Return a stored model output for these inputs, or compute and store it.
    stats, if given, counts hits and misses for the caller's cost line."""
    import hashlib

    from pipeline import db

    key = hashlib.sha256("|".join(key_parts).encode()).hexdigest()
    hit = db.client().table("research_cache").select("value").eq("key", key).execute().data
    if hit:
        if stats is not None:
            stats["hits"] = stats.get("hits", 0) + 1
        return hit[0]["value"]
    if stats is not None:
        stats["misses"] = stats.get("misses", 0) + 1
    value = fn()
    db.upsert("research_cache", [{"key": key, "value": value}], on_conflict="key")
    return value


if __name__ == "__main__":
    # python -m pipeline.llm  → which model IDs actually answer for these keys
    from dotenv import load_dotenv

    load_dotenv()
    _log = lambda *a, **k: None  # noqa: E731  (don't write pings to the usage table)
    ping = 'Reply with the JSON {"ok": true}'
    candidates = [("gemini", m) for m in (
        "gemini-flash-latest", "gemini-flash-lite-latest", "gemini-3.5-flash", "gemini-3.5-flash-lite",
        "gemini-3.1-flash-lite", "gemini-2.5-flash", "gemini-pro-latest", "gemini-3.1-pro-preview",
    )] + [("groq", m) for m in ("openai/gpt-oss-20b", "openai/gpt-oss-120b", "qwen/qwen3.8-27b")]
    for provider, model in candidates:
        t0 = time.perf_counter()
        try:
            fn = gemini_json if provider == "gemini" else groq_json
            if provider == "gemini":
                fn(ping, purpose="check", model=model, retries=1, fallback=False)
            else:
                fn(ping, purpose="check", model=model, retries=1)
            result = "ok"
        except Exception as e:
            result = str(e).split(": ", 1)[-1][:110]
        print(f"{provider:<7}{model:<28}{time.perf_counter() - t0:5.1f}s  {result}")
