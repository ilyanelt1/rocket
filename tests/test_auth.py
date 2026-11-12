from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import hash_password, verify_password
from app.db import Base, get_db
from app.main import app
from app.models import User


@pytest.fixture
def auth_client() -> Generator[tuple[TestClient, object], None, None]:
    test_engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(test_engine)
    test_session_factory = sessionmaker(
        bind=test_engine, autoflush=False, expire_on_commit=False
    )
    db = test_session_factory()
    db.add_all(
        [
            User(
                username="admin",
                password_hash=hash_password("admin-password-123"),
                role="admin",
            ),
            User(
                username="operator",
                password_hash=hash_password("operator-password-123"),
                role="operator",
            ),
        ]
    )
    db.commit()

    def override_get_db():
        yield db

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as client:
        yield client, db

    app.dependency_overrides.clear()
    db.close()
    Base.metadata.drop_all(test_engine)
    test_engine.dispose()


def test_password_hash_is_salted_and_verified():
    first = hash_password("correct-horse-battery")
    second = hash_password("correct-horse-battery")

    assert first != second
    assert verify_password("correct-horse-battery", first)
    assert not verify_password("wrong-password", first)


def test_password_must_be_at_least_twelve_characters():
    with pytest.raises(ValueError, match="12 символов"):
        hash_password("short")


def test_dashboard_redirects_anonymous_user_to_login(auth_client):
    client, _ = auth_client

    response = client.get("/", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_login_and_logout(auth_client):
    client, _ = auth_client

    login_response = client.post(
        "/login",
        data={"username": "admin", "password": "admin-password-123"},
        follow_redirects=False,
    )
    assert login_response.status_code == 303
    assert login_response.headers["location"] == "/"

    dashboard_response = client.get("/")
    assert dashboard_response.status_code == 200
    assert "Пользователи" in dashboard_response.text

    logout_response = client.post("/logout", follow_redirects=False)
    assert logout_response.status_code == 303
    assert logout_response.headers["location"] == "/login"
    assert client.get("/", follow_redirects=False).status_code == 303


def test_operator_cannot_manage_warehouses(auth_client):
    client, _ = auth_client
    client.post(
        "/login",
        data={"username": "operator", "password": "operator-password-123"},
    )

    response = client.post(
        "/warehouses",
        data={"name": "Новый склад"},
        follow_redirects=False,
    )

    assert response.status_code == 403


def test_operator_cannot_create_users(auth_client):
    client, _ = auth_client
    client.post(
        "/login",
        data={"username": "operator", "password": "operator-password-123"},
    )

    response = client.post(
        "/users",
        data={
            "username": "newuser",
            "password": "new-user-password-123",
            "role": "warehouse_manager",
        },
    )

    assert response.status_code == 403


def test_admin_can_create_user_with_role(auth_client):
    client, db = auth_client
    client.post(
        "/login",
        data={"username": "admin", "password": "admin-password-123"},
    )

    response = client.post(
        "/users",
        data={
            "username": "manager",
            "password": "manager-password-123",
            "role": "warehouse_manager",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    manager = db.query(User).filter_by(username="manager").one()
    assert manager.role == "warehouse_manager"
    assert verify_password("manager-password-123", manager.password_hash)
