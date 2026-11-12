from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import AuditLog, Operation, StockBalance, Warehouse


class StockError(Exception):
    pass


def _positive(qty: int):
    if qty <= 0:
        raise StockError("Количество должно быть больше нуля.")


def _warehouse(db: Session, warehouse_id: int) -> Warehouse:
    warehouse = db.get(Warehouse, warehouse_id)
    if not warehouse or warehouse.deleted_at:
        raise StockError("Склад не найден или находится в архиве.")
    return warehouse


def _balance(
    db: Session,
    product_id: int,
    warehouse_id: int,
    lock: bool = True,
) -> StockBalance:
    statement = select(StockBalance).where(
        StockBalance.product_id == product_id,
        StockBalance.warehouse_id == warehouse_id,
    )
    if lock:
        statement = statement.with_for_update()

    balance = db.scalar(statement)
    if balance is None:
        balance = StockBalance(
            product_id=product_id,
            warehouse_id=warehouse_id,
            physical_qty=0,
            reserved_qty=0,
        )
        db.add(balance)
        db.flush()

    return balance


def _existing(db: Session, key: str | None):
    if not key:
        return None
    return db.scalar(select(Operation).where(Operation.idempotency_key == key))


def _record(
    db: Session,
    kind: str,
    status: str,
    product_id: int,
    qty: int,
    source: int | None = None,
    destination: int | None = None,
    key: str | None = None,
    note: str | None = None,
    related: int | None = None,
) -> Operation:
    operation = Operation(
        type=kind,
        status=status,
        product_id=product_id,
        qty=qty,
        source_warehouse_id=source,
        destination_warehouse_id=destination,
        idempotency_key=key,
        note=note,
        related_operation_id=related,
        confirmed_at=(datetime.now(timezone.utc) if status == "DONE" else None),
    )
    db.add(operation)
    db.flush()
    return operation


def _run(
    db: Session,
    action,
    key: str | None = None,
    actor_user_id: int | None = None,
):
    try:
        result = action()
        if actor_user_id is not None:
            db.add(
                AuditLog(
                    actor_user_id=actor_user_id,
                    action=f"stock.{result.type.lower()}",
                    entity_type="operation",
                    entity_id=result.id,
                    details={
                        "operation_type": result.type,
                        "status": result.status,
                        "product_id": result.product_id,
                        "quantity": result.qty,
                        "source_warehouse_id": result.source_warehouse_id,
                        "destination_warehouse_id": result.destination_warehouse_id,
                    },
                )
            )
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


def receipt(
    db: Session,
    product_id: int,
    warehouse_id: int,
    qty: int,
    key: str | None = None,
    actor_user_id: int | None = None,
):
    def action():
        _positive(qty)
        old = _existing(db, key)
        if old:
            return old

        _warehouse(db, warehouse_id)
        balance = _balance(db, product_id, warehouse_id)
        balance.physical_qty += qty
        return _record(
            db,
            "RECEIPT",
            "DONE",
            product_id,
            qty,
            destination=warehouse_id,
            key=key,
        )

    return _run(db, action, key, actor_user_id)


def reserve_fbs(
    db: Session,
    product_id: int,
    warehouse_id: int,
    qty: int,
    key: str | None = None,
    actor_user_id: int | None = None,
):
    def action():
        _positive(qty)
        old = _existing(db, key)
        if old:
            return old

        _warehouse(db, warehouse_id)
        balance = _balance(db, product_id, warehouse_id)
        if balance.available_qty < qty:
            raise StockError(
                "Недостаточно доступного остатка на выбранном ФФ: "
                f"доступно {balance.available_qty}, требуется {qty}."
            )

        balance.reserved_qty += qty
        return _record(
            db,
            "FBS_RESERVE",
            "DONE",
            product_id,
            qty,
            source=warehouse_id,
            key=key,
        )

    return _run(db, action, key, actor_user_id)


def cancel_fbs(
    db: Session,
    reserve_id: int,
    key: str | None = None,
    actor_user_id: int | None = None,
):
    def action():
        old = _existing(db, key)
        if old:
            return old

        reserve = db.scalar(
            select(Operation).where(Operation.id == reserve_id).with_for_update()
        )
        if not reserve or reserve.type != "FBS_RESERVE" or reserve.status != "DONE":
            raise StockError("Активный резерв не найден.")

        balance = _balance(
            db,
            reserve.product_id,
            reserve.source_warehouse_id,
        )
        if balance.reserved_qty < reserve.qty:
            raise StockError("Резервный остаток повреждён.")

        balance.reserved_qty -= reserve.qty
        reserve.status = "CANCELLED"
        return _record(
            db,
            "FBS_CANCEL",
            "DONE",
            reserve.product_id,
            reserve.qty,
            source=reserve.source_warehouse_id,
            key=key,
            related=reserve.id,
        )

    return _run(db, action, key, actor_user_id)


def ship_fbs(
    db: Session,
    reserve_id: int,
    key: str | None = None,
    actor_user_id: int | None = None,
):
    def action():
        old = _existing(db, key)
        if old:
            return old

        reserve = db.scalar(
            select(Operation).where(Operation.id == reserve_id).with_for_update()
        )
        if not reserve or reserve.type != "FBS_RESERVE" or reserve.status != "DONE":
            raise StockError("Активный резерв не найден.")

        balance = _balance(
            db,
            reserve.product_id,
            reserve.source_warehouse_id,
        )
        if balance.physical_qty < reserve.qty or balance.reserved_qty < reserve.qty:
            raise StockError("Недостаточно физического или резервного остатка.")

        balance.physical_qty -= reserve.qty
        balance.reserved_qty -= reserve.qty
        reserve.status = "SHIPPED"
        return _record(
            db,
            "FBS_SHIP",
            "SHIPPED",
            reserve.product_id,
            reserve.qty,
            source=reserve.source_warehouse_id,
            key=key,
            related=reserve.id,
        )

    return _run(db, action, key, actor_user_id)


def transfer(
    db: Session,
    product_id: int,
    source_id: int,
    destination_id: int,
    qty: int,
    key: str | None = None,
    actor_user_id: int | None = None,
):
    def action():
        _positive(qty)
        old = _existing(db, key)
        if old:
            return old

        if source_id == destination_id:
            raise StockError("Склады отправления и получения должны различаться.")

        _warehouse(db, source_id)
        _warehouse(db, destination_id)
        balance = _balance(db, product_id, source_id)
        if balance.available_qty < qty:
            raise StockError(
                "Недостаточно доступного остатка на выбранном ФФ: "
                f"доступно {balance.available_qty}, требуется {qty}."
            )

        balance.physical_qty -= qty
        return _record(
            db,
            "TRANSFER",
            "IN_TRANSIT",
            product_id,
            qty,
            source=source_id,
            destination=destination_id,
            key=key,
        )

    return _run(db, action, key, actor_user_id)


def fbo_send(
    db: Session,
    product_id: int,
    source_id: int,
    qty: int,
    key: str | None = None,
    actor_user_id: int | None = None,
):
    def action():
        _positive(qty)
        old = _existing(db, key)
        if old:
            return old

        _warehouse(db, source_id)
        balance = _balance(db, product_id, source_id)
        if balance.available_qty < qty:
            raise StockError(
                "Недостаточно доступного остатка на выбранном ФФ: "
                f"доступно {balance.available_qty}, требуется {qty}."
            )

        balance.physical_qty -= qty
        return _record(
            db,
            "FBO_SEND",
            "IN_TRANSIT",
            product_id,
            qty,
            source=source_id,
            key=key,
        )

    return _run(db, action, key, actor_user_id)


def accept_movement(
    db: Session,
    operation_id: int,
    key: str | None = None,
    actor_user_id: int | None = None,
):
    def action():
        old = _existing(db, key)
        if old:
            return old

        operation = db.scalar(
            select(Operation).where(Operation.id == operation_id).with_for_update()
        )
        if (
            not operation
            or operation.type not in ("TRANSFER", "FBO_SEND")
            or operation.status != "IN_TRANSIT"
        ):
            raise StockError("Операция не найдена или уже обработана.")

        if operation.type == "TRANSFER":
            _warehouse(db, operation.destination_warehouse_id)
            balance = _balance(
                db,
                operation.product_id,
                operation.destination_warehouse_id,
            )
            balance.physical_qty += operation.qty

        operation.status = "ACCEPTED"
        operation.confirmed_at = datetime.now(timezone.utc)
        return _record(
            db,
            ("TRANSFER_RECEIVE" if operation.type == "TRANSFER" else "FBO_ACCEPT"),
            "DONE",
            operation.product_id,
            operation.qty,
            destination=operation.destination_warehouse_id,
            key=key,
            related=operation.id,
        )

    return _run(db, action, key, actor_user_id)


def reconcile(db: Session, product_id: int):
    physical = sum(
        db.scalars(
            select(StockBalance.physical_qty).where(
                StockBalance.product_id == product_id
            )
        ).all()
    )
    transit = sum(
        db.scalars(
            select(Operation.qty).where(
                Operation.product_id == product_id,
                Operation.status.in_(("IN_TRANSIT", "SHIPPED")),
            )
        ).all()
    )
    fbo_accepted = sum(
        db.scalars(
            select(Operation.qty).where(
                Operation.product_id == product_id,
                Operation.type == "FBO_SEND",
                Operation.status == "ACCEPTED",
            )
        ).all()
    )

    return {
        "product_id": product_id,
        "physical": physical,
        "in_transit": transit,
        "fbo_accepted": fbo_accepted,
        "total": physical + transit + fbo_accepted,
    }
