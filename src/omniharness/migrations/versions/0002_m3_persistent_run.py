"""m3 persistent run: commands, agent runs, budgets and lineage columns"""
import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

LINEAGE_COLUMNS = (
    "workflow_version", "thread_id", "parent_id", "attempt_index",
    "strategy_id", "outcome", "verdict",
)


def upgrade():
    for column in (
        sa.Column("workflow_version", sa.String(length=32), nullable=True),
        sa.Column("thread_id", sa.String(length=64), nullable=True),
        sa.Column("parent_id", sa.String(length=36), nullable=True),
        sa.Column("attempt_index", sa.Integer(), nullable=True),
        sa.Column("strategy_id", sa.String(length=64), nullable=True),
        sa.Column("outcome", sa.JSON(), nullable=True),
        sa.Column("verdict", sa.String(length=32), nullable=True),
    ):
        op.add_column("runs", column)

    op.create_table(
        "run_commands",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("sequence_number", sa.Integer(), nullable=False),
        sa.Column("command_type", sa.String(length=32), nullable=False),
        sa.Column("client_request_id", sa.String(length=100), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "client_request_id"),
        sa.UniqueConstraint("run_id", "sequence_number"),
    )
    op.create_index("ix_run_commands_run_id", "run_commands", ["run_id"], unique=False)

    op.create_table(
        "agent_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("parent_agent_run_id", sa.String(length=36), nullable=True),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("depth", sa.Integer(), nullable=False),
        sa.Column("model_config_id", sa.String(length=36), nullable=True),
        sa.Column("error_code", sa.String(length=80), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"]),
        sa.ForeignKeyConstraint(["parent_agent_run_id"], ["agent_runs.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_agent_runs_run_id", "agent_runs", ["run_id"], unique=False)

    op.create_table(
        "run_budgets",
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("max_model_calls", sa.Integer(), nullable=False),
        sa.Column("max_tool_calls", sa.Integer(), nullable=False),
        sa.Column("max_revisions", sa.Integer(), nullable=False),
        sa.Column("max_active_seconds", sa.Integer(), nullable=False),
        sa.Column("model_calls", sa.Integer(), nullable=False),
        sa.Column("tool_calls", sa.Integer(), nullable=False),
        sa.Column("revisions", sa.Integer(), nullable=False),
        sa.Column("active_seconds", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"]),
        sa.PrimaryKeyConstraint("run_id"),
    )


def downgrade():
    op.drop_table("run_budgets")
    op.drop_index("ix_agent_runs_run_id", table_name="agent_runs")
    op.drop_table("agent_runs")
    op.drop_index("ix_run_commands_run_id", table_name="run_commands")
    op.drop_table("run_commands")
    with op.batch_alter_table("runs") as batch_op:
        for name in reversed(LINEAGE_COLUMNS):
            batch_op.drop_column(name)
