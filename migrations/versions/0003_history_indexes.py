"""Add indexes used by operation history and audit lookups.

The baseline migration may already have created these indexes on a new database.
Inspect first so this revision is safe for both fresh and legacy installations.
"""

from alembic import op
from sqlalchemy import inspect

revision = "0003_history_indexes"
down_revision = "0002_ensure_minimum_stock"
branch_labels = None
depends_on = None

_INDEXES = (
    ("operations", "ix_operations_created_at", ["created_at"]),
    ("operations", "ix_operations_product_id", ["product_id"]),
    (
        "operations",
        "ix_operations_history_created_id",
        ["created_at", "id"],
    ),
    (
        "operations",
        "ix_operations_product_created_at",
        ["product_id", "created_at"],
    ),
    ("operations", "ix_operations_source_warehouse_id", ["source_warehouse_id"]),
    (
        "operations",
        "ix_operations_destination_warehouse_id",
        ["destination_warehouse_id"],
    ),
    ("audit_logs", "ix_audit_logs_entity_id", ["entity_id"]),
)


def upgrade() -> None:
    connection = op.get_bind()
    inspector = inspect(connection)
    tables = set(inspector.get_table_names())
    for table_name, index_name, columns in _INDEXES:
        if table_name not in tables:
            continue
        existing = {item["name"] for item in inspector.get_indexes(table_name)}
        if index_name not in existing:
            op.create_index(index_name, table_name, columns)


def downgrade() -> None:
    """Keep performance indexes in place; dropping them is not required."""
