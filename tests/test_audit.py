from sqlalchemy import select

from app.models import AuditLog, Product, Warehouse


def test_admin_actions_and_stock_operations_are_audited(auth_client):
    client, db = auth_client
    login = client.post(
        "/login",
        data={"username": "admin", "password": "admin-password-123"},
        follow_redirects=False,
    )
    assert login.status_code == 303

    warehouse_response = client.post(
        "/warehouses",
        data={"name": "Основной склад"},
        follow_redirects=False,
    )
    assert warehouse_response.status_code == 303

    product = Product(sku="AUDIT-1", name="Тестовый товар")
    warehouse = Warehouse(name="Операционный склад")
    db.add_all([product, warehouse])
    db.flush()

    operation_response = client.post(
        "/operations",
        data={
            "kind": "RECEIPT",
            "product_id": product.id,
            "warehouse_id": warehouse.id,
            "qty": 7,
            "idempotency_key": "audit-receipt-1",
        },
        follow_redirects=False,
    )
    assert operation_response.status_code == 303

    repeated_operation_response = client.post(
        "/operations",
        data={
            "kind": "RECEIPT",
            "product_id": product.id,
            "warehouse_id": warehouse.id,
            "qty": 7,
            "idempotency_key": "audit-receipt-1",
        },
        follow_redirects=False,
    )
    assert repeated_operation_response.status_code == 303

    user_response = client.post(
        "/users",
        data={
            "username": "new-operator",
            "password": "new-operator-password-123",
            "role": "operator",
        },
        follow_redirects=False,
    )
    assert user_response.status_code == 303

    events = db.scalars(select(AuditLog).order_by(AuditLog.id)).all()
    actions = [event.action for event in events]

    assert "auth.login" in actions
    assert "warehouse.create" in actions
    assert "stock.receipt" in actions
    assert actions.count("stock.receipt") == 1
    assert "user.create" in actions
    assert all(event.actor_user_id is not None for event in events)

    stock_event = next(event for event in events if event.action == "stock.receipt")
    assert stock_event.details["quantity"] == 7
    assert stock_event.details["operation_type"] == "RECEIPT"


def test_audit_log_is_visible_only_to_admin(auth_client):
    client, _ = auth_client

    client.post(
        "/login",
        data={"username": "admin", "password": "admin-password-123"},
    )
    admin_dashboard = client.get("/")
    assert "Журнал действий" in admin_dashboard.text

    client.post("/logout")
    client.post(
        "/login",
        data={"username": "operator", "password": "operator-password-123"},
    )
    operator_dashboard = client.get("/")
    assert "Журнал действий" not in operator_dashboard.text



def test_failed_login_is_audited_without_password(auth_client):
    client, db = auth_client

    response = client.post(
        "/login",
        data={"username": "unknown-user", "password": "never-store-this-password"},
    )

    assert response.status_code == 401
    event = db.scalars(
        select(AuditLog).where(AuditLog.action == "auth.login_failed")
    ).one()
    assert event.actor_user_id is None
    assert event.details == {
        "username": "unknown-user",
        "reason": "invalid_credentials",
    }
    assert "password" not in str(event.details)


def test_warehouse_rename_and_archive_are_audited(auth_client):
    client, db = auth_client
    client.post(
        "/login",
        data={"username": "admin", "password": "admin-password-123"},
    )

    create_response = client.post(
        "/warehouses",
        data={"name": "Склад до изменения"},
        follow_redirects=False,
    )
    assert create_response.status_code == 303
    warehouse = db.scalars(
        select(Warehouse).where(Warehouse.name == "Склад до изменения")
    ).one()

    rename_response = client.post(
        f"/warehouses/{warehouse.id}/rename",
        data={"name": "Склад после изменения"},
        follow_redirects=False,
    )
    assert rename_response.status_code == 303

    archive_response = client.post(
        f"/warehouses/{warehouse.id}/delete",
        follow_redirects=False,
    )
    assert archive_response.status_code == 303

    rename_event = db.scalars(
        select(AuditLog).where(AuditLog.action == "warehouse.rename")
    ).one()
    archive_event = db.scalars(
        select(AuditLog).where(AuditLog.action == "warehouse.archive")
    ).one()

    assert rename_event.details == {
        "old_name": "Склад до изменения",
        "new_name": "Склад после изменения",
    }
    assert archive_event.details["name"] == "Склад после изменения"
    assert "archived_at" in archive_event.details
