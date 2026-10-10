"""Шифрование данных владельцем сервиса.

    python -m app.data_crypto generate-key      новый ключ в stdout, больше ничего
    python -m app.data_crypto encrypt-existing  зашифровать старые строки и аудио, перешифровать старым ключом
    python -m app.data_crypto status            сколько значений открытых, зашифрованных, со старым ключом

Команды печатают только числа. Тексты, имена файлов и ключи в вывод не попадают.
"""

import argparse
import os
import stat
import sys
import tempfile
from pathlib import Path

from sqlalchemy import select

from app import crypto, models  # noqa: F401  (модели нужны, чтобы метаданные знали все таблицы)
from app.audio_storage import audio_aad
from app.config import Settings
from app.db import Base, make_database
from app.encrypted_types import EncryptedJSON, encrypted_columns, raw_values

BATCH = 200


def _json_state(value):
    """Для JSON-столбцов открытым считается любое значение, кроме строки enc:v1:..."""
    return crypto.key_state(value) if isinstance(value, str) else "plain"


def _classify(column, value):
    return _json_state(value) if isinstance(column.type, EncryptedJSON) else crypto.key_state(value)


def _target(column, value, label):
    """Новое значение для хранения: открытое шифруем, чужой ключ меняем на текущий."""
    if isinstance(column.type, EncryptedJSON):
        if isinstance(value, str) and crypto.is_encrypted(value):
            return crypto.encrypt_json(crypto.decrypt_json(value, label), label)
        return crypto.encrypt_json(value, label)
    if crypto.is_encrypted(value):
        return crypto.encrypt_text(crypto.decrypt_text(value, label), label)
    return crypto.encrypt_text(value, label)


def _batches(db, table, column, *, lock=True):
    """Страницы по первичному ключу. Строки блокируются, чтобы не затереть правку приложения."""
    primary = table.c["id"]
    last = None
    while True:
        query = select(primary, column).where(column.is_not(None)).order_by(primary).limit(BATCH)
        if last is not None:
            query = query.where(primary > last)
        rows = db.execute(query.with_for_update() if lock else query).all()
        if not rows:
            return
        yield rows
        last = rows[-1][0]


def encrypt_database(sessions):
    """Возвращает {метка: {"encrypted": n, "rotated": n, "skipped": n, "errors": n}}."""
    report = {}
    for table, column, label in encrypted_columns(Base.metadata):
        counts = {"encrypted": 0, "rotated": 0, "skipped": 0, "errors": 0}
        with raw_values(), sessions() as db:
            for rows in _batches(db, table, column):
                for row_id, value in rows:
                    if value is None:
                        continue
                    state = _classify(column, value)
                    if state == "current":
                        counts["skipped"] += 1
                        continue
                    try:
                        new = _target(column, value, label)
                    except crypto.EncryptionError:
                        counts["errors"] += 1
                        continue
                    db.execute(table.update().where(table.c["id"] == row_id).values({column.name: new}))
                    counts["rotated" if state == "old" else "encrypted"] += 1
                db.commit()
        report[label] = counts
    return report


def database_status(sessions):
    report = {}
    for table, column, label in encrypted_columns(Base.metadata):
        counts = {"plain": 0, "current": 0, "old": 0}
        with raw_values(), sessions() as db:
            for rows in _batches(db, table, column, lock=False):
                for _, value in rows:
                    if value is not None:
                        counts[_classify(column, value)] += 1
        report[label] = counts
    return report


def audio_files(root):
    root = Path(root)
    if not root.is_dir():
        return
    for path in sorted(root.glob("*.audio")):
        info = path.lstat()
        if stat.S_ISREG(info.st_mode):
            yield path


def audio_state(path):
    with path.open("rb") as source:
        head = source.read(len(crypto.AUDIO_MAGIC) + crypto.KEY_ID_BYTES)
    if not crypto.is_encrypted_bytes(head):
        return "plain"
    ring = crypto.state().ring
    return "current" if ring is not None and crypto.audio_key_id(head) == ring.current_id else "old"


def encrypt_audio(root):
    counts = {"encrypted": 0, "rotated": 0, "skipped": 0, "errors": 0}
    for path in audio_files(root):
        state = audio_state(path)
        if state == "current":
            counts["skipped"] += 1
            continue
        aad = audio_aad(path.name)
        try:
            plain = crypto.decrypt_bytes(path.read_bytes(), aad)
            sealed = crypto.encrypt_bytes(plain, aad)
        except (OSError, crypto.EncryptionError):
            counts["errors"] += 1
            continue
        fd, name = tempfile.mkstemp(prefix=".sealed-", dir=path.parent)
        temp = Path(name)
        try:
            with os.fdopen(fd, "wb") as target:
                target.write(sealed)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temp, path)
        except OSError:
            temp.unlink(missing_ok=True)
            counts["errors"] += 1
            continue
        counts["rotated" if state == "old" else "encrypted"] += 1
    return counts


def audio_status(root):
    counts = {"plain": 0, "current": 0, "old": 0}
    for path in audio_files(root):
        try:
            counts[audio_state(path)] += 1
        except OSError:
            pass
    return counts


def print_counts(title, counts):
    print(title + " " + " ".join(f"{name}={number}" for name, number in counts.items()))


def run_generate_key(_settings):
    print(crypto.generate_key())
    return 0


def run_encrypt_existing(settings):
    if not crypto.writes_encrypted():
        print("Нужен NOTES_DATA_ENCRYPTION=required и файл ключа NOTES_DATA_KEY_FILE", file=sys.stderr)
        return 2
    engine, sessions = make_database(settings.database_url)
    try:
        failures = 0
        for label, counts in encrypt_database(sessions).items():
            print_counts(label, counts)
            failures += counts["errors"]
        counts = encrypt_audio(settings.audio_storage_path)
        print_counts("audio", counts)
        failures += counts["errors"]
    finally:
        engine.dispose()
    return 1 if failures else 0


def run_status(settings):
    engine, sessions = make_database(settings.database_url)
    try:
        for label, counts in database_status(sessions).items():
            print_counts(label, counts)
        print_counts("audio", audio_status(settings.audio_storage_path))
    finally:
        engine.dispose()
    return 0


COMMANDS = {
    "generate-key": run_generate_key,
    "encrypt-existing": run_encrypt_existing,
    "status": run_status,
}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Шифрование данных beresta")
    parser.add_argument("command", choices=sorted(COMMANDS))
    args = parser.parse_args(argv)
    if args.command == "generate-key":
        return run_generate_key(None)
    settings = Settings.from_env()
    try:
        crypto.configure_from_settings(settings)
    except crypto.EncryptionConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return COMMANDS[args.command](settings)


if __name__ == "__main__":
    sys.exit(main())
