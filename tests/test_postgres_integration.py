import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import Operation, Product, StockBalance, Warehouse
from app.services import StockError, accept_movement, receipt, transfer


TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="Set TEST_DATABASE_URL to run PostgreSQL locking integration tests",
)


@pytest.fixture
def postgres_db():
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL is not configured")

    engine = create_engine(
        TEST_DATABASE_URL,
        pool_pre_ping=True,
        connect_args={"connect_timeout": 5},
    )

    Base.metadata.drop_all(engine)

    Base.metadata.create_all(engine)

    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    with session_factory.begin() as db:
        product = Product(sku="PG-LOCK-101", name="PostgreSQL lock test")
        north = Warehouse(name="PG Север")
        south = Warehouse(name="PG Юг")
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
                    physical_qty=0,
                    reserved_qty=0,
                ),
            ]
        )

        ids = {
            "product": product.id,
            "north": north.id,
            "south": south.id,
        }

    yield engine, session_factory, ids

    Base.metadata.drop_all(engine)
    engine.dispose()


def _create_movement(session_factory, ids):
    with session_factory() as db:
        movement = transfer(
            db,
            ids["product"],
            ids["north"],
            ids["south"],
            2,
        )
        return movement.id


def _assert_received_once(session_factory, ids):
    with session_factory() as db:
        destination_qty = db.scalar(
            select(StockBalance.physical_qty).where(
                StockBalance.product_id == ids["product"],
                StockBalance.warehouse_id == ids["south"],
            )
        )
        receive_count = db.scalar(
            select(func.count())
            .select_from(Operation)
            .where(Operation.type == "TRANSFER_RECEIVE")
        )

    assert destination_qty == 2
    assert receive_count == 1


def test_postgres_concurrent_confirmation_with_same_key_is_idempotent(postgres_db):
    _, session_factory, ids = postgres_db
    movement_id = _create_movement(session_factory, ids)
    barrier = Barrier(2)

    def confirm(_):
        with session_factory() as db:
            barrier.wait(timeout=10)
            operation = accept_movement(db, movement_id, "pg-same-confirm-key")
            return operation.id

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(confirm, range(2)))

    assert results[0] == results[1]
    _assert_received_once(session_factory, ids)


def test_postgres_concurrent_confirmation_with_different_keys_applies_once(
    postgres_db,
):
    _, session_factory, ids = postgres_db
    movement_id = _create_movement(session_factory, ids)
    barrier = Barrier(2)

    def confirm(key):
        with session_factory() as db:
            barrier.wait(timeout=10)
            try:
                accept_movement(db, movement_id, key)
            except StockError:
                return "rejected"
            return "accepted"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(confirm, ("pg-confirm-key-a", "pg-confirm-key-b")))

    assert sorted(results) == ["accepted", "rejected"]
    _assert_received_once(session_factory, ids)


def test_postgres_concurrent_transfers_cannot_oversell_source(postgres_db):
    _, session_factory, ids = postgres_db
    barrier = Barrier(2)

    def move(key):
        with session_factory() as db:
            barrier.wait(timeout=10)
            try:
                transfer(
                    db,
                    ids["product"],
                    ids["north"],
                    ids["south"],
                    7,
                    key,
                )
            except StockError:
                return "rejected"
            return "accepted"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(move, ("pg-oversell-a", "pg-oversell-b")))

    assert sorted(results) == ["accepted", "rejected"]
    with session_factory() as db:
        source_qty = db.scalar(
            select(StockBalance.physical_qty).where(
                StockBalance.product_id == ids["product"],
                StockBalance.warehouse_id == ids["north"],
            )
        )
        destination_qty = db.scalar(
            select(StockBalance.physical_qty).where(
                StockBalance.product_id == ids["product"],
                StockBalance.warehouse_id == ids["south"],
            )
        )
        movement_count = db.scalar(
            select(func.count())
            .select_from(Operation)
            .where(Operation.type == "TRANSFER")
        )

    assert source_qty == 3
    assert destination_qty == 0
    assert movement_count == 1

    with session_factory() as db:
        transit_qty = db.scalar(
            select(func.sum(Operation.qty)).where(
                Operation.type == "TRANSFER",
                Operation.status == "IN_TRANSIT",
            )
        )

    assert transit_qty == 7
    assert source_qty + transit_qty == 10


def test_postgres_concurrent_receipt_with_same_key_applies_once(postgres_db):
    _, session_factory, ids = postgres_db
    barrier = Barrier(2)

    def add_receipt(_):
        with session_factory() as db:
            barrier.wait(timeout=10)
            operation = receipt(
                db,
                ids["product"],
                ids["north"],
                5,
                "pg-same-receipt-key",
            )
            return operation.id

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(add_receipt, range(2)))

    assert results[0] == results[1]
    with session_factory() as db:
        source_qty = db.scalar(
            select(StockBalance.physical_qty).where(
                StockBalance.product_id == ids["product"],
                StockBalance.warehouse_id == ids["north"],
            )
        )
        receipt_count = db.scalar(
            select(func.count())
            .select_from(Operation)
            .where(
                Operation.type == "RECEIPT",
                Operation.idempotency_key == "pg-same-receipt-key",
            )
        )

    assert source_qty == 15
    assert receipt_count == 1
