from fastapi import APIRouter, Depends, Header, Query, Request
from sqlalchemy import select

from app.models import Reminder
from app.reminder_time import resolve_reminder_time
from app.reminders import cancel_reminder, create_reminder, edit_reminder, owned_reminder, reminder_view
from app.schemas import (
    ReminderCancel,
    ReminderCreate,
    ReminderEdit,
    ReminderResponse,
    ReminderTimeRequest,
    ReminderTimeResponse,
)
from app.security import get_login_session
from app.services import owned_note


def build_router(database):
    router = APIRouter(prefix="/api/v1", tags=["reminders"])

    @router.post(
        "/reminders/resolve-time", response_model=ReminderTimeResponse,
        description="Resolve local time in an IANA zone before confirmation. Does not create a reminder. "
        "Return both UTC instants for a DST fold; reject gaps and wholly past times. Requires session and CSRF.",
        responses={401: {"description": "Browser session required"}, 403: {"description": "CSRF or Origin rejected"}},
    )
    def resolve_time(body: ReminderTimeRequest, request: Request, db=Depends(database)):
        get_login_session(request, db, write=True)
        return resolve_reminder_time(body)

    @router.get("/notes/{note_id}/reminders", response_model=list[ReminderResponse])
    def list_reminders(
        note_id: str, request: Request, limit: int = Query(100, ge=1, le=100),
        offset: int = Query(0, ge=0), db=Depends(database),
    ):
        session = get_login_session(request, db)
        owned_note(db, note_id, session.user_id)
        rows = db.scalars(select(Reminder).where(
            Reminder.note_id == note_id, Reminder.user_id == session.user_id,
        ).order_by(Reminder.scheduled_at, Reminder.id).offset(offset).limit(limit))
        return [reminder_view(db, row) for row in rows]

    @router.get("/reminders/{reminder_id}", response_model=ReminderResponse)
    def get_reminder(reminder_id: str, request: Request, db=Depends(database)):
        session = get_login_session(request, db)
        return reminder_view(db, owned_reminder(db, reminder_id, session.user_id))

    @router.post("/notes/{note_id}/reminders", response_model=ReminderResponse, status_code=201)
    def add_reminder(
        note_id: str, body: ReminderCreate, request: Request,
        idempotency_key: str = Header(min_length=1, max_length=100), db=Depends(database),
    ):
        session = get_login_session(request, db, write=True)
        row = create_reminder(db, session.user_id, note_id, body, idempotency_key)
        db.commit()
        return reminder_view(db, row)

    @router.patch("/reminders/{reminder_id}", response_model=ReminderResponse)
    def update_reminder(reminder_id: str, body: ReminderEdit, request: Request, db=Depends(database)):
        session = get_login_session(request, db, write=True)
        row = edit_reminder(db, session.user_id, reminder_id, body)
        db.commit()
        return reminder_view(db, row)

    @router.post("/reminders/{reminder_id}/cancel", response_model=ReminderResponse)
    def remove_reminder(reminder_id: str, body: ReminderCancel, request: Request, db=Depends(database)):
        session = get_login_session(request, db, write=True)
        row = cancel_reminder(db, session.user_id, reminder_id, body.generation)
        db.commit()
        return reminder_view(db, row)

    return router
