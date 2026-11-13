"""Create missing application tables without replacing existing data.

This baseline intentionally uses checkfirst=True so it can be applied both to an
empty database and to an existing CheckStock database. It does not alter tables
that already exist; follow-up revisions own incremental schema changes.
"""

from alembic import op

from app.db import Base
from app import models  # noqa: F401

revision = "0001_initial_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    """Keep baseline tables and user data; destructive downgrade is unsupported."""
