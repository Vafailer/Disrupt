"""AI assistant requests and the switch for recommendations."""
import sqlalchemy as sa
from alembic import op

revision = "f3a82c91d507"
down_revision = "e61f893ac204"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("users", sa.Column(
        "assistant_recommendations_enabled", sa.Boolean(), server_default=sa.true(), nullable=False,
    ))
    op.create_table(
        "assistant_requests",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("question", sa.Text(), nullable=True),
        sa.Column("days", sa.Integer(), nullable=True),
        sa.Column("input_note_ids", sa.JSON(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("idempotency_key", sa.String(100), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("units", sa.Integer(), nullable=False),
        sa.Column("lease_until", sa.Float(), nullable=True),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("started_at", sa.Float(), nullable=True),
        sa.Column("finished_at", sa.Float(), nullable=True),
        sa.CheckConstraint("kind IN ('ask', 'recommend', 'digest')", name="ck_assistant_requests_kind"),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed')", name="ck_assistant_requests_status",
        ),
        sa.UniqueConstraint("user_id", "idempotency_key", name="uq_assistant_requests_user_key"),
    )
    op.create_index("ix_assistant_requests_user_created", "assistant_requests", ["user_id", "created_at"])
    op.create_index("ix_assistant_requests_status", "assistant_requests", ["status"])


def downgrade():
    op.drop_index("ix_assistant_requests_status", table_name="assistant_requests")
    op.drop_index("ix_assistant_requests_user_created", table_name="assistant_requests")
    op.drop_table("assistant_requests")
    op.drop_column("users", "assistant_recommendations_enabled")
