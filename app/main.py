import asyncio
import copy
import re
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import delete, select, text
from sqlalchemy.exc import IntegrityError

from app.config import Settings
from app.db import make_database
from app.error_logging import log_error
from app.models import Job, LoginSession, Note, ProviderBudget, Revision, User
from app.providers import DEMO_TEXT
from app.schemas import ConclusionEdit, Credentials, NoteEdit, TextCapture
from app.security import (
    COOKIE_NAME,
    DUMMY_PASSWORD_HASH,
    check_origin,
    get_login_session,
    hash_password,
    hash_token,
    throttle,
    verify_password,
)
from app.services import capture_text, edit_note, job_view, note_view, owned_note
from app.worker import Worker

STATIC = Path(__file__).parent / "static"


class BodyLimit:
    """Bound the actual body, including chunked requests, before JSON parsing."""

    def __init__(self, app, maximum=131072):
        self.app, self.maximum = app, maximum

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] not in {"POST", "PUT", "PATCH"}:
            return await self.app(scope, receive, send)
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body.extend(message.get("body", b""))
            if len(body) > self.maximum:
                response = JSONResponse({"detail": "Слишком большой запрос"}, status_code=413)
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


def create_app(settings: Settings | None = None, provider=None):
    settings = settings or Settings.from_env()
    engine, sessions = make_database(settings.database_url)

    @asynccontextmanager
    async def lifespan(app):
        stop = asyncio.Event()
        task = None
        if settings.auto_worker:
            worker = Worker(sessions, settings, provider)

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
    app.add_middleware(BodyLimit)

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
        return JSONResponse(
            status_code=422,
            content={
                "detail": "Проверьте обязательные поля, формат и длину ввода",
                "fields": [".".join(str(part) for part in error["loc"]) for error in exc.errors()],
            },
        )

    def database():
        with sessions() as db:
            yield db

    @app.get("/health")
    def health(db=Depends(database)):
        db.execute(text("SELECT 1"))
        return {"status": "ok", "provider": settings.provider, "simulation": settings.provider == "mock"}

    @app.get("/api/v1/example")
    def example():
        return {"text": DEMO_TEXT, "simulation": settings.provider == "mock"}

    def issue_session(db, response, user, request):
        # Rotate the current browser session on login/registration.
        old = request.cookies.get(COOKIE_NAME)
        if old:
            db.execute(delete(LoginSession).where(LoginSession.token_hash == hash_token(old)))
        db.execute(delete(LoginSession).where(LoginSession.expires_at < time.time()))
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        db.add(
            LoginSession(
                token_hash=hash_token(token),
                user_id=user.id,
                csrf_token=csrf,
                expires_at=time.time() + settings.session_seconds,
            )
        )
        db.commit()
        response.set_cookie(
            COOKIE_NAME,
            token,
            httponly=True,
            secure=settings.secure_cookies,
            samesite="lax",
            max_age=settings.session_seconds,
            path="/",
        )
        return {"id": user.id, "username": user.username, "csrf_token": csrf}

    @app.post("/api/v1/auth/register", status_code=201)
    def register(body: Credentials, request: Request, response: Response, db=Depends(database)):
        check_origin(request)
        if not settings.allow_registration:
            raise HTTPException(403, "Регистрация закрыта")
        ip = request.client.host if request.client else "unknown"
        throttle(db, "register:" + ip)
        user = User(username=body.username.lower(), password_hash=hash_password(body.password))
        db.add(user)
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            raise HTTPException(409, "Это имя уже занято") from None
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
        return {"id": user.id, "username": user.username, "csrf_token": session.csrf_token}

    @app.get("/api/v1/provider/usage")
    def provider_usage(request: Request, db=Depends(database)):
        session = get_login_session(request, db)
        user = db.get(User, session.user_id)
        if settings.provider == "mock":
            return {"provider": "mock", "simulation": True}
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
        return job_view(db, capture_text(db, session.user_id, body.text, idempotency_key, settings))

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

    @app.get("/api/v1/notes")
    def list_notes(
        request: Request,
        limit: int = Query(20, ge=1, le=100),
        offset: int = Query(0, ge=0),
        db=Depends(database),
    ):
        session = get_login_session(request, db)
        notes = db.scalars(
            select(Note)
            .where(Note.user_id == session.user_id)
            .order_by(Note.updated_at.desc(), Note.id)
            .offset(offset)
            .limit(limit)
        ).all()
        return [
            {"id": n.id, "title": n.title, "version": n.version, "updated_at": n.updated_at} for n in notes
        ]

    @app.get("/api/v1/notes/{note_id}")
    def read_note(note_id: str, request: Request, db=Depends(database)):
        session = get_login_session(request, db)
        return note_view(db, owned_note(db, note_id, session.user_id))

    @app.get("/api/v1/notes/{note_id}/revisions")
    def revisions(note_id: str, request: Request, db=Depends(database)):
        session = get_login_session(request, db)
        owned_note(db, note_id, session.user_id)
        rows = db.scalars(
            select(Revision).where(Revision.note_id == note_id).order_by(Revision.version.desc()).limit(100)
        ).all()
        return [{"version": r.version, "created_at": r.created_at, **r.snapshot} for r in rows]

    @app.patch("/api/v1/notes/{note_id}")
    def update_note(note_id: str, body: NoteEdit, request: Request, db=Depends(database)):
        session = get_login_session(request, db, write=True)
        note = owned_note(db, note_id, session.user_id)
        edit_note(db, note, body.version, title=body.title, markdown=body.markdown)
        return note_view(db, note)

    @app.patch("/api/v1/notes/{note_id}/conclusions/{conclusion_id}")
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
        return note_view(db, note)

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC / "index.html")

    return app
