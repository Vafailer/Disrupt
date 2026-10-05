from urllib.parse import quote_plus

import pytest

from app.config import Settings


def test_database_password_file_builds_postgresql_url(tmp_path, monkeypatch):
    secret = tmp_path / "postgres-password"
    secret.write_text("p@ss:/word\n", encoding="utf-8")
    monkeypatch.delenv("NOTES_DATABASE_URL", raising=False)
    monkeypatch.setenv("NOTES_DATABASE_PASSWORD_FILE", str(secret))
    monkeypatch.setenv("NOTES_DATABASE_USER", "notes")
    monkeypatch.setenv("NOTES_DATABASE_HOST", "db")
    monkeypatch.setenv("NOTES_DATABASE_PORT", "5432")
    monkeypatch.setenv("NOTES_DATABASE_NAME", "notes")
    settings = Settings.from_env()
    assert settings.database_url == f"postgresql+psycopg://notes:{quote_plus('p@ss:/word')}@db:5432/notes"


def test_database_url_and_password_file_are_mutually_exclusive(tmp_path, monkeypatch):
    secret = tmp_path / "postgres-password"
    secret.write_text("password", encoding="utf-8")
    monkeypatch.setenv("NOTES_DATABASE_URL", "sqlite:///test.db")
    monkeypatch.setenv("NOTES_DATABASE_PASSWORD_FILE", str(secret))
    with pytest.raises(ValueError, match="either"):
        Settings.from_env()


def test_service_secret_is_separate_and_not_in_repr(tmp_path, monkeypatch):
    secret = tmp_path / 'internal-secret'
    secret.write_text('test-internal-service-' + 'a' * 32 + '\n', encoding='utf-8')
    monkeypatch.setenv('NOTES_INTERNAL_API_TOKEN_FILE', str(secret))
    monkeypatch.delenv('NOTES_INTERNAL_API_TOKEN', raising=False)
    settings = Settings.from_env()
    assert settings.internal_api_token.startswith('test-internal-service-')
    assert settings.internal_api_token not in repr(settings)
    monkeypatch.setenv('NOTES_INTERNAL_API_TOKEN', 'test-env-service-' + 'b' * 32)
    with pytest.raises(ValueError, match='either'):
        Settings.from_env()


def test_short_service_secret_rejected():
    with pytest.raises(ValueError, match='32'):
        Settings(internal_api_token='too-short')
