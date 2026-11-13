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


ALLOWED_OPERATION_TYPES = {
    "RECEIPT",
    "TRANSFER",
    "TRANSFER_RECEIVE",
    "FBS_RESERVE",
    "FBS_CANCEL",
    "FBS_SHIP",
    "FBO_SEND",
    "FBO_ACCEPT",
}
ALLOWED_STATUSES = {"DONE", "IN_TRANSIT", "ACCEPTED", "CANCELLED", "SHIPPED"}


def _build_filters(
    q: str,
    product_id: int | None,
    operation_type: str,
    status: str,
    warehouse_id: int | None,
    date_from: date | None,
    date_to: date | None,
):
    if operation_type and operation_type not in ALLOWED_OPERATION_TYPES:
        raise HTTPException(422, "Неизвестный тип операции")
    if status and status not in ALLOWED_STATUSES:
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
    return filters


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


def _csv_value(column_key: str, value):
    if column_key == "created_at":
        return _to_utc_iso(value)
    if column_key in {
        "source_warehouse",
        "destination_warehouse",
        "actor_name",
        "note",
    }:
        return value or ""
    return value


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
    columns = (
        ("ID операции", "id", Operation.id.label("id")),
        ("Дата и время (UTC)", "created_at", Operation.created_at.label("created_at")),
        ("Тип операции", "type", Operation.type.label("type")),
        ("SKU", "sku", Product.sku.label("sku")),
        ("Товар", "product_name", Product.name.label("product_name")),
        ("Количество", "qty", Operation.qty.label("qty")),
        (
            "Склад-источник",
            "source_warehouse",
            source.name.label("source_warehouse"),
        ),
        (
            "Склад назначения",
            "destination_warehouse",
            destination.name.label("destination_warehouse"),
        ),
        ("Статус", "status", Operation.status.label("status")),
        ("Пользователь", "actor_name", actor_name),
        ("Примечание", "note", Operation.note.label("note")),
    )
    statement = (
        select(*(column for _, _, column in columns))
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
    writer.writerow(header for header, _, _ in columns)

    for row in rows:
        writer.writerow(
            _csv_safe(_csv_value(key, getattr(row, key)))
            for _, key, _ in columns
        )

    content = "\\ufeff" + output.getvalue()
    filename = f"checkstock_operations_{datetime.now(timezone.utc):%Y%m%d}.csv"
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
