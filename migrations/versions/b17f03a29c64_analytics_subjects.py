"""Content-free event subjects, delivery outcomes and usage test snapshot."""

import sqlalchemy as sa
from alembic import op

revision = "b17f03a29c64"
down_revision = "c7e921ab064f"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("product_events", sa.Column("subject_id", sa.String(36), nullable=True))
    op.add_column("product_events", sa.Column("outcome", sa.String(32), nullable=True))
    op.add_column("provider_usage", sa.Column("is_test", sa.Boolean(), server_default=sa.false(), nullable=False))


def downgrade():
    op.drop_column("provider_usage", "is_test")
    op.drop_column("product_events", "outcome")
    op.drop_column("product_events", "subject_id")
