import os

from sqlalchemy import select

from .db import Base, SessionLocal, engine
from .migrations import apply_schema_migrations
from .auth import hash_password
from .models import Product, StockBalance, User, Warehouse


def seed():
    Base.metadata.create_all(bind=engine)
    apply_schema_migrations(engine)
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
        products_by_sku = {
            product.sku: product for product in db.scalars(select(Product)).all()
        }
        warehouses_by_name = {
            warehouse.name: warehouse
            for warehouse in db.scalars(select(Warehouse)).all()
        }
        initial = {
            ("SKU-101", "Север"): 10,
            ("SKU-101", "Юг"): 6,
            ("SKU-102", "Север"): 5,
            ("SKU-102", "Юг"): 0,
            ("SKU-103", "Север"): 0,
            ("SKU-103", "Юг"): 4,
        }

        for (sku, warehouse_name), qty in initial.items():
            row = db.scalar(
                select(StockBalance).where(
                    StockBalance.product_id == products_by_sku[sku].id,
                    StockBalance.warehouse_id == warehouses_by_name[warehouse_name].id,
                )
            )
            if row is None:
                db.add(
                    StockBalance(
                        product_id=products_by_sku[sku].id,
                        warehouse_id=warehouses_by_name[warehouse_name].id,
                        physical_qty=qty,
                        reserved_qty=0,
                    )
                )

    username = os.getenv("ADMIN_USERNAME")
    password = os.getenv("ADMIN_PASSWORD")
    if username and password:
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


if __name__ == "__main__":
    seed()
