from datetime import datetime, timezone
import pytest
from sqlalchemy import select
from app.models import Operation, StockBalance, Warehouse
from app.services import (StockError, accept_movement, cancel_fbs, fbo_send,
                          receipt, reconcile, reserve_fbs, ship_fbs, transfer)

def bal(db,p,w): return db.scalar(select(StockBalance).where(StockBalance.product_id==p,StockBalance.warehouse_id==w))

def test_receipt_increases_physical(db,ids):
    receipt(db,ids["product"],ids["north"],3)
    assert bal(db,ids["product"],ids["north"]).physical_qty==13

def test_fbs_reserve_reduces_available_not_physical(db,ids):
    reserve_fbs(db,ids["product"],ids["north"],4)
    b=bal(db,ids["product"],ids["north"])
    assert (b.physical_qty,b.available_qty,b.reserved_qty)==(10,6,4)

def test_fbs_cancel_restores_available(db,ids):
    op=reserve_fbs(db,ids["product"],ids["north"],4)
    cancel_fbs(db,op.id)
    b=bal(db,ids["product"],ids["north"])
    assert (b.available_qty,b.reserved_qty)==(10,0)

def test_fbs_ship_reduces_physical_and_reserved_and_tracks_customer(db,ids):
    reserve=reserve_fbs(db,ids["product"],ids["north"],4)
    ship=ship_fbs(db,reserve.id)
    b=bal(db,ids["product"],ids["north"])
    assert (b.physical_qty,b.reserved_qty)==(6,0)
    assert ship.status=="SHIPPED" and ship.type=="FBS_SHIP"

def test_transfer_and_acceptance(db,ids):
    op=transfer(db,ids["product"],ids["north"],ids["south"],3)
    assert bal(db,ids["product"],ids["north"]).physical_qty==7
    assert op.status=="IN_TRANSIT"
    accept_movement(db,op.id)
    assert bal(db,ids["product"],ids["south"]).physical_qty==9
    assert db.get(Operation,op.id).status=="ACCEPTED"

def test_fbo_send_and_acceptance(db,ids):
    op=fbo_send(db,ids["product"],ids["north"],2)
    assert bal(db,ids["product"],ids["north"]).physical_qty==8
    accept_movement(db,op.id)
    assert db.get(Operation,op.id).status=="ACCEPTED"
    assert db.scalar(select(Operation).where(Operation.type=="FBO_ACCEPT")).qty==2

def test_shortage_on_selected_warehouse_does_not_mutate(db,ids):
    with pytest.raises(StockError):
        transfer(db,ids["product"],ids["south"],ids["north"],7)
    assert bal(db,ids["product"],ids["south"]).physical_qty==6
    assert bal(db,ids["product"],ids["north"]).physical_qty==10

def test_warehouse_soft_delete_keeps_balance_and_history(db,ids):
    receipt(db,ids["product"],ids["north"],2)
    wh=db.get(Warehouse,ids["north"]); wh.deleted_at=datetime.now(timezone.utc); db.commit()
    assert bal(db,ids["product"],ids["north"]).physical_qty==12
    with pytest.raises(StockError): receipt(db,ids["product"],ids["north"],1)
    assert db.scalar(select(Operation).where(Operation.type=="RECEIPT")) is not None

def test_idempotency_does_not_double_confirm_or_apply(db,ids):
    first=receipt(db,ids["product"],ids["north"],5,"receipt-key")
    second=receipt(db,ids["product"],ids["north"],5,"receipt-key")
    assert first.id==second.id
    assert bal(db,ids["product"],ids["north"]).physical_qty==15

    movement=transfer(db,ids["product"],ids["north"],ids["south"],2)
    accepted=accept_movement(db,movement.id,"accept-key")
    repeated=accept_movement(db,movement.id,"accept-key")
    assert accepted.id==repeated.id
    assert bal(db,ids["product"],ids["south"]).physical_qty==8

def test_reconciliation_counts_stock_and_goods_in_transit(db,ids):
    transfer(db,ids["product"],ids["north"],ids["south"],3)
    result=reconcile(db,ids["product"])
    assert result["physical"]==13
    assert result["in_transit"]==3
    assert result["total"]==16
