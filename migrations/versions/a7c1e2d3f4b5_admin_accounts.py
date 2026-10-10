"""Separate administrator accounts, sessions and audit log.

Revision ID: a7c1e2d3f4b5
Revises: f3a82c91d507
"""
import sqlalchemy as sa
from alembic import op

revision = "a7c1e2d3f4b5"
down_revision = "f3a82c91d507"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "admin_accounts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("username", sa.String(64), nullable=False),
        sa.Column("password_hash", sa.String(256), nullable=False),
        sa.Column("totp_secret", sa.String(64), nullable=False),
        sa.Column("totp_enabled", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("last_totp_step", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("last_login_at", sa.Float(), nullable=True),
        sa.Column("failed_attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("locked_until", sa.Float(), nullable=True),
        sa.Column("disabled", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.UniqueConstraint("username", name="uq_admin_accounts_username"),
    )
    op.create_table(
        "admin_sessions",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column("admin_id", sa.String(36), sa.ForeignKey("admin_accounts.id"), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("expires_at", sa.Float(), nullable=False),
        sa.Column("last_seen_at", sa.Float(), nullable=False),
        sa.Column("csrf_hash", sa.String(64), nullable=False),
        sa.Column("ip", sa.String(64), nullable=False),
    )
    op.create_index("ix_admin_sessions_admin_id", "admin_sessions", ["admin_id"])
    op.create_table(
        "admin_audit_log",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("admin_id", sa.String(36), sa.ForeignKey("admin_accounts.id"), nullable=True),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("target", sa.String(100), nullable=True),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("ip", sa.String(64), nullable=True),
        sa.Column("details", sa.JSON(), nullable=False),
    )
    op.create_index("ix_admin_audit_log_created_at", "admin_audit_log", ["created_at"])
    # An administrator is no longer a user with a role. The column stays for compatibility, but nothing reads it.
    op.execute("UPDATE users SET role = 'user' WHERE role = 'admin'")


def downgrade():
    # The old admin role is not restored. Grant access again with the previous release if needed.
    op.drop_index("ix_admin_audit_log_created_at", table_name="admin_audit_log")
    op.drop_table("admin_audit_log")
    op.drop_index("ix_admin_sessions_admin_id", table_name="admin_sessions")
    op.drop_table("admin_sessions")
    op.drop_table("admin_accounts")
