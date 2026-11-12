from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine


def apply_schema_migrations(engine: Engine) -> None:
    """Apply small, idempotent schema changes required by the running app."""
    with engine.begin() as connection:
        product_columns = {
            column["name"] for column in inspect(connection).get_columns("products")
        }
        if "minimum_stock" not in product_columns:
            connection.execute(
                text(
                    "ALTER TABLE products "
                    "ADD COLUMN minimum_stock INTEGER NOT NULL DEFAULT 0"
                )
            )
