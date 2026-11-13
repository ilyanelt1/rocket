"""Ensure the product minimum stock column exists on legacy databases."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = "0002_ensure_minimum_stock"
down_revision = "0001_initial_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    inspector = inspect(connection)
    if "products" not in inspector.get_table_names():
        raise RuntimeError("The products table is missing after the initial baseline.")

    columns = {column["name"] for column in inspector.get_columns("products")}
    if "minimum_stock" not in columns:
        op.add_column(
            "products",
            sa.Column(
                "minimum_stock",
                sa.Integer(),
                server_default=sa.text("0"),
                nullable=False,
            ),
        )


def downgrade() -> None:
    """Do not drop minimum_stock because it may contain configured business data."""
