from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.orm import Session

from app.db import Base
from app.models import Product


@pytest.mark.parametrize("existing_schema", [False, True])
def test_alembic_upgrade_preserves_existing_data_and_creates_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    existing_schema: bool,
):
    database_path = tmp_path / "checkstock.db"
    database_url = f"sqlite+pysqlite:///{database_path}"

    if existing_schema:
        engine = create_engine(database_url)
        Base.metadata.create_all(engine)
        with Session(engine) as db:
            db.add(Product(sku="LEGACY-1", name="Существующий товар"))
            db.commit()
        with engine.begin() as connection:
            connection.exec_driver_sql("ALTER TABLE products DROP COLUMN minimum_stock")
        engine.dispose()

    monkeypatch.setenv("DATABASE_URL", database_url)
    project_root = Path(__file__).resolve().parents[1]
    alembic_config = Config(str(project_root / "alembic.ini"))

    command.upgrade(alembic_config, "head")

    engine = create_engine(database_url)
    try:
        inspector = inspect(engine)
        assert {
            "products",
            "warehouses",
            "stock_balances",
            "operations",
            "users",
            "audit_logs",
            "alembic_version",
        }.issubset(set(inspector.get_table_names()))
        product_columns = {
            column["name"] for column in inspector.get_columns("products")
        }
        assert "minimum_stock" in product_columns

        if existing_schema:
            with Session(engine) as db:
                product = db.scalar(select(Product).where(Product.sku == "LEGACY-1"))
                assert product is not None
                assert product.name == "Существующий товар"
                assert product.minimum_stock == 0
    finally:
        engine.dispose()
