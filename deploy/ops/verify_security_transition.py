"""Compare decrypted data before/after migration in an isolated restore, never log content."""

import argparse
import hashlib
import json
import os
from pathlib import Path

from sqlalchemy import inspect, select

from app import crypto, models  # noqa: F401
from app.config import Settings
from app.db import Base, make_database


def fingerprint(connection, table, columns):
    rows = []
    for row in connection.execute(select(*(table.c[name] for name in columns))).mappings():
        values = dict(row)
        # The admin migration intentionally removes the old ordinary-user admin role.
        if table.name == "users" and values.get("role") == "admin":
            values["role"] = "user"
        rows.append(json.dumps(values, sort_keys=True, ensure_ascii=False, default=str))
    rows.sort()
    return {"count": len(rows), "sha256": hashlib.sha256("\n".join(rows).encode()).hexdigest()}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("record", "verify"))
    parser.add_argument("manifest")
    args = parser.parse_args()
    settings = Settings.from_env()
    crypto.configure_from_settings(settings)
    engine, _ = make_database(settings.database_url)
    path = Path(args.manifest)
    try:
        with engine.connect() as connection:
            if args.action == "record":
                present = inspect(connection)
                existing = set(present.get_table_names())
                report = {}
                for table in Base.metadata.sorted_tables:
                    if table.name not in existing:
                        continue
                    stored_columns = {column["name"] for column in present.get_columns(table.name)}
                    columns = [column.name for column in table.columns if column.name in stored_columns]
                    report[table.name] = {"columns": columns, **fingerprint(connection, table, columns)}
                with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
                    json.dump(report, stream)
            else:
                report = json.loads(path.read_text())
                for name, expected in report.items():
                    actual = fingerprint(connection, Base.metadata.tables[name], expected["columns"])
                    if any(actual[key] != expected[key] for key in ("count", "sha256")):
                        raise ValueError("Data changed during security transition")
        print(json.dumps({"action": args.action, "tables_verified": len(report), "content_logged": False}))
        return 0
    except Exception:
        print(json.dumps({"ok": False, "error": "security_transition_verification_failed"}))
        return 1
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
