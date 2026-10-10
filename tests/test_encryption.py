"""Шифрование содержимого на диске. Тестовые ключи создаются в tmp_path, настоящих секретов здесь нет."""

import base64
import hashlib
import io
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from app import crypto, data_crypto, models  # noqa: F401
from app.admin_export import build_archive
from app.admin_metrics import aggregate
from app.audio_storage import AudioStorage, AudioStorageUnavailable
from app.config import Settings
from app.db import Base
from app.encrypted_types import encrypted_columns
from app.main import create_app
from app.models import AssistantRequest, Capture, Item, Note, Revision, User
from app.providers import DEMO_TEXT
from tests.conftest import register
from tests.test_admin_metrics import FILTER, KEY, NOW, seed
from tests.test_audio_storage import wav
from tests.test_notes import ready_note
from tests.test_transcripts import patch, upload

PREFIX = "enc:v1:"


def key_file(tmp_path, name):
    path = tmp_path / name
    path.write_text(crypto.generate_key() + "\n", encoding="ascii")
    return str(path)


def raw(app, sql, **params):
    with app.state.engine.connect() as connection:
        return connection.execute(text(sql), params).all()


# ---- Блок 1. Криптография без базы ----


def test_round_trip_text_json_and_empty_values():
    for value in ("Привет, мир", "", "line\nbreak \u0000 ✓", "x" * 20000):
        sealed = crypto.encrypt_text(value, "notes.markdown")
        assert sealed.startswith(PREFIX) and (value == "" or value not in sealed)
        assert crypto.decrypt_text(sealed, "notes.markdown") == value
    document = {"a": [1, 2, {"б": None}], "в": "текст"}
    sealed = crypto.encrypt_json(document, "revisions.snapshot")
    assert isinstance(sealed, str) and sealed.startswith(PREFIX)
    assert crypto.decrypt_json(sealed, "revisions.snapshot") == document
    assert crypto.encrypt_json(None, "revisions.snapshot") is None


def test_format_key_id_and_fresh_nonce():
    first = crypto.encrypt_text("same", "notes.title")
    second = crypto.encrypt_text("same", "notes.title")
    assert first != second
    prefix, version, key_id, body = first.split(":", 3)
    assert (prefix, version) == ("enc", "v1")
    assert key_id == crypto.state().ring.current_id and len(key_id) == 8
    assert len(base64.urlsafe_b64decode(body)) == 12 + len("same".encode()) + 16


def test_tampered_value_is_refused():
    sealed = crypto.encrypt_text("secret", "notes.markdown")
    head, body = sealed.rsplit(":", 1)
    blob = bytearray(base64.urlsafe_b64decode(body))
    blob[-1] ^= 1
    tampered = head + ":" + base64.urlsafe_b64encode(bytes(blob)).decode()
    with pytest.raises(crypto.DecryptionFailed):
        crypto.decrypt_text(tampered, "notes.markdown")
    for broken in (PREFIX + "short", "enc:v1:" + sealed.split(":")[2] + ":!!!"):
        with pytest.raises(crypto.EncryptionError):
            crypto.decrypt_text(broken, "notes.markdown")


def test_value_copied_to_another_column_does_not_decrypt():
    sealed = crypto.encrypt_text("secret", "notes.markdown")
    with pytest.raises(crypto.DecryptionFailed):
        crypto.decrypt_text(sealed, "notes.title")
    snapshot = crypto.encrypt_json({"a": 1}, "revisions.snapshot")
    with pytest.raises(crypto.DecryptionFailed):
        crypto.decrypt_json(snapshot, "notes.conclusions")


def test_wrong_key_is_refused(tmp_path):
    sealed = crypto.encrypt_text("secret", "notes.markdown")
    crypto.configure("required", key_file(tmp_path, "other.key"))
    with pytest.raises(crypto.KeyUnavailable):
        crypto.decrypt_text(sealed, "notes.markdown")


def test_legacy_plaintext_passes_through_in_both_modes(tmp_path):
    assert crypto.decrypt_text("старый текст", "notes.markdown") == "старый текст"
    assert crypto.decrypt_json({"a": 1}, "revisions.snapshot") == {"a": 1}
    assert crypto.decrypt_json([1], "notes.conclusions") == [1]
    crypto.configure("off")
    assert crypto.decrypt_text("старый текст", "notes.markdown") == "старый текст"
    assert crypto.encrypt_text("открыто", "notes.markdown") == "открыто"
    assert crypto.encrypt_json({"a": 1}, "revisions.snapshot") == {"a": 1}


def test_encrypted_value_without_key_raises_clear_error():
    sealed = crypto.encrypt_text("secret", "notes.markdown")
    crypto.configure("off")
    with pytest.raises(crypto.KeyUnavailable, match="NOTES_DATA_KEY_FILE"):
        crypto.decrypt_text(sealed, "notes.markdown")


def test_off_mode_with_key_reads_encrypted_but_writes_plaintext(data_key_file):
    sealed = crypto.encrypt_text("secret", "notes.markdown")
    crypto.configure("off", str(data_key_file))
    assert crypto.decrypt_text(sealed, "notes.markdown") == "secret"
    assert crypto.encrypt_text("secret", "notes.markdown") == "secret"
    assert not crypto.writes_encrypted()


def test_rotation_reads_old_key_and_writes_with_current(tmp_path, data_key_file):
    old_id = crypto.state().ring.current_id
    sealed = crypto.encrypt_text("secret", "notes.markdown")
    new_file = key_file(tmp_path, "new.key")
    crypto.configure("required", new_file, str(data_key_file))
    assert crypto.state().ring.current_id != old_id
    assert crypto.decrypt_text(sealed, "notes.markdown") == "secret"
    assert crypto.key_state(sealed) == "old"
    fresh = crypto.encrypt_text("secret", "notes.markdown")
    assert crypto.key_state(fresh) == "current" and fresh.split(":")[2] == crypto.state().ring.current_id
    assert crypto.key_state("открытый") == "plain"
    crypto.configure("required", new_file, " , " + str(data_key_file) + " ,")
    assert crypto.decrypt_text(sealed, "notes.markdown") == "secret"


def test_key_id_is_first_hex_of_sha256(data_key_file):
    key = base64.b64decode(data_key_file.read_text().strip())
    assert len(key) == 32
    assert crypto.state().ring.current_id == hashlib.sha256(key).hexdigest()[:8]


def test_generated_key_is_32_random_bytes():
    first, second = crypto.generate_key(), crypto.generate_key()
    assert first != second and len(base64.b64decode(first)) == 32


def test_required_without_valid_key_file_fails(tmp_path):
    with pytest.raises(crypto.EncryptionConfigError):
        crypto.configure("required")
    with pytest.raises(crypto.EncryptionConfigError):
        crypto.configure("required", str(tmp_path / "missing.key"))
    short = tmp_path / "short.key"
    short.write_text(base64.b64encode(b"x" * 16).decode())
    with pytest.raises(crypto.EncryptionConfigError):
        crypto.configure("required", str(short))
    garbage = tmp_path / "garbage.key"
    garbage.write_text("не base64 !!!", encoding="utf-8")
    with pytest.raises(crypto.EncryptionConfigError):
        crypto.configure("required", str(garbage))
    with pytest.raises(crypto.EncryptionConfigError):
        crypto.configure("sometimes", key_file(tmp_path, "ok.key"))
    with pytest.raises(crypto.EncryptionConfigError):
        crypto.configure("required", key_file(tmp_path, "ok2.key"), str(tmp_path / "missing-old.key"))


def test_error_messages_do_not_contain_key_or_text(data_key_file):
    secret_text = "очень личная мысль"
    sealed = crypto.encrypt_text(secret_text, "notes.markdown")
    with pytest.raises(crypto.DecryptionFailed) as caught:
        crypto.decrypt_text(sealed, "notes.title")
    message = str(caught.value)
    assert secret_text not in message and data_key_file.read_text().strip() not in message


def test_audio_bytes_round_trip_tamper_and_aad():
    data = wav()
    sealed = crypto.encrypt_bytes(data, "audio:a.audio")
    assert sealed.startswith(crypto.AUDIO_MAGIC) and data not in sealed
    assert crypto.decrypt_bytes(sealed, "audio:a.audio") == data
    with pytest.raises(crypto.DecryptionFailed):
        crypto.decrypt_bytes(sealed, "audio:b.audio")
    with pytest.raises(crypto.DecryptionFailed):
        crypto.decrypt_bytes(sealed[:-1] + bytes([sealed[-1] ^ 1]), "audio:a.audio")
    assert crypto.decrypt_bytes(data, "audio:a.audio") == data  # Старый открытый файл.


def test_settings_and_startup_require_a_valid_key(tmp_path, data_key_file):
    with pytest.raises(ValueError):
        Settings(data_encryption="maybe")
    url = "sqlite:///" + (tmp_path / "startup.db").as_posix()
    with pytest.raises(crypto.EncryptionConfigError):
        create_app(Settings(database_url=url, auto_worker=False, data_encryption="required"))
    with pytest.raises(crypto.EncryptionConfigError):
        create_app(Settings(
            database_url=url, auto_worker=False, data_encryption="required",
            data_key_file=str(tmp_path / "missing.key"),
        ))
    app = create_app(Settings(
        database_url=url, auto_worker=False, data_encryption="required", data_key_file=str(data_key_file),
        audio_storage_path=str(tmp_path / "startup-audio"),
    ))
    app.state.engine.dispose()
    assert crypto.writes_encrypted()


def test_settings_read_encryption_from_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("NOTES_DATA_ENCRYPTION", "required")
    monkeypatch.setenv("NOTES_DATA_KEY_FILE", "/run/secrets/data_key")
    monkeypatch.setenv("NOTES_DATA_OLD_KEY_FILES", "/a,/b")
    settings = Settings.from_env()
    assert (settings.data_encryption, settings.data_key_file, settings.data_old_key_files) == (
        "required", "/run/secrets/data_key", "/a,/b",
    )
    monkeypatch.delenv("NOTES_DATA_ENCRYPTION")
    assert Settings.from_env().data_encryption == "off"


# ---- Блок 2. Столбцы ----

EXPECTED_COLUMNS = {
    "captures.original_text", "captures.transcript", "transcript_revisions.text", "notes.title", "notes.markdown",
    "notes.conclusions", "revisions.snapshot", "items.text", "items.source_quote", "items.due_text",
    "reminders.text", "inbox.response", "outbox.message_text", "assistant_requests.question",
    "assistant_requests.result", "admin_accounts.totp_secret", "feedback.subject", "feedback.description",
    "feedback.steps", "feedback.expected", "feedback.contact",
}


def test_encrypted_column_list_and_labels():
    found = encrypted_columns(Base.metadata)
    assert {label for _, _, label in found} == EXPECTED_COLUMNS
    for table, column, label in found:
        assert label == f"{table.name}.{column.name}"


def test_ids_statuses_names_and_hashes_stay_plain():
    plain = {"id", "username", "status", "name", "kind", "payload_hash", "request_hash", "token_hash", "password_hash"}
    encrypted = {column.name for _, column, _ in encrypted_columns(Base.metadata)}
    assert not plain & encrypted


# ---- Блок 3. Через API ----


def test_note_is_encrypted_in_database_and_readable_through_api(app, client):
    register(client)
    note = ready_note(app, client)
    assert note["original_text"] == DEMO_TEXT and note["title"] == "Запуск проекта"
    ((title, markdown, conclusions),) = raw(app, "SELECT title, markdown, conclusions FROM notes")
    assert title.startswith(PREFIX) and markdown.startswith(PREFIX)
    assert "Запуск проекта" not in title and "Оплата" not in markdown
    assert "Запуск в пятницу" not in conclusions and PREFIX in conclusions
    ((original,),) = raw(app, "SELECT original_text FROM captures")
    assert original.startswith(PREFIX) and "оплату" not in original
    for (value,) in raw(app, "SELECT text FROM items"):
        assert value.startswith(PREFIX) and "Дизайн" not in value
    for (value,) in raw(app, "SELECT snapshot FROM revisions"):
        assert PREFIX in value and "Запуск" not in value
    for label, counts in data_crypto.database_status(app.state.sessions).items():
        assert counts["plain"] == 0 and counts["old"] == 0, label


def test_edits_stay_encrypted(app, client):
    register(client)
    note = ready_note(app, client)
    changed = client.patch(
        "/api/v1/notes/" + note["id"], json={"version": 1, "title": "Новое название", "markdown": "Новый текст"},
    )
    assert changed.status_code == 200, changed.text
    ((title, markdown),) = raw(app, "SELECT title, markdown FROM notes")
    assert title.startswith(PREFIX) and markdown.startswith(PREFIX)
    assert "Новое" not in title + markdown
    assert client.get("/api/v1/notes/" + note["id"]).json()["title"] == "Новое название"


def test_search_finds_words_in_every_encrypted_field(app, client):
    register(client)
    note = ready_note(app, client)

    def found(query, **params):
        response = client.get("/api/v1/notes", params={"q": query, **params})
        assert response.status_code == 200, response.text
        return [row["id"] for row in response.json()]

    assert found("ОПЛАТУ") == [note["id"]]  # Исходный текст.
    assert found("запуск проекта") == [note["id"]]  # Название.
    assert found("Предварительный срок") == [note["id"]]  # Текст заметки.
    assert found("уточнит ситуацию") == [note["id"]]  # Текст заметки и пункт.
    assert found("%") == [] and found("_") == []
    assert found("такого слова нет") == []
    assert found("оплату", channel="telegram") == []
    assert found("оплату", input_kind="text") == [note["id"]]
    assert found("   ") == [note["id"]]  # Пустой поиск не фильтрует.
    client.patch("/api/v1/notes/" + note["id"], json={"version": 1, "title": "Редкаятема", "markdown": "Тело"})
    assert found("РЕДКАЯТЕМА") == [note["id"]]
    assert found("предварительный срок") == []  # Старый текст заметки больше не находится.


def test_search_finds_word_in_transcript_and_keeps_owner_isolation(app, client):
    capture_id, _ = upload(app, client, process=True)
    assert patch(client, capture_id, text="Исправленная расшифровка про велосипеды").status_code == 200
    found = client.get("/api/v1/notes", params={"q": "ВЕЛОСИПЕДЫ"}).json()
    assert len(found) == 1 and found[0]["input_kind"] == "audio"
    ((transcript, original),) = raw(app, "SELECT transcript, original_text FROM captures")
    assert transcript.startswith(PREFIX) and original.startswith(PREFIX)
    ((revision,),) = raw(app, "SELECT text FROM transcript_revisions WHERE version = 2")
    assert revision.startswith(PREFIX) and "велосипеды" not in revision
    with TestClient(app) as other:
        register(other, "other")
        assert other.get("/api/v1/notes", params={"q": "велосипеды"}).json() == []


def test_search_paging_and_order_are_kept(app, client):
    register(client)
    ids = []
    for number in range(3):
        note = ready_note_with_text(app, client, f"Самокат номер {number}", f"key-{number}")
        ids.append(note)
    first = client.get("/api/v1/notes", params={"q": "самокат", "limit": 2})
    assert [row["id"] for row in first.json()] == ids[::-1][:2]
    assert first.headers["X-Next-Notes-Offset"] == "2"
    second = client.get("/api/v1/notes", params={"q": "самокат", "limit": 2, "offset": 2})
    assert [row["id"] for row in second.json()] == ids[::-1][2:]
    assert "X-Next-Notes-Offset" not in second.headers


def ready_note_with_text(app, client, text_value, key):
    from app.worker import Worker

    response = client.post("/api/v1/captures/text", json={"text": text_value}, headers={"Idempotency-Key": key})
    assert response.status_code == 202, response.text
    Worker(app.state.sessions, app.state.settings).run_once()
    job = client.get("/api/v1/jobs/" + response.json()["id"]).json()
    assert job["status"] == "succeeded", job
    return job["note_id"]


def test_value_moved_to_another_column_fails_to_read(app, client):
    register(client)
    ready_note(app, client)
    ((markdown,),) = raw(app, "SELECT markdown FROM notes")
    with app.state.engine.begin() as connection:
        connection.execute(text("UPDATE notes SET title = :value"), {"value": markdown})
    with app.state.sessions() as db:
        with pytest.raises(crypto.DecryptionFailed):
            db.scalars(select(Note)).all()


def test_off_mode_stores_plaintext_and_reads_both(app_factory):
    app = app_factory(data_encryption="off", data_key_file="")
    assert not crypto.writes_encrypted()
    with TestClient(app) as client:
        register(client)
        note = ready_note(app, client)
        ((title, markdown),) = raw(app, "SELECT title, markdown FROM notes")
        assert title == "Запуск проекта" and not markdown.startswith(PREFIX)
        assert client.get("/api/v1/notes/" + note["id"]).json()["markdown"] == markdown


def test_encrypted_data_without_key_gives_clear_error(app, client):
    register(client)
    ready_note(app, client)
    crypto.configure("off")
    with app.state.sessions() as db:
        with pytest.raises(crypto.KeyUnavailable, match="NOTES_DATA_KEY_FILE"):
            db.scalars(select(Note)).all()


def test_admin_metrics_and_export_do_not_decrypt_content(app):
    seed(app.state.sessions)
    ((stored,),) = raw(app, "SELECT markdown FROM notes LIMIT 1")
    assert stored.startswith(PREFIX)
    crypto.reset()  # Без ключа любое чтение зашифрованного значения даёт ошибку.
    with app.state.sessions() as db:
        aggregate(db, FILTER["from"], FILTER["to"], "all", "all", KEY, now=NOW)
        build_archive(db, FILTER["from"], FILTER["to"], KEY, now=NOW)


# ---- Блок 4. Аудио ----


def test_audio_file_is_ciphertext_and_playback_returns_original(app, client):
    capture_id, _ = upload(app, client)
    root = app.state.audio_storage.root
    (path,) = list(root.glob("*.audio"))
    blob = path.read_bytes()
    assert blob.startswith(crypto.AUDIO_MAGIC) and wav() not in blob and b"RIFF" not in blob
    assert not [item for item in root.iterdir() if item.name.startswith(".")]
    assert path.stat().st_mode & 0o777 == 0o600
    assert client.get(f"/api/v1/captures/{capture_id}/audio").content == wav()
    with app.state.sessions() as db:
        capture = db.get(Capture, capture_id)
        assert capture.audio_sha256 == hashlib.sha256(wav()).hexdigest()
        assert capture.audio_bytes == len(wav())


def test_worker_transcribes_encrypted_audio(app, client):
    capture_id, _ = upload(app, client, process=True)
    assert client.get(f"/api/v1/captures/{capture_id}").json()["transcript"] == DEMO_TEXT


def publish(storage, data):
    capture_id = str(uuid.uuid4())
    with storage.stage(io.BytesIO(data)) as staged:
        info = storage.inspect(staged)
        return storage.publish(staged, capture_id, info)


def test_storage_refuses_tampered_or_moved_audio(tmp_path):
    storage = AudioStorage(tmp_path / "vault")
    saved = publish(storage, wav())
    path = storage.root / saved.key
    with storage.open_original(saved.key) as source:
        assert source.read() == wav()
    moved = storage.root / (str(uuid.uuid4()) + ".audio")
    moved.write_bytes(path.read_bytes())
    with pytest.raises(AudioStorageUnavailable):
        with storage.open_original(moved.name):
            pass
    blob = bytearray(path.read_bytes())
    blob[-1] ^= 1
    path.write_bytes(bytes(blob))
    with pytest.raises(AudioStorageUnavailable):
        with storage.open_original(saved.key):
            pass


def test_legacy_plain_audio_still_plays(tmp_path):
    storage = AudioStorage(tmp_path / "legacy")
    key = str(uuid.uuid4()) + ".audio"
    (storage.root / key).write_bytes(wav())
    with AudioStorage(storage.root, read_only=True).open_original(key) as source:
        assert source.read() == wav()


def test_off_mode_keeps_audio_plain_and_encrypted_audio_needs_key(tmp_path):
    storage = AudioStorage(tmp_path / "modes")
    sealed = publish(storage, wav())
    crypto.configure("off")
    plain = publish(storage, wav())
    assert (storage.root / plain.key).read_bytes() == wav()
    with storage.open_original(plain.key) as source:
        assert source.read() == wav()
    with pytest.raises(AudioStorageUnavailable):
        with storage.open_original(sealed.key):
            pass


# ---- Блок 5. CLI ----


def legacy_rows(app):
    """Строки, какими они были до шифрования."""
    crypto.configure("off")
    with app.state.sessions() as db:
        user = User(username="legacy-owner", password_hash="not-a-real-hash")
        db.add(user)
        db.flush()
        capture = Capture(
            user_id=user.id, original_text="старый секретный текст", idempotency_key="legacy-1",
            transcript="старая расшифровка",
        )
        db.add(capture)
        db.flush()
        note = Note(
            capture_id=capture.id, user_id=user.id, title="старое название", markdown="старый секретный markdown",
            conclusions=[{"id": "c1", "text": "старый секретный вывод"}], provider="mock",
        )
        db.add(note)
        db.flush()
        db.add(Item(user_id=user.id, note_id=note.id, kind="note", text="старый секретный пункт"))
        db.add(Revision(note_id=note.id, version=1, snapshot={"title": "старый секретный снимок"}))
        db.add(AssistantRequest(
            user_id=user.id, kind="ask", question="старый секретный вопрос", input_note_ids=[],
            result={"answer": "старый секретный ответ"}, idempotency_key="legacy-ask", request_hash="h" * 64,
        ))
        db.commit()
        return note.id


def dump(app):
    return raw(
        app,
        "SELECT n.title, n.markdown, n.conclusions, c.original_text, c.transcript, i.text, r.snapshot, "
        "a.question, a.result FROM notes n, captures c, items i, revisions r, assistant_requests a",
    )[0]


@pytest.fixture
def cli(app_factory, tmp_path, monkeypatch, capsys):
    app = app_factory()
    monkeypatch.setenv("NOTES_AUDIO_STORAGE_PATH", str(tmp_path / "audio"))

    def run(*arguments):
        capsys.readouterr()
        code = data_crypto.main(list(arguments))
        return code, capsys.readouterr()

    return app, run


def test_generate_key_prints_only_the_key(capsys):
    assert data_crypto.main(["generate-key"]) == 0
    output = capsys.readouterr().out
    assert output.count("\n") == 1 and len(base64.b64decode(output.strip())) == 32


def test_encrypt_existing_converts_legacy_rows_and_audio_and_is_idempotent(cli, tmp_path, data_key_file):
    app, run = cli
    legacy_rows(app)
    audio_root = tmp_path / "audio"
    audio_root.mkdir(exist_ok=True)
    legacy_audio = audio_root / (str(uuid.uuid4()) + ".audio")
    legacy_audio.write_bytes(wav())
    before = dump(app)
    assert all(not value.startswith(PREFIX) for value in before)
    crypto.configure("required", str(data_key_file))

    code, shown = run("status")
    assert code == 0 and "notes.markdown plain=1 current=0 old=0" in shown.out and "audio plain=1" in shown.out

    code, shown = run("encrypt-existing")
    assert code == 0, shown
    assert "notes.markdown encrypted=1 rotated=0 skipped=0 errors=0" in shown.out
    assert "audio encrypted=1" in shown.out
    assert "секрет" not in shown.out + shown.err and data_key_file.read_text().strip() not in shown.out + shown.err
    after = dump(app)
    assert all(PREFIX in value for value in after)
    assert not any("старый" in value or "секретн" in value for value in after)
    assert legacy_audio.read_bytes().startswith(crypto.AUDIO_MAGIC)
    with AudioStorage(audio_root, read_only=True).open_original(legacy_audio.name) as source:
        assert source.read() == wav()
    with app.state.sessions() as db:
        note = db.scalar(select(Note))
        assert (note.title, note.markdown) == ("старое название", "старый секретный markdown")
        assert note.conclusions == [{"id": "c1", "text": "старый секретный вывод"}]
        assert db.scalar(select(AssistantRequest)).result == {"answer": "старый секретный ответ"}

    sealed_audio = legacy_audio.read_bytes()
    code, shown = run("encrypt-existing")
    assert code == 0
    assert "notes.markdown encrypted=0 rotated=0 skipped=1 errors=0" in shown.out
    assert dump(app) == after and legacy_audio.read_bytes() == sealed_audio

    code, shown = run("status")
    assert "notes.markdown plain=0 current=1 old=0" in shown.out and "audio plain=0 current=1 old=0" in shown.out


def test_encrypt_existing_rotates_to_the_current_key(cli, tmp_path, data_key_file, monkeypatch):
    app, run = cli
    legacy_rows(app)
    audio_root = tmp_path / "audio"
    audio_root.mkdir(exist_ok=True)
    audio = audio_root / (str(uuid.uuid4()) + ".audio")
    audio.write_bytes(wav())
    crypto.configure("required", str(data_key_file))
    assert run("encrypt-existing")[0] == 0
    old_dump, old_audio = dump(app), audio.read_bytes()

    new_file = key_file(tmp_path, "rotated.key")
    monkeypatch.setenv("NOTES_DATA_KEY_FILE", new_file)
    monkeypatch.setenv("NOTES_DATA_OLD_KEY_FILES", str(data_key_file))
    code, shown = run("status")
    assert "notes.markdown plain=0 current=0 old=1" in shown.out and "audio plain=0 current=0 old=1" in shown.out
    code, shown = run("encrypt-existing")
    assert code == 0 and "notes.markdown encrypted=0 rotated=1" in shown.out and "audio encrypted=0 rotated=1" in shown.out
    assert dump(app) != old_dump and audio.read_bytes() != old_audio

    # Старый ключ больше не нужен.
    monkeypatch.delenv("NOTES_DATA_OLD_KEY_FILES")
    crypto.configure("required", new_file)
    with app.state.sessions() as db:
        assert db.scalar(select(Note)).markdown == "старый секретный markdown"
    with AudioStorage(audio_root, read_only=True).open_original(audio.name) as source:
        assert source.read() == wav()
    code, shown = run("status")
    assert "notes.markdown plain=0 current=1 old=0" in shown.out


def test_encrypt_existing_refuses_without_required_mode(cli, monkeypatch):
    app, run = cli
    monkeypatch.setenv("NOTES_DATA_ENCRYPTION", "off")
    code, shown = run("encrypt-existing")
    assert code == 2 and "required" in shown.err
    monkeypatch.setenv("NOTES_DATA_ENCRYPTION", "required")
    monkeypatch.delenv("NOTES_DATA_KEY_FILE")
    code, shown = run("status")
    assert code == 2


def test_status_counts_only_numbers(cli, data_key_file):
    app, run = cli
    legacy_rows(app)
    crypto.configure("required", str(data_key_file))
    code, shown = run("status")
    assert code == 0
    assert "старый" not in shown.out and "legacy" not in shown.out
    labels = {line.split()[0] for line in shown.out.splitlines()}
    assert EXPECTED_COLUMNS <= labels and "audio" in labels
