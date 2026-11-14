"""Preserve provider reasoning separately from displayed answers."""

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade():
    # Defaults keep pre-upgrade rows readable without guessing their provenance.
    op.add_column("messages", sa.Column("reasoning_content", sa.Text(), nullable=False, server_default=""))
    op.add_column("messages", sa.Column("output_metadata", sa.JSON(), nullable=False, server_default="{}"))


def downgrade():
    op.drop_column("messages", "output_metadata")
    op.drop_column("messages", "reasoning_content")
