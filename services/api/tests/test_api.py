from unittest.mock import AsyncMock, MagicMock, patch

import redis
from fastapi.testclient import TestClient

from services.api.app import main
from services.api.app.main import app

client = TestClient(app)


def test_healthz():
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readyz_redis_up():
    with patch.object(main.r, "ping", return_value=True):
        response = client.get("/readyz")
        assert response.status_code == 200
        assert response.json() == {"status": "ready"}


def test_readyz_redis_down():
    with patch.object(main.r, "ping", side_effect=redis.RedisError("Connection refused")):
        response = client.get("/readyz")
        assert response.status_code == 503
        assert response.json()["detail"] == "redis unavailable"


def test_submit_job_unauthorized():
    mock_resp = MagicMock(status_code=401)
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = mock_resp
        response = client.post("/jobs", headers={"Authorization": "invalid-token"})
        assert response.status_code == 401
        assert response.json()["detail"] == "unauthorized"


def test_submit_job_success():
    mock_resp = MagicMock(status_code=200)
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = mock_resp
        with patch.object(main.r, "lpush") as mock_lpush:
            response = client.post("/jobs", headers={"Authorization": "valid-token"})
            assert response.status_code == 200
            assert response.json() == {"queued": True}
            mock_lpush.assert_called_once_with("jobs", "job")


def test_stats():
    with patch.object(main.r, "llen", return_value=3), patch.object(main.r, "get", return_value=b"5"):
        response = client.get("/stats")
        assert response.status_code == 200
        assert response.json() == {"queued": 3, "done": 5}


def test_chaos_endpoints_disabled():
    with patch.object(main, "ENABLE_CHAOS", False):
        assert client.post("/chaos/crash").status_code == 404
        assert client.post("/chaos/errors?rate=0.5").status_code == 404
        assert client.post("/chaos/latency?ms=100").status_code == 404
        assert client.post("/chaos/reset").status_code == 404


def test_chaos_endpoints_enabled():
    with patch.object(main, "ENABLE_CHAOS", True):
        # test errors
        resp = client.post("/chaos/errors?rate=0.4")
        assert resp.status_code == 200
        assert resp.json()["error_rate"] == 0.4

        # test latency
        resp = client.post("/chaos/latency?ms=50")
        assert resp.status_code == 200
        assert resp.json()["latency_ms"] == 50

        # test reset
        resp = client.post("/chaos/reset")
        assert resp.status_code == 200
        assert resp.json() == {"error_rate": 0.0, "latency_ms": 0}

        # test crash calls os._exit(1)
        with patch("os._exit") as mock_exit:
            resp = client.post("/chaos/crash")
            mock_exit.assert_called_once_with(1)
