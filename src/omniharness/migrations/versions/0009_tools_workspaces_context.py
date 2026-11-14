"""M4 workspaces, approval evidence, artifacts and context configuration."""

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('workspaces',
        sa.Column('id', sa.String(length=36), nullable=False, primary_key=True),
        sa.Column('name', sa.String(length=160), nullable=False),
        sa.Column('host_path', sa.Text(), nullable=False),
        sa.Column('physical_key', sa.String(length=64), nullable=True, unique=True),
        sa.Column('policy_version', sa.Integer(), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('probe', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table('workspace_policies',
        sa.Column('workspace_id', sa.String(length=36), sa.ForeignKey('workspaces.id'), nullable=False, primary_key=True),
        sa.Column('version', sa.Integer(), nullable=False, primary_key=True),
        sa.Column('grant', sa.JSON(), nullable=False),
        sa.Column('tool_policy', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table('workspace_leases',
        sa.Column('workspace_id', sa.String(length=36), sa.ForeignKey('workspaces.id'), nullable=False, primary_key=True),
        sa.Column('run_id', sa.String(length=36), sa.ForeignKey('runs.id'), nullable=False),
        sa.Column('owner_epoch', sa.String(length=80), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_workspace_leases_run_id', 'workspace_leases', ['run_id'], unique=False)
    op.create_table('workspace_snapshots',
        sa.Column('id', sa.String(length=64), nullable=False, primary_key=True),
        sa.Column('workspace_id', sa.String(length=36), sa.ForeignKey('workspaces.id'), nullable=False),
        sa.Column('run_id', sa.String(length=36), sa.ForeignKey('runs.id'), nullable=False),
        sa.Column('task_id', sa.String(length=120), nullable=True),
        sa.Column('policy_version', sa.Integer(), nullable=False),
        sa.Column('manifest', sa.JSON(), nullable=False),
        sa.Column('workdir', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_workspace_snapshots_run_id', 'workspace_snapshots', ['run_id'], unique=False)
    op.create_index('ix_workspace_snapshots_workspace_id', 'workspace_snapshots', ['workspace_id'], unique=False)
    op.create_table('tool_operations',
        sa.Column('id', sa.String(length=64), nullable=False, primary_key=True),
        sa.Column('run_id', sa.String(length=36), sa.ForeignKey('runs.id'), nullable=False),
        sa.Column('operation_record_id', sa.String(length=64), sa.ForeignKey('operation_records.operation_id'), nullable=True),
        sa.Column('agent_run_id', sa.String(length=36), nullable=True),
        sa.Column('task_id', sa.String(length=120), nullable=False),
        sa.Column('tool_call_id', sa.String(length=200), nullable=False),
        sa.Column('tool_name', sa.String(length=120), nullable=False),
        sa.Column('tool_version', sa.String(length=120), nullable=False),
        sa.Column('parameters', sa.JSON(), nullable=False),
        sa.Column('binding_fingerprint', sa.String(length=64), nullable=False),
        sa.Column('status', sa.String(length=32), nullable=False),
        sa.Column('result', sa.JSON(), nullable=True),
        sa.Column('error_code', sa.String(length=80), nullable=True),
        sa.Column('sandbox_id', sa.String(length=64), nullable=True),
        sa.Column('command_id', sa.String(length=128), nullable=True),
        sa.Column('snapshot_id', sa.String(length=64), sa.ForeignKey('workspace_snapshots.id'), nullable=True),
        sa.Column('slot_claimed', sa.Boolean(), nullable=False),
        sa.Column('reclaimed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index('ix_tool_operations_run_id', 'tool_operations', ['run_id'], unique=False)
    op.create_index('ix_tool_operations_status', 'tool_operations', ['status'], unique=False)
    op.create_table('approvals',
        sa.Column('id', sa.String(length=36), nullable=False, primary_key=True),
        sa.Column('operation_id', sa.String(length=64), sa.ForeignKey('tool_operations.id'), nullable=False, unique=True),
        sa.Column('fingerprint', sa.String(length=64), nullable=False),
        sa.Column('status', sa.String(length=24), nullable=False),
        sa.Column('decision_request_id', sa.String(length=100), nullable=True),
        sa.Column('requested_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('decision_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index('ix_approvals_status', 'approvals', ['status'], unique=False)
    op.create_table('artifact_versions',
        sa.Column('id', sa.String(length=36), nullable=False, primary_key=True),
        sa.Column('run_id', sa.String(length=36), sa.ForeignKey('runs.id'), nullable=False),
        sa.Column('operation_id', sa.String(length=64), nullable=True),
        sa.Column('workspace_id', sa.String(length=36), sa.ForeignKey('workspaces.id'), nullable=True),
        sa.Column('kind', sa.String(length=64), nullable=False),
        sa.Column('name', sa.String(length=200), nullable=False),
        sa.Column('sha256', sa.String(length=64), nullable=False),
        sa.Column('size_bytes', sa.Integer(), nullable=False),
        sa.Column('storage_path', sa.Text(), nullable=False),
        sa.Column('metadata_json', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_artifact_versions_operation_id', 'artifact_versions', ['operation_id'], unique=False)
    op.create_index('ix_artifact_versions_run_id', 'artifact_versions', ['run_id'], unique=False)
    op.create_table('evaluation_sessions',
        sa.Column('id', sa.String(length=36), nullable=False, primary_key=True),
        sa.Column('name', sa.String(length=160), nullable=False),
        sa.Column('specification', sa.JSON(), nullable=False),
        sa.Column('results', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )
    with op.batch_alter_table("runs") as batch:
        batch.add_column(sa.Column("workspace_id", sa.String(36), nullable=True))
        batch.create_foreign_key("fk_runs_workspace_id", "workspaces", ["workspace_id"], ["id"])
        batch.create_index("ix_runs_workspace_id", ["workspace_id"])
    op.add_column("model_configs", sa.Column("context_policy", sa.JSON(), nullable=False, server_default="{}"))


def downgrade():
    op.drop_column("model_configs", "context_policy")
    with op.batch_alter_table("runs") as batch:
        batch.drop_index("ix_runs_workspace_id")
        batch.drop_constraint("fk_runs_workspace_id", type_="foreignkey")
        batch.drop_column("workspace_id")
    op.drop_table('evaluation_sessions')
    op.drop_table('artifact_versions')
    op.drop_table('approvals')
    op.drop_table('tool_operations')
    op.drop_table('workspace_snapshots')
    op.drop_table('workspace_leases')
    op.drop_table('workspace_policies')
    op.drop_table('workspaces')
