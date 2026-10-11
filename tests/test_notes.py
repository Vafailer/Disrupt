import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models import Capture, Job, LoginSession, Note
from app.providers import DEMO_TEXT, MockProvider, ProviderError
from app.worker import Worker
from tests.conftest import register


def submit(client, text=DEMO_TEXT, key="request-1"):
    return client.post("/api/v1/captures/text", json={"text": text}, headers={"Idempotency-Key": key})


def ready_note(app, client):
    response = submit(client)
    assert response.status_code == 202, response.text
    Worker(app.state.sessions, app.state.settings).run_once()
    job = client.get("/api/v1/jobs/" + response.json()["id"]).json()
    assert job["status"] == "succeeded", job
    return client.get("/api/v1/notes/" + job["note_id"]).json()


@pytest.mark.parametrize("name", ["Ab3", "User.Name_123-X", "A" * 64])
def test_ascii_username_can_register_and_log_in(client, name):
    account = register(client, name)
    assert account["username"] == name.lower()
    assert client.post("/api/v1/auth/logout").status_code == 204
    response = client.post(
        "/api/v1/auth/login",
        json={"username": name.swapcase(), "password": "test-only-password-123"},
    )
    assert response.status_code == 200
    assert response.json()["username"] == name.lower()


def test_full_note_lifecycle_and_restart(app, client, app_factory):
    register(client)
    note = ready_note(app, client)
    assert note["original_text"] == DEMO_TEXT
    assert note["provider"] == "mock"
    assert note["conclusions"][0]["status"] == "proposed"
    path = "/api/v1/notes/" + note["id"]
    changed = client.patch(
        path, json={"version": 1, "title": "Моя редакция", "markdown": "## План\n\nПозвонить"}
    )
    assert changed.status_code == 200
    conclusion_id = note["conclusions"][0]["id"]
    accepted = client.patch(path + "/conclusions/" + conclusion_id, json={"version": 2, "status": "accepted"})
    assert accepted.status_code == 200
    assert accepted.json()["version"] == 3
    assert accepted.json()["original_text"] == DEMO_TEXT
    history = client.get(path + "/revisions").json()
    assert [r["version"] for r in history] == [3, 2, 1]
    assert history[-1]["conclusions"][0]["status"] == "proposed"
    with TestClient(app_factory()) as restarted:
        restarted.cookies.update(client.cookies)
        restored = restarted.get(path).json()
        assert restored["title"] == "Моя редакция"
        assert restored["conclusions"][0]["status"] == "accepted"
        assert restored["original_text"] == DEMO_TEXT


def test_duplicate_request_and_conflicting_body(app, client):
    register(client)
    first, duplicate = submit(client), submit(client)
    assert first.json()["id"] == duplicate.json()["id"]
    assert submit(client, text="Другой текст").status_code == 409
    Worker(app.state.sessions, app.state.settings).run_once()
    assert submit(client).json()["status"] == "succeeded"
    with app.state.sessions() as db:
        assert len(db.scalars(select(Capture)).all()) == 1
        assert len(db.scalars(select(Note)).all()) == 1


def test_isolation_for_every_resource(app, client):
    register(client, "owner")
    note = ready_note(app, client)
    job_id = client.get("/api/v1/jobs").json()[0]["id"]
    path = "/api/v1/notes/" + note["id"]
    with TestClient(app) as other:
        register(other, "other")
        assert other.get("/api/v1/notes").json() == []
        assert other.get("/api/v1/jobs").json() == []
        for url in [path, path + "/revisions", "/api/v1/jobs/" + job_id]:
            assert other.get(url).status_code == 404
        assert other.patch(path, json={"version": 1, "title": "x", "markdown": "x"}).status_code == 404
        assert (
            other.patch(
                path + "/conclusions/" + note["conclusions"][0]["id"],
                json={"version": 1, "status": "accepted"},
            ).status_code
            == 404
        )
        assert submit(other).status_code == 202  # Idempotency keys are scoped to the owner.
    assert client.get(path).json()["version"] == 1


def test_version_conflict_does_not_overwrite(app, client):
    register(client)
    note = ready_note(app, client)
    path = "/api/v1/notes/" + note["id"]
    assert (
        client.patch(path, json={"version": 1, "title": "Первая правка", "markdown": "Смысл"}).status_code
        == 200
    )
    assert (
        client.patch(path, json={"version": 1, "title": "Устаревшая", "markdown": "Потеря"}).status_code
        == 409
    )
    assert (
        client.patch(
            path + "/conclusions/" + note["conclusions"][0]["id"], json={"version": 1, "status": "rejected"}
        ).status_code
        == 409
    )
    assert client.get(path).json()["title"] == "Первая правка"
    assert len(client.get(path + "/revisions").json()) == 2


@pytest.mark.parametrize(
    "body",
    [
        {"text": " \n "},
        {"text": "x" * 12001},
        {"text": "x", "user_id": "attacker"},
        {"text": 123},
        {"text": "a\x00b"},
    ],
)
def test_invalid_input(client, body):
    register(client)
    assert (
        client.post("/api/v1/captures/text", json=body, headers={"Idempotency-Key": "a"}).status_code == 422
    )
    assert client.get("/api/v1/jobs").json() == []


def test_request_body_size_limit(client):
    assert client.post("/api/v1/auth/login", content=b"x" * 131073).status_code == 413


def test_empty_conclusions_and_exact_original(app, client):
    register(client)
    original = "  Мысль без уверенного вывода.\n\n  Ещё одна.  "
    job = submit(client, text=original).json()
    Worker(app.state.sessions, app.state.settings).run_once()
    note_id = client.get("/api/v1/jobs/" + job["id"]).json()["note_id"]
    note = client.get("/api/v1/notes/" + note_id).json()
    assert note["original_text"] == original
    assert note["conclusions"] == []


def test_error_preserves_original_without_retry(app, client):
    class Failing:
        calls = 0

        def structure(self, text, *, categories=()):
            self.calls += 1
            raise ProviderError("provider_timeout_unknown")

    register(client)
    job = submit(client).json()
    provider = Failing()
    worker = Worker(app.state.sessions, app.state.settings, provider)
    assert worker.run_once()
    assert not worker.run_once()
    after = client.get("/api/v1/jobs/" + job["id"]).json()
    assert after["status"] == "failed"
    assert after["error_code"] == "provider_timeout_unknown"
    assert after["original_text"] == DEMO_TEXT
    assert after["note_id"] is None
    assert provider.calls == 1


def test_ungrounded_quote_is_rejected(app, client):
    class BadProvider:
        def structure(self, text, *, categories=()):
            note = MockProvider().structure(DEMO_TEXT)
            note.conclusions[0].source_quote = "Этого никто не писал"
            return note

    register(client)
    job = submit(client).json()
    Worker(app.state.sessions, app.state.settings, BadProvider()).run_once()
    assert client.get("/api/v1/jobs/" + job["id"]).json()["error_code"] == "ungrounded_quote"
    assert client.get("/api/v1/notes").json() == []


def test_expired_running_job_never_replayed(app, client):
    register(client)
    job = submit(client).json()
    with app.state.sessions() as db:
        row = db.get(Job, job["id"])
        row.status, row.lease_until = "running", time.time() - 1
        db.commit()
    assert not Worker(app.state.sessions, app.state.settings).run_once()
    after = client.get("/api/v1/jobs/" + job["id"]).json()
    assert after["error_code"] == "execution_unknown"
    assert after["original_text"] == DEMO_TEXT


def test_queued_job_survives_restart(app, client, app_factory):
    register(client)
    job = submit(client).json()
    restarted = app_factory()
    assert Worker(restarted.state.sessions, restarted.state.settings).run_once()
    assert client.get("/api/v1/jobs/" + job["id"]).json()["status"] == "succeeded"


def test_two_workers_do_not_process_same_job(app, client):
    register(client)
    submit(client)
    workers = [Worker(app.state.sessions, app.state.settings) for _ in range(2)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        result = list(pool.map(lambda w: w.run_once(), workers))
    assert result.count(True) == 1
    assert len(client.get("/api/v1/notes").json()) == 1


def test_csrf_origin_logout_and_expiry(app, client):
    assert client.get("/api/v1/notes").status_code == 401
    assert (
        client.post(
            "/api/v1/auth/register",
            json={"username": "evil", "password": "long-password"},
            headers={"Origin": "https://evil.example"},
        ).status_code
        == 403
    )
    register(client)
    assert submit(client).status_code == 202
    assert (
        client.post(
            "/api/v1/captures/text",
            json={"text": "x"},
            headers={"Idempotency-Key": "b", "X-CSRF-Token": "wrong"},
        ).status_code
        == 403
    )
    assert client.post("/api/v1/auth/logout", headers={"Origin": "https://evil.example"}).status_code == 403
    with app.state.sessions() as db:
        session = db.scalar(select(LoginSession))
        session.expires_at = time.time() - 1
        db.commit()
    assert client.get("/api/v1/auth/me").status_code == 401
    login = client.post(
        "/api/v1/auth/login", json={"username": "tester", "password": "test-only-password-123"}
    )
    assert login.status_code == 200
    assert "HttpOnly" in login.headers["set-cookie"]
    assert "SameSite=lax" in login.headers["set-cookie"]
    client.headers["X-CSRF-Token"] = login.json()["csrf_token"]
    assert client.post("/api/v1/auth/logout").status_code == 204
    assert client.get("/api/v1/auth/me").status_code == 401


def test_login_throttled_and_password_never_echoed(client):
    register(client)
    for _ in range(10):
        assert (
            client.post(
                "/api/v1/auth/login", json={"username": "tester", "password": "incorrect-password"}
            ).status_code
            == 401
        )
    assert (
        client.post(
            "/api/v1/auth/login", json={"username": "tester", "password": "incorrect-password"}
        ).status_code
        == 429
    )
    response = client.post("/api/v1/auth/login", json={"username": "tester", "password": "short"})
    assert response.status_code == 422
    assert "short" not in response.text


def test_queue_limit(client, app):
    register(client)
    for i in range(app.state.settings.max_pending_per_user):
        assert submit(client, key=f"key-{i}").status_code == 202
    assert submit(client, key="overflow").status_code == 429
    assert submit(client, key="key-0").status_code == 202


def test_static_page_and_health(client):
    assert client.get("/health").json()["simulation"] is True
    assert client.get("/").status_code == 200
    assert "default-src 'self'" in client.get("/").headers["content-security-policy"]
    assert client.get("/static/app.js").status_code == 200


def test_api_docs_are_closed_by_default_and_open_on_request(app_factory):
    paths = ("/docs", "/redoc", "/openapi.json")
    with TestClient(app_factory()) as closed:
        assert [closed.get(path).status_code for path in paths] == [404, 404, 404]
    with TestClient(app_factory()) as default:
        # The contract export still works without the public route.
        assert default.app.openapi()["info"]["version"] == "0.1.0"
    with TestClient(app_factory(api_docs_enabled=True)) as opened:
        assert opened.get("/openapi.json").json()["info"]["version"] == "0.1.0"
        assert opened.get("/docs").status_code == 200


def make_sources(app, client):
    texts = {"web": "Обычная запись из браузера", "tg": "Запись из бота Telegram", "voice": "Голос из бота Telegram"}
    notes = {}
    for name, text in texts.items():
        job = submit(client, text, key="source-" + name).json()
        Worker(app.state.sessions, app.state.settings).run_once()
        notes[name] = client.get("/api/v1/jobs/" + job["id"]).json()
    with app.state.sessions() as db:
        for name, channel, kind in [("tg", "telegram", "text"), ("voice", "telegram", "audio")]:
            capture = db.get(Capture, notes[name]["capture_id"])
            capture.channel, capture.input_kind = channel, kind
        db.commit()
    return {name: note["note_id"] for name, note in notes.items()}


def test_note_list_and_detail_expose_source(app, client):
    register(client)
    ids = make_sources(app, client)
    rows = {row["id"]: row for row in client.get("/api/v1/notes").json()}
    assert (rows[ids["web"]]["channel"], rows[ids["web"]]["input_kind"]) == ("web", "text")
    assert (rows[ids["tg"]]["channel"], rows[ids["tg"]]["input_kind"]) == ("telegram", "text")
    assert (rows[ids["voice"]]["channel"], rows[ids["voice"]]["input_kind"]) == ("telegram", "audio")
    detail = client.get("/api/v1/notes/" + ids["tg"]).json()
    assert detail["channel"] == "telegram" and detail["input_kind"] == "text"
    assert client.get("/api/v1/notes/" + ids["web"]).json()["channel"] == "web"


def listed(client, **params):
    response = client.get("/api/v1/notes", params=params)
    assert response.status_code == 200, response.text
    return {row["id"] for row in response.json()}


def test_note_list_filters_by_channel_and_kind(app, client):
    register(client)
    ids = make_sources(app, client)
    assert listed(client, channel="telegram") == {ids["tg"], ids["voice"]}
    assert listed(client, channel="web") == {ids["web"]}
    assert listed(client, input_kind="audio") == {ids["voice"]}
    assert listed(client, input_kind="text") == {ids["web"], ids["tg"]}
    assert listed(client, channel="telegram", input_kind="text") == {ids["tg"]}
    assert listed(client, channel="web", input_kind="audio") == set()
    assert listed(client, channel="telegram", q="бота") == {ids["tg"], ids["voice"]}
    assert listed(client, channel="telegram", q="браузера") == set()
    assert listed(client, channel="telegram", category_id="none") == {ids["tg"], ids["voice"]}


@pytest.mark.parametrize("params", [{"channel": "email"}, {"input_kind": "video"}, {"channel": ""}])
def test_note_list_rejects_unknown_source(client, params):
    register(client)
    assert client.get("/api/v1/notes", params=params).status_code == 422


def test_source_filters_keep_owner_isolation(app, client):
    register(client, "owner")
    make_sources(app, client)
    with TestClient(app) as other:
        register(other, "other")
        assert other.get("/api/v1/notes", params={"channel": "telegram"}).json() == []
        assert other.get("/api/v1/notes", params={"input_kind": "audio", "q": "бота"}).json() == []
