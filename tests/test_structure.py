import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.models import Category, Item, Note, Outbox, ProductEvent, Reminder
from app.providers import DEMO_TEXT, MockProvider
from app.worker import Worker
from tests.conftest import register
from tests.test_notes import ready_note, submit


def manual(client, text="Точная запись", key=None):
    response = client.post(
        "/api/v1/captures/text",
        json={"text": text, "processing_mode": "manual"},
        headers={"Idempotency-Key": key or str(uuid.uuid4())},
    )
    assert response.status_code == 202, response.text
    return client.get("/api/v1/notes/" + response.json()["note_id"]).json()


def add_item(client, note, *, kind="task", text="Позвонить Оле"):
    response = client.post(
        f"/api/v1/notes/{note['id']}/items",
        json={"version": note["version"], "kind": kind, "text": text},
    )
    assert response.status_code == 201, response.text
    return response.json()


def event_counts(app):
    with app.state.sessions() as db:
        return dict(db.execute(select(ProductEvent.name, func.count()).group_by(ProductEvent.name)).all())


def test_model_items_category_and_unconfirmed_time(app, client):
    register(client)
    category = client.post("/api/v1/categories", json={"name": "рАБота"}).json()
    note = ready_note(app, client)
    assert note["category_id"] == category["id"]
    assert note["category_name"] == "рАБота"
    assert note["structure_confirmed_at"] is None
    assert [i["kind"] for i in note["items"]] == ["note", "task"]
    assert all(i["note_id"] == note["id"] and i["source_quote"] in DEMO_TEXT for i in note["items"])
    task = note["items"][1]
    assert task["due_text"] == "завтра"
    assert task["due_at"] is None and task["status"] == "open"
    stored = client.get(f"/api/v1/notes/{note['id']}/revisions").json()[0]["items"]
    # The proposal is computed on read and never stored in a revision.
    assert stored == [{k: v for k, v in i.items() if k != "proposed_reminder"} for i in note["items"]]
    assert len(client.get("/api/v1/categories").json()) == 1
    with app.state.sessions() as db:
        assert db.scalar(select(func.count()).select_from(Reminder)) == 0


@pytest.mark.parametrize("change", ["quote", "time", "ownership"])
def test_invalid_model_items_do_not_save_partial_structure(app, client, change):
    register(client)
    job = submit(client).json()

    class BadProvider:
        def structure(self, text, *, categories=()):
            result = MockProvider().structure(text).model_dump()
            if change == "quote":
                result["items"][0]["source_quote"] = "Несуществующий факт"
            elif change == "time":
                result["items"][1]["due_text"] = "2030-01-01"
            else:
                result["items"][0]["user_id"] = "attacker"
            from app.schemas import StructuredNote

            return StructuredNote.model_validate(result)

    assert Worker(app.state.sessions, app.state.settings, BadProvider()).run_once()
    after = client.get("/api/v1/jobs/" + job["id"]).json()
    assert after["status"] == "failed" and after["original_text"] == DEMO_TEXT
    with app.state.sessions() as db:
        for model in (Note, Item, Category):
            assert db.scalar(select(func.count()).select_from(model)) == 0


def test_worker_keeps_category_id_after_rename_during_model_call(app, client):
    owner = register(client)
    category = client.post("/api/v1/categories", json={"name": "Работа"}).json()

    class RenameDuringCall:
        def structure(self, text, *, categories=()):
            assert categories == ("Работа",)
            with app.state.sessions() as db:
                row = db.get(Category, category["id"])
                assert row.user_id == owner["id"]
                row.name, row.version = "Проект", 2
                db.commit()
            return MockProvider().structure(text, categories=categories)

    job = submit(client).json()
    assert Worker(app.state.sessions, app.state.settings, RenameDuringCall()).run_once()
    note_id = client.get("/api/v1/jobs/" + job["id"]).json()["note_id"]
    note = client.get("/api/v1/notes/" + note_id).json()
    assert note["category_id"] == category["id"] and note["category_name"] == "Проект"
    assert len(client.get("/api/v1/categories").json()) == 1


def test_item_edit_history_conflicts_and_structure_confirmation(app, client):
    register(client)
    note = add_item(client, manual(client))
    path = f"/api/v1/notes/{note['id']}"
    item = note["items"][0]
    assert note["structure_confirmed_at"] is not None
    body = {
        "version": note["version"],
        "kind": "idea",
        "text": "Может быть, позвонить позже",
        "status": "open",
    }
    response = client.patch(path + "/items/" + item["id"], json=body)
    assert response.status_code == 200
    changed = response.json()
    assert changed["version"] == note["version"] + 1
    assert changed["items"][0]["version"] == 2
    assert changed["items"][0]["kind"] == "idea"
    assert changed["original_text"] == "Точная запись"
    assert client.patch(path + "/items/" + item["id"], json=body).status_code == 409
    history = client.get(path + "/revisions").json()
    assert history[-1]["items"] == []
    assert history[-2]["items"][0]["kind"] == "task"
    assert history[0]["items"][0]["kind"] == "idea"
    assert event_counts(app)["structure_confirmed"] == 2


def test_confirmation_and_get_does_not_record_opening(app, client):
    register(client)
    note = manual(client)
    before = event_counts(app)
    path = "/api/v1/notes/" + note["id"]
    client.get(path)
    client.get("/api/v1/notes", params={"q": "запись"})
    assert event_counts(app) == before
    response = client.post(path + "/confirm-structure", json={"version": 1})
    assert response.status_code == 200
    assert response.json()["structure_confirmed_at"] is not None
    assert client.post(path + "/confirm-structure", json={"version": 1}).status_code == 409
    with app.state.sessions() as db:
        opened = db.scalar(select(ProductEvent).where(ProductEvent.name == "note_opened"))
        confirmed = db.scalar(select(ProductEvent).where(ProductEvent.name == "structure_confirmed"))
        assert opened.occurred_at <= confirmed.occurred_at
    changed = client.patch(
        path, json={"version": 2, "title": "Исправлено", "markdown": "Без потери оригинала"}
    ).json()
    assert changed["structure_confirmed_at"] >= response.json()["structure_confirmed_at"]


def test_task_completion_cancels_reminders_atomically(app, client):
    owner = register(client)
    note = add_item(client, manual(client))
    task = note["items"][0]
    ids = []
    with app.state.sessions() as db:
        for status in ["pending", "leased", "authorized", "sent"]:
            reminder = Reminder(
                user_id=owner["id"],
                note_id=note["id"],
                item_id=task["id"],
                scheduled_at=time.time() + 600,
                timezone="Europe/Moscow",
                text="Позвонить",
            )
            db.add(reminder)
            db.flush()
            box = Outbox(
                reminder_id=reminder.id, user_id=owner["id"], bot_id=1, chat_id=2, generation=1, status=status
            )
            db.add(box)
            db.flush()
            ids.append((reminder.id, box.id))
        db.commit()
    path = f"/api/v1/notes/{note['id']}/items/{task['id']}"
    body = {"version": note["version"], "kind": "task", "text": task["text"], "status": "completed"}
    assert client.patch(path, json={**body, "version": 1}).status_code == 409
    with app.state.sessions() as db:
        assert all(db.get(Reminder, rid).generation == 1 for rid, _ in ids)
    done = client.patch(path, json=body)
    assert done.status_code == 200
    with app.state.sessions() as db:
        assert all(
            db.get(Reminder, rid).status == "cancelled" and db.get(Reminder, rid).generation == 2
            for rid, _ in ids
        )
        assert [db.get(Outbox, bid).status for _, bid in ids] == ["cancelled", "cancelled", "unknown", "sent"]
    back = client.patch(path, json={**body, "version": done.json()["version"], "status": "open"})
    assert back.status_code == 200
    with app.state.sessions() as db:
        assert all(db.get(Reminder, rid).status == "cancelled" for rid, _ in ids)
    assert event_counts(app)["task_completed"] == 1


def test_category_reuse_rename_version_and_none_filter(client):
    register(client)
    category = client.post("/api/v1/categories", json={"name": "  Учёба  "}).json()
    assert client.post("/api/v1/categories", json={"name": "УЧЁБА"}).json()["id"] == category["id"]
    second = client.post("/api/v1/categories", json={"name": "Дом"}).json()
    path = "/api/v1/categories/" + category["id"]
    assert client.patch(path, json={"version": 1, "name": "дОМ"}).status_code == 409
    assert client.patch(path, json={"version": 1, "name": "Обучение"}).json()["version"] == 2
    assert client.patch(path, json={"version": 1, "name": "Старая правка"}).status_code == 409
    note = manual(client)
    note_path = "/api/v1/notes/" + note["id"]
    changed = client.patch(note_path + "/category", json={"version": 1, "category_id": second["id"]}).json()
    assert changed["category_name"] == "Дом"
    assert client.get("/api/v1/notes", params={"category_id": "none"}).json() == []
    assert client.get("/api/v1/notes", params={"category_id": second["id"]}).json()[0]["id"] == note["id"]
    assert client.patch(note_path + "/category", json={"version": 2, "category_id": None}).status_code == 200
    assert len(client.get("/api/v1/notes", params={"category_id": "none"}).json()) == 1


def test_search_30_records_cyrillic_original_edits_literals_and_pages(client):
    register(client)
    notes = [manual(client, text=f"Запись номер {i} о САМОКАТЕ", key=f"record-{i}") for i in range(30)]
    first = client.get("/api/v1/notes", params={"q": "самокате", "limit": 10})
    assert first.status_code == 200 and len(first.json()) == 10
    assert first.headers["X-Next-Notes-Offset"] == "10"
    found = first.json()
    for offset in [10, 20]:
        response = client.get("/api/v1/notes", params={"q": "СамоКатЕ", "limit": 10, "offset": offset})
        found += response.json()
    assert len({n["id"] for n in found}) == 30
    assert "X-Next-Notes-Offset" not in response.headers
    target = notes[0]
    path = "/api/v1/notes/" + target["id"]
    assert (
        client.patch(path, json={"version": 1, "title": "Заголовок", "markdown": "Исправлено"}).status_code
        == 200
    )
    assert client.get("/api/v1/notes", params={"q": "номер 0"}).json()[0]["id"] == target["id"]
    assert client.get("/api/v1/notes", params={"q": "ИСПРАВЛЕНО"}).json()[0]["id"] == target["id"]
    add_item(client, client.get(path).json(), text="Купить акварель", kind="idea")
    assert client.get("/api/v1/notes", params={"q": "АКВАРЕЛЬ"}).json()[0]["id"] == target["id"]
    assert client.get("/api/v1/notes", params={"q": "%"}).json() == []
    literal = manual(client, "Скидка 20%_летом")
    assert client.get("/api/v1/notes", params={"q": "%_"}).json()[0]["id"] == literal["id"]
    assert client.get("/api/v1/notes", params={"q": "x\x00y"}).status_code == 422
    assert client.get("/api/v1/notes", params={"q": "x" * 201}).status_code == 422


def test_owned_structure_search_actions_and_csrf(app, client):
    register(client)
    owner_note = add_item(client, manual(client))
    owner_category = client.post("/api/v1/categories", json={"name": "Секретная"}).json()
    path = "/api/v1/notes/" + owner_note["id"]
    with TestClient(app) as other:
        register(other, "other")
        assert other.get("/api/v1/categories").json() == []
        assert other.get("/api/v1/notes", params={"q": "Точная"}).json() == []
        assert other.get("/api/v1/notes", params={"category_id": owner_category["id"]}).status_code == 404
        assert (
            other.patch(
                "/api/v1/categories/" + owner_category["id"], json={"version": 1, "name": "Чужая"}
            ).status_code
            == 404
        )
        own_note = manual(other)
        assert (
            other.patch(
                f"/api/v1/notes/{own_note['id']}/items/{owner_note['items'][0]['id']}",
                json={
                    "version": 1,
                    "kind": "task",
                    "text": "Чужая правка",
                    "status": "open",
                },
            ).status_code
            == 404
        )
        assert other.get("/api/v1/notes/" + own_note["id"]).json()["version"] == 1
        assert (
            other.patch(
                f"/api/v1/notes/{own_note['id']}/category",
                json={"version": 1, "category_id": owner_category["id"]},
            ).status_code
            == 404
        )
        for suffix, body in [
            ("/items", {"version": 2, "kind": "task", "text": "Взлом"}),
            ("/confirm-structure", {"version": 2}),
            ("/opened", {"operation_id": str(uuid.uuid4())}),
            ("/original-opened", {"operation_id": str(uuid.uuid4())}),
        ]:
            assert other.post(path + suffix, json=body).status_code == 404
    assert (
        client.post(
            path + "/confirm-structure", json={"version": 2}, headers={"X-CSRF-Token": "bad"}
        ).status_code
        == 403
    )
    own_item = owner_note["items"][0]
    invalid = client.patch(
        path + "/items/" + own_item["id"],
        json={"version": 2, "kind": "idea", "text": "Оставить", "status": "completed"},
    )
    assert invalid.status_code == 422
    assert client.get(path).json()["version"] == 2


def test_explicit_actions_deduplicate_without_query_content(app, client):
    register(client)
    note = manual(client)
    search_id, opened_id = str(uuid.uuid4()), str(uuid.uuid4())
    body = {"operation_id": search_id}
    for _ in range(2):
        assert client.post("/api/v1/search/events", json=body).status_code == 204
    path = "/api/v1/notes/" + note["id"]
    for _ in range(2):
        assert (
            client.post(
                path + "/opened", json={"operation_id": opened_id, "search_operation_id": search_id}
            ).status_code
            == 204
        )
    assert (
        client.post(
            path + "/opened",
            json={"operation_id": str(uuid.uuid4()), "search_operation_id": str(uuid.uuid4())},
        ).status_code
        == 422
    )
    assert client.post("/api/v1/search/events", json={**body, "query": "private text"}).status_code == 422
    assert client.post(path + "/original-opened", json={"operation_id": str(uuid.uuid4())}).status_code == 204
    counts = event_counts(app)
    assert counts["search_submitted"] == counts["search_result_opened"] == counts["note_opened"] == 1
    assert counts["original_opened"] == 1


def test_concurrent_item_and_note_edit_have_one_winner(app, client):
    register(client)
    note = add_item(client, manual(client))
    path = "/api/v1/notes/" + note["id"]

    def change(index):
        if index == 0:
            return client.patch(path, json={"version": 2, "title": "Правка", "markdown": "Новая версия"})
        return client.patch(
            path + "/items/" + note["items"][0]["id"],
            json={"version": 2, "kind": "task", "text": "Позвонить позже", "status": "open"},
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(change, range(2)))
    assert sorted(r.status_code for r in responses) == [200, 409]
    after = client.get(path).json()
    assert after["version"] == 3 and after["original_text"] == "Точная запись"
    assert [r["version"] for r in client.get(path + "/revisions").json()] == [3, 2, 1]


def test_category_concurrent_case_variants_use_one_id(client):
    register(client)
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(
            pool.map(
                lambda name: client.post("/api/v1/categories", json={"name": name}), ["Работа", "РАБОТА"]
            )
        )
    assert all(r.status_code == 201 for r in responses)
    assert len({r.json()["id"] for r in responses}) == 1


def test_item_limit_rolls_back_note_version_and_events(app, client):
    owner = register(client)
    note = manual(client)
    with app.state.sessions() as db:
        db.add_all(
            [
                Item(user_id=owner["id"], note_id=note["id"], kind="note", text=f"Элемент {i}")
                for i in range(30)
            ]
        )
        db.commit()
    before = event_counts(app)
    response = client.post(
        f"/api/v1/notes/{note['id']}/items", json={"version": 1, "kind": "idea", "text": "Лишняя"}
    )
    assert response.status_code == 429
    after = client.get("/api/v1/notes/" + note["id"]).json()
    assert after["version"] == 1 and len(after["items"]) == 30
    assert event_counts(app) == before
