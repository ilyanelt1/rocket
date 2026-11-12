from datetime import datetime, timezone

from app.models import Product, StockBalance, Warehouse


def _login(client):
    response = client.post(
        "/login",
        data={"username": "admin", "password": "admin-password-123"},
        follow_redirects=False,
    )
    assert response.status_code == 303


def test_dashboard_requires_authentication(auth_client):
    client, _ = auth_client

    response = client.get("/", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_dashboard_shows_stock_summary_for_active_warehouses(auth_client):
    client, db = auth_client
    product_with_stock = Product(sku="DASH-101", name="Товар с остатком")
    product_without_stock = Product(sku="DASH-102", name="Товар без остатка")
    north = Warehouse(name="Дашборд Север")
    archived = Warehouse(name="Дашборд Архив")
    db.add_all([product_with_stock, product_without_stock, north, archived])
    db.flush()
    archived.deleted_at = datetime.now(timezone.utc)
    db.add(
        StockBalance(
            product_id=product_with_stock.id,
            warehouse_id=north.id,
            physical_qty=12,
            reserved_qty=5,
        )
    )
    db.add(
        StockBalance(
            product_id=product_with_stock.id,
            warehouse_id=archived.id,
            physical_qty=100,
            reserved_qty=0,
        )
    )
    db.commit()
    _login(client)

    response = client.get("/")

    assert response.status_code == 200
    assert "Сводка склада" in response.text
    assert "Физический остаток" in response.text
    assert "Резерв FBS" in response.text
    assert "Доступно" in response.text
    assert "Товар без остатка" in response.text
    assert "Товар с остатком" in response.text
    assert ">12</h3>" in response.text
    assert ">5</h3>" in response.text
    assert ">7</h3>" in response.text
    assert ">1</h3>" in response.text
