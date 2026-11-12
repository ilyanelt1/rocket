import csv
import io
import os
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, aliased
from starlette.middleware.sessions import SessionMiddleware

from .audit import record_audit_event
from .auth import get_current_user, require_roles
from .db import Base, engine, get_db
from .models import AuditLog, Operation, Product, StockBalance, User, Warehouse
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


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    Base.metadata.create_all(bind=engine)
    username = os.getenv("ADMIN_USERNAME")
    password = os.getenv("ADMIN_PASSWORD")
    if username and password:
        from .auth import hash_password
        from .db import SessionLocal

        with SessionLocal.begin() as db:
            admin = db.scalar(select(User).where(User.username == username))
            if admin is None:
                db.add(
                    User(
                        username=username,
                        password_hash=hash_password(password),
                        role="admin",
                    )
                )
    yield


app = FastAPI(title="CheckStock", lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("SESSION_SECRET") or secrets.token_urlsafe(32),
    https_only=os.getenv("COOKIE_SECURE", "false").lower() == "true",
    same_site="lax",
)
templates = Jinja2Templates(directory="templates")


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
        "index.html",
        context={
            "request": request,
            "error": str(exc),
            "products": [],
            "warehouses": [],
            "balances": [],
            "archived": [],
            "operations": [],
            "transit": [],
        },
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
    from .auth import verify_password

    user = db.scalar(select(User).where(User.username == username.strip()))
    if (
        user is None
        or not user.is_active
        or not verify_password(password, user.password_hash)
    ):
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
    operations = db.scalars(
        select(Operation).order_by(Operation.created_at.desc()).limit(100)
    ).all()
    transit = [
        operation
        for operation in operations
        if operation.status in ("IN_TRANSIT", "SHIPPED")
    ]
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
            "operations": operations,
            "transit": transit,
            "names": names,
            "product_names": product_names,
            "error": None,
        },
    )


@app.get("/history")
def operation_history(
    request: Request,
    q: str = "",
    product_id: int | None = None,
    operation_type: str = "",
    status: str = "",
    warehouse_id: int | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    page: int = Query(1, ge=1),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    allowed_types = {
        "RECEIPT",
        "TRANSFER",
        "TRANSFER_RECEIVE",
        "FBS_RESERVE",
        "FBS_CANCEL",
        "FBS_SHIP",
        "FBO_SEND",
        "FBO_ACCEPT",
    }
    allowed_statuses = {
        "DONE",
        "IN_TRANSIT",
        "ACCEPTED",
        "CANCELLED",
        "SHIPPED",
    }
    if operation_type and operation_type not in allowed_types:
        raise HTTPException(422, "Неизвестный тип операции")
    if status and status not in allowed_statuses:
        raise HTTPException(422, "Неизвестный статус")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "Начальная дата не может быть позже конечной")

    filters = []
    if q.strip():
        term = f"%{q.strip()}%"
        filters.append(
            or_(
                Product.sku.ilike(term),
                Product.name.ilike(term),
                Operation.note.ilike(term),
            )
        )
    if product_id is not None:
        filters.append(Operation.product_id == product_id)
    if operation_type:
        filters.append(Operation.type == operation_type)
    if status:
        filters.append(Operation.status == status)
    if warehouse_id is not None:
        filters.append(
            or_(
                Operation.source_warehouse_id == warehouse_id,
                Operation.destination_warehouse_id == warehouse_id,
            )
        )
    if date_from:
        filters.append(
            Operation.created_at
            >= datetime.combine(date_from, time.min, tzinfo=timezone.utc)
        )
    if date_to:
        filters.append(
            Operation.created_at
            < datetime.combine(
                date_to + timedelta(days=1), time.min, tzinfo=timezone.utc
            )
        )

    base_query = select(Operation).join(Product).where(*filters)
    total = (
        db.scalar(
            select(func.count()).select_from(Operation).join(Product).where(*filters)
        )
        or 0
    )
    page_size = 50
    pages = max(1, (total + page_size - 1) // page_size)
    if page > pages and total:
        page = pages
    operations = db.scalars(
        base_query.order_by(Operation.created_at.desc(), Operation.id.desc())
        .limit(page_size)
        .offset((page - 1) * page_size)
    ).all()

    products = db.scalars(select(Product).order_by(Product.sku)).all()
    warehouses = db.scalars(select(Warehouse).order_by(Warehouse.name)).all()
    warehouse_names = {
        item.id: item.name + (" (архив)" if item.deleted_at else "")
        for item in warehouses
    }
    operation_ids = [item.id for item in operations]
    actor_names = {}
    if operation_ids:
        actor_rows = db.execute(
            select(AuditLog.entity_id, User.username)
            .join(User, User.id == AuditLog.actor_user_id)
            .where(
                AuditLog.entity_type == "operation",
                AuditLog.action.like("stock.%"),
                AuditLog.entity_id.in_(operation_ids),
            )
        ).all()
        actor_names = {entity_id: username for entity_id, username in actor_rows}

    return templates.TemplateResponse(
        request,
        "history.html",
        {
            "request": request,
            "user": user,
            "operations": operations,
            "products": products,
            "warehouses": warehouses,
            "warehouse_names": warehouse_names,
            "actor_names": actor_names,
            "product_names": {
                item.id: f"{item.sku} — {item.name}" for item in products
            },
            "operation_types": sorted(allowed_types),
            "statuses": sorted(allowed_statuses),
            "filters": {
                "q": q,
                "product_id": product_id,
                "operation_type": operation_type,
                "status": status,
                "warehouse_id": warehouse_id,
                "date_from": date_from.isoformat() if date_from else "",
                "date_to": date_to.isoformat() if date_to else "",
            },
            "page": page,
            "pages": pages,
            "total": total,
        },
    )


@app.get("/history/export")
def export_operation_history(
    q: str = "",
    product_id: int | None = None,
    operation_type: str = "",
    status: str = "",
    warehouse_id: int | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    allowed_types = {
        "RECEIPT",
        "TRANSFER",
        "TRANSFER_RECEIVE",
        "FBS_RESERVE",
        "FBS_CANCEL",
        "FBS_SHIP",
        "FBO_SEND",
        "FBO_ACCEPT",
    }
    allowed_statuses = {"DONE", "IN_TRANSIT", "ACCEPTED", "CANCELLED", "SHIPPED"}
    if operation_type and operation_type not in allowed_types:
        raise HTTPException(422, "Неизвестный тип операции")
    if status and status not in allowed_statuses:
        raise HTTPException(422, "Неизвестный статус")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "Начальная дата не может быть позже конечной")

    source = aliased(Warehouse)
    destination = aliased(Warehouse)
    actor_name = (
        select(User.username)
        .join(AuditLog, AuditLog.actor_user_id == User.id)
        .where(
            AuditLog.entity_type == "operation",
            AuditLog.entity_id == Operation.id,
            AuditLog.action.like("stock.%"),
        )
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .limit(1)
        .scalar_subquery()
    )
    statement = (
        select(
            Operation.id,
            Operation.created_at,
            Operation.type,
            Product.sku,
            Product.name,
            Operation.qty,
            source.name,
            destination.name,
            Operation.status,
            actor_name,
            Operation.note,
        )
        .join(Product, Product.id == Operation.product_id)
        .outerjoin(source, source.id == Operation.source_warehouse_id)
        .outerjoin(destination, destination.id == Operation.destination_warehouse_id)
    )
    filters = []
    if q.strip():
        term = f"%{q.strip()}%"
        filters.append(
            or_(
                Product.sku.ilike(term),
                Product.name.ilike(term),
                Operation.note.ilike(term),
            )
        )
    if product_id is not None:
        filters.append(Operation.product_id == product_id)
    if operation_type:
        filters.append(Operation.type == operation_type)
    if status:
        filters.append(Operation.status == status)
    if warehouse_id is not None:
        filters.append(
            or_(
                Operation.source_warehouse_id == warehouse_id,
                Operation.destination_warehouse_id == warehouse_id,
            )
        )
    if date_from:
        filters.append(
            Operation.created_at
            >= datetime.combine(date_from, time.min, tzinfo=timezone.utc)
        )
    if date_to:
        filters.append(
            Operation.created_at
            < datetime.combine(
                date_to + timedelta(days=1), time.min, tzinfo=timezone.utc
            )
        )
    if filters:
        statement = statement.where(*filters)

    rows = db.execute(
        statement.order_by(Operation.created_at.desc(), Operation.id.desc())
    ).all()

    output = io.StringIO()
    writer = csv.writer(output, delimiter=";", lineterminator="\r\n")
    writer.writerow(
        [
            "ID операции",
            "Дата и время (UTC)",
            "Тип операции",
            "SKU",
            "Товар",
            "Количество",
            "Склад-источник",
            "Склад назначения",
            "Статус",
            "Пользователь",
            "Примечание",
        ]
    )

    for row in rows:
        values = [
            row[0],
            (row[1].replace(tzinfo=timezone.utc) if row[1].tzinfo is None else row[1])
            .astimezone(timezone.utc)
            .isoformat(),
            row[2],
            row[3],
            row[4],
            row[5],
            row[6] or "",
            row[7] or "",
            row[8],
            row[9] or "",
            row[10] or "",
        ]
        safe_values = [
            (
                f"'{value}"
                if isinstance(value, str)
                and value.startswith(("=", "+", "-", "@", "\\t", "\\r"))
                else value
            )
            for value in values
        ]
        writer.writerow(safe_values)

    content = "\ufeff" + output.getvalue()
    filename = f"checkstock_operations_{datetime.now(timezone.utc):%Y%m%d}.csv"
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


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
    reserve_id: int | None = Form(None),
    operation_id: int | None = Form(None),
    idempotency_key: str | None = Form(None),
    user: User = Depends(require_roles("admin", "warehouse_manager", "operator")),
    db: Session = Depends(get_db),
):
    try:
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
    except (StockError, TypeError) as exc:
        raise HTTPException(409, str(exc)) from exc
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
