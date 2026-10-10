from alembic import context

from app import models  # noqa: F401
from app.config import Settings
from app.db import Base, make_database

settings = Settings.from_env()
if context.is_offline_mode():
    context.configure(url=settings.database_url, target_metadata=Base.metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    engine, _ = make_database(settings.database_url)
    with engine.connect() as connection:
        sqlite = connection.dialect.name == "sqlite"
        if sqlite:
            # Batch migrations rebuild a table (copy, drop, rename). With foreign keys on, SQLite refuses to drop
            # a table that other rows point to. The pragma only works outside a transaction, so it runs first,
            # and the keys are checked once the migrations are done.
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            connection.commit()
        context.configure(connection=connection, target_metadata=Base.metadata, render_as_batch=True)
        with context.begin_transaction():
            context.run_migrations()
            if sqlite:
                broken = connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
                if broken:
                    raise RuntimeError(f"Migration left {len(broken)} broken foreign keys")
        if sqlite:
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
    engine.dispose()
