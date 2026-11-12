from datetime import datetime, timezone

from sqlalchemy import select

from app.models import AuditLog, Operation, Product, User, Warehouse


def _login(client):
    response = client.post(
        "/login",
        data={"username": "admin", "password": "admin-password-123"},
        follow_redirects=False,
    )
    assert response.status_code == 303


def _seed_export_data(db):
    product = Product(sku="CSV-101", name="CSV товар")
    source = Warehouse(name="CSV Север")
    destination = Warehouse(name="CSV Юг")
    db.add_all([product, source, destination])
    db.flush()

    operation = Operation(
        type="TRANSFER",
        status="IN_TRANSIT",
        product_id=product.id,
        source_warehouse_id=source.id,
        destination_warehouse_id=destination.id,
        qty=7,
        note="обычная операция",
        created_at=datetime(2026, 11, 12, 10, 30, tzinfo=timezone.utc),
    )
    db.add(operation)
    db.flush()
    admin = db.scalar(select(User).where(User.username == "admin"))
    db.add(
        AuditLog(
            actor_user_id=admin.id,
            action="stock.transfer",
            entity_type="operation",
            entity_id=operation.id,
            details={},
        )
    )
    db.commit()
    return operation, source, destination


def test_history_export_requires_authentication(auth_client):
    client, _ = auth_client

    response = client.get("/history/export", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_history_export_contains_filtered_operation_and_excel_bom(auth_client):
    client, db = auth_client
    operation, source, destination = _seed_export_data(db)
    _login(client)

    response = client.get(
        "/history/export",
        params={
            "operation_type": "TRANSFER",
            "status": "IN_TRANSIT",
            "warehouse_id": source.id,
            "date_from": "2026-11-12",
            "date_to": "2026-11-12",
        },
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment;" in response.headers["content-disposition"]
    content = response.content.decode("utf-8-sig")
    assert "ID операции;Дата и время (UTC)" in content
    assert str(operation.id) in content
    assert "CSV-101" in content
    assert "CSV товар" in content
    assert "CSV Север" in content
    assert "CSV Юг" in content
    assert "admin" in content
    assert "TRANSFER" in content


def test_history_export_applies_search_filter_and_prevents_formula_injection(
    auth_client,
):
    client, db = auth_client
    operation, _, _ = _seed_export_data(db)
    operation.note = '=HYPERLINK("https://example.com")'
    db.commit()
    _login(client)

    response = client.get("/history/export", params={"q": "CSV-101"})

    assert response.status_code == 200
    content = response.content.decode("utf-8-sig")
    assert str(operation.id) in content
    assert "'=HYPERLINK(" in content
    assert "обычная операция" not in content


def test_history_export_rejects_invalid_filters(auth_client):
    client, _ = auth_client
    _login(client)

    response = client.get(
        "/history/export",
        params={"operation_type": "UNKNOWN"},
    )

    assert response.status_code == 422
