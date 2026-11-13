from datetime import datetime, timezone

from sqlalchemy import select

from app.models import Operation, Product, StockBalance, Warehouse


def _login(client):
    response = client.post(
        "/login",
        data={"username": "admin", "password": "admin-password-123"},
        follow_redirects=False,
    )
    assert response.status_code == 303


def test_stock_shortage_renders_readable_error_without_mutating_balances(auth_client):
    client, db = auth_client
    product = Product(sku="SHORT-101", name="Товар с нехваткой")
    north = Warehouse(name="Север для нехватки")
    south = Warehouse(name="Юг для нехватки")
    db.add_all([product, north, south])
    db.flush()
    db.add_all(
        [
            StockBalance(
                product_id=product.id,
                warehouse_id=north.id,
                physical_qty=10,
                reserved_qty=0,
            ),
            StockBalance(
                product_id=product.id,
                warehouse_id=south.id,
                physical_qty=1,
                reserved_qty=0,
            ),
        ]
    )
    db.commit()
    _login(client)

    response = client.post(
        "/operations",
        data={
            "kind": "TRANSFER",
            "product_id": product.id,
            "warehouse_id": south.id,
            "destination_id": north.id,
            "qty": 2,
        },
        follow_redirects=False,
    )

    assert response.status_code == 409
    assert "Операция не выполнена" in response.text
    assert "Недостаточно доступного остатка на выбранном ФФ" in response.text
    assert "Вернуться к остаткам" in response.text
    assert (
        db.scalar(
            select(StockBalance).where(
                StockBalance.product_id == product.id,
                StockBalance.warehouse_id == south.id,
            )
        ).physical_qty
        == 1
    )
    assert (
        db.scalar(
            select(StockBalance).where(
                StockBalance.product_id == product.id,
                StockBalance.warehouse_id == north.id,
            )
        ).physical_qty
        == 10
    )
    assert (
        db.scalar(select(Operation).where(Operation.product_id == product.id)) is None
    )


def test_dashboard_lists_all_active_transit_even_when_not_in_latest_100(auth_client):
    client, db = auth_client
    product = Product(sku="TRANSIT-101", name="Старая отправка")
    north = Warehouse(name="Север тестовый")
    south = Warehouse(name="Юг тестовый")
    db.add_all([product, north, south])
    db.flush()

    old_transit = Operation(
        type="TRANSFER",
        status="IN_TRANSIT",
        product_id=product.id,
        source_warehouse_id=north.id,
        destination_warehouse_id=south.id,
        qty=3,
        created_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
    )
    recent_operations = [
        Operation(
            type="RECEIPT",
            status="DONE",
            product_id=product.id,
            destination_warehouse_id=north.id,
            qty=1,
            created_at=datetime(2026, 11, 1, tzinfo=timezone.utc),
        )
        for _ in range(101)
    ]
    db.add_all([old_transit, *recent_operations])
    db.commit()
    _login(client)

    response = client.get("/")

    assert response.status_code == 200
    assert "Товары в пути" in response.text
    assert "TRANSIT-101 — Старая отправка" in response.text
    assert "Север тестовый" in response.text
    assert "Юг тестовый" in response.text
    assert ">3</td>" in response.text


def test_receipt_accepts_blank_hidden_optional_operation_fields(auth_client):
    client, db = auth_client
    product = Product(sku="FORM-101", name="Приход с пустыми необязательными полями")
    warehouse = Warehouse(name="Склад для проверки формы")
    db.add_all([product, warehouse])
    db.flush()
    db.add(
        StockBalance(
            product_id=product.id,
            warehouse_id=warehouse.id,
            physical_qty=5,
            reserved_qty=0,
        )
    )
    db.commit()
    _login(client)

    response = client.post(
        "/operations",
        data={
            "kind": "RECEIPT",
            "product_id": product.id,
            "warehouse_id": str(warehouse.id),
            "destination_id": "",
            "reserve_id": "",
            "operation_id": "",
            "qty": "3",
            "idempotency_key": "",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    balance = db.scalar(
        select(StockBalance).where(
            StockBalance.product_id == product.id,
            StockBalance.warehouse_id == warehouse.id,
        )
    )
    assert balance.physical_qty == 8
    assert (
        db.scalar(
            select(Operation).where(
                Operation.product_id == product.id,
                Operation.type == "RECEIPT",
            )
        )
        is not None
    )
