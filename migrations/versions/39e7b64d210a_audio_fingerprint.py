"""Audio fingerprint and verified media type.

Revision ID: 39e7b64d210a
Revises: 2c7a84b1e903
"""

import sqlalchemy as sa
from alembic import op

revision = "39e7b64d210a"
down_revision = "2c7a84b1e903"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("captures", sa.Column("audio_sha256", sa.String(64), nullable=True))
    op.add_column("captures", sa.Column("audio_media_type", sa.String(32), nullable=True))


def downgrade():
    op.drop_column("captures", "audio_media_type")
    op.drop_column("captures", "audio_sha256")
