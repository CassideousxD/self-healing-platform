import asyncio
import os
import random

import httpx
import redis
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from prometheus_fastapi_instrumentator import Instrumentator

app = FastAPI(title="api")
Instrumentator().instrument(app).expose(app)

AUTH_URL = os.getenv("AUTH_URL", "http://auth:8000")
ENABLE_CHAOS = os.getenv("ENABLE_CHAOS", "false") == "true"
r = redis.Redis.from_url(os.getenv("REDIS_URL", "redis://redis:6379"))
chaos = {"error_rate": 0.0, "latency_ms": 0}

SKIP = ("/chaos", "/healthz", "/readyz", "/metrics")


@app.middleware("http")
async def chaos_middleware(request, call_next):
    if request.url.path.startswith(SKIP):
        return await call_next(request)
    if chaos["latency_ms"]:
        await asyncio.sleep(chaos["latency_ms"] / 1000)
    if random.random() < chaos["error_rate"]:
        return JSONResponse({"error": "injected failure"}, status_code=500)
    return await call_next(request)


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/readyz")
def readyz():
    try:
        r.ping()
    except redis.RedisError:
        raise HTTPException(503, "redis unavailable")
    return {"status": "ready"}


@app.post("/jobs")
async def submit_job(authorization: str = Header(...)):
    async with httpx.AsyncClient(timeout=2) as c:
        resp = await c.get(f"{AUTH_URL}/verify", params={"token": authorization})
    if resp.status_code != 200:
        raise HTTPException(401, "unauthorized")
    r.lpush("jobs", "job")
    return {"queued": True}


@app.get("/stats")
def stats():
    return {"queued": r.llen("jobs"), "done": int(r.get("jobs_done") or 0)}


# --- chaos endpoints (dev/demo only) ---
def _require_chaos():
    if not ENABLE_CHAOS:
        raise HTTPException(404)


@app.post("/chaos/crash")
def crash():
    _require_chaos()
    os._exit(1)  # hard kill, simulates a real crash


@app.post("/chaos/errors")
def set_errors(rate: float):
    _require_chaos()
    chaos["error_rate"] = rate
    return chaos


@app.post("/chaos/latency")
def set_latency(ms: int):
    _require_chaos()
    chaos["latency_ms"] = ms
    return chaos


@app.post("/chaos/reset")
def reset():
    _require_chaos()
    chaos.update(error_rate=0.0, latency_ms=0)
    return chaos
