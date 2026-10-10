import asyncio
import copy
import re
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import crypto
from app.admin_auth import AdminNetworkGate
from app.analytics import record_event
from app.audio_storage import AudioStorage
from app.config import Settings, parse_networks
from app.db import make_database
from app.error_logging import log_error
from app.integration import IntegrationRejection
from app.limits import daily_state
from app.mailer import build_mailer
from app.models import Capture, Job, Note, ProviderBudget, Revision, User
from app.policy import POLICY_VERSION, require_consent
from app.providers import DEMO_TEXT
from app.routes.account import build_router as account_router
from app.routes.admin import ProtectedStaticFiles
from app.routes.admin import build_router as admin_router
from app.routes.assistant import build_router as assistant_router
from app.routes.audio import build_router as audio_router
from app.routes.auth_flows import build_router as auth_flows_router
from app.routes.brain import build_router as brain_router
from app.routes.feedback import build_router as feedback_router
from app.routes.internal import build_router as internal_router
from app.routes.reminders import build_router as reminders_router
from app.routes.structure import build_router as structure_router
from app.routes.transcripts import build_router as transcripts_router
from app.schemas import (
    CaptureResponse,
    ConclusionEdit,
    Credentials,
    NoteEdit,
    NoteResponse,
    NoteSummary,
    Registration,
    TextCapture,
)
from app.security import (
    COOKIE_NAME,
    DUMMY_PASSWORD_HASH,
    check_origin,
    get_login_session,
    hash_password,
    throttle,
    verify_password,
)
from app.services import (
    capture_text,
    capture_view,
    create_audio_capture,
    edit_note,
    job_view,
    note_view,
    owned_capture,
    owned_note,
    search_notes,
)
from app.sessions import create_session, user_payload
from app.worker import Worker

STATIC = Path(__file__).parent / "static"


def integration_error(status, message, *, code=None, operation_id=None, headers=None):
    code = code or {
        401: "unauthorized", 403: "forbidden", 404: "not_found", 409: "conflict",
        413: "input_too_large", 422: "invalid_input", 429: "rate_limited", 503: "unavailable",
    }.get(status, "internal_error")
    return JSONResponse(
        {"error": {"code": code, "message": message}, "operation_id": operation_id or secrets.token_hex(16)},
        status_code=status, headers=headers,
    )


class BodyLimit:
    """Bound the actual body, including chunked requests, before JSON parsing."""

    AUDIO_MAXIMUM = 10 * 1024 * 1024
    MULTIPART_OVERHEAD = 64 * 1024

    def __init__(self, app, maximum=131072):
        self.app, self.maximum = app, maximum

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] not in {"POST", "PUT", "PATCH"}:
            return await self.app(scope, receive, send)
        content_type = next((v.lower() for k, v in scope.get("headers", []) if k == b"content-type"), b"")
        maximum = self.maximum
        if (
            scope["method"] == "POST"
            and scope["path"] in {"/internal/v1/telegram/voice", "/api/v1/captures/audio"}
            and content_type.split(b";", 1)[0].strip() == b"multipart/form-data"
        ):
            maximum = self.AUDIO_MAXIMUM + self.MULTIPART_OVERHEAD
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body.extend(message.get("body", b""))
            if len(body) > maximum:
                response = (
                    integration_error(413, "Слишком большой запрос")
                    if scope["path"].startswith("/internal/")
                    else JSONResponse({"detail": "Слишком большой запрос"}, status_code=413)
                )
                return await response(scope, receive, send)
            if not message.get("more_body", False):
                break
        delivered = False

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


def create_app(
    settings: Settings | None = None, provider=None, *, audio_storage=None, speech_provider=None, mailer=None,
):
    settings = settings or Settings.from_env()
    # Без верного ключа в режиме required приложение не стартует.
    crypto.configure_from_settings(settings)
    if audio_storage is None:
        audio_storage = AudioStorage(
            settings.audio_storage_path,
            ffmpeg_path=settings.audio_ffmpeg_path,
            ffprobe_path=settings.audio_ffprobe_path,
        )
    engine, sessions = make_database(settings.database_url)

    @asynccontextmanager
    async def lifespan(app):
        stop = asyncio.Event()
        task = None
        if settings.auto_worker:
            worker_storage = (
                AudioStorage(audio_storage.root, read_only=True)
                if isinstance(audio_storage, AudioStorage) else audio_storage
            )
            worker = Worker(
                sessions, settings, provider, audio_storage=worker_storage, speech_provider=speech_provider,
            )

            async def run_worker():
                while not stop.is_set():
                    try:
                        await asyncio.to_thread(worker.run_once)
                    except Exception as exc:
                        log_error(
                            "worker_iteration_failed",
                            "internal_error",
                            log_file=settings.error_log_file,
                            exception=exc,
                        )
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=0.5)
                    except TimeoutError:
                        pass

            task = asyncio.create_task(run_worker())
        yield
        stop.set()
        if task:
            await task
        engine.dispose()

    app = FastAPI(
        title="Умные заметки",
        version="0.1.0",
        lifespan=lifespan,
        description="Структурирование мыслей. По умолчанию работает имитация ИИ без сетевых запросов.",
    )
    app.state.settings = settings
    app.state.sessions = sessions
    app.state.engine = engine
    app.state.audio_storage = audio_storage
    app.state.mailer = mailer if mailer is not None else build_mailer(settings)
    app.add_middleware(BodyLimit)
    # Outside the private network the admin screen, its assets and its API simply do not exist (404).
    admin_networks = parse_networks(settings.admin_allowed_networks)
    app.add_middleware(AdminNetworkGate, networks=admin_networks)

    @app.middleware("http")
    async def security_headers(request, call_next):
        try:
            response = await call_next(request)
        except Exception as exc:
            request_id = secrets.token_hex(8)
            log_error(
                "request_failed",
                "internal_error",
                log_file=settings.error_log_file,
                request_id=request_id,
                method=request.method,
                exception=exc,
            )
            response = JSONResponse(
                status_code=500,
                content={"detail": "Внутренняя ошибка. Попробуйте позже.", "error_id": request_id},
            )
            if request.url.path.startswith("/internal/"):
                response = integration_error(503, "Временная ошибка. Повторите ту же операцию.", operation_id=request_id)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        if request.url.path not in {"/docs", "/redoc", "/docs/oauth2-redirect"}:
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
            )
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_input(request, exc):
        # Do not echo password, token, or original text in validation errors.
        if request.url.path.startswith("/internal/"):
            errors = exc.errors()
            fields = {tuple(e["loc"]) for e in errors}
            if request.url.path == "/internal/v1/telegram/link-request" and fields == {("body", "code")}:
                if all(e["type"] in {"string_too_short", "string_too_long", "string_pattern_mismatch"} for e in errors):
                    return integration_error(400, "Ссылка недействительна", code="invalid_link")
            if request.url.path == "/internal/v1/telegram/actions" and fields == {("body", "callback_token")}:
                if all(e["type"] in {"string_too_short", "string_too_long", "value_error"} for e in errors):
                    return integration_error(400, "Кнопка недействительна", code="action_expired")
            if request.url.path == "/internal/v1/telegram/updates" and fields == {("body", "text")}:
                if all(e["type"] == "string_too_long" for e in errors):
                    return integration_error(413, "Запись слишком большая", code="input_too_large")
                body = exc.body
                if isinstance(body, dict) and isinstance(body.get("text"), str) and not body["text"].strip():
                    return integration_error(400, "В записи нет текста", code="empty_input")
            return integration_error(422, "Проверьте формат и длину ввода")
        return JSONResponse(
            status_code=422,
            content={
                "detail": "Проверьте обязательные поля, формат и длину ввода",
                "fields": [".".join(str(part) for part in error["loc"]) for error in exc.errors()],
            },
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request, exc):
        if request.url.path.startswith("/internal/"):
            return integration_error(
                exc.status_code, str(exc.detail), headers=exc.headers,
                code=exc.code if isinstance(exc, IntegrationRejection) else None,
                operation_id=exc.operation_id if isinstance(exc, IntegrationRejection) else None,
            )
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)

    def database():
        with sessions() as db:
            yield db

    app.include_router(account_router(database))
    app.include_router(feedback_router(database))
    app.include_router(admin_router(database, STATIC, settings))
    app.include_router(internal_router(database, settings))
    app.include_router(structure_router(database))
    app.include_router(reminders_router(database))
    app.include_router(audio_router(database, settings, audio_storage, create_audio_capture))
    app.include_router(transcripts_router(database))
    app.include_router(brain_router(database, settings))
    app.include_router(assistant_router(database, settings))
    app.include_router(auth_flows_router(database, settings))
    from app.routes.email_auth import build_router as email_auth_router
    app.include_router(email_auth_router(database, settings))

    @app.get("/health")
    def health(db=Depends(database)):
        db.execute(text("SELECT 1"))
        return {"status": "ok", "provider": settings.provider, "simulation": settings.provider == "mock"}

    @app.get("/api/v1/example")
    def example():
        return {"text": DEMO_TEXT, "simulation": settings.provider == "mock"}

    def issue_session(db, response, user, request):
        return create_session(db, response, user, request, settings)

    @app.post("/api/v1/auth/register", status_code=201)
    def register(body: Registration, request: Request, response: Response, db=Depends(database)):
        check_origin(request)
        if not settings.allow_registration:
            raise HTTPException(403, "Регистрация закрыта")
        if settings.email_registration_enabled:
            raise HTTPException(403, "Создайте аккаунт через почту или Telegram")
        require_consent(body.accept_policy, body.policy_version)
        ip = request.client.host if request.client else "unknown"
        throttle(db, "register:" + ip)
        user = User(
            username=body.username.lower(), password_hash=hash_password(body.password),
            policy_version=POLICY_VERSION, policy_accepted_at=time.time(),
        )
        db.add(user)
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            raise HTTPException(409, "Это имя уже занято") from None
        record_event(db, user.id, "registered", user.id)
        return issue_session(db, response, user, request)

    @app.post("/api/v1/auth/login")
    def login(body: Credentials, request: Request, response: Response, db=Depends(database)):
        check_origin(request)
        username = body.username.lower()
        ip = request.client.host if request.client else "unknown"
        throttle(db, "login-ip:" + ip, limit=30)
        throttle(db, "login-user:" + username)
        user = db.scalar(select(User).where(User.username == username))
        valid = verify_password(body.password, user.password_hash if user else DUMMY_PASSWORD_HASH)
        if not user or not valid:
            raise HTTPException(401, "Неверное имя или пароль")
        return issue_session(db, response, user, request)

    @app.get("/api/v1/auth/me")
    def me(request: Request, db=Depends(database)):
        session = get_login_session(request, db)
        user = db.get(User, session.user_id)
        return user_payload(user, session.csrf_token)

    @app.get("/api/v1/provider/usage")
    def provider_usage(request: Request, db=Depends(database)):
        session = get_login_session(request, db)
        user = db.get(User, session.user_id)
        daily = daily_state(db, settings, user.id)
        if settings.provider == "mock":
            return {"provider": "mock", "simulation": True, **daily}
        budget = db.get(ProviderBudget, "cloudru")
        used = budget.reserved_calls if budget else 0
        return {
            "provider": "cloudru",
            "simulation": False,
            "model": settings.cloudru_model,
            "global_used": used,
            "global_limit": settings.live_call_limit,
            "global_remaining": max(0, settings.live_call_limit - used),
            "user_used": user.live_calls,
            "user_limit": settings.live_user_call_limit,
            "user_remaining": max(0, settings.live_user_call_limit - user.live_calls),
            **daily,
        }

    @app.post("/api/v1/auth/logout", status_code=204)
    def logout(request: Request, response: Response, db=Depends(database)):
        session = get_login_session(request, db, write=True)
        db.delete(session)
        db.commit()
        response.delete_cookie(COOKIE_NAME, path="/")

    @app.post("/api/v1/captures/text", status_code=202)
    def create_capture(
        body: TextCapture,
        request: Request,
        idempotency_key: str = Header(min_length=1, max_length=100),
        db=Depends(database),
    ):
        session = get_login_session(request, db, write=True)
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", idempotency_key):
            raise HTTPException(422, "Idempotency-Key должен содержать латинские буквы, цифры или _ . : -")
        result = capture_text(
            db, session.user_id, body.text, idempotency_key, settings, processing_mode=body.processing_mode,
        )
        if isinstance(result, Note):
            saved = {"capture_id": result.capture_id, "job_id": None, "status": "saved", "note_id": result.id}
            if body.processing_mode == "ai":
                saved["ai_limit_exceeded"] = True  # An AI request that ended as a note: daily limit spent.
            return saved
        return job_view(db, result)

    @app.get("/api/v1/captures/{capture_id}", response_model=CaptureResponse)
    def read_capture(capture_id: str, request: Request, db=Depends(database)):
        session = get_login_session(request, db)
        return capture_view(db, owned_capture(db, capture_id, session.user_id))

    @app.get("/api/v1/jobs")
    def list_jobs(request: Request, limit: int = Query(20, ge=1, le=100), db=Depends(database)):
        session = get_login_session(request, db)
        jobs = db.scalars(
            select(Job)
            .where(Job.user_id == session.user_id)
            .order_by(Job.created_at.desc(), Job.id)
            .limit(limit)
        ).all()
        return [job_view(db, job) for job in jobs]

    @app.get("/api/v1/jobs/{job_id}")
    def read_job(job_id: str, request: Request, db=Depends(database)):
        session = get_login_session(request, db)
        job = db.scalar(select(Job).where(Job.id == job_id, Job.user_id == session.user_id))
        if not job:
            raise HTTPException(404, "Задание не найдено")
        return job_view(db, job)

    @app.get("/api/v1/notes", response_model=list[NoteSummary], responses={200: {
        "headers": {"X-Next-Notes-Offset": {"schema": {"type": "integer"}, "description": "Next offset, omitted on last page"}},
    }})
    def list_notes(
        request: Request,
        response: Response,
        limit: int = Query(20, ge=1, le=100),
        offset: int = Query(0, ge=0),
        q: str | None = Query(None, max_length=200),
        category_id: str | None = Query(None, max_length=36),
        channel: Literal["web", "telegram"] | None = Query(None),
        input_kind: Literal["text", "audio"] | None = Query(None),
        db=Depends(database),
    ):
        session = get_login_session(request, db)
        notes = search_notes(
            db, session.user_id, q=q, category_id=category_id, channel=channel, input_kind=input_kind,
            limit=limit, offset=offset,
        )
        if len(notes) > limit:
            response.headers["X-Next-Notes-Offset"] = str(offset + limit)
        page = notes[:limit]
        sources = {
            row.id: (row.channel, row.input_kind)
            for row in db.scalars(select(Capture).where(
                Capture.user_id == session.user_id, Capture.id.in_([n.capture_id for n in page]),
            ))
        }
        return [
            {"id": n.id, "title": n.title, "version": n.version, "updated_at": n.updated_at,
             "category_id": n.category_id, "channel": sources[n.capture_id][0],
             "input_kind": sources[n.capture_id][1]} for n in page
        ]

    @app.get("/api/v1/notes/{note_id}", response_model=NoteResponse)
    def read_note(note_id: str, request: Request, db=Depends(database)):
        session = get_login_session(request, db)
        return note_view(db, owned_note(db, note_id, session.user_id), settings)

    @app.get("/api/v1/notes/{note_id}/revisions")
    def revisions(note_id: str, request: Request, db=Depends(database)):
        session = get_login_session(request, db)
        owned_note(db, note_id, session.user_id)
        rows = db.scalars(
            select(Revision).where(Revision.note_id == note_id).order_by(Revision.version.desc()).limit(100)
        ).all()
        return [{"version": r.version, "created_at": r.created_at, **r.snapshot} for r in rows]

    @app.patch("/api/v1/notes/{note_id}", response_model=NoteResponse)
    def update_note(note_id: str, body: NoteEdit, request: Request, db=Depends(database)):
        session = get_login_session(request, db, write=True)
        note = owned_note(db, note_id, session.user_id)
        edit_note(db, note, body.version, title=body.title, markdown=body.markdown)
        return note_view(db, note, settings)

    @app.patch("/api/v1/notes/{note_id}/conclusions/{conclusion_id}", response_model=NoteResponse)
    def update_conclusion(
        note_id: str, conclusion_id: str, body: ConclusionEdit, request: Request, db=Depends(database)
    ):
        session = get_login_session(request, db, write=True)
        note = owned_note(db, note_id, session.user_id)
        conclusions = copy.deepcopy(note.conclusions)
        conclusion = next((c for c in conclusions if c["id"] == conclusion_id), None)
        if conclusion is None:
            raise HTTPException(404, "Вывод не найден")
        conclusion["status"] = body.status
        edit_note(db, note, body.version, conclusions=conclusions)
        return note_view(db, note, settings)

    app.mount("/static", ProtectedStaticFiles(directory=STATIC, networks=admin_networks), name="static")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/privacy", include_in_schema=False)
    def privacy():
        return FileResponse(STATIC / "privacy.html")

    return app
