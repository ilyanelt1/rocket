import csv
import io
from datetime import date, datetime, time, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, aliased

from ..auth import get_current_user
from ..db import get_db
from ..models import AuditLog, Operation, Product, User, Warehouse


router = APIRouter()
templates = Jinja2Templates(directory="templates")


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
                and value.startswith(("=", "+", "-", "@", "\t", "\r"))
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
