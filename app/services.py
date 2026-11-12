from datetime import datetime, timezone
from sqlalchemy import select
from sqlalchemy.orm import Session
from .models import Operation, Product, StockBalance, Warehouse

class StockError(Exception):
    pass

def _positive(qty: int):
    if qty <= 0:
        raise StockError("Количество должно быть больше нуля.")

def _warehouse(db: Session, warehouse_id: int) -> Warehouse:
    wh = db.get(Warehouse, warehouse_id)
    if not wh or wh.deleted_at:
        raise StockError("Склад не найден или находится в архиве.")
    return wh

def _balance(db: Session, product_id: int, warehouse_id: int, lock: bool = True) -> StockBalance:
    stmt = select(StockBalance).where(
        StockBalance.product_id == product_id,
        StockBalance.warehouse_id == warehouse_id,
    )
    if lock:
        stmt = stmt.with_for_update()
    balance = db.scalar(stmt)
    if balance is None:
        balance = StockBalance(product_id=product_id, warehouse_id=warehouse_id, physical_qty=0, reserved_qty=0)
        db.add(balance)
        db.flush()
    return balance

def _existing(db: Session, key: str | None):
    return db.scalar(select(Operation).where(Operation.idempotency_key == key)) if key else None

def _record(db: Session, kind: str, status: str, product_id: int, qty: int,
            source: int | None = None, destination: int | None = None,
            key: str | None = None, note: str | None = None,
            related: int | None = None) -> Operation:
    op = Operation(type=kind, status=status, product_id=product_id, qty=qty,
                   source_warehouse_id=source, destination_warehouse_id=destination,
                   idempotency_key=key, note=note, related_operation_id=related,
                   confirmed_at=datetime.now(timezone.utc) if status == "DONE" else None)
    db.add(op)
    db.flush()
    return op

def _run(db: Session, action, key: str | None = None):
    try:
        result = action()
        db.commit()
        return result
    except Exception:
        db.rollback()
        # A concurrent request with the same key may have committed while this
        # transaction waited for its row lock or hit the unique constraint.
        if key:
            existing = _existing(db, key)
            if existing:
                return existing
        raise

def receipt(db: Session, product_id: int, warehouse_id: int, qty: int, key: str | None = None):
    def action():
        _positive(qty)
        old = _existing(db, key)
        if old: return old
        _warehouse(db, warehouse_id)
        b = _balance(db, product_id, warehouse_id)
        b.physical_qty += qty
        return _record(db, "RECEIPT", "DONE", product_id, qty, destination=warehouse_id, key=key)
    return _run(db, action, key)

def reserve_fbs(db: Session, product_id: int, warehouse_id: int, qty: int, key: str | None = None):
    def action():
        _positive(qty)
        old = _existing(db, key)
        if old: return old
        _warehouse(db, warehouse_id)
        b = _balance(db, product_id, warehouse_id)
        if b.available_qty < qty: raise StockError(f"Недостаточно доступного остатка на выбранном ФФ: доступно {b.available_qty}, требуется {qty}.")
        b.reserved_qty += qty
        return _record(db, "FBS_RESERVE", "DONE", product_id, qty, source=warehouse_id, key=key)
    return _run(db, action)

def cancel_fbs(db: Session, reserve_id: int, key: str | None = None):
    def action():
        old = _existing(db, key)
        if old: return old
        reserve = db.scalar(select(Operation).where(Operation.id == reserve_id).with_for_update())
        if not reserve or reserve.type != "FBS_RESERVE" or reserve.status != "DONE":
            raise StockError("Активный резерв не найден.")
        b = _balance(db, reserve.product_id, reserve.source_warehouse_id)
        if b.reserved_qty < reserve.qty: raise StockError("Резервный остаток повреждён.")
        b.reserved_qty -= reserve.qty
        reserve.status = "CANCELLED"
        return _record(db, "FBS_CANCEL", "DONE", reserve.product_id, reserve.qty,
                       source=reserve.source_warehouse_id, key=key, related=reserve.id)
    return _run(db, action)

def ship_fbs(db: Session, reserve_id: int, key: str | None = None):
    def action():
        old = _existing(db, key)
        if old: return old
        reserve = db.scalar(select(Operation).where(Operation.id == reserve_id).with_for_update())
        if not reserve or reserve.type != "FBS_RESERVE" or reserve.status != "DONE":
            raise StockError("Активный резерв не найден.")
        b = _balance(db, reserve.product_id, reserve.source_warehouse_id)
        if b.physical_qty < reserve.qty or b.reserved_qty < reserve.qty:
            raise StockError("Недостаточно физического или резервного остатка.")
        b.physical_qty -= reserve.qty
        b.reserved_qty -= reserve.qty
        reserve.status = "SHIPPED"
        return _record(db, "FBS_SHIP", "SHIPPED", reserve.product_id, reserve.qty,
                       source=reserve.source_warehouse_id, key=key, related=reserve.id)
    return _run(db, action)

def transfer(db: Session, product_id: int, source_id: int, destination_id: int, qty: int, key: str | None = None):
    def action():
        _positive(qty)
        old = _existing(db, key)
        if old: return old
        if source_id == destination_id: raise StockError("Склады отправления и получения должны различаться.")
        _warehouse(db, source_id); _warehouse(db, destination_id)
        b = _balance(db, product_id, source_id)
        if b.available_qty < qty: raise StockError(f"Недостаточно доступного остатка на выбранном ФФ: доступно {b.available_qty}, требуется {qty}.")
        b.physical_qty -= qty
        return _record(db, "TRANSFER", "IN_TRANSIT", product_id, qty, source=source_id, destination=destination_id, key=key)
    return _run(db, action)

def fbo_send(db: Session, product_id: int, source_id: int, qty: int, key: str | None = None):
    def action():
        _positive(qty)
        old = _existing(db, key)
        if old: return old
        _warehouse(db, source_id)
        b = _balance(db, product_id, source_id)
        if b.available_qty < qty: raise StockError(f"Недостаточно доступного остатка на выбранном ФФ: доступно {b.available_qty}, требуется {qty}.")
        b.physical_qty -= qty
        return _record(db, "FBO_SEND", "IN_TRANSIT", product_id, qty, source=source_id, key=key)
    return _run(db, action)

def accept_movement(db: Session, operation_id: int, key: str | None = None):
    def action():
        old = _existing(db, key)
        if old: return old
        op = db.scalar(select(Operation).where(Operation.id == operation_id).with_for_update())
        if not op or op.type not in ("TRANSFER", "FBO_SEND") or op.status != "IN_TRANSIT":
            raise StockError("Операция не найдена или уже обработана.")
        if op.type == "TRANSFER":
            _warehouse(db, op.destination_warehouse_id)
            b = _balance(db, op.product_id, op.destination_warehouse_id)
            b.physical_qty += op.qty
        op.status = "ACCEPTED"
        op.confirmed_at = datetime.now(timezone.utc)
        return _record(db, "TRANSFER_RECEIVE" if op.type == "TRANSFER" else "FBO_ACCEPT",
                       "DONE", op.product_id, op.qty, destination=op.destination_warehouse_id,
                       key=key, related=op.id)
    return _run(db, action)

def reconcile(db: Session, product_id: int):
    physical = sum(db.scalars(select(StockBalance.physical_qty).where(StockBalance.product_id == product_id)).all())
    transit = sum(db.scalars(select(Operation.qty).where(
        Operation.product_id == product_id,
        Operation.status.in_(("IN_TRANSIT", "SHIPPED"))
    )).all())
    fbo_accepted = sum(db.scalars(select(Operation.qty).where(
        Operation.product_id == product_id, Operation.type == "FBO_SEND", Operation.status == "ACCEPTED"
    )).all())
    return {"product_id": product_id, "physical": physical, "in_transit": transit,
            "fbo_accepted": fbo_accepted, "total": physical + transit + fbo_accepted}
