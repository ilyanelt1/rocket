from sqlalchemy import select
from .db import Base, SessionLocal, engine
from .models import Product, StockBalance, Warehouse

def seed():
    Base.metadata.create_all(bind=engine)
    with SessionLocal.begin() as db:
        products = [
            ("SKU-101", "Органайзер"),
            ("SKU-102", "Контейнер"),
            ("SKU-103", "Корзина"),
        ]
        for sku, name in products:
            if not db.scalar(select(Product).where(Product.sku == sku)):
                db.add(Product(sku=sku, name=name))
        for name in ("Север", "Юг"):
            if not db.scalar(select(Warehouse).where(Warehouse.name == name)):
                db.add(Warehouse(name=name))
    with SessionLocal.begin() as db:
        p = {x.sku:x for x in db.scalars(select(Product)).all()}
        w = {x.name:x for x in db.scalars(select(Warehouse)).all()}
        initial = {("SKU-101","Север"):10,("SKU-101","Юг"):6,
                   ("SKU-102","Север"):5,("SKU-102","Юг"):0,
                   ("SKU-103","Север"):0,("SKU-103","Юг"):4}
        for (sku, wh), qty in initial.items():
            row = db.scalar(select(StockBalance).where(
                StockBalance.product_id == p[sku].id, StockBalance.warehouse_id == w[wh].id))
            if row is None:
                db.add(StockBalance(product_id=p[sku].id, warehouse_id=w[wh].id,
                                    physical_qty=qty, reserved_qty=0))
if __name__ == "__main__":
    seed()
