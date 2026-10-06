"""Owned transcript edits. Saving never requeues AI or changes a note."""

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select

from app.analytics import record_event
from app.models import TranscriptRevision
from app.schemas import CaptureResponse, TranscriptEdit, TranscriptRevisionResponse, UserAction
from app.security import get_login_session
from app.services import capture_view, edit_transcript, owned_capture


def build_router(database):
    router = APIRouter(prefix="/api/v1/captures", tags=["transcripts"])

    @router.patch("/{capture_id}/transcript", response_model=CaptureResponse)
    def update(capture_id: str, body: TranscriptEdit, request: Request, db=Depends(database)):
        session = get_login_session(request, db, write=True)
        capture = owned_capture(db, capture_id, session.user_id, audio_only=True)
        edit_transcript(db, capture, body)
        db.commit()
        return capture_view(db, capture)

    @router.get("/{capture_id}/transcript-revisions", response_model=list[TranscriptRevisionResponse])
    def revisions(capture_id: str, request: Request, db=Depends(database)):
        session = get_login_session(request, db)
        capture = owned_capture(db, capture_id, session.user_id, audio_only=True)
        rows = db.scalars(select(TranscriptRevision).where(TranscriptRevision.capture_id == capture.id)
                          .order_by(TranscriptRevision.version.desc()).limit(100))
        return [{"version": row.version, "text": row.text, "origin": row.origin, "created_at": row.created_at}
                for row in rows]

    @router.post("/{capture_id}/original-opened", status_code=204)
    def original_opened(capture_id: str, body: UserAction, request: Request, db=Depends(database)):
        session = get_login_session(request, db, write=True)
        owned_capture(db, capture_id, session.user_id)
        record_event(db, session.user_id, "original_opened", body.operation_id)
        db.commit()

    return router
