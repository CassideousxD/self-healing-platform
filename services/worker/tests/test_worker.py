from unittest.mock import patch

from fastapi.testclient import TestClient

from services.worker.app import main
from services.worker.app.main import app, process_one

client = TestClient(app)


def test_healthz():
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readyz():
    response = client.get("/readyz")
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


def test_process_one_no_item():
    with patch.object(main.r, "brpop", return_value=None):
        processed = process_one(timeout=1)
        assert processed is False


def test_process_one_with_item():
    with (
        patch.object(main.r, "brpop", return_value=(b"jobs", b"job")),
        patch.object(main.r, "incr") as mock_incr,
        patch("time.sleep"),
        patch.object(main.JOBS_DONE, "inc") as mock_metric_inc,
    ):
        processed = process_one(timeout=1)
        assert processed is True
        mock_incr.assert_called_once_with("jobs_done")
        mock_metric_inc.assert_called_once()


def test_get_queue_depth_success():
    with patch.object(main.r, "llen", return_value=5):
        assert main.get_queue_depth() == 5.0
        response = client.get("/metrics")
        assert response.status_code == 200
        assert "jobs_queue_depth 5.0" in response.text


def test_get_queue_depth_redis_error():
    import math

    import redis

    with patch.object(main.r, "llen", side_effect=redis.RedisError("connection error")):
        assert math.isnan(main.get_queue_depth())
        response = client.get("/metrics")
        assert response.status_code == 200
        assert "jobs_queue_depth NaN" in response.text
