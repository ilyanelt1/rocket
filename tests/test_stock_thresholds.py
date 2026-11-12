from datetime import datetime, timezone

from sqlalchemy import inspect, text, select
from sqlalchemy import create_engine

from app.migrations import apply_schema_migrations
from app.models import AuditLog, Product, StockBalance, Warehouse


def _login(client, username="admin", password="admin-password-123"):
    response = client.post(
        "/login",
        data={"username": username, "password": password},
        follow_redirects=False,
    )
    assert response.status_code == 303


def test_admin_can_update_minimum_stock_and_audit_it(auth_client):
    client, db = auth_client
    product = Product(sku="MIN-101", name="Пороговый товар")
    db.add(product)
    db.commit()
    product_id = product.id
    _login(client)

    response = client.post(
        f"/products/{product_id}/minimum-stock",
        data={"minimum_stock": "12"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    db.refresh(product)
    assert product.minimum_stock == 12
    event = db.scalar(
        select(AuditLog).where(
            AuditLog.action == "product.minimum_stock.update",
            AuditLog.entity_id == product_id,
        )
    )
    assert event is not None
    assert event.details == {
        "sku": "MIN-101",
        "previous_minimum_stock": 0,
        "minimum_stock": 12,
    }


def test_operator_cannot_update_minimum_stock(auth_client):
    client, db = auth_client
    product = Product(sku="MIN-102", name="Товар оператора")
    db.add(product)
    db.commit()
    product_id = product.id
    _login(client, "operator", "operator-password-123")

    response = client.post(
        f"/products/{product_id}/minimum-stock",
        data={"minimum_stock": "5"},
    )

    assert response.status_code == 403
    db.refresh(product)
    assert product.minimum_stock == 0


def test_dashboard_alert_uses_active_warehouse_available_stock(auth_client):
    client, db = auth_client
    product = Product(
        sku="MIN-103",
        name="Товар с минимальным остатком",
        minimum_stock=10,
    )
    north = Warehouse(name="Порог Север")
    south = Warehouse(name="Порог Юг")
    archived = Warehouse(name="Порог Архив")
    db.add_all([product, north, south, archived])
    db.flush()
    archived.deleted_at = datetime.now(timezone.utc)
    db.add_all(
        [
            StockBalance(
                product_id=product.id,
                warehouse_id=north.id,
                physical_qty=4,
                reserved_qty=1,
            ),
            StockBalance(
                product_id=product.id,
                warehouse_id=south.id,
                physical_qty=5,
                reserved_qty=0,
            ),
            StockBalance(
                product_id=product.id,
                warehouse_id=archived.id,
                physical_qty=100,
                reserved_qty=0,
            ),
        ]
    )
    db.commit()
    _login(client)

    response = client.get("/")

    assert response.status_code == 200
    assert "Товар с минимальным остатком" in response.text
    assert "Ниже минимума" in response.text
    assert "Минимальный остаток" in response.text
    assert ">8</td>" in response.text
    assert ">10</td>" in response.text


def test_minimum_stock_migration_is_idempotent_and_preserves_existing_products():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE products ("
                "id INTEGER PRIMARY KEY, sku VARCHAR(64), name VARCHAR(200))"
            )
        )
        connection.execute(
            text(
                "INSERT INTO products (id, sku, name) "
                "VALUES (1, 'OLD-101', 'Существующий товар')"
            )
        )

    apply_schema_migrations(engine)
    apply_schema_migrations(engine)

    with engine.connect() as connection:
        columns = {column["name"] for column in inspect(connection).get_columns("products")}
        row = connection.execute(
            text("SELECT sku, name, minimum_stock FROM products WHERE id = 1")
        ).one()

    assert "minimum_stock" in columns
    assert row == ("OLD-101", "Существующий товар", 0)
    engine.dispose()
