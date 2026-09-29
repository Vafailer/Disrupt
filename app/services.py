import time

from fastapi import HTTPException
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from app.models import Capture, Job, Note, Revision, new_id


def capture_text(db, user_id, text, key, settings):
    def existing():
        capture = db.scalar(select(Capture).where(Capture.user_id == user_id, Capture.idempotency_key == key))
        if capture:
            if capture.original_text != text:
                raise HTTPException(409, "Этот Idempotency-Key уже использован для другого текста")
            return db.scalar(select(Job).where(Job.capture_id == capture.id))

    found = existing()
    if found:
        return found
    pending = db.scalar(
        select(func.count())
        .select_from(Job)
        .where(Job.user_id == user_id, Job.status.in_(["queued", "running"]))
    )
    if pending >= settings.max_pending_per_user:
        raise HTTPException(429, "Слишком много записей ожидают обработки")
    capture = Capture(id=new_id(), user_id=user_id, original_text=text, idempotency_key=key)
    job = Job(id=new_id(), capture_id=capture.id, user_id=user_id, provider=settings.provider)
    try:
        db.add(capture)
        db.flush()
        db.add(job)
        db.commit()
    except IntegrityError:
        db.rollback()
        found = existing()
        if found:
            return found
        raise
    return job


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
