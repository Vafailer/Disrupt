import math
import re
import time
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError

from app.analytics import record_event
from app.audio_contracts import AUDIO_MEDIA_TYPES, MAX_AUDIO_BYTES, MAX_AUDIO_SECONDS, StoredAudioLike
from app.integration import IntegrationRejection
from app.models import (
    Capture,
    Category,
    Item,
    Job,
    Note,
    Outbox,
    Reminder,
    Revision,
    TranscriptRevision,
    User,
    new_id,
)

MAX_CATEGORIES = 100
MAX_ITEMS = 30


def lock_account(db, user_id):
    db.execute(update(User).where(User.id == user_id).values(source=User.source))


def capture_text(db, user_id, text, key, settings, *, processing_mode="ai", channel="web", commit=True):
    # Serialize quota checking for this account before counting queued jobs.
    lock_account(db, user_id)

    def existing():
        capture = db.scalar(select(Capture).where(Capture.user_id == user_id, Capture.idempotency_key == key))
        if capture:
            if (
                capture.input_kind != "text"
                or capture.original_text != text
                or capture.processing_mode != processing_mode
            ):
                raise HTTPException(409, "Этот Idempotency-Key уже использован для другого текста")
            if processing_mode == "manual":
                return db.scalar(select(Note).where(Note.capture_id == capture.id))
            return db.scalar(select(Job).where(Job.capture_id == capture.id))

    found = existing()
    if found:
        return found
    pending = db.scalar(
        select(func.count())
        .select_from(Job)
        .where(Job.user_id == user_id, Job.status.in_(["queued", "running"]))
    )
    if processing_mode == "ai" and pending >= settings.max_pending_per_user:
        raise HTTPException(429, "Слишком много записей ожидают обработки")
    capture = Capture(
        id=new_id(),
        user_id=user_id,
        original_text=text,
        idempotency_key=key,
        processing_mode=processing_mode,
        channel=channel,
    )
    if processing_mode == "manual":
        result = Note(
            id=new_id(),
            capture_id=capture.id,
            user_id=user_id,
            provider="manual",
            title=text.strip().splitlines()[0][:200],
            markdown=text,
            conclusions=[],
            version=1,
        )
    else:
        result = Job(id=new_id(), capture_id=capture.id, user_id=user_id, provider=settings.provider)
    try:
        db.add(capture)
        db.flush()
        db.add(result)
        db.flush()
        if processing_mode == "manual":
            save_revision(db, result)
        record_event(db, user_id, "capture_saved", capture.id, channel)
        if commit:
            db.commit()
    except IntegrityError:
        if not commit:
            raise  # The caller owns the complete inbox/capture transaction.
        db.rollback()
        found = existing()
        if found:
            return found
        raise
    return result


def create_audio_capture(
    db, *, capture_id, user_id, idempotency_key, stored_audio: StoredAudioLike, channel, settings
) -> Job:
    """Create in the caller's transaction, after durable storage publication."""
    try:
        valid_id = str(UUID(capture_id)) == capture_id
    except (ValueError, TypeError, AttributeError):
        valid_id = False
    if (
        not valid_id
        or channel not in {"web", "telegram"}
        or not isinstance(idempotency_key, str)
        or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", idempotency_key)
        or stored_audio.key != f"{capture_id}.audio"
    ):
        raise IntegrationRejection(422, "invalid_input", "Некорректные параметры аудиозаписи")
    info = stored_audio.info
    if (
        type(info.byte_count) is not int
        or info.byte_count <= 0
        or not isinstance(info.duration_seconds, (int, float))
        or isinstance(info.duration_seconds, bool)
        or not math.isfinite(info.duration_seconds)
        or info.duration_seconds <= 0
        or not isinstance(info.sha256, str)
        or not re.fullmatch(r"[0-9a-f]{64}", info.sha256)
        or not isinstance(info.media_type, str)
        or info.media_type not in AUDIO_MEDIA_TYPES
    ):
        raise IntegrationRejection(422, "invalid_input", "Некорректные сведения об аудио")
    if info.byte_count > MAX_AUDIO_BYTES or info.duration_seconds > MAX_AUDIO_SECONDS:
        raise IntegrationRejection(413, "input_too_large", "Аудиозапись слишком большая или длинная")

    lock_account(db, user_id)
    capture = db.scalar(
        select(Capture).where(Capture.user_id == user_id, Capture.idempotency_key == idempotency_key)
    )
    if capture:
        if (
            capture.input_kind != "audio"
            or capture.audio_sha256 != info.sha256
            or capture.audio_bytes != info.byte_count
            or capture.audio_media_type != info.media_type
            or capture.audio_seconds != info.duration_seconds
            or capture.processing_mode != "ai"
            or capture.channel != channel
        ):
            raise IntegrationRejection(409, "conflict", "Этот Idempotency-Key уже использован для другой записи")
        return db.scalar(select(Job).where(Job.capture_id == capture.id))
    pending = db.scalar(
        select(func.count()).select_from(Job).where(Job.user_id == user_id, Job.status.in_(["queued", "running"]))
    )
    if pending >= settings.max_pending_per_user:
        # Temporary queue saturation must not be remembered as a terminal Inbox rejection.
        raise HTTPException(429, "Слишком много записей ожидают обработки")
    capture = Capture(
        id=capture_id, user_id=user_id, idempotency_key=idempotency_key, channel=channel,
        input_kind="audio", processing_mode="ai", original_text="", transcript=None,
        audio_key=stored_audio.key, audio_sha256=info.sha256, audio_bytes=info.byte_count,
        audio_seconds=info.duration_seconds, audio_media_type=info.media_type,
    )
    db.add(capture)
    db.flush()
    job = Job(id=new_id(), capture_id=capture_id, user_id=user_id, provider=settings.provider)
    db.add(job)
    db.flush()
    record_event(db, user_id, "capture_saved", capture_id, channel)
    return job


def owned_note(db, note_id, user_id):
    if "\x00" in note_id:
        raise HTTPException(404, "Заметка не найдена")
    note = db.scalar(select(Note).where(Note.id == note_id, Note.user_id == user_id))
    if note is None:
        raise HTTPException(404, "Заметка не найдена")
    return note


def item_view(item):
    return {
        "id": item.id,
        "note_id": item.note_id,
        "kind": item.kind,
        "text": item.text,
        "status": item.status,
        "version": item.version,
        "source_quote": item.source_quote,
        "due_text": item.due_text,
        "due_at": item.due_at,
    }


def snapshot(db, note):
    category = db.scalar(
        select(Category).where(Category.id == note.category_id, Category.user_id == note.user_id)
    )
    items = db.scalars(
        select(Item)
        .where(Item.note_id == note.id, Item.user_id == note.user_id)
        .order_by(Item.position, Item.id)
    ).all()
    return {
        "title": note.title,
        "markdown": note.markdown,
        "conclusions": note.conclusions,
        "category_id": note.category_id,
        "category_name": category.name if category else None,
        "structure_confirmed_at": note.structure_confirmed_at,
        "items": [item_view(item) for item in items],
    }


def save_revision(db, note):
    db.add(Revision(note_id=note.id, version=note.version, snapshot=snapshot(db, note)))


def advance_note(db, note, expected_version, **changes):
    lock_account(db, note.user_id)
    changed = db.execute(
        update(Note)
        .where(Note.id == note.id, Note.user_id == note.user_id, Note.version == expected_version)
        .values(**changes, version=Note.version + 1, updated_at=time.time()),
        execution_options={"synchronize_session": False},
    ).rowcount
    if not changed:
        db.rollback()
        raise HTTPException(409, "Заметка уже изменена. Обновите её перед сохранением")
    db.refresh(note)


def confirm_structure(db, note):
    note.structure_confirmed_at = time.time()
    record_event(db, note.user_id, "note_opened", f"{note.id}:{note.version}:checked", subject_id=note.id)
    record_event(db, note.user_id, "structure_confirmed", f"{note.id}:{note.version}", subject_id=note.id)


def edit_note(db, note, expected_version, **changes):
    advance_note(db, note, expected_version, **changes)
    if "title" in changes or "markdown" in changes:
        confirm_structure(db, note)
        record_event(db, note.user_id, "note_edited", f"{note.id}:{note.version}", subject_id=note.id)
    db.flush()
    save_revision(db, note)
    db.commit()
    return note


def note_view(db, note):
    capture = db.get(Capture, note.capture_id)
    return {
        "id": note.id,
        "capture_id": note.capture_id,
        "original_text": capture.original_text,
        **audio_metadata(db, capture),
        **snapshot(db, note),
        "version": note.version,
        "provider": note.provider,
        "created_at": note.created_at,
        "updated_at": note.updated_at,
    }


def audio_metadata(db, capture):
    origin = db.scalar(select(TranscriptRevision.origin).where(
        TranscriptRevision.capture_id == capture.id, TranscriptRevision.version == capture.transcript_version,
    )) if capture.transcript is not None else None
    return {
        "input_kind": capture.input_kind, "transcript": capture.transcript,
        "transcript_version": capture.transcript_version,
        "transcript_origin": origin or ("legacy" if capture.transcript is not None else None),
        "audio_seconds": capture.audio_seconds, "audio_media_type": capture.audio_media_type,
    }


def owned_capture(db, capture_id, user_id, *, audio_only=False):
    capture = db.scalar(select(Capture).where(Capture.id == capture_id, Capture.user_id == user_id))
    if capture is None or (audio_only and capture.input_kind != "audio"):
        raise HTTPException(404, "Запись не найдена")
    return capture


def capture_view(db, capture):
    job = db.scalar(select(Job).where(Job.capture_id == capture.id))
    return {
        "capture_id": capture.id, "original_text": capture.original_text,
        "processing_mode": capture.processing_mode, **audio_metadata(db, capture),
        "note_id": db.scalar(select(Note.id).where(Note.capture_id == capture.id)),
        "job": job_view(db, job) if job else None,
    }


def edit_transcript(db, capture, body):
    lock_account(db, capture.user_id)
    db.refresh(capture)
    job = db.scalar(select(Job).where(Job.capture_id == capture.id))
    if job and job.status in {"queued", "running"}:
        raise HTTPException(409, "Дождитесь завершения обработки перед правкой расшифровки")
    if capture.transcript_version != body.version:
        raise HTTPException(409, "Расшифровка уже изменена. Обновите её перед сохранением")
    if capture.transcript == body.text:
        return capture
    # Preserve an old transcript imported without a revision, without guessing its date.
    if capture.transcript is not None and db.scalar(select(TranscriptRevision.id).where(
        TranscriptRevision.capture_id == capture.id, TranscriptRevision.version == capture.transcript_version,
    )) is None:
        db.add(TranscriptRevision(capture_id=capture.id, version=capture.transcript_version,
                                  text=capture.transcript, origin="legacy", created_at=None))
    changed = db.execute(update(Capture).where(
        Capture.id == capture.id, Capture.user_id == capture.user_id, Capture.transcript_version == body.version,
    ).values(transcript=body.text, transcript_version=Capture.transcript_version + 1),
        execution_options={"synchronize_session": False}).rowcount
    if not changed:
        db.rollback()
        raise HTTPException(409, "Расшифровка уже изменена. Обновите её перед сохранением")
    db.refresh(capture)
    db.add(TranscriptRevision(capture_id=capture.id, version=capture.transcript_version,
                              text=body.text, origin="user", created_at=time.time()))
    record_event(db, capture.user_id, "transcript_edited", f"{capture.id}:{capture.transcript_version}")
    db.flush()
    return capture


def owned_category(db, category_id, user_id):
    if "\x00" in category_id:
        raise HTTPException(404, "Категория не найдена")
    category = db.scalar(select(Category).where(Category.id == category_id, Category.user_id == user_id))
    if category is None:
        raise HTTPException(404, "Категория не найдена")
    return category


def category_view(category):
    return {"id": category.id, "name": category.name, "version": category.version}


def ensure_category(db, user_id, name, *, proposed=False):
    lock_account(db, user_id)
    name = name.strip()
    categories = db.scalars(select(Category).where(Category.user_id == user_id).order_by(Category.id)).all()
    for category in categories:
        if category.name.casefold() == name.casefold():
            return category
    if len(categories) >= MAX_CATEGORIES:
        if proposed:
            return None
        raise HTTPException(429, "Можно создать не больше 100 категорий")
    category = Category(user_id=user_id, name=name, version=1)
    db.add(category)
    db.flush()
    return category


def rename_category(db, category, name, version):
    lock_account(db, category.user_id)
    conflict = next(
        (
            c
            for c in db.scalars(select(Category).where(Category.user_id == category.user_id))
            if c.id != category.id and c.name.casefold() == name.casefold()
        ),
        None,
    )
    if conflict:
        raise HTTPException(409, "Категория с таким именем уже есть")
    changed = db.execute(
        update(Category)
        .where(Category.id == category.id, Category.user_id == category.user_id, Category.version == version)
        .values(name=name, version=Category.version + 1),
        execution_options={"synchronize_session": False},
    ).rowcount
    if not changed:
        raise HTTPException(409, "Категория уже изменена. Обновите её перед сохранением")
    db.refresh(category)
    record_event(db, category.user_id, "category_changed", f"{category.id}:{category.version}")
    db.commit()
    return category


def change_note_category(db, note, category_id, version):
    if category_id is not None:
        owned_category(db, category_id, note.user_id)
    advance_note(db, note, version, category_id=category_id)
    confirm_structure(db, note)
    record_event(db, note.user_id, "category_changed", f"{note.id}:{note.version}")
    db.flush()
    save_revision(db, note)
    db.commit()


def create_item(db, note, body):
    advance_note(db, note, body.version)
    count = db.scalar(
        select(func.count()).select_from(Item).where(Item.note_id == note.id, Item.user_id == note.user_id)
    )
    if count >= MAX_ITEMS:
        raise HTTPException(429, "В записи может быть не больше 30 элементов")
    position = db.scalar(select(func.max(Item.position)).where(Item.note_id == note.id))
    db.add(
        Item(
            user_id=note.user_id,
            note_id=note.id,
            kind=body.kind,
            text=body.text,
            status="open",
            version=1,
            position=0 if position is None else position + 1,
        )
    )
    confirm_structure(db, note)
    record_event(db, note.user_id, "note_edited", f"{note.id}:{note.version}", subject_id=note.id)
    db.flush()
    save_revision(db, note)
    db.commit()


def cancel_item_reminders(db, user_id, item_id):
    cancelled = (
        db.execute(
            update(Reminder)
            .where(
                Reminder.item_id == item_id,
                Reminder.user_id == user_id,
                Reminder.status.in_(["confirmed", "sent", "unknown", "blocked", "failed"]),
            )
            .values(status="cancelled", generation=Reminder.generation + 1)
            .returning(Reminder.id)
        )
        .scalars()
        .all()
    )
    if not cancelled:
        return
    cancel_reminder_deliveries(db, user_id, cancelled)


def cancel_reminder_deliveries(db, user_id, reminder_ids):
    db.execute(
        update(Outbox)
        .where(
            Outbox.reminder_id.in_(reminder_ids),
            Outbox.user_id == user_id,
            Outbox.status.in_(["pending", "leased", "retryable"]),
        )
        .values(status="cancelled")
    )
    db.execute(
        update(Outbox)
        .where(
            Outbox.reminder_id.in_(reminder_ids),
            Outbox.user_id == user_id,
            Outbox.status == "authorized",
        )
        .values(status="unknown", error_code="cancelled_after_authorize")
    )


def edit_item(db, note, item_id, body):
    if "\x00" in item_id:
        raise HTTPException(404, "Элемент не найден")
    if body.status == "completed" and body.kind != "task":
        raise HTTPException(422, "Выполненной можно отметить только задачу")
    advance_note(db, note, body.version)
    item = db.scalar(
        select(Item).where(Item.id == item_id, Item.note_id == note.id, Item.user_id == note.user_id)
    )
    if item is None:
        raise HTTPException(404, "Элемент не найден")
    content_changed = item.text != body.text or item.kind != body.kind
    completed = body.kind == "task" and body.status == "completed" and item.status != "completed"
    # A type correction also invalidates reminders belonging to the former task.
    if completed or (item.kind == "task" and body.kind != "task"):
        cancel_item_reminders(db, note.user_id, item.id)
    item.kind, item.text, item.status = body.kind, body.text, body.status
    item.version += 1
    if content_changed:
        confirm_structure(db, note)
        record_event(db, note.user_id, "note_edited", f"{note.id}:{note.version}", subject_id=note.id)
    if completed:
        record_event(db, note.user_id, "task_completed", f"{item.id}:{item.version}")
    db.flush()
    save_revision(db, note)
    db.commit()


def search_notes(db, user_id, *, q=None, category_id=None, limit=20, offset=0):
    if q and "\x00" in q:
        raise HTTPException(422, "Уберите нулевой символ из поиска")
    statement = select(Note).where(Note.user_id == user_id)
    if category_id == "none":
        statement = statement.where(Note.category_id.is_(None))
    elif category_id is not None:
        owned_category(db, category_id, user_id)
        statement = statement.where(Note.category_id == category_id)
    if q and q.strip():
        value = q.strip()
        if db.bind.dialect.name == "sqlite":

            def contains(column):
                return func.unicode_casefold(column).contains(value.casefold(), autoescape=True)
        else:
            pattern = "%" + value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"

            def contains(column):
                return column.ilike(pattern, escape="\\")

        item_match = (
            select(Item.id)
            .where(
                Item.note_id == Note.id,
                Item.user_id == user_id,
                contains(Item.text),
            )
            .exists()
        )
        statement = statement.join(Capture, Capture.id == Note.capture_id).where(
            Capture.user_id == user_id,
            or_(
                contains(Note.title),
                contains(Note.markdown),
                contains(Capture.original_text),
                contains(Capture.transcript),
                item_match,
            ),
        )
    return db.scalars(
        statement.order_by(Note.updated_at.desc(), Note.id).offset(offset).limit(limit + 1)
    ).all()


def job_view(db, job):
    capture = db.get(Capture, job.capture_id)
    note_id = db.scalar(select(Note.id).where(Note.capture_id == job.capture_id))
    return {
        "id": job.id,
        "capture_id": job.capture_id,
        "status": job.status,
        "provider": job.provider,
        "error_code": job.error_code,
        "note_id": note_id,
        "original_text": capture.original_text,
        "created_at": job.created_at,
        "finished_at": job.finished_at,
    }
