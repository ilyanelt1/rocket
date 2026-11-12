from app.auth import hash_password, verify_password

import pytest
from app.models import AuditLog, User


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



def test_admin_can_change_user_role_and_audit(auth_client):
    client, db = auth_client
    client.post(
        "/login",
        data={"username": "admin", "password": "admin-password-123"},
    )

    response = client.post(
        "/users/2/role",
        data={"role": "warehouse_manager"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    operator = db.query(User).filter_by(username="operator").one()
    assert operator.role == "warehouse_manager"
    event = db.query(AuditLog).filter_by(action="user.role_change").one()
    assert event.details == {
        "username": "operator",
        "old_role": "operator",
        "new_role": "warehouse_manager",
    }


def test_admin_can_deactivate_and_reactivate_user(auth_client):
    client, db = auth_client
    client.post(
        "/login",
        data={"username": "admin", "password": "admin-password-123"},
    )

    deactivate = client.post("/users/2/deactivate", follow_redirects=False)
    assert deactivate.status_code == 303
    operator = db.query(User).filter_by(username="operator").one()
    assert operator.is_active is False

    blocked_login = client.post(
        "/login",
        data={"username": "operator", "password": "operator-password-123"},
    )
    assert blocked_login.status_code == 401

    activate = client.post("/users/2/activate", follow_redirects=False)
    assert activate.status_code == 303
    assert operator.is_active is True

    allowed_login = client.post(
        "/login",
        data={"username": "operator", "password": "operator-password-123"},
        follow_redirects=False,
    )
    assert allowed_login.status_code == 303
    assert db.query(AuditLog).filter_by(action="user.deactivate").count() == 1
    assert db.query(AuditLog).filter_by(action="user.activate").count() == 1


def test_last_active_admin_cannot_be_deactivated_or_demoted(auth_client):
    client, db = auth_client
    client.post(
        "/login",
        data={"username": "admin", "password": "admin-password-123"},
    )

    deactivate = client.post("/users/1/deactivate", follow_redirects=False)
    demote = client.post(
        "/users/1/role",
        data={"role": "operator"},
        follow_redirects=False,
    )

    assert deactivate.status_code == 409
    assert demote.status_code == 409
    admin = db.query(User).filter_by(username="admin").one()
    assert admin.is_active is True
    assert admin.role == "admin"


def test_admin_cannot_change_own_role_or_deactivate_self(auth_client):
    client, db = auth_client
    client.post(
        "/login",
        data={"username": "admin", "password": "admin-password-123"},
    )

    demote = client.post(
        "/users/1/role",
        data={"role": "operator"},
        follow_redirects=False,
    )
    deactivate = client.post("/users/1/deactivate", follow_redirects=False)

    assert demote.status_code == 409
    assert deactivate.status_code == 409
    admin = db.query(User).filter_by(username="admin").one()
    assert admin.is_active is True
    assert admin.role == "admin"


def test_last_admin_protection_allows_change_when_another_admin_exists(auth_client):
    client, db = auth_client
    client.post(
        "/login",
        data={"username": "admin", "password": "admin-password-123"},
    )
    client.post(
        "/users",
        data={
            "username": "second-admin",
            "password": "second-admin-password",
            "role": "admin",
        },
    )
    second_admin = db.query(User).filter_by(username="second-admin").one()
    first_admin = db.query(User).filter_by(username="admin").one()

    client.post("/logout", follow_redirects=False)
    client.post(
        "/login",
        data={"username": "second-admin", "password": "second-admin-password"},
    )

    demote = client.post(
        f"/users/{first_admin.id}/role",
        data={"role": "operator"},
        follow_redirects=False,
    )
    assert demote.status_code == 303
    assert first_admin.role == "operator"

    promote = client.post(
        f"/users/{first_admin.id}/role",
        data={"role": "admin"},
        follow_redirects=False,
    )
    assert promote.status_code == 303

    deactivate = client.post(
        f"/users/{first_admin.id}/deactivate",
        follow_redirects=False,
    )

    assert deactivate.status_code == 303
    assert first_admin.is_active is False
    assert second_admin.is_active is True
