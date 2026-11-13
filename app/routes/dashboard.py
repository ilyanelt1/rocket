from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..db import get_db
from ..models import AuditLog, Operation, Product, StockBalance, User, Warehouse
from ..templating import templates

router = APIRouter()


@router.get("/")
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
