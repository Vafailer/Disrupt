"""Session-bound feedback and an admin-only inbox."""
import hashlib
import time
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from pydantic import Field, field_validator
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.feedback_models import Feedback
from app.schemas import StrictModel
from app.security import get_login_session, require_admin, throttle


class FeedbackCreate(StrictModel):
    kind: Literal['bug', 'idea', 'question', 'other']
    subject: str = Field(min_length=1, max_length=160)
    description: str = Field(min_length=1, max_length=5000)
    steps: str = Field(default='', max_length=3000)
    expected: str = Field(default='', max_length=2000)
    contact: str = Field(default='', max_length=200)

    @field_validator('subject', 'description', 'steps', 'expected', 'contact')
    @classmethod
    def no_null_bytes(cls, value):
        if '\x00' in value:
            raise ValueError('Text cannot contain null bytes')
        return value

    @field_validator('subject', 'description')
    @classmethod
    def nonblank(cls, value):
        if not value.strip() or '\x00' in value:
            raise ValueError('Expected nonblank text')
        return value


class FeedbackReceipt(StrictModel):
    id: str


class FeedbackView(FeedbackCreate):
    id: str
    user_id: str
    status: Literal['new', 'in_progress', 'resolved']
    version: int
    created_at: float
    updated_at: float


class FeedbackUpdate(StrictModel):
    status: Literal['new', 'in_progress', 'resolved']
    version: int = Field(ge=1)


def view(row):
    fields = FeedbackView.model_fields
    return {key: getattr(row, key) for key in fields}


def build_router(database):
    router = APIRouter(tags=['feedback'])

    @router.post('/api/v1/feedback', response_model=FeedbackReceipt, status_code=201)
    def create(body: FeedbackCreate, request: Request, db=Depends(database),
               key: str = Header(alias='Idempotency-Key', min_length=1, max_length=100, pattern=r'^[A-Za-z0-9_.:-]+$')):
        session = get_login_session(request, db, write=True)
        digest = hashlib.sha256(body.model_dump_json().encode()).hexdigest()

        def replay():
            row = db.scalar(select(Feedback).where(Feedback.user_id == session.user_id, Feedback.idempotency_key == key))
            if row and row.payload_hash != digest:
                raise HTTPException(409, 'Это обращение уже отправлено с другим содержимым.')
            return {'id': row.id} if row else None

        existing = replay()
        if existing:
            return existing
        throttle(db, 'feedback:' + session.user_id, limit=5)
        row = Feedback(user_id=session.user_id, idempotency_key=key, payload_hash=digest, **body.model_dump())
        db.add(row)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            existing = replay()
            if existing:
                return existing
            raise
        return {'id': row.id}

    @router.get('/api/admin/feedback', response_model=list[FeedbackView])
    def inbox(request: Request, response: Response, db=Depends(database),
              status: Literal['all', 'new', 'in_progress', 'resolved'] = 'new',
              limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0)):
        require_admin(request, db)
        query = select(Feedback)
        if status != 'all':
            query = query.where(Feedback.status == status)
        rows = db.scalars(query.order_by(Feedback.created_at.desc(), Feedback.id.desc()).offset(offset).limit(limit + 1)).all()
        if len(rows) > limit:
            response.headers['X-Next-Feedback-Offset'] = str(offset + limit)
        response.headers['Cache-Control'] = 'no-store'
        return [view(row) for row in rows[:limit]]

    @router.patch('/api/admin/feedback/{feedback_id}', response_model=FeedbackView)
    def change(feedback_id: str, body: FeedbackUpdate, request: Request, db=Depends(database)):
        require_admin(request, db)
        get_login_session(request, db, write=True)
        if db.get(Feedback, feedback_id) is None:
            raise HTTPException(404, 'Обращение не найдено')
        changed = db.execute(update(Feedback).where(Feedback.id == feedback_id, Feedback.version == body.version).values(
            status=body.status, version=Feedback.version + 1, updated_at=time.time(),
        )).rowcount
        if not changed:
            db.rollback()
            raise HTTPException(409, 'Статус уже изменился. Обновите список обращений.')
        db.commit()
        return view(db.get(Feedback, feedback_id))

    return router
