import os

os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import Product, StockBalance, Warehouse


@pytest.fixture
def db():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = session_factory()

    product_one = Product(sku="SKU-1", name="Тест")
    product_two = Product(sku="SKU-2", name="Другой")
    north = Warehouse(name="Север")
    south = Warehouse(name="Юг")

    session.add_all([product_one, product_two, north, south])
    session.flush()
    session.add_all(
        [
            StockBalance(
                product_id=product_one.id,
                warehouse_id=north.id,
                physical_qty=10,
                reserved_qty=0,
            ),
            StockBalance(
                product_id=product_one.id,
                warehouse_id=south.id,
                physical_qty=6,
                reserved_qty=0,
            ),
            StockBalance(
                product_id=product_two.id,
                warehouse_id=north.id,
                physical_qty=0,
                reserved_qty=0,
            ),
        ]
    )
    session.commit()

    yield session

    session.close()
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture
def ids(db):
    return {
        "product": db.query(Product).filter_by(sku="SKU-1").one().id,
        "other": db.query(Product).filter_by(sku="SKU-2").one().id,
        "north": db.query(Warehouse).filter_by(name="Север").one().id,
        "south": db.query(Warehouse).filter_by(name="Юг").one().id,
    }
