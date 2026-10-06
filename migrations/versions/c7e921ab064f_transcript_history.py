"""Preserve transcript revisions without rewriting capture originals.

Revision ID: c7e921ab064f
Revises: 9a16c3d80b24
"""

import sqlalchemy as sa
from alembic import op

revision = "c7e921ab064f"
down_revision = "9a16c3d80b24"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "transcript_revisions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("capture_id", sa.String(36), sa.ForeignKey("captures.id"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("origin", sa.String(16), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=True),
        sa.UniqueConstraint("capture_id", "version"),
        sa.CheckConstraint("origin IN ('stt', 'user', 'legacy')", name="ck_transcript_origin"),
    )
    op.create_index("ix_transcript_revisions_capture_id", "transcript_revisions", ["capture_id"])
    # Old dates and origins are unknown. Do not invent a recognition or edit event.
    op.execute(sa.text(
        "INSERT INTO transcript_revisions (id,capture_id,version,text,origin,created_at) "
        "SELECT id,id,transcript_version,transcript,'legacy',NULL FROM captures "
        "WHERE input_kind='audio' AND transcript IS NOT NULL"
    ))


def downgrade():
    op.drop_index("ix_transcript_revisions_capture_id", table_name="transcript_revisions")
    op.drop_table("transcript_revisions")
