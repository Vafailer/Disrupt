"""Widen short text columns that now hold ciphertext.

Шифртекст длиннее открытого текста, поэтому короткие String(n) становятся Text.
Сами данные здесь не меняются. Их шифрует `python -m app.data_crypto encrypt-existing`.

Revision ID: c9e4a1b2d3f6
Revises: b8d2f3e4a5c6
"""
import sqlalchemy as sa
from alembic import op

revision = "c9e4a1b2d3f6"
down_revision = "b8d2f3e4a5c6"
branch_labels = None
depends_on = None

COLUMNS = (
    ("notes", "title", 200, False),
    ("items", "due_text", 300, True),
    ("admin_accounts", "totp_secret", 64, False),
    ("feedback", "subject", 160, False),
    ("feedback", "contact", 200, False),
)


def sqlite():
    # SQLite does not enforce VARCHAR length. Rebuilding notes there would also change its foreign keys
    # and break older downgrades, so the change is only made where it matters.
    return op.get_bind().dialect.name == "sqlite"


def upgrade():
    if sqlite():
        return
    for table, column, length, nullable in COLUMNS:
        with op.batch_alter_table(table) as batch:
            batch.alter_column(
                column, existing_type=sa.String(length), type_=sa.Text(), existing_nullable=nullable,
            )


def downgrade():
    if sqlite():
        return
    # После шифрования значения длиннее исходных. Вернуть короткий тип можно только до `encrypt-existing`.
    for table, column, length, nullable in COLUMNS:
        with op.batch_alter_table(table) as batch:
            batch.alter_column(
                column, existing_type=sa.Text(), type_=sa.String(length), existing_nullable=nullable,
            )
