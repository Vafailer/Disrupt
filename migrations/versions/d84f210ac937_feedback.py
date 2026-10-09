"""Add user feedback inbox.

Revision ID: d84f210ac937
Revises: b17f03a29c64
"""
import sqlalchemy as sa
from alembic import op

revision = 'd84f210ac937'
down_revision = 'b17f03a29c64'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('feedback',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('user_id', sa.String(36), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('idempotency_key', sa.String(100), nullable=False),
        sa.Column('payload_hash', sa.String(64), nullable=False),
        sa.Column('kind', sa.String(16), nullable=False),
        sa.Column('subject', sa.String(160), nullable=False),
        sa.Column('description', sa.Text(), nullable=False),
        sa.Column('steps', sa.Text(), nullable=False),
        sa.Column('expected', sa.Text(), nullable=False),
        sa.Column('contact', sa.String(200), nullable=False),
        sa.Column('status', sa.String(16), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.Float(), nullable=False),
        sa.Column('updated_at', sa.Float(), nullable=False),
        sa.UniqueConstraint('user_id', 'idempotency_key', name='uq_feedback_user_key'),
        sa.CheckConstraint("kind IN ('bug','idea','question','other')", name='ck_feedback_kind'),
        sa.CheckConstraint("status IN ('new','in_progress','resolved')", name='ck_feedback_status'),
    )
    op.create_index('ix_feedback_user_id','feedback',['user_id'])
    op.create_index('ix_feedback_created_at','feedback',['created_at'])


def downgrade():
    op.drop_table('feedback')
