"""Telegram adapter API. The bot never chooses an owner or touches the database."""

import hashlib
import json
import time

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.analytics import record_event
from app.contracts import (
    ActionRequest,
    ActionResponse,
    AuthorizeRequest,
    AuthorizeResponse,
    ClaimRequest,
    ClaimResponse,
    ResultRequest,
    ResultResponse,
)
from app.deliveries import authorize_delivery, claim_deliveries, record_delivery_result
from app.integration import IntegrationRejection
from app.models import Inbox, Item, LinkRequest, Note, Outbox, Reminder, TelegramIdentity, User, new_id
from app.schemas import (
    IntegrationError,
    LinkResponse,
    TelegramCaptureResponse,
    TelegramLink,
    TelegramText,
)
from app.security import hash_token, require_internal_service
from app.services import cancel_item_reminders, capture_text, lock_account, save_revision


def stored_response(row):
    refusal = row.response.get("_rejection")
    if refusal is not None:
        raise IntegrationRejection(
            refusal["status"], refusal["code"], refusal["message"], operation_id=row.id,
        )
    return row.response


def process_update(db, body, operation, handler):
    if hasattr(body, "chat_id") and body.chat_id != body.telegram_user_id:
        raise HTTPException(403, "Поддерживаются только личные чаты")
    payload_hash = hashlib.sha256(
        json.dumps({"operation": operation, **body.model_dump()}, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()

    def replay():
        row = db.scalar(select(Inbox).where(Inbox.bot_id == body.bot_id, Inbox.update_id == body.update_id))
        if row:
            if row.payload_hash != payload_hash:
                raise HTTPException(409, "Update уже использован с другим содержимым")
            return row

    previous = replay()
    if previous is not None:
        return stored_response(previous)
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
        try:
            # A refusal must not commit any partially performed business operation.
            with db.begin_nested():
                row.response = handler(row.id)
        except IntegrationRejection as exc:
            row.response = {
                "_rejection": {"status": exc.status_code, "code": exc.code, "message": exc.detail},
            }
        db.commit()  # Never acknowledge a message before this commit succeeds.
    except IntegrityError:
        db.rollback()
        previous = replay()
        if previous is not None:
            return stored_response(previous)
        raise HTTPException(409, "Конфликт операции") from None
    return stored_response(row)


def telegram_identity(db, body):
    identity = db.scalar(
        select(TelegramIdentity).where(
            TelegramIdentity.bot_id == body.bot_id,
            TelegramIdentity.telegram_user_id == body.telegram_user_id,
        )
    )
    if identity is None:
        raise IntegrationRejection(403, "telegram_not_linked", "Сначала подтвердите связь в веб-приложении")
    if hasattr(body, "chat_id") and identity.chat_id != body.chat_id:
        raise HTTPException(403, "Личный чат не совпадает с подтверждённой связью")
    return identity


def complete_telegram_task(db, body):
    identity = telegram_identity(db, body)
    lock_account(db, identity.user_id)
    delivery = db.scalar(select(Outbox).where(Outbox.callback_token_hash == hash_token(body.callback_token)))
    if delivery is None:
        raise IntegrationRejection(409, "action_expired", "Кнопка недействительна или устарела")
    if (delivery.user_id, delivery.bot_id, delivery.chat_id) != (
        identity.user_id, body.bot_id, identity.chat_id,
    ):
        raise IntegrationRejection(403, "action_forbidden", "Кнопка принадлежит другому аккаунту")
    reminder = db.get(Reminder, delivery.reminder_id)
    item = db.get(Item, reminder.item_id) if reminder and reminder.item_id else None
    if (
        reminder is None or item is None or reminder.user_id != identity.user_id
        or item.user_id != identity.user_id or item.note_id != reminder.note_id or item.kind != "task"
        or delivery.authorized_at is None or delivery.status not in {"authorized", "sent", "unknown", "cancelled"}
    ):
        raise IntegrationRejection(409, "action_expired", "Кнопка больше не относится к действующей задаче")
    if item.status == "completed":
        return {"status": "already_completed"}
    if (
        reminder.status not in {"confirmed", "sent", "unknown"}
        or reminder.generation != delivery.generation or delivery.status == "cancelled"
    ):
        raise IntegrationRejection(409, "action_expired", "Напоминание отменено или изменено")
    note = db.scalar(select(Note).where(Note.id == item.note_id, Note.user_id == identity.user_id))
    if note is None:
        raise IntegrationRejection(409, "action_expired", "Запись больше недоступна")
    db.execute(
        update(Note).where(Note.id == note.id, Note.user_id == identity.user_id)
        .values(version=Note.version + 1, updated_at=time.time()),
        execution_options={"synchronize_session": False},
    )
    db.refresh(note)
    item.status = "completed"
    item.version += 1
    cancel_item_reminders(db, identity.user_id, item.id)
    record_event(db, identity.user_id, "task_completed", f"{item.id}:{item.version}", "telegram")
    db.flush()
    save_revision(db, note)
    return {"status": "completed"}


def build_router(database, settings):
    router = APIRouter(
        prefix="/internal/v1",
        tags=["internal-v1"],
        dependencies=[Depends(require_internal_service)],
        responses={code: {"model": IntegrationError} for code in (400, 401, 403, 409, 413, 422, 429, 503)},
    )

    @router.post("/telegram/link-request", response_model=LinkResponse)
    def link_request(body: TelegramLink, db=Depends(database)):
        def handle(operation_id):
            now = time.time()
            link = db.scalar(select(LinkRequest).where(LinkRequest.code_hash == hash_token(body.code)))
            if link is None:
                raise IntegrationRejection(409, "invalid_link", "Ссылка недействительна")
            if link.expires_at <= now or link.status == "expired":
                raise IntegrationRejection(409, "link_expired", "Ссылка истекла")
            existing = db.scalar(
                select(TelegramIdentity).where(
                    TelegramIdentity.bot_id == body.bot_id,
                    TelegramIdentity.telegram_user_id == body.telegram_user_id,
                )
            )
            account_link = db.scalar(
                select(TelegramIdentity).where(
                    TelegramIdentity.bot_id == body.bot_id, TelegramIdentity.user_id == link.user_id,
                )
            )
            if (
                existing is not None and existing.user_id != link.user_id
                or account_link is not None and account_link.telegram_user_id != body.telegram_user_id
            ):
                raise IntegrationRejection(409, "link_conflict", "Telegram или аккаунт уже связан")
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
                raise IntegrationRejection(409, "link_conflict", "Код уже использован")
            return {"link_request_id": link.id, "status": "pending"}

        return process_update(db, body, "link", handle)

    # Not in the published OpenAPI contract: a small status poll for the adapter's welcome message.
    @router.get("/telegram/link-requests/{link_request_id}", include_in_schema=False)
    def link_request_status(link_request_id: str, telegram_user_id: int, db=Depends(database)):
        link = db.scalar(select(LinkRequest).where(LinkRequest.id == link_request_id))
        if link is None or link.telegram_user_id != telegram_user_id:
            raise HTTPException(404, "Запрос не найден")
        status = link.status
        if status in {"issued", "pending"} and link.expires_at <= time.time():
            status = "expired"
        username = None
        if status == "confirmed":
            username = db.scalar(select(User.username).where(User.id == link.user_id))
        return {"status": status, "username": username}

    @router.post("/telegram/updates", response_model=TelegramCaptureResponse)
    def telegram_text(body: TelegramText, db=Depends(database)):
        def handle(operation_id):
            identity = telegram_identity(db, body)
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
            if not isinstance(result, Note):
                db.add(Outbox(job_id=result.id, user_id=identity.user_id, bot_id=identity.bot_id,
                              chat_id=identity.chat_id, generation=1, status="pending"))
            # A stable capture link works before the asynchronous note exists.
            saved = {
                "capture_id": result.capture_id,
                "job_id": None if isinstance(result, Note) else result.id,
                "status": "saved",
                "note_url": settings.public_origin + "/?capture=" + result.capture_id,
            }
            if isinstance(result, Note) and body.processing_mode == "ai":
                saved["ai_limit_exceeded"] = True  # Daily AI limit spent, saved without AI.
            return saved

        return process_update(db, body, "text", handle)

    @router.post("/telegram/actions", response_model=ActionResponse)
    def telegram_action(body: ActionRequest, db=Depends(database)):
        return process_update(db, body, "action", lambda _: complete_telegram_task(db, body))

    @router.post("/deliveries/claim", response_model=ClaimResponse)
    def claim(body: ClaimRequest, db=Depends(database)):
        response = claim_deliveries(db, body, settings)
        db.commit()
        return response

    @router.post("/deliveries/{delivery_id}/authorize", response_model=AuthorizeResponse)
    def authorize(delivery_id: str, body: AuthorizeRequest, db=Depends(database)):
        response = authorize_delivery(db, delivery_id, body, settings)
        db.commit()
        return response

    @router.post("/deliveries/{delivery_id}/result", response_model=ResultResponse)
    def result(delivery_id: str, body: ResultRequest, db=Depends(database)):
        response = record_delivery_result(db, delivery_id, body)
        db.commit()
        return response

    return router
