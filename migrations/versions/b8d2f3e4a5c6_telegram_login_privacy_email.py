"""Telegram sign-in, password recovery, privacy consent, deletion requests and an email scaffold."""
import sqlalchemy as sa
from alembic import op

revision = "b8d2f3e4a5c6"
down_revision = "a7c1e2d3f4b5"
branch_labels = None
depends_on = None

OLD_TARGET = "(reminder_id IS NOT NULL AND job_id IS NULL) OR (reminder_id IS NULL AND job_id IS NOT NULL)"
NEW_TARGET = (
    "(reminder_id IS NOT NULL AND job_id IS NULL AND message_kind IS NULL) OR "
    "(reminder_id IS NULL AND job_id IS NOT NULL AND message_kind IS NULL) OR "
    "(reminder_id IS NULL AND job_id IS NULL AND message_kind IS NOT NULL)"
)


def upgrade():
    # users.is_test already exists (added with the admin metrics), so it is not touched here.
    op.add_column("users", sa.Column("email", sa.String(254), nullable=True))
    op.add_column("users", sa.Column("email_verified_at", sa.Float(), nullable=True))
    op.add_column("users", sa.Column("policy_version", sa.String(32), nullable=True))
    op.add_column("users", sa.Column("policy_accepted_at", sa.Float(), nullable=True))
    op.add_column("users", sa.Column("deletion_requested_at", sa.Float(), nullable=True))
    op.create_index("uq_users_email", "users", ["email"], unique=True)

    op.create_table(
        "telegram_logins",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("purpose", sa.String(16), server_default="login", nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("binding_hash", sa.String(64), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=True),
        sa.Column("policy_version", sa.String(32), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("bot_id", sa.BigInteger(), nullable=True),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=True),
        sa.Column("telegram_username", sa.String(64), nullable=True),
        sa.Column("expires_at", sa.Float(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("confirmed_at", sa.Float(), nullable=True),
        sa.Column("consumed_at", sa.Float(), nullable=True),
        sa.CheckConstraint("purpose IN ('login', 'delete')", name="ck_telegram_logins_purpose"),
        sa.CheckConstraint("status IN ('pending', 'confirmed', 'consumed')", name="ck_telegram_logins_status"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index(op.f("ix_telegram_logins_user_id"), "telegram_logins", ["user_id"], unique=False)

    op.create_table(
        "email_verifications",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("purpose", sa.String(16), nullable=False),
        sa.Column("channel", sa.String(16), server_default="email", nullable=False),
        sa.Column("email", sa.String(254), nullable=True),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.Float(), nullable=False),
        sa.Column("used_at", sa.Float(), nullable=True),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.CheckConstraint("purpose IN ('verify', 'reset')", name="ck_email_verifications_purpose"),
        sa.CheckConstraint("channel IN ('email', 'telegram')", name="ck_email_verifications_channel"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index(op.f("ix_email_verifications_user_id"), "email_verifications", ["user_id"], unique=False)

    # Durable account messages share the outbox with reminders and processing replies.
    with op.batch_alter_table("outbox") as batch:
        batch.drop_constraint("ck_outbox_target", type_="check")
        batch.add_column(sa.Column("message_kind", sa.String(32), nullable=True))
        batch.add_column(sa.Column("message_text", sa.Text(), nullable=True))
        batch.create_check_constraint("ck_outbox_target", NEW_TARGET)


def downgrade():
    # Account messages are short-lived and carry one-time links, so they are dropped, not migrated.
    op.execute(sa.text("DELETE FROM outbox WHERE message_kind IS NOT NULL"))
    with op.batch_alter_table("outbox") as batch:
        batch.drop_constraint("ck_outbox_target", type_="check")
        batch.drop_column("message_text")
        batch.drop_column("message_kind")
        batch.create_check_constraint("ck_outbox_target", OLD_TARGET)

    op.drop_index(op.f("ix_email_verifications_user_id"), table_name="email_verifications")
    op.drop_table("email_verifications")
    op.drop_index(op.f("ix_telegram_logins_user_id"), table_name="telegram_logins")
    op.drop_table("telegram_logins")

    op.drop_index("uq_users_email", table_name="users")
    op.drop_column("users", "deletion_requested_at")
    op.drop_column("users", "policy_accepted_at")
    op.drop_column("users", "policy_version")
    op.drop_column("users", "email_verified_at")
    op.drop_column("users", "email")
