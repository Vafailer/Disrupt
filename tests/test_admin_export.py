"""The ZIP archive for the contest organizers: files, definitions, pseudonyms and exclusions."""

import csv
import io
import zipfile
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import admin_cli
from app.admin_export import build_archive
from app.admin_metrics import aggregate, pseudonym
from app.models import AssistantRequest, Capture, Job, ProviderUsage, User, new_id
from tests.conftest import ADMIN_ADDRESS, admin_login
from tests.test_admin_metrics import FILTER, KEY, NOW, seed, stamp

FILES = {"README.txt", "daily.csv", "users.csv", "events.csv", "ai_calls.csv", "errors.csv", "errors_by_day.csv"}
URL = "/admin-api/v1/export.zip"


@pytest.fixture
def app(app_factory):
    return app_factory(analytics_pseudonym_key=KEY.decode())


@pytest.fixture
def client(admin_client):
    return admin_client


@pytest.fixture
def populated(app, monkeypatch):
    ids = seed(app.state.sessions)
    original, summary = build_archive, aggregate
    monkeypatch.setattr("app.routes.admin.build_archive", lambda *args, **kwargs: original(*args, **kwargs, now=NOW))
    monkeypatch.setattr("app.routes.admin.aggregate", lambda *args, **kwargs: summary(*args, **kwargs, now=NOW))
    with app.state.sessions() as db:
        ids["late_test"] = db.scalar(select(User.id).where(User.username.like("late_test_%")))
        ids["was_test"] = db.scalar(select(User.id).where(User.username.like("was_test_%")))
    return ids


def archive(client, **params):
    response = client.get(URL, params={**FILTER, **params})
    assert response.status_code == 200, response.text
    with zipfile.ZipFile(io.BytesIO(response.content)) as bundle:
        return response, {name: bundle.read(name).decode("utf-8") for name in bundle.namelist()}


def table(files, name):
    return list(csv.DictReader(io.StringIO(files[name])))


def who(user_id):
    return pseudonym(KEY, "user", user_id)


def test_archive_contains_the_agreed_files_and_download_headers(client, populated):
    response, files = archive(client)
    assert response.headers["content-type"] == "application/zip"
    assert response.headers["content-disposition"] == 'attachment; filename="beresta-metrics-2026-10-05-2026-10-06.zip"'
    assert response.headers["cache-control"] == "no-store"
    assert set(files) == FILES
    header = {name: files[name].splitlines()[0] for name in FILES if name.endswith(".csv")}
    assert header["daily.csv"] == ("date,dau,new_users,captures_text,captures_audio,captures_web,captures_telegram,"
                                   "ai_calls,ai_calls_per_dau,errors")
    assert header["users.csv"] == "user_id,registered_date,first_target_scenario_date,active_days,channel_mix"
    assert header["events.csv"] == "user_id,timestamp_utc,event,channel"
    assert header["ai_calls.csv"] == ("timestamp_utc,user_id,kind,model,status,latency_ms,input_tokens,"
                                      "output_tokens,cost")
    assert header["errors.csv"] == "timestamp_utc,date_msk,source,error_code"
    assert header["errors_by_day.csv"] == "date_msk,source,error_code,count"


def test_daily_counts_real_users_only_in_dau_and_matches_the_dashboard(client, populated):
    _, files = archive(client)
    daily = table(files, "daily.csv")
    assert daily == [
        {"date": "2026-10-05", "dau": "1", "new_users": "1", "captures_text": "1", "captures_audio": "0",
         "captures_web": "0", "captures_telegram": "1", "ai_calls": "1", "ai_calls_per_dau": "1.0000", "errors": "0"},
        {"date": "2026-10-06", "dau": "2", "new_users": "1", "captures_text": "1", "captures_audio": "0",
         "captures_web": "1", "captures_telegram": "0", "ai_calls": "1", "ai_calls_per_dau": "0.5000", "errors": "0"},
    ]
    dashboard = client.get("/admin-api/v1/summary", params=FILTER).json()
    assert [row["dau"] for row in dashboard["daily"]] == [int(row["dau"]) for row in daily]
    # The day with nobody active stays in the table, and the ratio is empty rather than zero.
    _, empty = archive(client, **{"from": "2026-10-03", "to": "2026-10-04"})
    assert [(r["date"], r["dau"], r["ai_calls_per_dau"]) for r in table(empty, "daily.csv")] == [
        ("2026-10-03", "0", ""), ("2026-10-04", "0", "")]


def test_users_are_pseudonymous_and_real_users_have_a_first_scenario_date(client, populated):
    _, files = archive(client)
    users = {row["user_id"]: row for row in table(files, "users.csv")}
    assert users[who(populated["ai"])] == {
        "user_id": who(populated["ai"]), "registered_date": "2026-10-05", "first_target_scenario_date": "2026-10-05",
        "active_days": "2", "channel_mix": "telegram:1|web:2"}
    assert users[who(populated["manual"])]["first_target_scenario_date"] == "2026-10-06"
    assert users[who(populated["manual"])]["active_days"] == "1"
    # Registered after the period, or marked as test: not in the file at all.
    assert who(populated["pending"]) not in users and who(populated["late_test"]) not in users
    # A person who never finished a scenario has no first-scenario date.
    assert users[who(populated["was_test"])]["first_target_scenario_date"] == ""
    assert all(name.startswith("user_") and len(name) == 37 for name in users)


def test_events_are_content_free_utc_and_skip_test_accounts(client, populated):
    _, files = archive(client)
    events = table(files, "events.csv")
    assert {row["user_id"] for row in events} == {who(populated["ai"]), who(populated["manual"])}
    assert {"registered", "capture_saved", "note_opened", "structure_confirmed", "processing_completed"} <= {
        row["event"] for row in events}
    for row in events:
        assert datetime.fromisoformat(row["timestamp_utc"]).utcoffset() == timedelta(0)
        assert set(row) == {"user_id", "timestamp_utc", "event", "channel"}
    saved = next(row for row in events if row["event"] == "capture_saved" and row["user_id"] == who(populated["ai"]))
    assert saved["timestamp_utc"] == "2026-10-05T06:02:00+00:00" and saved["channel"] == "telegram"
    # Outside the period: the pending account's sign-in on 7 October.
    assert who(populated["pending"]) not in files["events.csv"]


def test_ai_calls_come_from_receipts_with_kinds_and_unknown_cost_left_empty(app, client, populated):
    with app.state.sessions() as db:
        db.add_all([
            ProviderUsage(user_id=populated["ai"], operation_id=new_id(), request_id=new_id() + ":assistant",
                          kind="llm", channel="web", model="mock", status="succeeded", cost=Decimal(0),
                          occurred_at=stamp("2026-10-06T13:00:00")),
            ProviderUsage(user_id=populated["manual"], operation_id=new_id(), request_id=new_id(), kind="stt",
                          channel="telegram", model="mock", status="succeeded", occurred_at=stamp("2026-10-06T14:00:00")),
            # A team account's call must not appear, even though the row itself is not marked.
            ProviderUsage(user_id=populated["late_test"], operation_id=new_id(), request_id=new_id(), kind="llm",
                          channel="web", model="mock", status="succeeded", occurred_at=stamp("2026-10-06T15:00:00")),
        ])
        db.commit()
    _, files = archive(client)
    calls = table(files, "ai_calls.csv")
    assert [row["kind"] for row in calls] == ["llm", "llm", "assistant", "stt"]
    first, failed = calls[0], calls[1]
    assert first == {"timestamp_utc": "2026-10-05T06:02:00+00:00", "user_id": who(populated["ai"]), "kind": "llm",
                     "model": "mock", "status": "succeeded", "latency_ms": "100", "input_tokens": "100",
                     "output_tokens": "50", "cost": "0.25000000"}
    assert failed["cost"] == "" and failed["input_tokens"] == "" and failed["status"] == "failed"
    assert who(populated["late_test"]) not in files["ai_calls.csv"]
    assert table(files, "daily.csv")[1]["ai_calls"] == "3"


def test_errors_cover_jobs_and_assistant_requests_with_counts_per_day(app, client, populated):
    with app.state.sessions() as db:
        for owner in (populated["ai"], populated["late_test"]):
            capture = Capture(id=new_id(), user_id=owner, original_text="Synthetic", idempotency_key=new_id(),
                              created_at=stamp("2026-10-06T09:00:00"))
            db.add(capture)
            db.flush()
            db.add(Job(user_id=owner, capture_id=capture.id, provider="mock", status="failed",
                       error_code="provider_timeout", created_at=stamp("2026-10-06T09:59:00"),
                       finished_at=stamp("2026-10-06T10:00:00")))
            db.add(AssistantRequest(user_id=owner, kind="ask", status="failed", error_code="provider_http_error",
                                    input_note_ids=[], idempotency_key=new_id(), request_hash="synthetic", units=1,
                                    created_at=stamp("2026-10-06T11:59:00"), finished_at=stamp("2026-10-06T12:00:00")))
        db.commit()
    _, files = archive(client)
    assert table(files, "errors.csv") == [
        {"timestamp_utc": "2026-10-06T07:00:00+00:00", "date_msk": "2026-10-06", "source": "job",
         "error_code": "provider_timeout"},
        {"timestamp_utc": "2026-10-06T09:00:00+00:00", "date_msk": "2026-10-06", "source": "assistant",
         "error_code": "provider_http_error"},
    ]
    assert table(files, "errors_by_day.csv") == [
        {"date_msk": "2026-10-06", "source": "assistant", "error_code": "provider_http_error", "count": "1"},
        {"date_msk": "2026-10-06", "source": "job", "error_code": "provider_timeout", "count": "1"},
    ]
    assert [row["errors"] for row in table(files, "daily.csv")] == ["0", "2"]


def test_no_usernames_text_or_raw_identifiers_anywhere_in_the_archive(client, populated):
    _, files = archive(client)
    raw = [populated["ai"], populated["manual"], populated["pending"], populated["job"], populated["note"]]
    for name, content in files.items():
        for hidden in populated["private"] + raw:
            assert hidden not in content, (name, hidden)
    assert "password" not in "".join(files.values()).lower()


def test_pseudonyms_are_stable_between_downloads_and_match_the_dashboard(client, populated):
    _, first = archive(client)
    _, second = archive(client)
    assert first == second
    usage = client.get("/admin-api/v1/summary", params=FILTER).json()["usage"]
    assert {row["user_pseudonym"] for row in usage} == {who(populated["ai"])}


def test_another_key_gives_other_pseudonyms(app_factory, client, populated, admin_secret):
    _, files = archive(client)
    other = app_factory(analytics_pseudonym_key="another-synthetic-pseudonym-key-0000")
    with TestClient(other, client=ADMIN_ADDRESS) as second:
        assert admin_login(other, second, admin_secret).status_code == 200
        _, changed = archive(second)
    old = {row["user_id"] for row in table(files, "users.csv")}
    new = {row["user_id"] for row in table(changed, "users.csv")}
    assert old and new and old.isdisjoint(new)
    assert table(files, "daily.csv") == table(changed, "daily.csv")


def test_readme_gives_the_contest_definitions_in_russian(client, populated):
    _, files = archive(client)
    text = files["README.txt"]
    for phrase in ["Реальный пользователь", "хотя бы один целевой сценарий", "DAU", "Europe/Moscow", "Тестовые аккаунты",
                   "псевдонимами", "HMAC-SHA256", "2026-10-05", "2026-10-06", "2026-10-07T09:00:00+00:00"]:
        assert phrase in text, phrase
    assert "—" not in text and "один и тот же псевдоним во всех файлах и во всех архивах" in text


def test_readme_warns_when_pseudonyms_depend_on_a_temporary_key(app_factory, populated, admin_secret):
    keyless = app_factory()
    with TestClient(keyless, client=ADMIN_ADDRESS) as client:
        assert admin_login(keyless, client, admin_secret).status_code == 200
        _, files = archive(client)
    assert "после перезапуска сервиса" in files["README.txt"]


@pytest.mark.parametrize("params,status", [
    ({"from": "2026-07-06", "to": "2026-10-05"}, 200),  # Exactly 92 days.
    ({"from": "2026-07-05", "to": "2026-10-05"}, 422),
    ({"from": "2026-10-06", "to": "2026-10-05"}, 422),
    ({"from": "2026-10-05", "to": "2026-10-08"}, 422),  # In the future.
    ({"from": "2026-02-30", "to": "2026-10-05"}, 422),
    ({"from": "05.10.2026", "to": "2026-10-05"}, 422),
    ({"from": "2026-10-05"}, 422),
])
def test_period_rules(client, populated, params, status):
    response = client.get(URL, params=params)
    assert response.status_code == status
    assert response.headers["cache-control"] == "no-store"
    if status == 200:
        assert len(table(archive(client, **params)[1], "daily.csv")) == 92


def test_size_cap_refuses_instead_of_building_a_huge_archive(client, populated, monkeypatch):
    monkeypatch.setattr("app.admin_export.MAX_ROWS", 3)
    response = client.get(URL, params=FILTER)
    assert response.status_code == 413 and "период" in response.json()["detail"].lower()
    monkeypatch.setattr("app.admin_export.MAX_ROWS", 250_000)
    monkeypatch.setattr("app.admin_export.MAX_HISTORY", 3)
    assert client.get(URL, params=FILTER).status_code == 413


def test_export_is_written_to_the_audit_log_without_content(client, populated):
    archive(client)
    rows = client.get("/admin-api/v1/audit").json()
    export = next(row for row in rows if row["action"] == "export_zip")
    assert export["admin"] == "owner" and export["details"]["from"] == "2026-10-05" and export["details"]["to"] == "2026-10-06"
    assert set(export["details"]) == {"from", "to", "days", "users", "events", "ai_calls", "errors"}
    client.get("/admin-api/v1/export", params=FILTER)
    assert client.get("/admin-api/v1/audit").json()[0]["action"] == "export_csv"
    # A refused or invalid request leaves no export entry.
    client.get(URL, params={"from": "2026-10-06", "to": "2026-10-05"})
    assert [row["action"] for row in client.get("/admin-api/v1/audit").json()].count("export_zip") == 1


def test_archive_needs_an_administrator_and_a_private_address(app, populated, private_client):
    assert private_client.get(URL, params=FILTER).status_code == 401
    with TestClient(app, client=("8.8.8.8", 1)) as outside:
        assert outside.get(URL, params=FILTER).status_code == 404


def test_marking_an_account_as_test_removes_it_from_the_archive(client, populated):
    _, before = archive(client)
    assert who(populated["ai"]) in before["users.csv"]
    with client.app.state.sessions() as db:
        name = db.get(User, populated["ai"]).username
    admin_cli.main(["mark-test-user", name])
    _, after = archive(client)
    assert all(who(populated["ai"]) not in content for content in after.values())
    assert [row["dau"] for row in table(after, "daily.csv")] == ["0", "1"]
    assert [row["ai_calls"] for row in table(after, "daily.csv")] == ["0", "0"]
    admin_cli.main(["unmark-test-user", name])
    _, again = archive(client)
    assert again["users.csv"] == before["users.csv"]
