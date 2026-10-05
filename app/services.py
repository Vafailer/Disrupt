import time

from fastapi import HTTPException
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from app.analytics import record_event
from app.models import Capture, Job, Note, Revision, User, new_id


def capture_text(db, user_id, text, key, settings, *, processing_mode="ai", channel="web", commit=True):
    # Serialize quota checking for this account before counting queued jobs.
    db.execute(update(User).where(User.id == user_id).values(source=User.source))
    def existing():
        capture = db.scalar(select(Capture).where(Capture.user_id == user_id, Capture.idempotency_key == key))
        if capture:
            if capture.original_text != text or capture.processing_mode != processing_mode:
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
        id=new_id(), user_id=user_id, original_text=text, idempotency_key=key,
        processing_mode=processing_mode, channel=channel,
    )
    if processing_mode == "manual":
        result = Note(
            id=new_id(), capture_id=capture.id, user_id=user_id, provider="manual",
            title=text.strip().splitlines()[0][:200], markdown=text, conclusions=[], version=1,
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


def owned_note(db, note_id, user_id):
    note = db.scalar(select(Note).where(Note.id == note_id, Note.user_id == user_id))
    if note is None:
        raise HTTPException(404, "Заметка не найдена")
    return note


def snapshot(note):
    return {"title": note.title, "markdown": note.markdown, "conclusions": note.conclusions}


def save_revision(db, note):
    db.add(Revision(note_id=note.id, version=note.version, snapshot=snapshot(note)))


def edit_note(db, note, expected_version, **changes):
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
    save_revision(db, note)
    if "title" in changes or "markdown" in changes:
        record_event(db, note.user_id, "note_edited", f"{note.id}:{note.version}")
    db.commit()
    return note


def note_view(db, note):
    capture = db.get(Capture, note.capture_id)
    return {
        "id": note.id,
        "capture_id": note.capture_id,
        "original_text": capture.original_text,
        **snapshot(note),
        "version": note.version,
        "provider": note.provider,
        "created_at": note.created_at,
        "updated_at": note.updated_at,
    }


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
