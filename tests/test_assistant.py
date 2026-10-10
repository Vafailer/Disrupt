"""AI assistant: ask, recommendations and digest. Synthetic notes, mock provider, no network."""

import json
import time

import httpx
import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text

from app.assistant_retrieval import AssistantContext, NoteDoc, rank_notes
from app.db import make_database
from app.limits import assistant_units_used, units_used
from app.models import AssistantRequest, Capture, Item, Note, ProviderUsage, User, new_id
from app.providers import (
    NOT_FOUND_ANSWER,
    CloudRuProvider,
    MockProvider,
    ProviderError,
    validate_assistant,
)
from app.worker import Worker
from tests.conftest import register

OPEN = "/api/v1/assistant"


def add_note(app, user_id, title, markdown, items=(), age_days=1):
    created = time.time() - age_days * 86400
    with app.state.sessions() as db:
        capture = Capture(id=new_id(), user_id=user_id, original_text=markdown, idempotency_key=new_id(),
                          processing_mode="manual", created_at=created)
        db.add(capture)
        db.flush()
        note = Note(
            id=new_id(), capture_id=capture.id, user_id=user_id, title=title, markdown=markdown,
            conclusions=[], provider="mock", created_at=created, updated_at=created,
        )
        db.add(note)
        db.flush()
        item_ids = []
        for position, (kind, item_text) in enumerate(items):
            item = Item(
                id=new_id(), user_id=user_id, note_id=note.id, kind=kind, text=item_text,
                status="open", position=position,
            )
            db.add(item)
            item_ids.append(item.id)
        db.commit()
        return note.id, item_ids


def post(client, path, key, body=None):
    return client.post(OPEN + path, json=body, headers={"Idempotency-Key": key}) if body is not None else (
        client.post(OPEN + path, headers={"Idempotency-Key": key})
    )


def work(app, provider=None):
    worker = Worker(app.state.sessions, app.state.settings, provider=provider or MockProvider())
    return worker.run_once()


def read(client, request_id):
    response = client.get(f"{OPEN}/requests/{request_id}")
    assert response.status_code == 200, response.text
    return response.json()


def count(app, model, *where):
    with app.state.sessions() as db:
        return db.scalar(select(func.count()).select_from(model).where(*where))


def second_client(app, name="other"):
    client = TestClient(app)
    register(client, name)
    return client


def test_ask_is_grounded_in_the_owners_notes(app, client):
    user = register(client)
    note_id, _ = add_note(app, user["id"], "Запуск", "Дизайн готов.\nОплату ещё не сделали.")
    add_note(app, user["id"], "Огород", "Купить семена моркови.")
    response = post(client, "/ask", "ask-1", {"question": "Что с оплатой?"})
    assert response.status_code == 202
    assert set(response.json()) == {"id", "status"} and response.json()["status"] == "queued"
    assert work(app) is True
    view = read(client, response.json()["id"])
    assert view["status"] == "succeeded" and view["units"] == 1 and view["error_code"] is None
    citation = view["result"]["citations"][0]
    assert citation["note_id"] == note_id and citation["note_title"] == "Запуск"
    assert citation["quote"] in "Дизайн готов.\nОплату ещё не сделали."
    assert view["input_note_ids"] == [note_id]  # The unrelated note never reached the model.
    assert count(app, ProviderUsage, ProviderUsage.operation_id == view["id"]) == 1
    assert work(app) is False


def test_ask_without_matching_notes_costs_nothing_and_skips_the_provider(app, client):
    user = register(client)
    add_note(app, user["id"], "Огород", "Купить семена моркови.")
    request_id = post(client, "/ask", "ask-none", {"question": "Какие у меня планы на космос?"}).json()["id"]
    assert work(app) is True
    view = read(client, request_id)
    assert view["status"] == "succeeded" and view["units"] == 0
    assert view["result"] == {"answer_markdown": NOT_FOUND_ANSWER, "citations": []}
    assert count(app, ProviderUsage) == 0
    with app.state.sessions() as db:
        assert assistant_units_used(db, app.state.settings, user["id"]) == 0


def test_ask_validates_input(client):
    register(client)
    assert post(client, "/ask", "k1", {"question": "да"}).status_code == 422
    assert post(client, "/ask", "k2", {"question": "Что с оплатой?", "days": 10}).status_code == 422
    assert post(client, "/ask", "k3", {"question": "Что с оплатой?", "extra": 1}).status_code == 422
    assert post(client, "/ask", "bad key!", {"question": "Что с оплатой?"}).status_code == 422
    assert client.post(OPEN + "/ask", json={"question": "Что с оплатой?"}).status_code == 422  # No key.


def test_write_routes_need_session_and_csrf(app, client):
    assert post(client, "/ask", "anon", {"question": "Что с оплатой?"}).status_code == 401
    register(client)
    del client.headers["X-CSRF-Token"]
    assert post(client, "/ask", "no-csrf", {"question": "Что с оплатой?"}).status_code == 403
    assert client.patch(OPEN + "/settings", json={"recommendations_enabled": False}).status_code == 403


def test_recommendations_use_notes_and_open_tasks(app, client):
    user = register(client)
    note_id, item_ids = add_note(
        app, user["id"], "Запуск", "Дизайн готов.", items=[("task", "Сделать оплату"), ("idea", "Скидка")],
    )
    old_id, _ = add_note(app, user["id"], "Давнее", "Старая мысль.", items=[("task", "Старая задача")], age_days=200)
    request_id = post(client, "/recommendations", "rec-1").json()["id"]
    assert work(app) is True
    view = read(client, request_id)
    assert view["status"] == "succeeded"
    suggestions = view["result"]["suggestions"]
    assert 1 <= len(suggestions) <= 5
    assert suggestions[0]["kind"] == "task" and suggestions[0]["quote"] == "Сделать оплату"
    assert suggestions[0]["notes"] == [{"id": note_id, "title": "Запуск"}]
    assert old_id in view["input_note_ids"]  # An old note with an open task is included.


def test_recommendations_can_be_turned_off_and_on(app, client):
    register(client)
    assert client.get(OPEN + "/settings").json() == {"recommendations_enabled": True}
    response = client.patch(OPEN + "/settings", json={"recommendations_enabled": False})
    assert response.status_code == 200 and response.json() == {"recommendations_enabled": False}
    assert client.get(OPEN + "/settings").json() == {"recommendations_enabled": False}
    blocked = post(client, "/recommendations", "rec-off")
    assert blocked.status_code == 409 and "выключены" in blocked.json()["detail"]
    assert count(app, AssistantRequest) == 0
    assert client.patch(OPEN + "/settings", json={"recommendations_enabled": "no"}).status_code == 422
    client.patch(OPEN + "/settings", json={"recommendations_enabled": True})
    assert post(client, "/recommendations", "rec-on").status_code == 202


def test_digest_covers_the_period_and_lists_open_tasks(app, client):
    user = register(client)
    note_id, item_ids = add_note(
        app, user["id"], "Неделя", "Обсудили запуск проекта.", items=[("task", "Позвонить подрядчику")],
    )
    add_note(app, user["id"], "Давнее", "Не должно попасть в сводку.", age_days=30)
    request_id = post(client, "/digest", "dig-1", {}).json()["id"]
    assert work(app) is True
    view = read(client, request_id)
    assert view["status"] == "succeeded" and view["days"] == 7
    assert view["input_note_ids"] == [note_id]
    result = view["result"]
    assert result["highlights"][0]["note_title"] == "Неделя"
    assert result["open_tasks"] == [
        {"id": item_ids[0], "text": "Позвонить подрядчику", "note_id": note_id, "note_title": "Неделя"}
    ]
    assert isinstance(result["themes"], list)
    assert post(client, "/digest", "dig-bad", {"days": 3}).status_code == 422


class UngroundedProvider(MockProvider):
    def assistant(self, kind, context, *, question=None):
        if kind == "ask":
            return {"answer_markdown": "Ответ", "citations": [
                {"note_id": context.notes[0]["id"], "quote": "Этого нет в заметке"}]}
        return super().assistant(kind, context, question=question)


def test_ungrounded_quote_fails_the_request_without_a_result(app, client):
    user = register(client)
    add_note(app, user["id"], "Запуск", "Оплату ещё не сделали.")
    request_id = post(client, "/ask", "ask-bad", {"question": "Что с оплатой?"}).json()["id"]
    assert work(app, UngroundedProvider()) is True
    view = read(client, request_id)
    assert view["status"] == "failed" and view["error_code"] == "ungrounded_quote"
    assert view["result"] is None and "заметк" in view["error_message"]
    assert work(app, UngroundedProvider()) is False  # Never retried.
    assert count(app, ProviderUsage, ProviderUsage.status == "failed") == 1
    with app.state.sessions() as db:  # The call started, so the unit stays spent.
        assert assistant_units_used(db, app.state.settings, user["id"]) == 1


def context_of(*notes):
    return AssistantContext(list(notes), [i for n in notes for i in n["items"]])


NOTE = {"id": "n1", "title": "Запуск", "date": "2026-01-01", "text": "Оплату ещё не сделали.",
        "items": [{"id": "i1", "kind": "task", "text": "Сделать оплату", "status": "open"},
                  {"id": "i2", "kind": "task", "text": "Закрытая", "status": "done"}]}


def test_validation_rules_for_every_kind():
    context = context_of(NOTE)
    ok_ask = {"answer_markdown": "Оплаты нет.", "citations": [{"note_id": "n1", "quote": "Оплату ещё"}]}
    assert validate_assistant("ask", ok_ask, context) == ok_ask
    for bad in (
        {"answer_markdown": "Оплаты нет.", "citations": []},
        {"answer_markdown": "Оплаты нет.", "citations": [{"note_id": "n2", "quote": "Оплату ещё"}]},
        {"answer_markdown": "Оплаты нет.", "citations": [{"note_id": "n1", "quote": "оплату ещё"}]},
        {"answer_markdown": "Оплаты нет.", "citations": [{"note_id": "n1", "quote": "  "}]},
    ):
        with pytest.raises(ProviderError) as error:
            validate_assistant("ask", bad, context)
        assert error.value.code in {"ungrounded_quote", "provider_invalid_response"}
    empty = {"answer_markdown": NOT_FOUND_ANSWER, "citations": []}
    assert validate_assistant("ask", empty, context) == empty
    with pytest.raises(ProviderError) as error:
        validate_assistant("ask", {**ok_ask, "unexpected": 1}, context)
    assert error.value.code == "provider_invalid_response"

    suggestion = {"kind": "task", "title": "Оплата", "text": "Сделать", "note_ids": ["n1"], "quote": "Сделать оплату"}
    assert validate_assistant("recommend", {"suggestions": [suggestion]}, context)["suggestions"] == [suggestion]
    for bad in (
        {**suggestion, "quote": "Выдумка"}, {**suggestion, "note_ids": ["zzz"]},
        {**suggestion, "note_ids": ["n1", "n1"]},
    ):
        with pytest.raises(ProviderError):
            validate_assistant("recommend", {"suggestions": [bad]}, context)
    with pytest.raises(ProviderError) as error:
        validate_assistant("recommend", {"suggestions": [{**suggestion, "kind": "magic"}]}, context)
    assert error.value.code == "provider_invalid_response"
    with pytest.raises(ProviderError):
        validate_assistant("recommend", {"suggestions": [suggestion] * 6}, context)

    digest = {"summary_markdown": "Итоги", "highlights": [{"note_id": "n1", "quote": "Оплату"}],
              "open_tasks": ["i1"], "themes": ["оплата"]}
    assert validate_assistant("digest", digest, context) == digest
    for bad in ({**digest, "open_tasks": ["i2"]}, {**digest, "open_tasks": ["nope"]},
                {**digest, "highlights": [{"note_id": "n1", "quote": "Нет такого"}]}):
        with pytest.raises(ProviderError) as error:
            validate_assistant("digest", bad, context)
        assert error.value.code == "ungrounded_quote"


def test_retrieval_ranks_by_keywords_and_ignores_unrelated_notes():
    docs = [
        NoteDoc("a", "Сад", "Посадить морковь и свёклу", 1),
        NoteDoc("b", "Запуск", "Оплата пока не готова, дизайн готов", 2),
        NoteDoc("c", "Оплата аренды", "Заплатить за офис, оплата до пятницы", 3),
    ]
    ranked = [doc.id for doc in rank_notes("когда будет оплата?", docs)]
    assert ranked[0] == "c" and set(ranked) == {"b", "c"}
    assert rank_notes("пустота", docs) == [] and rank_notes("", docs) == []


def test_retrieval_caps_prompt_size(app, client):
    from app.assistant_retrieval import PROMPT_CHARS, context_for_digest

    user = register(client)
    for number in range(30):
        add_note(app, user["id"], f"Заметка {number}", "слово " * 1000)
    with app.state.sessions() as db:
        context = context_for_digest(db, user["id"], 7)
    size = sum(len(n["title"]) + len(n["text"]) for n in context.notes)
    assert 0 < len(context.notes) < 30 and size <= PROMPT_CHARS
    assert all(len(n["text"]) <= 900 for n in context.notes)


def test_owner_isolation(app, client):
    owner = register(client)
    note_id, _ = add_note(app, owner["id"], "Секрет", "Пароль от сейфа лежит в шкафу.")
    stranger = second_client(app)
    request_id = post(client, "/ask", "own-1", {"question": "Где пароль от сейфа?"}).json()["id"]
    assert stranger.get(f"{OPEN}/requests/{request_id}").status_code == 404
    assert work(app) is True
    assert stranger.get(f"{OPEN}/requests/{request_id}").status_code == 404
    assert stranger.get(OPEN + "/requests").json() == []
    assert [row["id"] for row in client.get(OPEN + "/requests").json()] == [request_id]
    # The stranger's own question never sees the owner's notes.
    other_id = post(stranger, "/ask", "own-2", {"question": "Где пароль от сейфа?"}).json()["id"]
    assert work(app) is True
    view = read(stranger, other_id)
    assert view["input_note_ids"] == [] and view["result"]["citations"] == []
    # Settings are per user.
    stranger.patch(OPEN + "/settings", json={"recommendations_enabled": False})
    assert client.get(OPEN + "/settings").json() == {"recommendations_enabled": True}
    assert note_id not in json.dumps(stranger.get(OPEN + "/requests").json())


def test_list_returns_latest_first_and_limits(app, client):
    register(client)
    ids = [post(client, "/digest", f"list-{n}", {}).json()["id"] for n in range(3)]
    with app.state.sessions() as db:
        for number, request_id in enumerate(ids):
            db.get(AssistantRequest, request_id).created_at = 1000 + number
        db.commit()
    rows = client.get(OPEN + "/requests?limit=2").json()
    assert [row["id"] for row in rows] == [ids[2], ids[1]]
    assert client.get(OPEN + "/requests?limit=0").status_code == 422


def test_daily_limit_is_shared_and_answers_429_at_zero(app_factory):
    app = app_factory(daily_unit_limit=2)
    with TestClient(app) as client:
        user = register(client)
        assert post(client, "/digest", "lim-1", {}).status_code == 202
        assert post(client, "/digest", "lim-2", {}).status_code == 202
        limited = post(client, "/digest", "lim-3", {})
        assert limited.status_code == 429
        body = limited.json()
        assert "лимит" in body["detail"] and body["limit_resets_at"]
        assert post(client, "/digest", "lim-1", {}).status_code == 202  # A replay is free.
        usage = client.get("/api/v1/provider/usage").json()
        assert usage["daily_units_used"] == 2 and usage["daily_units_remaining"] == 0
        # Text captures share the same allowance.
        capture = client.post("/api/v1/captures/text", json={"text": "Мысль"}, headers={"Idempotency-Key": "c1"})
        assert capture.json().get("ai_limit_exceeded") is True
        with app.state.sessions() as db:
            assert units_used(db, app.state.settings, user["id"]) == 2


def test_failure_before_the_provider_call_is_free(app_factory):
    app = app_factory(daily_unit_limit=1)
    with TestClient(app) as client:
        user = register(client)
        add_note(app, user["id"], "Неделя", "Обсудили запуск.")
        request_id = post(client, "/digest", "free-1", {}).json()["id"]
        with app.state.sessions() as db:  # Simulates a request that failed before dispatch.
            row = db.get(AssistantRequest, request_id)
            row.status, row.error_code, row.finished_at = "failed", "budget_exhausted", time.time()
            db.commit()
            assert row.started_at is None
            assert assistant_units_used(db, app.state.settings, user["id"]) == 0
        assert post(client, "/digest", "free-2", {}).status_code == 202


def test_idempotent_replay_does_not_double_charge(app, client):
    user = register(client)
    first = post(client, "/ask", "same-key", {"question": "Что с оплатой?"})
    again = post(client, "/ask", "same-key", {"question": "Что с оплатой?"})
    assert first.status_code == again.status_code == 202 and first.json() == again.json()
    assert count(app, AssistantRequest) == 1
    with app.state.sessions() as db:
        assert assistant_units_used(db, app.state.settings, user["id"]) == 1
    conflict = post(client, "/ask", "same-key", {"question": "Совсем другой вопрос"})
    assert conflict.status_code == 409
    assert post(client, "/digest", "same-key", {}).status_code == 409
    assert count(app, AssistantRequest) == 1


def test_pending_requests_are_capped(app_factory):
    app = app_factory(max_pending_per_user=2)
    with TestClient(app) as client:
        register(client)
        assert post(client, "/digest", "p1", {}).status_code == 202
        assert post(client, "/digest", "p2", {}).status_code == 202
        assert post(client, "/digest", "p3", {}).status_code == 429


def test_expired_lease_fails_without_calling_the_provider(app, client):
    register(client)
    request_id = post(client, "/digest", "lease-1", {}).json()["id"]
    with app.state.sessions() as db:
        row = db.get(AssistantRequest, request_id)
        row.status, row.lease_until = "running", time.time() - 5
        db.commit()
    assert work(app) is False
    view = read(client, request_id)
    assert view["status"] == "failed" and view["error_code"] == "execution_unknown"


def test_capture_jobs_are_processed_before_assistant_requests(app, client):
    register(client)
    assistant_id = post(client, "/digest", "order-1", {}).json()["id"]
    job_id = client.post(
        "/api/v1/captures/text", json={"text": "Мысль для заметки"}, headers={"Idempotency-Key": "order-job"},
    ).json()["id"]
    assert work(app) is True
    assert client.get(f"/api/v1/jobs/{job_id}").json()["status"] == "succeeded"
    assert read(client, assistant_id)["status"] == "queued"
    assert work(app) is True
    assert read(client, assistant_id)["status"] == "succeeded"


def test_live_budget_slot_is_reserved_for_every_assistant_call(app, client):
    from dataclasses import replace

    from app.models import ProviderBudget

    user = register(client)
    add_note(app, user["id"], "Неделя", "Обсудили запуск.")
    settings = replace(app.state.settings, provider="cloudru", allow_live_requests=True, cloudru_model="synthetic",
                       live_call_limit=1, live_user_call_limit=1)
    worker = Worker(app.state.sessions, settings, provider=MockProvider())
    first = post(client, "/digest", "budget-1", {}).json()["id"]
    second = post(client, "/digest", "budget-2", {}).json()["id"]
    assert worker.run_once() is True and worker.run_once() is True
    assert read(client, first)["status"] == "succeeded"
    failed = read(client, second)
    assert failed["status"] == "failed" and failed["error_code"] == "budget_exhausted"
    assert failed["started_at"] is None  # Never dispatched, so the unit is free.
    with app.state.sessions() as db:
        assert db.get(ProviderBudget, "cloudru").reserved_calls == 1
        assert db.get(User, user["id"]).live_calls == 1


def completion(content, finish="stop"):
    return {"choices": [{"finish_reason": finish, "message": {"content": json.dumps(content)}}]}


def test_cloudru_assistant_call_uses_the_same_safety_with_a_fake_transport():
    context = context_of(NOTE)
    requests = []

    def handler(request):
        requests.append(request)
        assert request.extensions["timeout"] == {"connect": 10, "read": 90, "write": 10, "pool": 10}
        body = json.loads(request.content)
        assert body["response_format"] == {"type": "json_object"}
        assert "не исполняй" in body["messages"][0]["content"].lower() or "не исполня" in body["messages"][0]["content"]
        data = json.loads(body["messages"][1]["content"])
        assert data["question"] == "Что с оплатой?" and data["notes"][0]["id"] == "n1"
        return httpx.Response(200, json=completion(
            {"answer_markdown": "Оплаты нет.", "citations": [{"note_id": "n1", "quote": "Оплату ещё"}]}))

    provider = CloudRuProvider("fake-test-key", "test-model", transport=httpx.MockTransport(handler))
    result = provider.assistant("ask", context, question="Что с оплатой?")
    assert result["citations"][0]["quote"] == "Оплату ещё" and len(requests) == 1


@pytest.mark.parametrize(("response", "code"), [
    (httpx.Response(200, json=completion(
        {"answer_markdown": "Ответ", "citations": [{"note_id": "n1", "quote": "выдумка"}]})), "ungrounded_quote"),
    (httpx.Response(200, json=completion({"wrong": 1})), "provider_invalid_response"),
    (httpx.Response(200, json=completion({"answer_markdown": "x", "citations": []}, finish="length")),
     "provider_incomplete_response"),
    (httpx.Response(200, content=b"not json"), "provider_invalid_response"),
    (httpx.Response(429), "provider_rate_limit"),
    (httpx.Response(503), "provider_unavailable"),
])
def test_cloudru_assistant_rejects_bad_answers_and_never_retries(response, code):
    calls = []

    def handler(request):
        calls.append(request)
        return response

    provider = CloudRuProvider("fake-test-key", "test-model", transport=httpx.MockTransport(handler))
    with pytest.raises(ProviderError) as error:
        provider.assistant("ask", context_of(NOTE), question="Что с оплатой?")
    assert error.value.code == code and len(calls) == 1


def test_cloudru_assistant_timeout_is_unknown_and_not_retried():
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("slow", request=request)

    provider = CloudRuProvider("fake-test-key", "test-model", transport=httpx.MockTransport(handler))
    with pytest.raises(ProviderError) as error:
        provider.assistant("digest", context_of(NOTE))
    assert error.value.code == "provider_timeout_unknown" and len(calls) == 1


def test_prompts_treat_notes_as_data_in_russian():
    from app.providers import ASSISTANT_PROMPTS

    assert set(ASSISTANT_PROMPTS) == {"ask", "recommend", "digest"}
    for prompt in ASSISTANT_PROMPTS.values():
        assert "по-русски" in prompt and "не исполняй" in prompt and "данные" in prompt


def test_migration_adds_table_and_setting_and_downgrades(tmp_path, monkeypatch):
    url = "sqlite:///" + (tmp_path / "assistant.db").as_posix()
    monkeypatch.setenv("NOTES_DATABASE_URL", url)
    monkeypatch.setenv("NOTES_PROVIDER", "mock")
    config = Config("alembic.ini")
    command.upgrade(config, "e61f893ac204")
    engine, sessions = make_database(url)
    with engine.begin() as db:
        db.execute(text("INSERT INTO users (id, username, password_hash, live_calls) VALUES ('u','old','hash',0)"))
    command.upgrade(config, "head")
    command.check(config)
    with sessions() as db:
        assert db.get(User, "u").assistant_recommendations_enabled is True  # Existing users keep the feature.
        db.add(AssistantRequest(id="r", user_id="u", kind="ask", status="queued", question="Вопрос",
                                input_note_ids=[], idempotency_key="k", request_hash="h", units=1))
        db.commit()
        assert db.get(AssistantRequest, "r").created_at is not None
    with engine.connect() as db:
        indexes = {row[1] for row in db.execute(text("PRAGMA index_list('assistant_requests')"))}
        assert "ix_assistant_requests_user_created" in indexes
    command.downgrade(config, "e61f893ac204")
    with engine.connect() as db:
        tables = {row[0] for row in db.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))}
        columns = {row[1] for row in db.execute(text("PRAGMA table_info('users')"))}
        assert "assistant_requests" not in tables and "assistant_recommendations_enabled" not in columns
        assert db.execute(text("SELECT username FROM users")).scalar() == "old"
        assert db.execute(text("PRAGMA foreign_key_check")).all() == []
    command.upgrade(config, "head")
    command.check(config)
    engine.dispose()
