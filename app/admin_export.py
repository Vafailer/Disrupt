"""ZIP archive of content-free metrics for the contest organizers.

No usernames, no note text, no raw account IDs. Test accounts are excluded everywhere.
"""

import csv
import io
import math
import time
import zipfile
from collections import Counter, defaultdict
from datetime import date, timedelta

from fastapi import HTTPException
from sqlalchemy import select

from app.admin_metrics import ACTIVE, calendar_day, completion, midnight, money, pseudonym, safe_cell, utc
from app.models import AssistantRequest, Capture, Job, Note, ProductEvent, ProviderUsage, User

MAX_DAYS = 92
MAX_ROWS = 250_000
MAX_HISTORY = 500_000
MAX_BYTES = 64 * 1024 * 1024
ORDER = {"capture_saved": 0, "note_opened": 1, "structure_confirmed": 2, "note_edited": 2}


def archive_period(start, end, now):
    try:
        first, last = date.fromisoformat(start), date.fromisoformat(end)
    except ValueError:
        raise HTTPException(422, "Проверьте даты периода") from None
    if first > last:
        raise HTTPException(422, "Начало периода позже его конца")
    if (last - first).days + 1 > MAX_DAYS:
        raise HTTPException(422, f"Период не может быть длиннее {MAX_DAYS} дней")
    if last > calendar_day(now):
        raise HTTPException(422, "Конец периода не может быть в будущем")
    return first, last


def too_large():
    return HTTPException(413, "Слишком много данных для одного архива. Выберите период короче.")


def make_csv(header, rows):
    out = io.StringIO(newline="")
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(header)
    for count, row in enumerate(rows, 1):
        if count > MAX_ROWS:
            raise too_large()
        writer.writerow([safe_cell(cell) for cell in row])
    return out.getvalue()


def readme(first, last, generated, stable_key):
    pseudonyms = (
        "Идентификаторы пользователей заменены псевдонимами. Это HMAC-SHA256 от внутреннего ID аккаунта "
        "с секретным ключом сервиса, обрезанный до 32 символов. Один человек получает один и тот же "
        "псевдоним во всех файлах и во всех архивах, пока ключ не меняют. По псевдониму нельзя узнать "
        "имя, контакты или записи человека."
        if stable_key else
        "Идентификаторы пользователей заменены псевдонимами. Это HMAC-SHA256 от внутреннего ID аккаунта "
        "с ключом, который сервис создал при запуске. Внутри этого архива один человек везде имеет один "
        "псевдоним, но в архиве, скачанном после перезапуска сервиса, псевдонимы будут другими. "
        "По псевдониму нельзя узнать имя, контакты или записи человека."
    )
    return "\n".join([
        "Архив метрик beresta",
        "",
        f"Период с {first.isoformat()} по {last.isoformat()} включительно, дни по Europe/Moscow.",
        f"Архив собран {generated} (UTC).",
        "",
        "ОПРЕДЕЛЕНИЯ",
        "",
        "Реальный пользователь. Человек, который выполнил хотя бы один целевой сценарий. "
        "Сценарий с ИИ. Человек сохранил запись с обработкой ИИ, открыл готовую заметку и подтвердил "
        "структуру или исправил заметку. Сценарий без ИИ. Человек сохранил запись вручную, открыл её, "
        "затем нашёл заметку поиском или подтвердил напоминание. Сценарий считается по всей истории "
        "аккаунта до конца периода, а не только внутри выбранных дат.",
        "",
        "DAU. Число уникальных реальных пользователей за календарный день по Europe/Moscow. "
        "В день попадает тот, кто в этот день сохранил запись, открыл результат, исправил заметку, "
        "подтвердил напоминание или выполнил задачу. Вход, фоновая обработка и доставка уведомлений "
        "активностью не считаются. Один человек в двух каналах считается один раз.",
        "",
        "Исключения. Тестовые аккаунты и аккаунты команды в архив не попадают совсем. Не попадают "
        "и события, которые были записаны, когда аккаунт был тестовым.",
        "",
        "Что считается по всем нетестовым аккаунтам, а не только по реальным пользователям. "
        "Записи (captures_*), вызовы ИИ (ai_calls), ошибки (errors) и файл events.csv. "
        "DAU, new_users и active_days считаются иначе. DAU и new_users берут только реальных "
        "пользователей. active_days в users.csv показывает дни с активностью для любого аккаунта.",
        "",
        "ФАЙЛЫ",
        "",
        "daily.csv. По одной строке на каждый день периода, даже без активности. Столбцы date, dau, "
        "new_users (реальные пользователи, зарегистрированные в этот день), captures_text, "
        "captures_audio, captures_web, captures_telegram, ai_calls, ai_calls_per_dau (пусто, если dau "
        "равен нулю), errors.",
        "",
        "users.csv. Один человек в строке. Столбцы user_id (псевдоним), registered_date (пусто, если "
        "дата регистрации неизвестна), first_target_scenario_date (пусто, если человек ещё не "
        "реальный пользователь), active_days (дни с активностью внутри периода), channel_mix "
        "(активные события по каналам внутри периода, например telegram:2|web:5).",
        "",
        "events.csv. События продукта без содержимого. Столбцы user_id (псевдоним), timestamp_utc, "
        "event, channel.",
        "",
        "ai_calls.csv. Вызовы ИИ с квитанцией. Столбцы timestamp_utc, user_id (псевдоним), kind (llm, "
        "stt или assistant), model, status, latency_ms, input_tokens, output_tokens, cost (в рублях, "
        "пусто, если стоимость неизвестна). Старые вызовы без квитанции сюда не входят. Пустая "
        "ячейка всегда означает, что значение неизвестно.",
        "",
        "errors.csv. Каждая ошибка обработки заметки или запроса к ассистенту. Столбцы timestamp_utc, "
        "date_msk, source (job или assistant), error_code.",
        "",
        "errors_by_day.csv. Те же ошибки, посчитанные по дням. Столбцы date_msk, source, error_code, count.",
        "",
        "ПСЕВДОНИМЫ",
        "",
        pseudonyms,
        "",
        "В архиве нет имён пользователей, паролей, текстов записей, расшифровок, аудио и контактов.",
        "",
    ])


def build_archive(db, start, end, key, *, stable_key=True, now=None):
    """Return (ZIP bytes, row counts). Everything is read from one snapshot of the database."""
    now = time.time() if now is None else now
    first, last = archive_period(start, end, now)
    low = midnight(first)
    high = min(midnight(last + timedelta(days=1)), math.nextafter(now, math.inf))
    days = [first + timedelta(days=offset) for offset in range((last - first).days + 1)]

    def user_id(raw):
        return pseudonym(key, "user", raw)

    accounts = {row.id: row for row in db.execute(
        select(User.id, User.created_at, User.first_login_at).where(User.is_test.is_(False))
    )}
    history = list(db.scalars(
        select(ProductEvent).join(User, User.id == ProductEvent.user_id)
        .where(User.is_test.is_(False), ProductEvent.is_test.is_(False), ProductEvent.occurred_at < high)
        .order_by(ProductEvent.occurred_at, ProductEvent.id).limit(MAX_HISTORY + 1)
    ))
    if len(history) > MAX_HISTORY:
        raise too_large()
    history.sort(key=lambda e: (e.occurred_at, ORDER.get(e.name, 3), e.id))
    captures = dict(db.execute(
        select(Capture.id, Capture.processing_mode).join(User, User.id == Capture.user_id).where(User.is_test.is_(False))
    ).all())
    notes = dict(db.execute(
        select(Note.id, Note.capture_id).join(User, User.id == Note.user_id).where(User.is_test.is_(False))
    ).all())

    by_user = defaultdict(list)
    for event in history:
        by_user[event.user_id].append(event)
    horizon = math.nextafter(high, -math.inf)
    first_target = {}
    for uid, own in by_user.items():
        done = [t for mode in ("ai", "manual")
                if (t := completion(own, captures, notes, mode, 0.0, horizon)) is not None]
        if done:
            first_target[uid] = min(done)

    window = [event for event in history if event.occurred_at >= low]
    real_by_day, active_days, channel_mix = defaultdict(set), defaultdict(set), defaultdict(Counter)
    for event in window:
        if event.name not in ACTIVE:
            continue
        day = calendar_day(event.occurred_at)
        active_days[event.user_id].add(day)
        channel_mix[event.user_id][event.channel] += 1
        if event.user_id in first_target:
            real_by_day[day].add(event.user_id)

    def registered(uid):
        row = accounts[uid]
        return row.created_at if row.created_at is not None else row.first_login_at

    new_by_day = Counter(
        calendar_day(registered(uid)) for uid in first_target
        if uid in accounts and registered(uid) is not None and low <= registered(uid) < high
    )
    touched = {event.user_id for event in window}
    user_rows = []
    for uid in accounts:
        stamp = registered(uid)
        if not (stamp is not None and stamp < high or uid in touched):
            continue
        mix = "|".join(f"{name}:{count}" for name, count in sorted(channel_mix[uid].items()))
        user_rows.append((
            user_id(uid),
            calendar_day(stamp).isoformat() if stamp is not None else None,
            calendar_day(first_target[uid]).isoformat() if uid in first_target else None,
            len(active_days[uid]), mix or None,
        ))
    user_rows.sort(key=lambda row: row[0])

    counts = defaultdict(Counter)
    for created, kind, channel in db.execute(
        select(Capture.created_at, Capture.input_kind, Capture.channel).join(User, User.id == Capture.user_id)
        .where(User.is_test.is_(False), Capture.created_at >= low, Capture.created_at < high)
    ):
        day = calendar_day(created)
        counts[day]["captures_" + ("audio" if kind == "audio" else "text")] += 1
        if channel in {"web", "telegram"}:
            counts[day]["captures_" + channel] += 1

    usage = list(db.execute(
        select(ProviderUsage.occurred_at, ProviderUsage.user_id, ProviderUsage.kind, ProviderUsage.request_id,
               ProviderUsage.model, ProviderUsage.status, ProviderUsage.latency_ms, ProviderUsage.input_tokens,
               ProviderUsage.output_tokens, ProviderUsage.cost)
        .join(User, User.id == ProviderUsage.user_id)
        .where(User.is_test.is_(False), ProviderUsage.is_test.is_(False),
               ProviderUsage.occurred_at >= low, ProviderUsage.occurred_at < high)
        .order_by(ProviderUsage.occurred_at, ProviderUsage.id).limit(MAX_ROWS + 1)
    ))
    if len(usage) > MAX_ROWS:
        raise too_large()
    calls = Counter(calendar_day(row.occurred_at) for row in usage)

    errors = []
    for model, source in ((Job, "job"), (AssistantRequest, "assistant")):
        errors.extend((row.finished_at, source, row.error_code or "unknown") for row in db.execute(
            select(model.finished_at, model.error_code).join(User, User.id == model.user_id)
            .where(User.is_test.is_(False), model.status == "failed",
                   model.finished_at >= low, model.finished_at < high)
        ))
    errors.sort(key=lambda row: (row[0], row[1], row[2]))
    errors_by_day = Counter((calendar_day(at), source, code) for at, source, code in errors)

    daily = []
    for day in days:
        dau = len(real_by_day[day])
        counter = counts[day]
        daily.append((
            day.isoformat(), dau, new_by_day[day], counter["captures_text"], counter["captures_audio"],
            counter["captures_web"], counter["captures_telegram"], calls[day],
            f"{calls[day] / dau:.4f}" if dau else None, sum(n for (d, _, _), n in errors_by_day.items() if d == day),
        ))

    files = {
        "daily.csv": make_csv(
            ("date", "dau", "new_users", "captures_text", "captures_audio", "captures_web",
             "captures_telegram", "ai_calls", "ai_calls_per_dau", "errors"), daily),
        "users.csv": make_csv(
            ("user_id", "registered_date", "first_target_scenario_date", "active_days", "channel_mix"), user_rows),
        "events.csv": make_csv(
            ("user_id", "timestamp_utc", "event", "channel"),
            ((user_id(e.user_id), utc(e.occurred_at), e.name, e.channel) for e in window)),
        "ai_calls.csv": make_csv(
            ("timestamp_utc", "user_id", "kind", "model", "status", "latency_ms", "input_tokens",
             "output_tokens", "cost"),
            ((utc(r.occurred_at), user_id(r.user_id), "assistant" if r.request_id.endswith(":assistant") else r.kind,
              r.model, r.status, r.latency_ms, r.input_tokens, r.output_tokens, money(r.cost)) for r in usage)),
        "errors.csv": make_csv(
            ("timestamp_utc", "date_msk", "source", "error_code"),
            ((utc(at), calendar_day(at).isoformat(), source, code) for at, source, code in errors)),
        "errors_by_day.csv": make_csv(
            ("date_msk", "source", "error_code", "count"),
            ((d.isoformat(), source, code, n) for (d, source, code), n in sorted(errors_by_day.items()))),
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("README.txt", readme(first, last, utc(now), stable_key))
        for name, content in files.items():
            archive.writestr(name, content)
    data = buffer.getvalue()
    if len(data) > MAX_BYTES:
        raise too_large()
    stats = {"days": len(days), "users": len(user_rows), "events": len(window), "ai_calls": len(usage),
             "errors": len(errors)}
    return data, stats
