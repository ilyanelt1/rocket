import os
from datetime import datetime, timezone
from typing import Annotated
from pydantic import BeforeValidator

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from .audit import record_audit_event
from .auth import (
    get_current_user,
    require_roles,
    verify_password,
    verify_dummy_password,
)
from .db import get_db
from .models import AuditLog, Operation, Product, StockBalance, User, Warehouse
from .routes.history import router as history_router
from .templating import templates
from .services import (
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


def empty_string_to_none(value):
    if value == "":
        return None
    return value


OptionalFormInt = Annotated[int | None, BeforeValidator(empty_string_to_none)]


def _get_session_secret() -> str:
    secret = os.getenv("SESSION_SECRET")
    if not secret:
        raise RuntimeError("SESSION_SECRET is required; configure a stable secret.")
    if len(secret) < 32:
        raise RuntimeError("SESSION_SECRET must contain at least 32 characters.")
    return secret


app = FastAPI(title="CheckStock")
app.add_middleware(
    SessionMiddleware,
    secret_key=_get_session_secret(),
    https_only=os.getenv("COOKIE_SECURE", "false").lower() == "true",
    same_site="lax",
)
app.include_router(history_router)


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault(
        "Permissions-Policy", "camera=(), microphone=(), geolocation=()"
    )
    return response


@app.get("/health/live", include_in_schema=False)
def health_live():
    return {"status": "ok"}


@app.get("/health/ready", include_in_schema=False)
def health_ready(db: Session = Depends(get_db)):
    try:
        db.execute(text("SELECT 1"))
    except SQLAlchemyError:
        return Response(
            content='{"status":"error","database":"unavailable"}',
            status_code=503,
            media_type="application/json",
        )
    return {"status": "ok", "database": "ok"}


def _lock_active_admins(db: Session) -> list[User]:
    return db.scalars(
        select(User)
        .where(User.role == "admin", User.is_active.is_(True))
        .order_by(User.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).all()


@app.exception_handler(StockError)
async def stock_error_handler(request: Request, exc: StockError):
    return templates.TemplateResponse(
        request,
        "error.html",
        {"error": str(exc)},
        status_code=409,
    )


@app.get("/login")
def login_page(request: Request):
    if request.session.get("user_id"):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"error": None})


@app.post("/login")
def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    user = db.scalar(select(User).where(User.username == username.strip()))
    password_valid = (
        verify_password(password, user.password_hash)
        if user is not None and user.is_active
        else verify_dummy_password(password)
    )
    if user is None or not user.is_active or not password_valid:
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Неверный логин или пароль"},
            status_code=401,
        )
    request.session.clear()
    request.session["user_id"] = user.id
    record_audit_event(db, user.id, "auth.login", "user", user.id)
    db.commit()
    return RedirectResponse("/", status_code=303)


@app.post("/logout")
def logout(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    record_audit_event(db, user.id, "auth.logout", "user", user.id)
    db.commit()
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/")
def dashboard(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    products = db.scalars(select(Product).order_by(Product.sku)).all()
    warehouses = db.scalars(
        select(Warehouse).where(Warehouse.deleted_at.is_(None)).order_by(Warehouse.name)
    ).all()
    archived = db.scalars(
        select(Warehouse)
        .where(Warehouse.deleted_at.is_not(None))
        .order_by(Warehouse.name)
    ).all()
    balances = db.scalars(select(StockBalance)).all()
    stock_totals = db.execute(
        select(
            func.coalesce(func.sum(StockBalance.physical_qty), 0),
            func.coalesce(func.sum(StockBalance.reserved_qty), 0),
        )
        .join(Warehouse, Warehouse.id == StockBalance.warehouse_id)
        .where(Warehouse.deleted_at.is_(None))
    ).one()
    active_stock_by_product = (
        select(
            StockBalance.product_id.label("product_id"),
            func.sum(StockBalance.physical_qty - StockBalance.reserved_qty).label(
                "available_qty"
            ),
        )
        .join(Warehouse, Warehouse.id == StockBalance.warehouse_id)
        .where(Warehouse.deleted_at.is_(None))
        .group_by(StockBalance.product_id)
        .subquery()
    )
    available_qty = func.coalesce(active_stock_by_product.c.available_qty, 0)
    out_of_stock_products = db.execute(
        select(
            Product.id,
            Product.sku,
            Product.name,
            available_qty.label("available_qty"),
        )
        .outerjoin(
            active_stock_by_product,
            active_stock_by_product.c.product_id == Product.id,
        )
        .where(available_qty <= 0)
        .order_by(Product.sku)
    ).all()
    low_stock_products = db.execute(
        select(
            Product.id,
            Product.sku,
            Product.name,
            Product.minimum_stock,
            available_qty.label("available_qty"),
        )
        .outerjoin(
            active_stock_by_product,
            active_stock_by_product.c.product_id == Product.id,
        )
        .where(
            Product.minimum_stock > 0,
            available_qty <= Product.minimum_stock,
        )
        .order_by(Product.sku)
    ).all()
    operations = db.scalars(
        select(Operation).order_by(Operation.created_at.desc()).limit(100)
    ).all()
    transit = db.scalars(
        select(Operation)
        .where(Operation.status.in_(("IN_TRANSIT", "SHIPPED")))
        .order_by(Operation.created_at.desc(), Operation.id.desc())
    ).all()
    names = {
        warehouse.id: warehouse.name + (" (архив)" if warehouse.deleted_at else "")
        for warehouse in db.scalars(select(Warehouse)).all()
    }
    product_names = {
        product.id: f"{product.sku} — {product.name}" for product in products
    }
    users = db.scalars(select(User).order_by(User.username)).all()
    audit_logs = (
        db.scalars(
            select(AuditLog).order_by(AuditLog.created_at.desc()).limit(100)
        ).all()
        if user.role == "admin"
        else []
    )
    user_names = {account.id: account.username for account in users}

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "request": request,
            "user": user,
            "users": users,
            "audit_logs": audit_logs,
            "user_names": user_names,
            "products": products,
            "warehouses": warehouses,
            "archived": archived,
            "balances": balances,
            "stock_summary": {
                "product_count": len(products),
                "warehouse_count": len(warehouses),
                "physical_qty": stock_totals[0],
                "reserved_qty": stock_totals[1],
                "available_qty": stock_totals[0] - stock_totals[1],
                "out_of_stock_count": len(out_of_stock_products),
                "low_stock_count": len(low_stock_products),
            },
            "out_of_stock_products": out_of_stock_products,
            "low_stock_products": low_stock_products,
            "operations": operations,
            "transit": transit,
            "names": names,
            "product_names": product_names,
            "products_by_id": {product.id: product for product in products},
            "warehouses_by_id": {
                warehouse.id: warehouse for warehouse in warehouses + archived
            },
            "error": None,
        },
    )


@app.post("/products/{product_id}/minimum-stock")
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


@app.post("/warehouses")
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


@app.post("/warehouses/{warehouse_id}/rename")
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


@app.post("/warehouses/{warehouse_id}/delete")
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


@app.post("/users")
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


@app.post("/users/{user_id}/role")
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


@app.post("/users/{user_id}/deactivate")
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


@app.post("/users/{user_id}/activate")
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


@app.post("/operations")
def create_operation(
    kind: str = Form(...),
    product_id: int = Form(...),
    qty: int = Form(...),
    warehouse_id: int | None = Form(None),
    destination_id: int | None = Form(None),
    reserve_id: OptionalFormInt = Form(None),
    operation_id: OptionalFormInt = Form(None),
    idempotency_key: str | None = Form(None),
    user: User = Depends(require_roles("admin", "warehouse_manager", "operator")),
    db: Session = Depends(get_db),
):
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


@app.get("/api/reconcile/{product_id}")
def reconcile_product(
    product_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not db.get(Product, product_id):
        raise HTTPException(404, "Товар не найден")
    return reconcile(db, product_id)
