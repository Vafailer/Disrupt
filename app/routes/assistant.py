import re
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import Field
from sqlalchemy import select

from app.assistant import AssistantLimitReached, create_request, owned_request, request_view
from app.models import AssistantRequest, User
from app.schemas import StrictModel
from app.security import get_login_session


class AssistantAsk(StrictModel):
    question: str = Field(min_length=3, max_length=1000)
    days: Literal[7, 30, 90, 365] = 90


class AssistantDigest(StrictModel):
    days: Literal[7, 30] = 7


class AssistantSettingsPatch(StrictModel):
    recommendations_enabled: bool


def build_router(database, settings):
    router = APIRouter(prefix="/api/v1/assistant", tags=["assistant"])

    def queue(db, request, key, kind, **fields):
        session = get_login_session(request, db, write=True)
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", key):
            raise HTTPException(422, "Idempotency-Key должен содержать латинские буквы, цифры или _ . : -")
        try:
            row = create_request(db, settings, session.user_id, kind, key, **fields)
        except AssistantLimitReached as limit:
            return JSONResponse({"detail": str(limit), "limit_resets_at": limit.resets_at}, status_code=429)
        return JSONResponse({"id": row.id, "status": row.status}, status_code=202)

    @router.post("/ask", status_code=202)
    def ask_question(
        body: AssistantAsk, request: Request,
        idempotency_key: str = Header(min_length=1, max_length=100), db=Depends(database),
    ):
        return queue(db, request, idempotency_key, "ask", question=body.question, days=body.days)

    @router.post("/recommendations", status_code=202)
    def create_recommendations(
        request: Request, idempotency_key: str = Header(min_length=1, max_length=100), db=Depends(database),
    ):
        return queue(db, request, idempotency_key, "recommend")

    @router.post("/digest", status_code=202)
    def create_digest(
        body: AssistantDigest, request: Request,
        idempotency_key: str = Header(min_length=1, max_length=100), db=Depends(database),
    ):
        return queue(db, request, idempotency_key, "digest", days=body.days)

    @router.get("/requests")
    def list_requests(request: Request, limit: int = Query(10, ge=1, le=50), db=Depends(database)):
        session = get_login_session(request, db)
        rows = db.scalars(
            select(AssistantRequest).where(AssistantRequest.user_id == session.user_id)
            .order_by(AssistantRequest.created_at.desc(), AssistantRequest.id).limit(limit)
        ).all()
        return [request_view(db, row) for row in rows]

    @router.get("/requests/{request_id}")
    def read_request(request_id: str, request: Request, db=Depends(database)):
        session = get_login_session(request, db)
        return request_view(db, owned_request(db, request_id, session.user_id))

    @router.get("/settings")
    def read_settings(request: Request, db=Depends(database)):
        session = get_login_session(request, db)
        return {"recommendations_enabled": db.get(User, session.user_id).assistant_recommendations_enabled}

    @router.patch("/settings")
    def update_settings(body: AssistantSettingsPatch, request: Request, db=Depends(database)):
        session = get_login_session(request, db, write=True)
        user = db.get(User, session.user_id)
        user.assistant_recommendations_enabled = body.recommendations_enabled
        db.commit()
        return {"recommendations_enabled": user.assistant_recommendations_enabled}

    return router
