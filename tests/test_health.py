from unittest.mock import Mock

from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

from app.db import get_db
from app.main import app


def test_health_endpoints_are_public_and_security_headers_are_present(auth_client):
    client, _ = auth_client

    live = client.get("/health/live")
    ready = client.get("/health/ready")

    assert live.status_code == 200
    assert live.json() == {"status": "ok"}
    assert ready.status_code == 200
    assert ready.json() == {"status": "ok", "database": "ok"}
    assert live.headers["x-content-type-options"] == "nosniff"
    assert live.headers["x-frame-options"] == "DENY"
    assert live.headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert live.headers["permissions-policy"] == (
        "camera=(), microphone=(), geolocation=()"
    )


def test_readiness_returns_503_when_database_is_unavailable(auth_client):
    client, _ = auth_client

    class UnavailableDatabase:
        def execute(self, statement):
            raise SQLAlchemyError("database unavailable")

    def override_get_db():
        yield UnavailableDatabase()

    app.dependency_overrides[get_db] = override_get_db
    response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "error", "database": "unavailable"}


def test_liveness_does_not_require_database():
    with TestClient(app) as client:
        response = client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
