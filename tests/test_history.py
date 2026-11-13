import csv
import io
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
    assert f"<tr><td>{operations[1].id}</td>" in response.text
    assert f"<tr><td>{operations[0].id}</td>" not in response.text
    assert f"<tr><td>{operations[2].id}</td>" not in response.text
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
    assert f"<tr><td>{operations[0].id}</td>" not in response.text
    assert f"<tr><td>{operations[1].id}</td>" in response.text
    assert f"<tr><td>{operations[2].id}</td>" in response.text
    assert product.sku in response.text


def test_history_rejects_invalid_date_range(auth_client):
    client, _ = auth_client
    _login(client)

    response = client.get(
        "/history",
        params={"date_from": "2026-11-12", "date_to": "2026-11-01"},
    )

    assert response.status_code == 422


def test_history_and_export_use_the_same_filters(auth_client):
    client, db = auth_client
    product, _, archived, operations = _seed_history(db)
    _login(client)
    params = {
        "q": product.sku,
        "operation_type": "TRANSFER",
        "status": "IN_TRANSIT",
        "warehouse_id": archived.id,
    }

    page = client.get("/history", params=params)
    export = client.get("/history/export", params=params)

    assert page.status_code == 200
    assert export.status_code == 200
    assert f"<tr><td>{operations[1].id}</td>" in page.text
    exported_rows = list(
        csv.reader(io.StringIO(export.text.lstrip("\ufeff")), delimiter=";")
    )
    exported_ids = {int(row[0]) for row in exported_rows[1:]}
    assert exported_ids == {operations[1].id}


def test_history_export_escapes_formula_after_leading_whitespace(auth_client):
    client, db = auth_client
    _, _, _, operations = _seed_history(db)
    operations[0].note = " =1+1"
    db.commit()
    _login(client)

    response = client.get("/history/export", params={"q": "1+1"})

    assert response.status_code == 200
    assert response.content.startswith(b"\xef\xbb\xbf")
    assert "' =1+1" in response.text


def test_history_shows_latest_audit_actor_deterministically(auth_client):
    client, db = auth_client
    _, _, _, operations = _seed_history(db)
    db.add(
        AuditLog(
            actor_user_id=2,
            action="stock.receipt",
            entity_type="operation",
            entity_id=operations[0].id,
            details={"source": "newer event"},
            created_at=datetime.now(timezone.utc) + timedelta(seconds=1),
        )
    )
    db.commit()
    _login(client)

    response = client.get("/history")

    assert response.status_code == 200
    row = response.text.split(f"<tr><td>{operations[0].id}</td>", 1)[1].split(
        "</tr>", 1
    )[0]
    assert "operator" in row
    assert "admin" not in row


def test_history_and_export_reject_invalid_date_range_consistently(auth_client):
    client, _ = auth_client
    _login(client)
    params = {"date_from": "2026-11-12", "date_to": "2026-11-01"}

    assert client.get("/history", params=params).status_code == 422
    assert client.get("/history/export", params=params).status_code == 422


def test_history_and_csv_export_accept_blank_optional_integer_filters(auth_client):
    client, db = auth_client
    _seed_history(db)
    _login(client)
    params = {"product_id": "", "warehouse_id": ""}

    page = client.get("/history", params=params)
    export = client.get("/history/export", params=params)

    assert page.status_code == 200
    assert "История операций" in page.text
    assert export.status_code == 200
    assert export.headers["content-type"].startswith("text/csv")
    assert "ID операции;Дата и время (UTC)" in export.content.decode("utf-8-sig")
