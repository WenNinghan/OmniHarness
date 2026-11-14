"""Persist user clarification requests and idempotent responses."""
import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "clarifications",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("request_key", sa.String(120), nullable=False),
        sa.Column("task_id", sa.String(120)),
        sa.Column("stage", sa.String(32), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text()),
        sa.Column("options", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("answer", sa.Text()),
        sa.Column("client_request_id", sa.String(100)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("answered_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("run_id", "request_key"),
        sa.UniqueConstraint("run_id", "client_request_id"),
    )
    op.create_index("ix_clarifications_run_id", "clarifications", ["run_id"])


def downgrade():
    op.drop_index("ix_clarifications_run_id", table_name="clarifications")
    op.drop_table("clarifications")
