from datetime import datetime, timedelta, timezone

from app.models import AuditLog, Operation, Product, Warehouse


def _login(client):
    response = client.post(
        "/login",
        data={"username": "admin", "password": "admin-password-123"},
        follow_redirects=False,
    )
    assert response.status_code == 303


def _seed_history(db):
    product = Product(sku="SKU-HISTORY", name="Исторический товар")
    active = Warehouse(name="История Север")
    archived = Warehouse(
        name="История Архив",
        deleted_at=datetime.now(timezone.utc) - timedelta(days=1),
    )
    db.add_all([product, active, archived])
    db.flush()

    now = datetime.now(timezone.utc)
    operations = [
        Operation(
            type="RECEIPT",
            status="DONE",
            product_id=product.id,
            destination_warehouse_id=active.id,
            qty=5,
            created_at=now - timedelta(days=2),
        ),
        Operation(
            type="TRANSFER",
            status="IN_TRANSIT",
            product_id=product.id,
            source_warehouse_id=active.id,
            destination_warehouse_id=archived.id,
            qty=2,
            created_at=now - timedelta(days=1),
        ),
        Operation(
            type="FBS_SHIP",
            status="SHIPPED",
            product_id=product.id,
            source_warehouse_id=archived.id,
            qty=1,
            created_at=now,
        ),
    ]
    db.add_all(operations)
    db.flush()
    for operation in operations:
        db.add(
            AuditLog(
                actor_user_id=1,
                action=f"stock.{operation.type.lower()}",
                entity_type="operation",
                entity_id=operation.id,
                details={"operation_type": operation.type},
            )
        )
    db.commit()
    return product, active, archived, operations


def test_history_requires_authentication(auth_client):
    client, _ = auth_client

    response = client.get("/history", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_history_filters_by_type_status_and_archived_warehouse(auth_client):
    client, db = auth_client
    product, _, archived, operations = _seed_history(db)
    _login(client)

    response = client.get(
        "/history",
        params={
            "q": product.sku,
            "operation_type": "TRANSFER",
            "status": "IN_TRANSIT",
            "warehouse_id": archived.id,
        },
    )

    assert response.status_code == 200
    assert "Исторический товар" in response.text
    assert "<code>TRANSFER</code>" in response.text
    assert "История Архив (архив)" in response.text
    assert f">{operations[1].id}<" in response.text
    assert f">{operations[0].id}<" not in response.text
    assert f">{operations[2].id}<" not in response.text
    assert "admin" in response.text


def test_history_search_and_date_filters(auth_client):
    client, db = auth_client
    product, _, _, operations = _seed_history(db)
    _login(client)

    date_from = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
    date_to = datetime.now(timezone.utc).date().isoformat()
    response = client.get(
        "/history",
        params={
            "q": "Исторический товар",
            "date_from": date_from,
            "date_to": date_to,
        },
    )

    assert response.status_code == 200
    assert f">{operations[0].id}<" not in response.text
    assert f">{operations[1].id}<" in response.text
    assert f">{operations[2].id}<" in response.text
    assert product.sku in response.text


def test_history_rejects_invalid_date_range(auth_client):
    client, _ = auth_client
    _login(client)

    response = client.get(
        "/history",
        params={"date_from": "2026-11-12", "date_to": "2026-11-01"},
    )

    assert response.status_code == 422
