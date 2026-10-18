import os
import threading
import time
from contextlib import asynccontextmanager

import redis
from fastapi import FastAPI
from prometheus_client import Counter, Gauge
from prometheus_fastapi_instrumentator import Instrumentator


@asynccontextmanager
async def lifespan(app: FastAPI):
    threading.Thread(target=consume, daemon=True).start()
    yield


app = FastAPI(title="worker", lifespan=lifespan)
Instrumentator().instrument(app).expose(app)

r = redis.Redis.from_url(os.getenv("REDIS_URL", "redis://redis:6379"))
JOB_SECONDS = float(os.getenv("JOB_SECONDS", "1"))
JOBS_DONE = Counter("worker_jobs_processed_total", "Jobs processed")
JOBS_QUEUE_DEPTH = Gauge("jobs_queue_depth", "Current depth of jobs queue")


def get_queue_depth() -> float:
    try:
        return float(r.llen("jobs"))
    except Exception:
        return float("nan")


JOBS_QUEUE_DEPTH.set_function(get_queue_depth)

def process_one(timeout: int = 5) -> bool:
    item = r.brpop("jobs", timeout=timeout)
    if item:
        time.sleep(JOB_SECONDS)  # pretend to work
        r.incr("jobs_done")
        JOBS_DONE.inc()
        return True
    return False

def consume():
    while True:
        try:
            process_one(timeout=5)
        except redis.RedisError:
            time.sleep(2)  # redis down, back off and retry

@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/readyz")
def readyz():
    return {"status": "ready"}

