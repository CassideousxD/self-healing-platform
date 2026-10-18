import hmac, hashlib, os
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from prometheus_fastapi_instrumentator import Instrumentator

app = FastAPI(title="auth")
Instrumentator().instrument(app).expose(app)  # adds /metrics

SECRET = os.getenv("AUTH_SECRET", "dev-secret").encode()
USERS = {"demo": "demo123"}

def sign(username: str) -> str:
    return f"{username}.{hmac.new(SECRET, username.encode(), hashlib.sha256).hexdigest()}"

class Login(BaseModel):
    username: str
    password: str

@app.get("/healthz")
def healthz(): return {"status": "ok"}

@app.get("/readyz")
def readyz(): return {"status": "ready"}

@app.post("/login")
def login(body: Login):
    if USERS.get(body.username) != body.password:
        raise HTTPException(401, "bad credentials")
    return {"token": sign(body.username)}

@app.get("/verify")
def verify(token: str):
    user = token.split(".")[0]
    if not hmac.compare_digest(token, sign(user)):
        raise HTTPException(401, "invalid token")
    return {"valid": True, "user": user}
