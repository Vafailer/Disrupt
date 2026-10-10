"""Email registration creates an account only after a browser-bound code is confirmed."""
import sqlalchemy as sa
from alembic import op

revision = "d6a18f9c2e40"
down_revision = "c9e4a1b2d3f6"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "email_registration_pending",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("email", sa.String(254), nullable=False),
        sa.Column("password_hash", sa.String(256), nullable=False),
        sa.Column("binding_hash", sa.String(64), nullable=False),
        sa.Column("code_hash", sa.String(64), nullable=False),
        sa.Column("policy_version", sa.String(32), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("expires_at", sa.Float(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("used_at", sa.Float(), nullable=True),
        sa.CheckConstraint("attempts >= 0 AND attempts <= 5", name="ck_email_registration_attempts"),
    )
    op.create_index("ix_email_registration_pending_email", "email_registration_pending", ["email"])


def downgrade():
    op.drop_index("ix_email_registration_pending_email", table_name="email_registration_pending")
    op.drop_table("email_registration_pending")
