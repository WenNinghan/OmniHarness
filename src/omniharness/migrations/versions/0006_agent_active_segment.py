"""Track an Agent active interval whose duration has not yet been settled."""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("run_budgets", sa.Column("active_segment_open", sa.Boolean(),
                                          server_default=sa.false(), nullable=False))


def downgrade():
    op.drop_column("run_budgets", "active_segment_open")
