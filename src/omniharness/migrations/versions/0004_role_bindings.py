"""m3 slice D: per-role model bindings (configuration only)"""
import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "role_bindings",
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("model_config_id", sa.String(length=36), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["model_config_id"], ["model_configs.id"]),
        sa.PrimaryKeyConstraint("role"),
    )


def downgrade():
    op.drop_table("role_bindings")
