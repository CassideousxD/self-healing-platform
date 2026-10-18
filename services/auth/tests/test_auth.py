from fastapi.testclient import TestClient

from services.auth.app.main import app, sign

client = TestClient(app)


def test_healthz():
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readyz():
    response = client.get("/readyz")
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


def test_login_success():
    response = client.post("/login", json={"username": "demo", "password": "demo123"})
    assert response.status_code == 200
    data = response.json()
    assert "token" in data
    assert data["token"].startswith("demo.")


def test_login_invalid_credentials():
    response = client.post("/login", json={"username": "demo", "password": "wrongpassword"})
    assert response.status_code == 401
    assert response.json()["detail"] == "bad credentials"


def test_verify_valid_token():
    token = sign("demo")
    response = client.get(f"/verify?token={token}")
    assert response.status_code == 200
    assert response.json() == {"valid": True, "user": "demo"}


def test_verify_tampered_token():
    token = sign("demo")
    tampered = token + "xyz"
    response = client.get(f"/verify?token={tampered}")
    assert response.status_code == 401
    assert response.json()["detail"] == "invalid token"
