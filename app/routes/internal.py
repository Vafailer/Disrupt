"""Telegram adapter API. The bot never chooses an owner or touches the database."""

import hashlib
import json
import time

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.models import Inbox, LinkRequest, Note, TelegramIdentity, new_id
from app.schemas import (
    IntegrationError,
    LinkResponse,
    TelegramCaptureResponse,
    TelegramLink,
    TelegramText,
)
from app.security import hash_token, require_internal_service
from app.services import capture_text


def process_update(db, body, operation, handler):
    if body.chat_id != body.telegram_user_id:
        raise HTTPException(403, "Поддерживаются только личные чаты")
    payload_hash = hashlib.sha256(
        json.dumps({"operation": operation, **body.model_dump()}, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()

    def replay():
        row = db.scalar(select(Inbox).where(Inbox.bot_id == body.bot_id, Inbox.update_id == body.update_id))
        if row:
            if row.payload_hash != payload_hash:
                raise HTTPException(409, "Update уже использован с другим содержимым")
            return row.response

    previous = replay()
    if previous is not None:
        return previous
    try:
        row = Inbox(
            id=new_id(),
            bot_id=body.bot_id,
            update_id=body.update_id,
            payload_hash=payload_hash,
            operation=operation,
            response={},
        )
        db.add(row)
        db.flush()  # Unique update is reserved before performing any business mutation.
        row.response = handler(row.id)
        db.commit()  # Never acknowledge a message before this commit succeeds.
        return row.response
    except IntegrityError:
        db.rollback()
        previous = replay()
        if previous is not None:
            return previous
        raise HTTPException(409, "Конфликт операции") from None


def build_router(database, settings):
    router = APIRouter(
        prefix="/internal/v1",
        tags=["internal-v1"],
        dependencies=[Depends(require_internal_service)],
        responses={code: {"model": IntegrationError} for code in (401, 403, 409, 413, 422, 429, 503)},
    )

    @router.post("/telegram/link-request", response_model=LinkResponse)
    def link_request(body: TelegramLink, db=Depends(database)):
        def handle(operation_id):
            now = time.time()
            link = db.scalar(select(LinkRequest).where(LinkRequest.code_hash == hash_token(body.code)))
            if link is None or link.expires_at <= now:
                raise HTTPException(409, "Код недействителен или истёк")
            changed = db.execute(
                update(LinkRequest)
                .where(
                    LinkRequest.id == link.id, LinkRequest.status == "issued", LinkRequest.expires_at > now
                )
                .values(
                    status="pending",
                    bot_id=body.bot_id,
                    telegram_user_id=body.telegram_user_id,
                    chat_id=body.chat_id,
                )
            ).rowcount
            if not changed:
                raise HTTPException(409, "Код уже использован")
            return {"link_request_id": link.id, "status": "pending"}

        return process_update(db, body, "link", handle)

    @router.post("/telegram/updates", response_model=TelegramCaptureResponse)
    def telegram_text(body: TelegramText, db=Depends(database)):
        def handle(operation_id):
            identity = db.scalar(
                select(TelegramIdentity).where(
                    TelegramIdentity.bot_id == body.bot_id,
                    TelegramIdentity.telegram_user_id == body.telegram_user_id,
                    TelegramIdentity.chat_id == body.chat_id,
                )
            )
            if identity is None:
                raise HTTPException(403, "Сначала подтвердите связь в веб-приложении")
            result = capture_text(
                db,
                identity.user_id,
                body.text,
                "telegram:" + operation_id,
                settings,
                processing_mode=body.processing_mode,
                channel="telegram",
                commit=False,
            )
            # A stable capture link works before the asynchronous note exists.
            return {
                "capture_id": result.capture_id,
                "job_id": None if isinstance(result, Note) else result.id,
                "status": "saved",
                "note_url": settings.public_origin + "/?capture=" + result.capture_id,
            }

        return process_update(db, body, "text", handle)

    return router
