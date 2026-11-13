from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..audit import record_audit_event
from ..auth import require_roles
from ..db import get_db
from ..models import Product, User, Warehouse

router = APIRouter()


def _lock_active_admins(db: Session) -> list[User]:
    return db.scalars(
        select(User)
        .where(User.role == "admin", User.is_active.is_(True))
        .order_by(User.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).all()


@router.post("/products/{product_id}/minimum-stock")
def update_product_minimum_stock(
    product_id: int,
    minimum_stock: int = Form(..., ge=0),
    user: User = Depends(require_roles("admin", "warehouse_manager")),
    db: Session = Depends(get_db),
):
    product = db.get(Product, product_id)
    if product is None:
        raise HTTPException(404, "Товар не найден")
    previous_minimum_stock = product.minimum_stock
    product.minimum_stock = minimum_stock
    record_audit_event(
        db,
        user.id,
        "product.minimum_stock.update",
        "product",
        product.id,
        {
            "sku": product.sku,
            "previous_minimum_stock": previous_minimum_stock,
            "minimum_stock": minimum_stock,
        },
    )
    db.commit()
    return RedirectResponse("/", status_code=303)


@router.post("/warehouses")
def add_warehouse(
    name: str = Form(...),
    user: User = Depends(require_roles("admin", "warehouse_manager")),
    db: Session = Depends(get_db),
):
    cleaned_name = name.strip()
    if not cleaned_name:
        raise HTTPException(422, "Название склада не может быть пустым")
    warehouse = Warehouse(name=cleaned_name)
    db.add(warehouse)
    db.flush()
    record_audit_event(
        db,
        user.id,
        "warehouse.create",
        "warehouse",
        warehouse.id,
        {"name": warehouse.name},
    )
    db.commit()
    return RedirectResponse("/", status_code=303)


@router.post("/warehouses/{warehouse_id}/rename")
def rename_warehouse(
    warehouse_id: int,
    name: str = Form(...),
    user: User = Depends(require_roles("admin", "warehouse_manager")),
    db: Session = Depends(get_db),
):
    warehouse = db.get(Warehouse, warehouse_id)
    if not warehouse or warehouse.deleted_at:
        raise HTTPException(404, "Склад не найден")
    cleaned_name = name.strip()
    if not cleaned_name:
        raise HTTPException(422, "Название склада не может быть пустым")
    warehouse.name = cleaned_name
    record_audit_event(
        db,
        user.id,
        "warehouse.rename",
        "warehouse",
        warehouse.id,
        {"name": cleaned_name},
    )
    db.commit()
    return RedirectResponse("/", status_code=303)


@router.post("/warehouses/{warehouse_id}/delete")
def delete_warehouse(
    warehouse_id: int,
    user: User = Depends(require_roles("admin", "warehouse_manager")),
    db: Session = Depends(get_db),
):
    warehouse = db.get(Warehouse, warehouse_id)
    if not warehouse:
        raise HTTPException(404, "Склад не найден")
    warehouse.deleted_at = datetime.now(timezone.utc)
    record_audit_event(
        db,
        user.id,
        "warehouse.archive",
        "warehouse",
        warehouse.id,
        {"name": warehouse.name},
    )
    db.commit()
    return RedirectResponse("/", status_code=303)


@router.post("/users")
def create_user(
    username: str = Form(...),
    password: str = Form(...),
    role: str = Form(...),
    user: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
):
    from .auth import hash_password

    cleaned_username = username.strip()
    if len(cleaned_username) < 3 or len(cleaned_username) > 80:
        raise HTTPException(422, "Логин должен содержать от 3 до 80 символов")
    if role not in {"admin", "warehouse_manager", "operator"}:
        raise HTTPException(422, "Неизвестная роль")
    try:
        account = User(
            username=cleaned_username,
            password_hash=hash_password(password),
            role=role,
        )
        db.add(account)
        db.flush()
        record_audit_event(
            db,
            user.id,
            "user.create",
            "user",
            account.id,
            {"username": account.username, "role": account.role},
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, "Пользователь с таким логином уже существует") from exc
    except ValueError as exc:
        db.rollback()
        raise HTTPException(422, str(exc)) from exc
    return RedirectResponse("/", status_code=303)


@router.post("/users/{user_id}/role")
def update_user_role(
    user_id: int,
    role: str = Form(...),
    user: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
):
    allowed_roles = {"admin", "warehouse_manager", "operator"}
    if role not in allowed_roles:
        raise HTTPException(422, "Неизвестная роль")
    if user_id == user.id:
        raise HTTPException(409, "Нельзя изменить собственную роль")

    active_admins = _lock_active_admins(db)
    if not any(account.id == user.id for account in active_admins):
        raise HTTPException(403, "Недостаточно прав для выполнения действия")
    account = db.scalar(
        select(User)
        .where(User.id == user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if account is None:
        raise HTTPException(404, "Пользователь не найден")
    if account.role == role:
        return RedirectResponse("/#users", status_code=303)
    if (
        account.is_active
        and account.role == "admin"
        and role != "admin"
        and len(active_admins) <= 1
    ):
        raise HTTPException(
            409, "Нельзя понизить роль последнего активного администратора"
        )

    old_role = account.role
    account.role = role
    record_audit_event(
        db,
        user.id,
        "user.role_change",
        "user",
        account.id,
        {"username": account.username, "old_role": old_role, "new_role": role},
    )
    db.commit()
    return RedirectResponse("/#users", status_code=303)


@router.post("/users/{user_id}/deactivate")
def deactivate_user(
    user_id: int,
    user: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
):
    if user_id == user.id:
        raise HTTPException(409, "Нельзя заблокировать собственную учётную запись")
    active_admins = _lock_active_admins(db)
    if not any(account.id == user.id for account in active_admins):
        raise HTTPException(403, "Недостаточно прав для выполнения действия")
    account = db.scalar(
        select(User)
        .where(User.id == user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if account is None:
        raise HTTPException(404, "Пользователь не найден")
    if not account.is_active:
        return RedirectResponse("/#users", status_code=303)
    if account.role == "admin" and len(active_admins) <= 1:
        raise HTTPException(
            409, "Нельзя заблокировать последнего активного администратора"
        )

    account.is_active = False
    record_audit_event(
        db,
        user.id,
        "user.deactivate",
        "user",
        account.id,
        {"username": account.username, "role": account.role},
    )
    db.commit()
    return RedirectResponse("/#users", status_code=303)


@router.post("/users/{user_id}/activate")
def activate_user(
    user_id: int,
    user: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
):
    account = db.scalar(
        select(User)
        .where(User.id == user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if account is None:
        raise HTTPException(404, "Пользователь не найден")
    if account.is_active:
        return RedirectResponse("/#users", status_code=303)

    account.is_active = True
    record_audit_event(
        db,
        user.id,
        "user.activate",
        "user",
        account.id,
        {"username": account.username, "role": account.role},
    )
    db.commit()
    return RedirectResponse("/#users", status_code=303)
