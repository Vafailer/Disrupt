"""Items and category versions.

Revision ID: 2c7a84b1e903
Revises: 651eb026c9bd
"""

import sqlalchemy as sa
from alembic import op

revision = "2c7a84b1e903"
down_revision = "651eb026c9bd"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("categories", sa.Column("version", sa.Integer(), nullable=False, server_default="1"))
    op.add_column("items", sa.Column("source_quote", sa.Text(), nullable=True))
    op.add_column("items", sa.Column("due_text", sa.String(300), nullable=True))
    op.add_column("items", sa.Column("position", sa.Integer(), nullable=False, server_default="0"))


def downgrade():
    op.drop_column("items", "position")
    op.drop_column("items", "due_text")
    op.drop_column("items", "source_quote")
    op.drop_column("categories", "version")
