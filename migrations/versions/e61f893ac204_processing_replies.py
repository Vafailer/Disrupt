"""Deliver completed Telegram captures using the existing durable outbox."""
import sqlalchemy as sa
from alembic import op

revision = "e61f893ac204"
down_revision = "d84f210ac937"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("outbox") as batch:
        batch.alter_column("reminder_id", existing_type=sa.String(36), nullable=True)
        batch.add_column(sa.Column("job_id", sa.String(36), nullable=True))
        batch.create_foreign_key("fk_outbox_processing_job", "jobs", ["job_id"], ["id"])
        batch.create_unique_constraint("uq_outbox_processing_job", ["job_id"])
        batch.create_check_constraint("ck_outbox_target", "(reminder_id IS NOT NULL AND job_id IS NULL) OR "
                                      "(reminder_id IS NULL AND job_id IS NOT NULL)")


def downgrade():
    connection = op.get_bind()
    if connection.execute(sa.text("SELECT count(*) FROM outbox WHERE job_id IS NOT NULL")).scalar():
        raise RuntimeError("Processing delivery state exists; automatic downgrade would discard it")
    with op.batch_alter_table("outbox") as batch:
        batch.drop_constraint("ck_outbox_target", type_="check")
        batch.drop_constraint("uq_outbox_processing_job", type_="unique")
        batch.drop_constraint("fk_outbox_processing_job", type_="foreignkey")
        batch.drop_column("job_id")
        batch.alter_column("reminder_id", existing_type=sa.String(36), nullable=False)
