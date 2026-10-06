import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.models import Capture, Inbox, Job, LinkRequest, Note, ProductEvent, TelegramIdentity, User
from app.providers import DEMO_TEXT
from app.worker import Worker
from tests.conftest import register

SERVICE_TOKEN = "test-service-secret-" + "a" * 32
HEADERS = {"Authorization": "Bearer " + SERVICE_TOKEN}
BASE = {"bot_id": 1234567890123, "telegram_user_id": 2345678901234, "chat_id": 2345678901234}


@pytest.fixture
def service(app_factory):
    app = app_factory(internal_api_token=SERVICE_TOKEN)
    with TestClient(app) as client:
        yield app, client


def pending_link(client, update_id=1):
    code = client.post("/api/v1/telegram/link-code").json()
    payload = {**BASE, "update_id": update_id, "code": code["code"]}
    result = client.post("/internal/v1/telegram/link-request", json=payload, headers=HEADERS)
    assert result.status_code == 200, result.text
    return code, payload, result.json()


def linked(client):
    account = register(client)
    code, _, _ = pending_link(client)
    response = client.post("/api/v1/telegram/links/" + code["link_request_id"] + "/confirm")
    assert response.status_code == 200, response.text
    return account


def message(client, update_id=2, text=DEMO_TEXT, mode="ai", **extra):
    return client.post(
        "/internal/v1/telegram/updates",
        headers=HEADERS,
        json={
            **BASE,
            "update_id": update_id,
            "text": text,
            "processing_mode": mode,
            **extra,
        },
    )


def count(app, model):
    with app.state.sessions() as db:
        return db.scalar(select(func.count()).select_from(model))


def test_link_requires_web_confirmation_and_hash_only(service):
    app, client = service
    owner = register(client)
    code, payload, response = pending_link(client)
    assert response == {"link_request_id": code["link_request_id"], "status": "pending"}
    rejected = message(client)
    assert rejected.status_code == 403
    assert rejected.json()["error"]["code"] == "telegram_not_linked"
    with app.state.sessions() as db:
        link = db.get(LinkRequest, code["link_request_id"])
        assert link.code_hash != code["code"] and len(link.code_hash) == 64
        assert link.user_id == owner["id"]
    assert client.post("/internal/v1/telegram/link-request", json=payload, headers=HEADERS).json() == response
    assert (
        client.post(
            "/internal/v1/telegram/link-request", json={**payload, "update_id": 9}, headers=HEADERS
        ).status_code
        == 409
    )
    path = "/api/v1/telegram/links/" + code["link_request_id"] + "/confirm"
    assert client.post(path, headers={"X-CSRF-Token": "wrong"}).status_code == 403
    assert client.post(path, headers={"Origin": "https://evil.example"}).status_code == 403
    with TestClient(app) as other:
        register(other, "other")
        assert other.post(path).status_code == 404
    assert client.post(path).json() == {"status": "confirmed"}
    assert client.post(path).json() == {"status": "confirmed"}
    assert count(app, TelegramIdentity) == 1
    assert message(client).json() == rejected.json()  # Terminal refusal stays terminal after linking.
    assert message(client, update_id=3).status_code == 200


def test_one_account_two_channels_and_stable_replay(service):
    app, client = service
    owner = linked(client)
    first = message(client).json()
    assert first["status"] == "saved" and first["job_id"]
    assert message(client).json() == first
    assert message(client, text="different").status_code == 409
    assert count(app, Capture) == count(app, Job) == 1
    assert count(app, Inbox) == 2  # link plus text, not retries
    assert Worker(app.state.sessions, app.state.settings).run_once()
    assert message(client).json() == first  # Response is stored, not rebuilt from later job status.
    capture = client.get("/api/v1/captures/" + first["capture_id"]).json()
    note = client.get("/api/v1/notes/" + capture["note_id"]).json()
    assert note["original_text"] == DEMO_TEXT
    with app.state.sessions() as db:
        assert db.get(Capture, first["capture_id"]).user_id == owner["id"]
        events = db.scalars(select(ProductEvent).where(ProductEvent.name == "capture_saved")).all()
        assert len(events) == 1 and events[0].channel == "telegram"
        assert events[0].user_id == owner["id"]
        assert DEMO_TEXT not in str(events[0].__dict__)
    with TestClient(app) as other:
        register(other, "other")
        assert other.get("/api/v1/captures/" + first["capture_id"]).status_code == 404
        assert other.get("/api/v1/notes/" + capture["note_id"]).status_code == 404


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}, {"Authorization": "Basic wrong"}])
def test_internal_auth(service, headers):
    app, client = service
    response = client.post(
        "/internal/v1/telegram/updates", json={**BASE, "update_id": 1, "text": "x"}, headers=headers
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"
    assert response.json()["operation_id"]
    assert count(app, Capture) == 0


def test_disabled_service(client):
    response = client.post(
        "/internal/v1/telegram/updates", json={**BASE, "update_id": 1, "text": "x"}, headers=HEADERS
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "unavailable"


@pytest.mark.parametrize(
    "extra,expected",
    [
        ({"user_id": "attacker"}, 422),
        ({"text": " "}, 400),
        ({"text": "x" * 12001}, 413),
        ({"chat_id": 1}, 403),
        ({"telegram_user_id": True}, 422),
        ({"bot_id": 2**63}, 422),
    ],
)
def test_invalid_update_does_not_persist(service, extra, expected):
    app, client = service
    linked(client)
    response = message(client, **extra)
    assert response.status_code == expected
    assert set(response.json()) == {"error", "operation_id"}
    assert count(app, Capture) == 0 and count(app, Inbox) == 1


def test_global_update_key_includes_operation_type(service):
    app, client = service
    linked(client)
    assert message(client, update_id=1).status_code == 409
    assert count(app, Capture) == 0


def test_concurrent_replay_is_single_capture(service):
    app, client = service
    linked(client)
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(lambda _: message(client), range(4)))
    assert all(r.status_code == 200 for r in responses), [r.text for r in responses]
    assert len({r.json()["capture_id"] for r in responses}) == 1
    assert count(app, Capture) == 1


def test_failure_rolls_back_inbox_capture_and_events(service, monkeypatch):
    from app.routes import internal

    app, client = service
    linked(client)
    original = internal.capture_text

    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("private text and secret must not leak")

    monkeypatch.setattr(internal, "capture_text", fail)
    response = message(client)
    assert response.status_code == 503
    assert "private" not in response.text
    assert count(app, Capture) == count(app, Job) == 0
    assert count(app, Inbox) == 1
    monkeypatch.setattr(internal, "capture_text", original)
    assert message(client).status_code == 200


def test_expired_and_replaced_codes(service):
    app, client = service
    register(client)
    first = client.post("/api/v1/telegram/link-code").json()
    second = client.post("/api/v1/telegram/link-code").json()
    for code in [first, second]:
        if code == second:
            with app.state.sessions() as db:
                db.get(LinkRequest, code["link_request_id"]).expires_at = time.time() - 1
                db.commit()
        response = client.post(
            "/internal/v1/telegram/link-request",
            headers=HEADERS,
            json={
                **BASE,
                "update_id": 1,
                "code": code["code"],
            },
        )
        assert response.status_code == 409
    assert count(app, TelegramIdentity) == 0


def test_expired_pending_not_confirmable(service):
    app, client = service
    register(client)
    code, _, _ = pending_link(client)
    with app.state.sessions() as db:
        db.get(LinkRequest, code["link_request_id"]).expires_at = time.time() - 1
        db.commit()
    assert client.post("/api/v1/telegram/links/" + code["link_request_id"] + "/confirm").status_code == 409
    assert count(app, TelegramIdentity) == 0


def test_no_automatic_merge_or_replacement(service):
    app, client = service
    owner = linked(client)
    with TestClient(app) as other:
        register(other, "other")
        code = other.post("/api/v1/telegram/link-code").json()
        result = other.post(
            "/internal/v1/telegram/link-request", headers=HEADERS,
            json={**BASE, "update_id": 3, "code": code["code"]},
        )
        assert result.status_code == 409 and result.json()["error"]["code"] == "link_conflict"
        assert other.post("/api/v1/telegram/links/" + code["link_request_id"] + "/confirm").status_code == 409
    with app.state.sessions() as db:
        assert db.scalar(select(TelegramIdentity)).user_id == owner["id"]


def test_manual_web_and_telegram_never_queue_ai(service):
    app, client = service
    linked(client)
    body = {"text": "  Точная запись\n\nБез обработки.  ", "processing_mode": "manual"}
    first = client.post("/api/v1/captures/text", json=body, headers={"Idempotency-Key": "manual"}).json()
    assert first["job_id"] is None and first["status"] == "saved"
    assert (
        client.post("/api/v1/captures/text", json=body, headers={"Idempotency-Key": "manual"}).json() == first
    )
    assert (
        client.post(
            "/api/v1/captures/text",
            json={**body, "processing_mode": "ai"},
            headers={"Idempotency-Key": "manual"},
        ).status_code
        == 409
    )
    tg = message(client, text=body["text"], mode="manual").json()
    assert tg["job_id"] is None
    assert count(app, Job) == 0
    assert not Worker(app.state.sessions, app.state.settings).run_once()
    note = client.get("/api/v1/notes/" + first["note_id"]).json()
    assert note["provider"] == "manual" and note["original_text"] == body["text"] == note["markdown"]
    assert len(client.get("/api/v1/notes/" + first["note_id"] + "/revisions").json()) == 1
    assert count(app, Note) == count(app, Capture) == 2


def test_registration_cannot_assign_admin(client, app):
    response = client.post(
        "/api/v1/auth/register",
        json={
            "username": "test",
            "password": "test-only-password",
            "role": "admin",
        },
    )
    assert response.status_code == 422
    owner = register(client)
    with app.state.sessions() as db:
        assert db.get(User, owner["id"]).role == "user"


def test_internal_errors_body_limit_and_no_echo(service):
    _, client = service
    response = client.post("/internal/v1/telegram/updates", content=b"x" * 131073, headers=HEADERS)
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "input_too_large"
    bad = message(client, text="\x00sensitive-original")
    assert bad.status_code == 422 and "sensitive-original" not in bad.text


def test_analytical_session_combines_channels_without_background_extension(service):
    from app.analytics import record_event

    app, client = service
    owner = register(client)
    with app.state.sessions() as db:
        start = time.time() + 10
        record_event(db, owner["id"], "capture_saved", "web-op", "web", occurred_at=start)
        record_event(db, owner["id"], "capture_saved", "tg-op", "telegram", occurred_at=start + 1700)
        record_event(
            db, owner["id"], "processing_completed", "background", "telegram", occurred_at=start + 3400
        )
        record_event(db, owner["id"], "capture_saved", "later", "web", occurred_at=start + 3600)
        record_event(db, owner["id"], "capture_saved", "later", "web", occurred_at=start + 3600)
        db.commit()
        events = {e.operation_id: e for e in db.scalars(select(ProductEvent)).all()}
        assert events["web-op"].session_id == events["tg-op"].session_id
        assert events["later"].session_id != events["tg-op"].session_id
        assert len([e for e in events.values() if e.operation_id == "later"]) == 1


def test_admin_page_and_static_files_require_server_role(app, client):
    for path in ("/admin", "/static/admin.html", "/static/admin.js", "/static/admin.css"):
        assert client.get(path).status_code == 401
    owner = register(client)
    for path in ("/admin", "/static/admin.html", "/static/admin.js", "/static/admin.css"):
        assert client.get(path).status_code == 403
    with app.state.sessions() as db:
        db.get(User, owner["id"]).role = "admin"
        db.commit()
    assert client.get("/admin").status_code == 200
    assert client.get("/static/admin.js").status_code == 200
    assert client.get("/static/admin.css").status_code == 200
    with app.state.sessions() as db:
        db.get(User, owner["id"]).role = "user"
        db.commit()
    assert client.get("/admin").status_code == 403


def test_concurrent_queue_limit_and_manual_bypass(app_factory):
    app = app_factory(internal_api_token=SERVICE_TOKEN, max_pending_per_user=1)
    with TestClient(app) as client:
        linked(client)
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda i: message(client, update_id=i), [2, 3]))
        assert sorted(r.status_code for r in responses) == [200, 429]
        assert count(app, Job) == 1
        assert message(client, update_id=4, mode="manual").status_code == 200
        assert count(app, Job) == 1 and count(app, Capture) == 2
