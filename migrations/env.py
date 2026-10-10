import sqlalchemy as sa
from alembic import context

from app import models  # noqa: F401
from app.config import Settings
from app.db import Base, make_database

settings = Settings.from_env()


def compare_type(context, inspected_column, metadata_column, inspected_type, metadata_type):
    # SQLite stores VARCHAR(n) and TEXT the same way and never checks the length, so String(n) -> Text
    # migrations are skipped there. PostgreSQL keeps the default strict comparison.
    if isinstance(metadata_type, sa.types.TypeDecorator):  # EncryptedText and EncryptedJSON
        metadata_type = metadata_type.impl
    if context.dialect.name == "sqlite" and isinstance(inspected_type, sa.String) and isinstance(metadata_type, sa.String):
        return False
    return None


if context.is_offline_mode():
    context.configure(url=settings.database_url, target_metadata=Base.metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    engine, _ = make_database(settings.database_url)
    with engine.connect() as connection:
        context.configure(
            connection=connection, target_metadata=Base.metadata, render_as_batch=True, compare_type=compare_type,
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()
