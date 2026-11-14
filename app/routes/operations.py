from fastapi import APIRouter, Depends, Form, HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from ..auth import get_current_user, require_roles
from ..db import get_db
from ..dependencies import parse_optional_form_int
from ..models import Product, User
from ..services import (
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

router = APIRouter()


@router.post("/operations")
def create_operation(
    kind: str = Form(...),
    product_id: int = Form(...),
    qty: int = Form(...),
    warehouse_id: str | None = Form(None),
    destination_id: str | None = Form(None),
    reserve_id: str | None = Form(None),
    operation_id: str | None = Form(None),
    idempotency_key: str | None = Form(None),
    user: User = Depends(require_roles("admin", "warehouse_manager", "operator")),
    db: Session = Depends(get_db),
):
    idempotency_key = (idempotency_key or "").strip() or None

    warehouse_id = parse_optional_form_int(warehouse_id, "склад")
    destination_id = parse_optional_form_int(destination_id, "склад получения")
    reserve_id = parse_optional_form_int(reserve_id, "ID резерва")
    operation_id = parse_optional_form_int(operation_id, "ID операции в пути")

    if kind in {"RECEIPT", "FBS_RESERVE", "FBO_SEND"} and warehouse_id is None:
        raise HTTPException(422, "Для этой операции необходимо выбрать склад.")
    if kind == "TRANSFER" and (warehouse_id is None or destination_id is None):
        raise HTTPException(422, "Для перемещения необходимо выбрать оба склада.")
    if kind in {"FBS_CANCEL", "FBS_SHIP"} and reserve_id is None:
        raise HTTPException(422, "Необходимо указать ID резерва.")
    if kind == "ACCEPT" and operation_id is None:
        raise HTTPException(422, "Необходимо указать ID операции в пути.")

    if kind == "RECEIPT":
        receipt(
            db,
            product_id,
            warehouse_id,
            qty,
            idempotency_key,
            actor_user_id=user.id,
        )
    elif kind == "FBS_RESERVE":
        reserve_fbs(
            db,
            product_id,
            warehouse_id,
            qty,
            idempotency_key,
            actor_user_id=user.id,
        )
    elif kind == "FBS_CANCEL":
        cancel_fbs(db, reserve_id, idempotency_key, actor_user_id=user.id)
    elif kind == "FBS_SHIP":
        ship_fbs(db, reserve_id, idempotency_key, actor_user_id=user.id)
    elif kind == "TRANSFER":
        transfer(
            db,
            product_id,
            warehouse_id,
            destination_id,
            qty,
            idempotency_key,
            actor_user_id=user.id,
        )
    elif kind == "FBO_SEND":
        fbo_send(
            db,
            product_id,
            warehouse_id,
            qty,
            idempotency_key,
            actor_user_id=user.id,
        )
    elif kind == "ACCEPT":
        accept_movement(db, operation_id, idempotency_key, actor_user_id=user.id)
    else:
        raise StockError("Неизвестный тип операции.")
    return RedirectResponse("/", status_code=303)


@router.get("/api/reconcile/{product_id}")
def reconcile_product(
    product_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not db.get(Product, product_id):
        raise HTTPException(404, "Товар не найден")
    return reconcile(db, product_id)
