"""Типы столбцов SQLAlchemy, которые шифруют значение при записи и расшифровывают при чтении.

Метка столбца вида `notes.markdown` входит в AAD, поэтому значение, скопированное в другой столбец,
не расшифруется. В SQL такие столбцы нельзя сравнивать и искать: случайный nonce даёт разный шифртекст.
"""

from contextlib import contextmanager
from contextvars import ContextVar

from sqlalchemy import JSON, Text
from sqlalchemy.types import TypeDecorator

from app import crypto

_raw = ContextVar("encrypted_columns_raw", default=False)


@contextmanager
def raw_values():
    """Внутри блока значения читаются и пишутся как лежат в базе. Нужно только для app.data_crypto."""
    token = _raw.set(True)
    try:
        yield
    finally:
        _raw.reset(token)


class EncryptedText(TypeDecorator):
    impl = Text
    cache_ok = True

    def __init__(self, label: str):
        super().__init__()
        self.label = label

    def process_bind_param(self, value, dialect):
        if value is None or _raw.get():
            return value
        return crypto.encrypt_text(value, self.label)

    def process_result_value(self, value, dialect):
        if value is None or _raw.get():
            return value
        return crypto.decrypt_text(value, self.label)


class EncryptedJSON(TypeDecorator):
    impl = JSON
    cache_ok = True

    def __init__(self, label: str):
        super().__init__()
        self.label = label

    def process_bind_param(self, value, dialect):
        if value is None or _raw.get():
            return value
        return crypto.encrypt_json(value, self.label)

    def process_result_value(self, value, dialect):
        if value is None or _raw.get():
            return value
        return crypto.decrypt_json(value, self.label)


def encrypted_columns(metadata):
    """Все столбцы с шифрованием: (таблица, столбец, метка, тип)."""
    found = []
    for table in metadata.sorted_tables:
        for column in table.columns:
            if isinstance(column.type, (EncryptedText, EncryptedJSON)):
                found.append((table, column, column.type.label))
    return found
