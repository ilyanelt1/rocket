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
        },
        follow_redirects=False,
    )
    assert operation_response.status_code == 303

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
