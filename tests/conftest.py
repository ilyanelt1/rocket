import os
os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.db import Base
from app.models import Product, Warehouse, StockBalance

@pytest.fixture
def db():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    session = Session()
    p1=Product(sku="SKU-1",name="Тест"); p2=Product(sku="SKU-2",name="Другой")
    north=Warehouse(name="Север"); south=Warehouse(name="Юг")
    session.add_all([p1,p2,north,south]); session.flush()
    session.add_all([
        StockBalance(product_id=p1.id,warehouse_id=north.id,physical_qty=10,reserved_qty=0),
        StockBalance(product_id=p1.id,warehouse_id=south.id,physical_qty=6,reserved_qty=0),
        StockBalance(product_id=p2.id,warehouse_id=north.id,physical_qty=0,reserved_qty=0),
    ])
    session.commit()
    yield session
    session.close()
    Base.metadata.drop_all(engine)

@pytest.fixture
def ids(db):
    from app.models import Product, Warehouse
    return {"product":db.query(Product).filter_by(sku="SKU-1").one().id,
            "other":db.query(Product).filter_by(sku="SKU-2").one().id,
            "north":db.query(Warehouse).filter_by(name="Север").one().id,
            "south":db.query(Warehouse).filter_by(name="Юг").one().id}
