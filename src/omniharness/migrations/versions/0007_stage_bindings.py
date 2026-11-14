"""Store stage bindings independently of legacy Agent role bindings."""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade():
    table = op.create_table(
        "stage_binding_sets",
        sa.Column("id", sa.String(16), primary_key=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("bindings", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.bulk_insert(table, [{"id": "local", "version": 0, "bindings": {},
                            "updated_at": sa.func.now()}], multiinsert=False)


def downgrade():
    op.drop_table("stage_binding_sets")
