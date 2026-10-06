"""Reminder creation idempotency without rebuilding referenced tables.

Revision ID: 9a16c3d80b24
Revises: 39e7b64d210a
"""

import sqlalchemy as sa
from alembic import op

revision = "9a16c3d80b24"
down_revision = "39e7b64d210a"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("reminders", sa.Column("idempotency_key", sa.String(100), nullable=True))
    op.add_column("reminders", sa.Column("creation_hash", sa.String(64), nullable=True))
    op.create_index("uq_reminders_user_idempotency", "reminders", ["user_id", "idempotency_key"], unique=True)


def downgrade():
    op.drop_index("uq_reminders_user_idempotency", table_name="reminders")
    op.drop_column("reminders", "creation_hash")
    op.drop_column("reminders", "idempotency_key")
