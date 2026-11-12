import os
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from .auth import get_current_user, require_roles
from .db import Base, engine, get_db
from .models import Operation, Product, StockBalance, User, Warehouse
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

app = FastAPI(title="CheckStock")
app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("SESSION_SECRET", "local-development-secret-change-me"),
    https_only=os.getenv("COOKIE_SECURE", "false").lower() == "true",
    same_site="lax",
)
templates = Jinja2Templates(directory="templates")


@app.on_event("startup")
def startup():
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


@app.exception_handler(StockError)
async def stock_error_handler(request: Request, exc: StockError):
    return templates.TemplateResponse(
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
    return templates.TemplateResponse(
        "login.html", {"request": request, "error": None}
    )


@app.post("/login")
def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    from .auth import verify_password

    user = db.scalar(select(User).where(User.username == username.strip()))
    if user is None or not user.is_active or not verify_password(
        password, user.password_hash
    ):
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "Неверный логин или пароль"},
            status_code=401,
        )
    request.session.clear()
    request.session["user_id"] = user.id
    return RedirectResponse("/", status_code=303)


@app.post("/logout")
def logout(request: Request):
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

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "user": user,
            "users": users,
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


@app.post("/warehouses")
def add_warehouse(
    name: str = Form(...),
    user: User = Depends(require_roles("admin", "warehouse_manager")),
    db: Session = Depends(get_db),
):
    cleaned_name = name.strip()
    if not cleaned_name:
        raise HTTPException(422, "Название склада не может быть пустым")
    db.add(Warehouse(name=cleaned_name))
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
        db.add(
            User(
                username=cleaned_username,
                password_hash=hash_password(password),
                role=role,
            )
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, "Пользователь с таким логином уже существует") from exc
    except ValueError as exc:
        db.rollback()
        raise HTTPException(422, str(exc)) from exc
    return RedirectResponse("/", status_code=303)

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
            receipt(db, product_id, warehouse_id, qty, idempotency_key)
        elif kind == "FBS_RESERVE":
            reserve_fbs(db, product_id, warehouse_id, qty, idempotency_key)
        elif kind == "FBS_CANCEL":
            cancel_fbs(db, reserve_id, idempotency_key)
        elif kind == "FBS_SHIP":
            ship_fbs(db, reserve_id, idempotency_key)
        elif kind == "TRANSFER":
            transfer(db, product_id, warehouse_id, destination_id, qty, idempotency_key)
        elif kind == "FBO_SEND":
            fbo_send(db, product_id, warehouse_id, qty, idempotency_key)
        elif kind == "ACCEPT":
            accept_movement(db, operation_id, idempotency_key)
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
