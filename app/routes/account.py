import secrets
import time
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.analytics import record_event
from app.models import LinkRequest, TelegramIdentity, new_id
from app.schemas import LinkCodeResponse
from app.security import get_login_session, hash_token, throttle


def build_router(database):
    router = APIRouter(prefix="/api/v1/telegram", tags=["telegram-link"])

    @router.post("/link-code", response_model=LinkCodeResponse, status_code=201)
    def issue_code(request: Request, db=Depends(database)):
        session = get_login_session(request, db, write=True)
        throttle(db, "telegram-code:" + session.user_id, limit=5)
        now = time.time()
        db.execute(
            update(LinkRequest)
            .where(
                LinkRequest.user_id == session.user_id,
                LinkRequest.status.in_(["issued", "pending"]),
            )
            .values(status="expired")
        )
        code = secrets.token_urlsafe(32)
        link = LinkRequest(
            id=new_id(),
            user_id=session.user_id,
            code_hash=hash_token(code),
            expires_at=now + 600,
        )
        db.add(link)
        db.commit()
        return {
            "link_request_id": link.id,
            "code": code,
            "expires_at": datetime.fromtimestamp(link.expires_at, UTC).isoformat(),
        }

    @router.get("/links")
    def links(request: Request, db=Depends(database)):
        session = get_login_session(request, db)
        identities = db.scalars(
            select(TelegramIdentity).where(TelegramIdentity.user_id == session.user_id)
        ).all()
        pending = db.scalars(
            select(LinkRequest).where(
                LinkRequest.user_id == session.user_id,
                LinkRequest.status == "pending",
                LinkRequest.expires_at > time.time(),
            )
        ).all()
        return {
            "identities": [{
                "bot_id": i.bot_id, "telegram_user_id": i.telegram_user_id,
                "notifications_enabled": i.notifications_enabled, "delivery_status": i.delivery_status,
            } for i in identities],
            "pending": [
                {
                    "link_request_id": p.id,
                    "bot_id": p.bot_id,
                    "telegram_user_id": p.telegram_user_id,
                    "expires_at": datetime.fromtimestamp(p.expires_at, UTC).isoformat(),
                }
                for p in pending
            ],
        }

    @router.post("/links/{link_id}/confirm")
    def confirm(link_id: str, request: Request, db=Depends(database)):
        session = get_login_session(request, db, write=True)
        link = db.scalar(
            select(LinkRequest).where(LinkRequest.id == link_id, LinkRequest.user_id == session.user_id)
        )
        if not link:
            raise HTTPException(404, "Запрос не найден")
        if link.status == "confirmed":
            return {"status": "confirmed"}
        changed = db.execute(
            update(LinkRequest)
            .where(
                LinkRequest.id == link_id,
                LinkRequest.status == "pending",
                LinkRequest.expires_at > time.time(),
            )
            .values(status="confirmed")
        ).rowcount
        if not changed:
            raise HTTPException(409, "Запрос недействителен или истёк")
        existing = db.scalar(
            select(TelegramIdentity).where(
                TelegramIdentity.bot_id == link.bot_id,
                TelegramIdentity.telegram_user_id == link.telegram_user_id,
            )
        )
        if existing:
            if existing.user_id != session.user_id:
                raise HTTPException(409, "Telegram уже связан с другим аккаунтом")
        else:
            db.add(
                TelegramIdentity(
                    user_id=session.user_id,
                    bot_id=link.bot_id,
                    telegram_user_id=link.telegram_user_id,
                    chat_id=link.chat_id,
                )
            )
        try:
            db.flush()
            record_event(db, session.user_id, "telegram_linked", link.id)
            db.commit()
        except IntegrityError:
            db.rollback()
            raise HTTPException(409, "Аккаунт уже имеет другую связь с этим ботом") from None
        return {"status": "confirmed"}

    return router
