import json
import time
from datetime import datetime, timedelta
from datetime import time as clock
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.analytics import record_event
from app.models import Capture, Category, Item, Note, Reminder, new_id
from app.providers import MockProvider
from app.worker import Worker
from tests.conftest import register
from tests.test_internal import message
from tests.test_notes import ready_note, submit
from tests.test_processing_replies import processed
from tests.test_reminders import add as add_reminder
from tests.test_reminders import claim
from tests.test_reminders import reminders as reminders
from tests.test_structure import manual

MOSCOW = ZoneInfo("Europe/Moscow")
VLADIVOSTOK = ZoneInfo("Asia/Vladivostok")


def set_updated(app, note_id, value):
    with app.state.sessions.begin() as db:
        db.get(Note, note_id).updated_at = value


def local_stamp(days_ago, hour, minute=0, zone=MOSCOW):
    day = datetime.now(zone).date() - timedelta(days=days_ago)
    return datetime.combine(day, clock(hour, minute), tzinfo=zone).timestamp()


def seed_note(app, user_id, stamp, *, channel="web", kind="text", mode="ai", category_id=None):
    with app.state.sessions.begin() as db:
        capture = Capture(
            user_id=user_id, original_text="Секретный текст", idempotency_key=new_id(), created_at=stamp,
            channel=channel, input_kind=kind, processing_mode=mode,
        )
        db.add(capture)
        db.flush()
        note = Note(
            capture_id=capture.id, user_id=user_id, title="Секретная заметка", markdown="Секретный текст",
            conclusions=[], provider="manual" if mode == "manual" else "mock", created_at=stamp,
            updated_at=stamp, category_id=category_id,
        )
        db.add(note)
        db.flush()
        return note.id


def seed_items(app, user_id, note_id, *specs):
    ids = []
    with app.state.sessions.begin() as db:
        for kind, status in specs:
            item = Item(user_id=user_id, note_id=note_id, kind=kind, status=status, text="Секретная задача")
            db.add(item)
            db.flush()
            ids.append(item.id)
    return ids


def second_user(app, name="second"):
    other = TestClient(app)
    account = register(other, name)
    return other, account


# Related notes and graph

def test_related_endpoint_ranks_notes_and_isolates_owners(app, client):
    register(client)
    launch = manual(client, "Запуск проекта\nОплата ещё не готова, запуск в пятницу, проверить оплату")
    payment = manual(client, "Оплата в проекте\nПодключить оплату картой для проекта")
    manual(client, "Борщ\nСвёкла, капуста, мясо. Варить час.")
    other, _ = second_user(app)
    foreign = manual(other, "Запуск проекта\nОплата проекта, запуск в пятницу, проверить оплату")
    response = client.get(f"/api/v1/notes/{launch['id']}/related")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["note_id"] == launch["id"]
    assert [hit["note_id"] for hit in body["related"]] == [payment["id"]]
    hit = body["related"][0]
    assert set(hit) == {"note_id", "title", "category_id", "updated_at", "score", "shared_terms"}
    assert hit["title"] == "Оплата в проекте" and 0 < hit["score"] <= 1
    assert set(hit["shared_terms"]) == {"проекта", "оплата"}
    assert client.get(f"/api/v1/notes/{foreign['id']}/related").status_code == 404
    assert other.get(f"/api/v1/notes/{launch['id']}/related").status_code == 404
    assert [h["note_id"] for h in other.get(f"/api/v1/notes/{foreign['id']}/related").json()["related"]] == []
    assert client.get("/api/v1/notes/no-such-note/related").status_code == 404
    assert TestClient(app).get(f"/api/v1/notes/{launch['id']}/related").status_code == 401


def test_related_limit_and_category_bonus(app, client):
    register(client)
    category = client.post("/api/v1/categories", json={"name": "Работа"}).json()
    target = manual(client, "Бюджет месяца\nСписок расходов на месяц")
    near = manual(client, "Бюджет недели\nСписок расходов на неделю")
    manual(client, "Бюджет года\nСписок расходов на год")
    path = f"/api/v1/notes/{target['id']}/related"
    assert len(client.get(path).json()["related"]) == 2
    assert len(client.get(path, params={"limit": 1}).json()["related"]) == 1
    assert client.get(path, params={"limit": 0}).status_code == 422
    assert client.get(path, params={"limit": 21}).status_code == 422
    before = {h["note_id"]: h["score"] for h in client.get(path).json()["related"]}
    for note in (target, near):
        done = client.patch(
            f"/api/v1/notes/{note['id']}/category", json={"version": 1, "category_id": category["id"]},
        )
        assert done.status_code == 200, done.text
    after = {h["note_id"]: h["score"] for h in client.get(path).json()["related"]}
    assert after[near["id"]] > before[near["id"]]
    hits = client.get(path).json()["related"]
    assert hits[0]["note_id"] == near["id"] and hits[0]["category_id"] == category["id"]


def test_graph_has_deduplicated_edges_and_only_own_notes(app, client):
    register(client)
    launch = manual(client, "Запуск проекта\nОплата ещё не готова, запуск в пятницу, проверить оплату")
    payment = manual(client, "Оплата в проекте\nПодключить оплату картой для проекта")
    borsch = manual(client, "Борщ\nСвёкла, капуста, мясо. Варить час.")
    shopping = manual(client, "Купить свёклу и капусту\nДля борща")
    for index, note in enumerate((launch, payment, borsch, shopping)):
        set_updated(app, note["id"], 1000.0 + index)
    other, _ = second_user(app)
    foreign = manual(other, "Запуск проекта\nОплата проекта, запуск в пятницу, проверить оплату")
    graph = client.get("/api/v1/graph").json()
    assert [n["id"] for n in graph["nodes"]] == [shopping["id"], borsch["id"], payment["id"], launch["id"]]
    assert set(graph["nodes"][0]) == {"id", "title", "category_id", "updated_at"}
    pairs = [frozenset((e["source"], e["target"])) for e in graph["edges"]]
    assert sorted(pairs, key=sorted) == sorted(
        [frozenset((launch["id"], payment["id"])), frozenset((borsch["id"], shopping["id"]))], key=sorted
    )
    assert all(set(e) == {"source", "target", "score"} and 0 < e["score"] <= 1 for e in graph["edges"])
    assert foreign["id"] not in json.dumps(graph)
    newest = client.get("/api/v1/graph", params={"limit": 2}).json()
    assert [n["id"] for n in newest["nodes"]] == [shopping["id"], borsch["id"]]
    assert [frozenset((e["source"], e["target"])) for e in newest["edges"]] == [
        frozenset((borsch["id"], shopping["id"]))
    ]
    assert client.get("/api/v1/graph", params={"limit": 0}).status_code == 422
    assert other.get("/api/v1/graph").json()["edges"] == []
    assert TestClient(app).get("/api/v1/graph").status_code == 401


# Dashboard

def test_dashboard_counts_on_a_synthetic_dataset(app, client):
    owner = register(client)["id"]
    work = client.post("/api/v1/categories", json={"name": "Работа"}).json()["id"]
    home = client.post("/api/v1/categories", json={"name": "Дом"}).json()["id"]
    client.post("/api/v1/categories", json={"name": "Пусто"})
    first = seed_note(app, owner, local_stamp(0, 10), category_id=work)
    second = seed_note(app, owner, local_stamp(0, 11), channel="telegram", kind="audio")
    seed_note(app, owner, local_stamp(1, 23, 30), mode="manual", category_id=work)
    fourth = seed_note(app, owner, local_stamp(3, 12), category_id=home)
    seed_note(app, owner, local_stamp(20, 12))
    open_ids = seed_items(
        app, owner, first, ("task", "open"), ("task", "open"), ("task", "completed"), ("idea", "open"),
        ("note", "open"),
    )
    done_home = seed_items(app, owner, fourth, ("task", "completed"))
    seed_items(app, owner, second, ("task", "open"))
    with app.state.sessions.begin() as db:
        record_event(db, owner, "task_completed", f"{open_ids[2]}:2", occurred_at=local_stamp(2, 12))
        record_event(db, owner, "task_completed", f"{open_ids[2]}:4", occurred_at=local_stamp(1, 12))
        record_event(db, owner, "task_completed", f"{done_home[0]}:2", occurred_at=local_stamp(10, 12))
        now = time.time()
        for offset, status in ((3600, "confirmed"), (3 * 86400, "confirmed"), (10 * 86400, "confirmed"),
                               (1800, "cancelled")):
            db.add(Reminder(user_id=owner, note_id=first, scheduled_at=now + offset, timezone="Europe/Moscow",
                            text="Секретное напоминание", status=status))
        soon = now + 3600
    other, account = second_user(app)
    foreign = seed_note(app, account["id"], local_stamp(0, 9))
    seed_items(app, account["id"], foreign, ("task", "open"), ("idea", "open"))

    week = client.get("/api/v1/dashboard", params={"days": 7})
    assert week.status_code == 200, week.text
    data = week.json()
    assert data["days"] == 7 and data["timezone"] == "Europe/Moscow"
    today = datetime.now(MOSCOW).date()
    assert data["period"] == {"from": (today - timedelta(days=6)).isoformat(), "to": today.isoformat()}
    assert data["totals"] == {"notes": 5, "notes_in_period": 4}
    assert data["captures"] == {
        "total": 4, "by_channel": {"web": 3, "telegram": 1}, "by_input_kind": {"text": 3, "audio": 1},
        "ai": 3, "manual": 1,
    }
    series = data["notes_per_day"]
    assert [row["date"] for row in series] == [(today - timedelta(days=6 - i)).isoformat() for i in range(7)]
    assert [row["notes"] for row in series] == [0, 0, 0, 1, 0, 1, 2]
    assert data["tasks"] == {"open": 3, "completed": 2, "completed_in_period": 1, "completion_rate": 0.4}
    assert data["ideas"] == 1
    assert data["categories"] == [
        {"category_id": work, "name": "Работа", "notes": 2, "tasks_total": 3, "tasks_done": 1},
        {"category_id": home, "name": "Дом", "notes": 1, "tasks_total": 1, "tasks_done": 1},
        {"category_id": data["categories"][2]["category_id"], "name": "Пусто", "notes": 0,
         "tasks_total": 0, "tasks_done": 0},
    ]
    assert data["streak_days"] == 2
    assert data["reminders"]["upcoming_7d"] == 2
    assert data["reminders"]["next_timezone"] == "Europe/Moscow"
    assert datetime.fromisoformat(data["reminders"]["next_at"]).timestamp() == pytest.approx(soon, abs=1)
    assert data["ai"]["units_used"] == 3 and data["ai"]["units_limit"] == 30
    assert data["ai"]["units_remaining"] == 27 and data["ai"]["resets_at"]
    assert "Секрет" not in week.text

    month = client.get("/api/v1/dashboard").json()
    assert month["days"] == 30 and len(month["notes_per_day"]) == 30
    assert month["totals"]["notes_in_period"] == 5 and month["captures"]["total"] == 5
    assert month["tasks"]["completed_in_period"] == 2
    quarter = client.get("/api/v1/dashboard", params={"days": 90}).json()
    assert len(quarter["notes_per_day"]) == 90 and sum(r["notes"] for r in quarter["notes_per_day"]) == 5

    theirs = other.get("/api/v1/dashboard").json()
    assert theirs["totals"]["notes"] == 1 and theirs["tasks"]["open"] == 1 and theirs["ideas"] == 1
    assert theirs["categories"] == [] and theirs["reminders"]["upcoming_7d"] == 0
    assert theirs["reminders"]["next_at"] is None and theirs["streak_days"] == 1


def test_dashboard_for_a_new_account_is_empty_and_validates_days(app, client):
    register(client)
    data = client.get("/api/v1/dashboard", params={"days": 7}).json()
    assert data["totals"] == {"notes": 0, "notes_in_period": 0} and data["streak_days"] == 0
    assert data["tasks"] == {"open": 0, "completed": 0, "completed_in_period": 0, "completion_rate": None}
    assert data["captures"]["total"] == 0 and data["categories"] == []
    assert data["reminders"] == {"upcoming_7d": 0, "next_at": None, "next_timezone": None}
    assert data["ai"]["units_used"] == 0 and data["ai"]["units_remaining"] == 30
    assert client.get("/api/v1/dashboard", params={"days": 0}).status_code == 422
    assert client.get("/api/v1/dashboard", params={"days": 367}).status_code == 422
    assert client.get("/api/v1/dashboard", params={"days": "x"}).status_code == 422
    assert TestClient(app).get("/api/v1/dashboard").status_code == 401


def test_dashboard_without_a_daily_limit_has_no_remaining_units(app_factory):
    with TestClient(app_factory(daily_unit_limit=0)) as client:
        register(client)
        ai = client.get("/api/v1/dashboard").json()["ai"]
        assert ai["units_limit"] == 0 and ai["units_remaining"] is None


@pytest.mark.parametrize(("days_with_captures", "streak"), [
    ([0, 1, 2, 4], 3),
    ([1, 2], 2),
    ([2], 0),
    ([0], 1),
    ([0, 1, 2, 3, 4, 5, 6, 7, 8, 9], 10),
    ([], 0),
])
def test_streak_counts_consecutive_local_days(app, client, days_with_captures, streak):
    owner = register(client)["id"]
    for days_ago in days_with_captures:
        seed_note(app, owner, local_stamp(days_ago, 12))
    assert client.get("/api/v1/dashboard", params={"days": 7}).json()["streak_days"] == streak


def test_days_follow_the_configured_time_zone(app_factory):
    vladivostok = app_factory(limit_timezone="Asia/Vladivostok")
    moscow = app_factory()
    with TestClient(vladivostok) as east, TestClient(moscow) as west:
        owner = register(east)["id"]
        west.cookies.update(east.cookies)
        late = local_stamp(1, 23, 30, VLADIVOSTOK)
        early = local_stamp(0, 0, 30, VLADIVOSTOK)
        for stamp in (late, early):
            seed_note(vladivostok, owner, stamp)
        today = datetime.now(VLADIVOSTOK).date()
        data = east.get("/api/v1/dashboard", params={"days": 7}).json()
        assert data["timezone"] == "Asia/Vladivostok" and data["period"]["to"] == today.isoformat()
        assert [row["notes"] for row in data["notes_per_day"]] == [0, 0, 0, 0, 0, 1, 1]
        assert data["notes_per_day"][-2]["date"] == (today - timedelta(days=1)).isoformat()
        # Seven hours earlier on the Moscow clock both captures belong to the same calendar day.
        assert sorted(row["notes"] for row in west.get("/api/v1/dashboard", params={"days": 7}).json()[
            "notes_per_day"]) == [0, 0, 0, 0, 0, 0, 2]


# Reminder proposals in the note detail

def test_note_detail_proposes_a_reminder_for_an_open_task_with_a_deadline(app, client):
    register(client)
    note = ready_note(app, client)
    task = next(item for item in note["items"] if item["kind"] == "task")
    tomorrow = datetime.now(MOSCOW).date() + timedelta(days=1)
    assert task["due_text"] == "завтра"
    assert task["proposed_reminder"] == {
        "local_time": f"{tomorrow:%Y-%m-%d}T09:00:00", "timezone": "Europe/Moscow", "label": "завтра в 09:00",
    }
    assert [i["proposed_reminder"] for i in note["items"] if i["kind"] != "task"] == [None]
    stored = client.get(f"/api/v1/notes/{note['id']}/revisions").json()[0]["items"]
    assert all("proposed_reminder" not in item for item in stored)
    assert client.get(f"/api/v1/notes/{note['id']}").json()["items"] == note["items"]


def test_proposal_goes_away_with_a_reminder_and_returns_after_cancel(app, client):
    register(client)
    note = ready_note(app, client)
    task = next(item for item in note["items"] if item["kind"] == "task")
    path = f"/api/v1/notes/{note['id']}"

    def current():
        return next(i for i in client.get(path).json()["items"] if i["id"] == task["id"])["proposed_reminder"]

    created = add_reminder(client, {"note": note["id"], "item": task["id"]}, key="proposal-1")
    assert created.status_code == 201, created.text
    assert current() is None
    cancelled = client.post(f"/api/v1/reminders/{created.json()['id']}/cancel", json={"generation": 1})
    assert cancelled.status_code == 200, cancelled.text
    assert current()["label"] == "завтра в 09:00"
    done = client.patch(f"{path}/items/{task['id']}", json={
        "version": note["version"], "kind": "task", "text": task["text"], "status": "completed",
    })
    assert done.status_code == 200, done.text
    assert all(i["proposed_reminder"] is None for i in done.json()["items"])
    assert current() is None


@pytest.mark.parametrize("due_text", [None, "скоро", "на следующей неделе", "вчера"])
def test_no_proposal_without_a_readable_future_deadline(app, client, due_text):
    register(client)
    note = ready_note(app, client)
    with app.state.sessions.begin() as db:
        for item in db.scalars(select(Item).where(Item.note_id == note["id"])):
            item.due_text = due_text
    assert all(i["proposed_reminder"] is None for i in client.get(f"/api/v1/notes/{note['id']}").json()["items"])


def test_proposal_uses_the_configured_time_zone_and_is_private(app_factory):
    app = app_factory(limit_timezone="Asia/Vladivostok")
    with TestClient(app) as client:
        register(client)
        note = ready_note(app, client)
        task = next(item for item in note["items"] if item["kind"] == "task")
        assert task["proposed_reminder"]["timezone"] == "Asia/Vladivostok"
        tomorrow = datetime.now(VLADIVOSTOK).date() + timedelta(days=1)
        assert task["proposed_reminder"]["local_time"] == f"{tomorrow:%Y-%m-%d}T09:00:00"
        other, _ = second_user(app)
        assert other.get(f"/api/v1/notes/{note['id']}").status_code == 404


# Telegram hint

def test_processing_reply_offers_a_reminder_for_a_task_with_a_deadline(reminders):
    app, client, ids = reminders
    processed(app, client)
    delivery = claim(client).json()["items"][0]
    text = delivery["text"]
    assert text.startswith("Заметка готова.")
    assert text.split("\n")[-1] == (
        "Предлагаю напоминание: завтра в 09:00, «Андрей завтра уточнит ситуацию.». "
        "Подтвердить или изменить время: " + delivery["note_url"] + "#remind"
    )
    assert "\n\nПредлагаю напоминание" in text
    assert len(text.encode("utf-16-le")) <= 4096 * 2


def test_processing_reply_has_no_hint_without_a_deadline_or_with_a_reminder(reminders):
    app, client, ids = reminders
    assert message(client, update_id=300, text="Просто мысль без задач").status_code == 200
    assert Worker(app.state.sessions, app.state.settings, provider=MockProvider()).run_once()
    plain = claim(client).json()["items"][0]
    assert plain["text"].startswith("Заметка готова.") and "Предлагаю" not in plain["text"]
    assert message(client, update_id=301).status_code == 200
    assert Worker(app.state.sessions, app.state.settings, provider=MockProvider()).run_once()
    with app.state.sessions() as db:
        # due_text is encrypted, so it is compared after reading, not in SQL.
        item = next(row for row in db.scalars(select(Item).where(Item.kind == "task")) if row.due_text == "завтра")
        target = {"note": item.note_id, "item": item.id}
    assert add_reminder(client, target, key="telegram-hint").status_code == 201
    confirmed = claim(client).json()["items"]
    assert len(confirmed) == 1 and "Предлагаю" not in confirmed[0]["text"]


def test_hint_keeps_the_telegram_length_limit(reminders):
    app, client, ids = reminders
    saved = processed(app, client)
    with app.state.sessions.begin() as db:
        db.scalar(select(Note).where(Note.capture_id == saved["capture_id"])).markdown = "😀" * 6000
    text = claim(client).json()["items"][0]["text"]
    assert len(text.encode("utf-16-le")) <= 4096 * 2
    assert text.startswith("Заметка готова.") and "Полный результат в Beresta." in text
    assert text.split("\n")[-1].startswith("Предлагаю напоминание: завтра в 09:00")
    assert text.endswith("#remind")


# AI categories

class Named(MockProvider):
    name = "Идеи для отпуска"

    def structure(self, text, *, categories=()):
        return super().structure(text, categories=categories).model_copy(update={"category_name": self.name})


def process(app, client, provider, key="request-1"):
    response = submit(client, key=key)
    assert response.status_code == 202, response.text
    assert Worker(app.state.sessions, app.state.settings, provider).run_once()
    job = client.get("/api/v1/jobs/" + response.json()["id"]).json()
    assert job["status"] == "succeeded", job
    return client.get("/api/v1/notes/" + job["note_id"]).json()


def test_ai_category_is_a_normal_category_created_at_once_and_renamable(app, client):
    register(client)
    note = process(app, client, Named())
    categories = client.get("/api/v1/categories").json()
    assert [c["name"] for c in categories] == ["Идеи для отпуска"]
    assert note["category_id"] == categories[0]["id"] and note["category_name"] == "Идеи для отпуска"
    path = "/api/v1/categories/" + categories[0]["id"]
    renamed = client.patch(path, json={"version": categories[0]["version"], "name": "Отпуск"})
    assert renamed.status_code == 200 and renamed.json()["name"] == "Отпуск"
    assert client.get("/api/v1/notes/" + note["id"]).json()["category_name"] == "Отпуск"
    other = manual(client, "Ещё одна запись")
    moved = client.patch(
        f"/api/v1/notes/{other['id']}/category", json={"version": 1, "category_id": categories[0]["id"]},
    )
    assert moved.status_code == 200 and moved.json()["category_name"] == "Отпуск"


def test_ai_category_reuses_an_existing_name_regardless_of_case(app, client):
    register(client)
    existing = client.post("/api/v1/categories", json={"name": "идеи для отпуска"}).json()
    note = process(app, client, Named())
    assert [c["id"] for c in client.get("/api/v1/categories").json()] == [existing["id"]]
    assert note["category_id"] == existing["id"]


def test_ai_category_at_the_limit_leaves_the_note_uncategorized_without_failing(app, client):
    owner = register(client)["id"]
    with app.state.sessions.begin() as db:
        db.add_all([Category(user_id=owner, name=f"Категория {i}") for i in range(100)])
    note = process(app, client, Named())
    assert note["category_id"] is None and note["category_name"] is None
    assert len(client.get("/api/v1/categories").json()) == 100
