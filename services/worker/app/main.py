import os, threading, time
import redis
from fastapi import FastAPI
from prometheus_client import Counter
from prometheus_fastapi_instrumentator import Instrumentator

app = FastAPI(title="worker")
Instrumentator().instrument(app).expose(app)

r = redis.Redis.from_url(os.getenv("REDIS_URL", "redis://redis:6379"))
JOB_SECONDS = float(os.getenv("JOB_SECONDS", "1"))
JOBS_DONE = Counter("worker_jobs_processed_total", "Jobs processed")

def consume():
    while True:
        try:
            item = r.brpop("jobs", timeout=5)
            if item:
                time.sleep(JOB_SECONDS)  # pretend to work
                r.incr("jobs_done")
                JOBS_DONE.inc()
        except redis.RedisError:
            time.sleep(2)  # redis down, back off and retry

threading.Thread(target=consume, daemon=True).start()

@app.get("/healthz")
def healthz(): return {"status": "ok"}

@app.get("/readyz")
def readyz(): return {"status": "ready"}
