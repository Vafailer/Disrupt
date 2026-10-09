"""Audio intake. Core supplies transactions/models; storage never calls STT."""
import re
from contextlib import contextmanager
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import ValidationError
from sqlalchemy import func, select
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.responses import StreamingResponse

from app.audio_storage import AudioStorageUnavailable, AudioTooLarge, UnsupportedAudio
from app.integration import IntegrationRejection
from app.models import Capture, Job, Outbox, new_id
from app.routes.internal import process_update, telegram_identity
from app.schemas import TelegramOperation
from app.security import get_login_session, require_internal_service
from app.services import job_view, lock_account, saved_audio_view, saved_without_ai


class VoiceUpdate(TelegramOperation):
    processing_mode: Literal["ai"]
    audio_sha256: str


@contextmanager
def audio_errors():
    try:
        yield
    except AudioTooLarge:
        raise IntegrationRejection(413, "input_too_large", "Аудиозапись слишком большая или длинная") from None
    except UnsupportedAudio:
        raise IntegrationRejection(400, "unsupported_audio", "Не удалось прочитать аудиофайл") from None
    except AudioStorageUnavailable:
        raise HTTPException(503, "Хранилище аудио временно недоступно") from None


def _form(form, fields):
    items = list(form.multi_items())
    if len(items) != len(fields) or {key for key, _ in items} != fields:
        raise HTTPException(422, "Проверьте поля аудиозаписи")
    if not isinstance(form["audio"], UploadFile):
        raise HTTPException(422, "Нужен аудиофайл")
    if any(not isinstance(form[key], str) for key in fields - {"audio"}):
        raise HTTPException(422, "Неверный формат поля")
    if form["processing_mode"] != "ai":
        raise HTTPException(422, "Для аудио нужен режим ai")


def build_router(database, settings, storage, create_audio_capture):
    router = APIRouter(tags=["audio"])

    def create(db, user_id, key, staged, channel):
        # Check capacity under the same account lock before allocating an original.
        # Otherwise every 429 retry could leave another unreferenced 10 MiB file.
        lock_account(db, user_id)
        pending = db.scalar(select(func.count()).select_from(Job).where(
            Job.user_id == user_id, Job.status.in_(["queued", "running"]),
        ))
        if pending >= settings.max_pending_per_user:
            raise HTTPException(429, "Слишком много записей ожидают обработки")
        with audio_errors():
            info = storage.inspect(staged)
            capture_id = new_id()
            stored = storage.publish(staged, capture_id, info)
        # Leave published files intact on any DB failure, including unknown commit.
        return create_audio_capture(
            db, capture_id=capture_id, user_id=user_id, idempotency_key=key,
            stored_audio=stored, channel=channel, settings=settings,
        )

    def telegram_save(db, fields, source):
        with audio_errors(), storage.stage(source) as staged:
            try:
                body = VoiceUpdate(**fields, audio_sha256=staged.sha256)
            except ValidationError:
                raise HTTPException(422, "Проверьте формат полей") from None

            def handle(operation_id):
                identity = telegram_identity(db, body)
                job = create(db, identity.user_id, "telegram:" + operation_id, staged, "telegram")
                if isinstance(job, Job):
                    # Durable Telegram reply with the text result, only when AI processing was queued.
                    db.add(Outbox(job_id=job.id, user_id=identity.user_id, bot_id=identity.bot_id,
                                  chat_id=identity.chat_id, generation=1, status="pending"))
                saved = {
                    "capture_id": job.capture_id if isinstance(job, Job) else job.id,
                    "job_id": job.id if isinstance(job, Job) else None, "status": "saved",
                }
                saved["note_url"] = settings.public_origin + "/?capture=" + saved["capture_id"]
                if isinstance(job, Capture):
                    saved["ai_limit_exceeded"] = True  # Daily AI limit spent, the recording is kept without AI.
                return saved

            return process_update(db, body, "voice", handle)

    @router.post("/internal/v1/telegram/voice", dependencies=[Depends(require_internal_service)])
    async def telegram_voice(request: Request, db=Depends(database)):
        async with request.form(max_files=1, max_fields=5, max_part_size=1024) as form:
            _form(form, {"audio", "bot_id", "update_id", "telegram_user_id", "chat_id", "processing_mode"})
            fields = {key: form[key] for key in form if key != "audio"}
            # Convert multipart decimal integers strictly before Pydantic validation.
            for key in ("bot_id", "update_id", "telegram_user_id", "chat_id"):
                if not re.fullmatch(r"[0-9]{1,19}", fields[key]):
                    raise HTTPException(422, "Неверный идентификатор Telegram")
                fields[key] = int(fields[key])
            return await run_in_threadpool(telegram_save, db, fields, form["audio"].file)

    def web_save(db, user_id, key, source):
        with audio_errors(), storage.stage(source) as staged:
            lock_account(db, user_id)
            existing = db.scalar(select(Capture).where(Capture.user_id == user_id, Capture.idempotency_key == key))
            if existing:
                if (
                    existing.input_kind != "audio" or existing.channel != "web"
                    or (existing.processing_mode != "ai" and not saved_without_ai(db, existing))
                    or existing.audio_sha256 != staged.sha256
                ):
                    raise HTTPException(409, "Этот ключ уже использован для другой записи")
                job = db.scalar(select(Job).where(Job.capture_id == existing.id)) or (
                    existing if existing.processing_mode == "manual" else None
                )
                if job is None:
                    raise HTTPException(503, "Задание временно недоступно")
            else:
                job = create(db, user_id, key, staged, "web")
            db.commit()
            return job_view(db, job) if isinstance(job, Job) else saved_audio_view(db, job)

    @router.post("/api/v1/captures/audio", status_code=202)
    async def web_audio(request: Request, db=Depends(database)):
        session = get_login_session(request, db, write=True)
        keys = request.headers.getlist("idempotency-key")
        if len(keys) != 1 or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", keys[0]):
            raise HTTPException(422, "Нужен корректный Idempotency-Key")
        async with request.form(max_files=1, max_fields=1, max_part_size=1024) as form:
            _form(form, {"audio", "processing_mode"})
            return await run_in_threadpool(web_save, db, session.user_id, keys[0], form["audio"].file)

    @router.get("/api/v1/captures/{capture_id}/audio")
    def download(capture_id: str, request: Request, db=Depends(database)):
        session = get_login_session(request, db)
        capture = db.scalar(select(Capture).where(Capture.id == capture_id, Capture.user_id == session.user_id))
        if capture is None or capture.input_kind != "audio":
            raise HTTPException(404, "Аудиозапись не найдена")
        formats = {"audio/ogg": "ogg", "audio/wav": "wav", "audio/webm": "webm"}
        extension = formats.get(capture.audio_media_type)
        if extension is None:
            raise HTTPException(503, "Аудиозапись временно недоступна")
        # Open before headers so a missing file produces a proper 503, not a broken 200.
        context = storage.open_original(capture.audio_key)
        try:
            source = context.__enter__()
        except OSError:
            raise HTTPException(503, "Аудиозапись временно недоступна") from None

        def chunks():
            try:
                while chunk := source.read(65536):
                    yield chunk
            finally:
                context.__exit__(None, None, None)

        return StreamingResponse(chunks(), media_type=capture.audio_media_type, background=BackgroundTask(source.close), headers={
            "Content-Disposition": f'attachment; filename="{capture.id}.{extension}"',
            "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
        })

    return router
