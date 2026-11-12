from datetime import datetime, timezone
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session
from .db import Base, engine, get_db
from .models import Operation, Product, StockBalance, Warehouse
from .services import StockError, accept_movement, cancel_fbs, fbo_send, receipt, reconcile, reserve_fbs, ship_fbs, transfer

app = FastAPI(title="CheckStock")
templates = Jinja2Templates(directory="templates")

@app.on_event("startup")
def startup():
    Base.metadata.create_all(bind=engine)

@app.exception_handler(StockError)
async def stock_error_handler(request: Request, exc: StockError):
    return templates.TemplateResponse("index.html", context={
        "request": request, "error": str(exc), "products": [], "warehouses": [],
        "balances": [], "archived": [], "operations": [], "transit": []
    }, status_code=409)

@app.get("/")
def dashboard(request: Request, db: Session = Depends(get_db)):
    products = db.scalars(select(Product).order_by(Product.sku)).all()
    warehouses = db.scalars(select(Warehouse).where(Warehouse.deleted_at.is_(None)).order_by(Warehouse.name)).all()
    archived = db.scalars(select(Warehouse).where(Warehouse.deleted_at.is_not(None)).order_by(Warehouse.name)).all()
    balances = db.scalars(select(StockBalance)).all()
    operations = db.scalars(select(Operation).order_by(Operation.created_at.desc()).limit(100)).all()
    transit = [o for o in operations if o.status in ("IN_TRANSIT", "SHIPPED")]
    names = {w.id: w.name + (" (архив)" if w.deleted_at else "") for w in db.scalars(select(Warehouse)).all()}
    product_names = {p.id: f"{p.sku} — {p.name}" for p in products}
    return templates.TemplateResponse("index.html", {"request":request,"products":products,
        "warehouses":warehouses,"archived":archived,"balances":balances,"operations":operations,
        "transit":transit,"names":names,"product_names":product_names,"error":None})

@app.post("/warehouses")
def add_warehouse(name: str = Form(...), db: Session = Depends(get_db)):
    db.add(Warehouse(name=name.strip())); db.commit()
    return RedirectResponse("/", status_code=303)

@app.post("/warehouses/{warehouse_id}/rename")
def rename_warehouse(warehouse_id: int, name: str = Form(...), db: Session = Depends(get_db)):
    wh = db.get(Warehouse, warehouse_id)
    if not wh or wh.deleted_at: raise HTTPException(404, "Склад не найден")
    wh.name = name.strip(); db.commit()
    return RedirectResponse("/", status_code=303)

@app.post("/warehouses/{warehouse_id}/delete")
def delete_warehouse(warehouse_id: int, db: Session = Depends(get_db)):
    wh = db.get(Warehouse, warehouse_id)
    if not wh: raise HTTPException(404, "Склад не найден")
    wh.deleted_at = datetime.now(timezone.utc); db.commit()
    return RedirectResponse("/", status_code=303)

@app.post("/operations")
def create_operation(kind: str = Form(...), product_id: int = Form(...), qty: int = Form(...),
                     warehouse_id: int | None = Form(None), destination_id: int | None = Form(None),
                     reserve_id: int | None = Form(None), operation_id: int | None = Form(None),
                     idempotency_key: str | None = Form(None), db: Session = Depends(get_db)):
    try:
        if kind == "RECEIPT": receipt(db, product_id, warehouse_id, qty, idempotency_key)
        elif kind == "FBS_RESERVE": reserve_fbs(db, product_id, warehouse_id, qty, idempotency_key)
        elif kind == "FBS_CANCEL": cancel_fbs(db, reserve_id, idempotency_key)
        elif kind == "FBS_SHIP": ship_fbs(db, reserve_id, idempotency_key)
        elif kind == "TRANSFER": transfer(db, product_id, warehouse_id, destination_id, qty, idempotency_key)
        elif kind == "FBO_SEND": fbo_send(db, product_id, warehouse_id, qty, idempotency_key)
        elif kind == "ACCEPT": accept_movement(db, operation_id, idempotency_key)
        else: raise StockError("Неизвестный тип операции.")
    except (StockError, TypeError) as exc:
        raise HTTPException(409, str(exc))
    return RedirectResponse("/", status_code=303)

@app.get("/api/reconcile/{product_id}")
def reconcile_product(product_id: int, db: Session = Depends(get_db)):
    if not db.get(Product, product_id): raise HTTPException(404, "Товар не найден")
    return reconcile(db, product_id)
