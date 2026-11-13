import csv
import io
from datetime import date, datetime, time, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, aliased

from ..auth import get_current_user
from ..db import get_db
from ..models import AuditLog, Operation, Product, User, Warehouse
from ..templating import templates


router = APIRouter()
@router.get("/history")
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
    filters = _build_filters(
        q, product_id, operation_type, status, warehouse_id, date_from, date_to
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
            .order_by(
                AuditLog.entity_id,
                AuditLog.created_at.desc(),
                AuditLog.id.desc(),
            )
        ).all()
        for entity_id, username in actor_rows:
            actor_names.setdefault(entity_id, username)

    template_name = (
        "history_results.html"
        if request.headers.get("HX-Request") == "true"
        else "history.html"
    )
    return templates.TemplateResponse(
        request,
        template_name,
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
            "operation_types": sorted(ALLOWED_OPERATION_TYPES),
            "statuses": sorted(ALLOWED_STATUSES),
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


def _csv_safe(value):
    """Prefix spreadsheet formula-like values, including those after whitespace."""
    if isinstance(value, str) and value.lstrip().startswith(
        ("=", "+", "-", "@", "\t", "\r")
    ):
        return "'" + value
    return value


def _to_utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


@router.get("/history/export")
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
    filters = _build_filters(
        q, product_id, operation_type, status, warehouse_id, date_from, date_to
    )

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
        .label("actor_name")
    )
    statement = (
        select(
            Operation.id.label("id"),
            Operation.created_at.label("created_at"),
            Operation.type.label("type"),
            Product.sku.label("sku"),
            Product.name.label("product_name"),
            Operation.qty.label("qty"),
            source.name.label("source_warehouse"),
            destination.name.label("destination_warehouse"),
            Operation.status.label("status"),
            actor_name,
            Operation.note.label("note"),
        )
        .join(Product, Product.id == Operation.product_id)
        .outerjoin(source, source.id == Operation.source_warehouse_id)
        .outerjoin(destination, destination.id == Operation.destination_warehouse_id)
        .where(*filters)
    )
    rows = db.execute(
        statement.order_by(Operation.created_at.desc(), Operation.id.desc())
    ).all()

    output = io.StringIO()
    writer = csv.writer(output, delimiter=";", lineterminator="\\r\\n")
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
        writer.writerow(
            _csv_safe(value)
            for value in (
                row.id,
                _to_utc_iso(row.created_at),
                row.type,
                row.sku,
                row.product_name,
                row.qty,
                row.source_warehouse or "",
                row.destination_warehouse or "",
                row.status,
                row.actor_name or "",
                row.note or "",
            )
        )

    content = "\\ufeff" + output.getvalue()
    filename = f"checkstock_operations_{datetime.now(timezone.utc):%Y%m%d}.csv"
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
