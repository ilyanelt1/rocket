from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.models import Operation, StockBalance, Warehouse
from app.services import (
    StockError,
    accept_movement,
    cancel_fbs,
    fbo_send,
    receipt,
    reconcile,
    reserve_fbs,
    ship_fbs,
    transfer,
)


def bal(db, product_id, warehouse_id):
    return db.scalar(
        select(StockBalance).where(
            StockBalance.product_id == product_id,
            StockBalance.warehouse_id == warehouse_id,
        )
    )


def test_receipt_increases_physical(db, ids):
    receipt(db, ids["product"], ids["north"], 3)
    assert bal(db, ids["product"], ids["north"]).physical_qty == 13


def test_fbs_reserve_reduces_available_not_physical(db, ids):
    reserve_fbs(db, ids["product"], ids["north"], 4)
    balance = bal(db, ids["product"], ids["north"])
    assert (
        balance.physical_qty,
        balance.available_qty,
        balance.reserved_qty,
    ) == (10, 6, 4)


def test_fbs_cancel_restores_available(db, ids):
    operation = reserve_fbs(db, ids["product"], ids["north"], 4)
    cancel_fbs(db, operation.id)
    balance = bal(db, ids["product"], ids["north"])
    assert (balance.available_qty, balance.reserved_qty) == (10, 0)


def test_fbs_ship_reduces_physical_and_reserved_and_tracks_customer(db, ids):
    reserve = reserve_fbs(db, ids["product"], ids["north"], 4)
    shipment = ship_fbs(db, reserve.id)
    balance = bal(db, ids["product"], ids["north"])
    assert (balance.physical_qty, balance.reserved_qty) == (6, 0)
    assert shipment.status == "SHIPPED"
    assert shipment.type == "FBS_SHIP"


def test_transfer_and_acceptance(db, ids):
    operation = transfer(
        db,
        ids["product"],
        ids["north"],
        ids["south"],
        3,
    )
    assert bal(db, ids["product"], ids["north"]).physical_qty == 7
    assert operation.status == "IN_TRANSIT"

    accept_movement(db, operation.id)
    assert bal(db, ids["product"], ids["south"]).physical_qty == 9
    assert db.get(Operation, operation.id).status == "ACCEPTED"


def test_fbo_send_and_acceptance(db, ids):
    operation = fbo_send(db, ids["product"], ids["north"], 2)
    assert bal(db, ids["product"], ids["north"]).physical_qty == 8

    accept_movement(db, operation.id)
    assert db.get(Operation, operation.id).status == "ACCEPTED"
    accepted = db.scalar(select(Operation).where(Operation.type == "FBO_ACCEPT"))
    assert accepted.qty == 2


def test_shortage_on_selected_warehouse_does_not_mutate(db, ids):
    with pytest.raises(StockError):
        transfer(db, ids["product"], ids["south"], ids["north"], 7)

    assert bal(db, ids["product"], ids["south"]).physical_qty == 6
    assert bal(db, ids["product"], ids["north"]).physical_qty == 10


def test_warehouse_soft_delete_keeps_balance_and_history(db, ids):
    receipt(db, ids["product"], ids["north"], 2)
    warehouse = db.get(Warehouse, ids["north"])
    warehouse.deleted_at = datetime.now(timezone.utc)
    db.commit()

    assert bal(db, ids["product"], ids["north"]).physical_qty == 12
    with pytest.raises(StockError):
        receipt(db, ids["product"], ids["north"], 1)

    assert db.scalar(select(Operation).where(Operation.type == "RECEIPT")) is not None


def test_idempotency_does_not_double_confirm_or_apply(db, ids):
    first = receipt(db, ids["product"], ids["north"], 5, "receipt-key")
    second = receipt(db, ids["product"], ids["north"], 5, "receipt-key")
    assert first.id == second.id
    assert bal(db, ids["product"], ids["north"]).physical_qty == 15

    movement = transfer(
        db,
        ids["product"],
        ids["north"],
        ids["south"],
        2,
    )
    accepted = accept_movement(db, movement.id, "accept-key")
    repeated = accept_movement(db, movement.id, "accept-key")
    assert accepted.id == repeated.id
    assert bal(db, ids["product"], ids["south"]).physical_qty == 8


def test_reconciliation_counts_stock_and_goods_in_transit(db, ids):
    transfer(db, ids["product"], ids["north"], ids["south"], 3)
    result = reconcile(db, ids["product"])
    assert result["physical"] == 13
    assert result["in_transit"] == 3
    assert result["total"] == 16

    movement = db.scalar(select(Operation).where(Operation.type == "TRANSFER"))
    accept_movement(db, movement.id)
    accepted = reconcile(db, ids["product"])
    assert accepted["physical"] == 16
    assert accepted["in_transit"] == 0
    assert accepted["total"] == 16


def test_reconciliation_keeps_fbs_reservation_and_cancellation_neutral(db, ids):
    reserve = reserve_fbs(db, ids["product"], ids["north"], 4)
    reserved = reconcile(db, ids["product"])
    assert reserved["physical"] == 16
    assert reserved["in_transit"] == 0
    assert reserved["total"] == 16

    cancel_fbs(db, reserve.id)
    cancelled = reconcile(db, ids["product"])
    assert cancelled["physical"] == 16
    assert cancelled["in_transit"] == 0
    assert cancelled["total"] == 16


def test_reconciliation_counts_fbs_shipment_until_customer_delivery(db, ids):
    reserve = reserve_fbs(db, ids["product"], ids["north"], 4)
    ship_fbs(db, reserve.id)

    result = reconcile(db, ids["product"])
    assert result["physical"] == 12
    assert result["in_transit"] == 4
    assert result["fbo_accepted"] == 0
    assert result["total"] == 16


def test_reconciliation_counts_fbo_in_transit_then_accepted(db, ids):
    operation = fbo_send(db, ids["product"], ids["north"], 2)

    in_transit = reconcile(db, ids["product"])
    assert in_transit["physical"] == 14
    assert in_transit["in_transit"] == 2
    assert in_transit["fbo_accepted"] == 0
    assert in_transit["total"] == 16

    accept_movement(db, operation.id)
    accepted = reconcile(db, ids["product"])
    assert accepted["physical"] == 14
    assert accepted["in_transit"] == 0
    assert accepted["fbo_accepted"] == 2
    assert accepted["total"] == 16
