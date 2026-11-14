"""Durable invocation-bound budget reservations."""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "run_budget_reservations",
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("invocation_id", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=24), nullable=False),
        sa.Column("binding_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"]),
        sa.PrimaryKeyConstraint("run_id", "invocation_id"),
    )


def downgrade():
    op.drop_table("run_budget_reservations")
