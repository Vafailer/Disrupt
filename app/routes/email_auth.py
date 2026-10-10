"""Email/password registration with a six-digit code, disabled until owner mail acceptance."""

import hmac
import re
import secrets
import time
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError

from app.analytics import record_event
from app.mailer import MAIL_DISABLED_TEXT, MailError
from app.models import EmailRegistrationPending as Pending
from app.models import User
from app.policy import POLICY_VERSION, require_consent
from app.schemas import EmailCredentials, EmailRegistrationConfirm, EmailRegistrationStart
from app.security import (
    DUMMY_PASSWORD_HASH,
    check_origin,
    hash_password,
    hash_token,
    throttle,
    verify_password,
)
from app.sessions import create_session

COOKIE = "notes_email_registration"
PATH = "/api/v1/auth/email"
LIFETIME = 600
INVALID_CODE = "Код не подошёл или истёк. Проверьте его или запросите новый"


def digest(binding, code):
    # The 256-bit cookie is absent from DB. A DB dump alone cannot brute-force a six-digit code.
    return hash_token(binding + ":" + code)


def retire(row, now):
    row.used_at = now
    row.password_hash = row.binding_hash = row.code_hash = ""


def available_name(db, email):
    base = re.sub(r"[^a-z0-9_.-]", "", email.split("@", 1)[0])[:40]
    if len(base) < 3:
        base = "user"
    for suffix in ("", *("_" + secrets.token_hex(4) for _ in range(5))):
        name = base + suffix
        if db.scalar(select(User.id).where(func.lower(User.username) == name)) is None:
            return name
    raise HTTPException(409, "Не удалось создать аккаунт. Попробуйте ещё раз")


def build_router(database, settings):
    router = APIRouter(tags=["auth"])

    def enabled():
        if not settings.email_registration_enabled:
            raise HTTPException(503, MAIL_DISABLED_TEXT)

    @router.get(PATH + "/options")
    def options(request: Request):
        return {
            "login_enabled": settings.email_registration_enabled,
            "registration_enabled": bool(
                settings.email_registration_enabled and settings.allow_registration and request.app.state.mailer.enabled
            ),
        }

    @router.post(PATH + "/registration/start", status_code=202)
    def start(body: EmailRegistrationStart, request: Request, response: Response, db=Depends(database)):
        check_origin(request)
        enabled()
        if not settings.allow_registration:
            raise HTTPException(403, "Регистрация закрыта")
        require_consent(body.accept_policy, body.policy_version)
        mailer = request.app.state.mailer
        if not mailer.enabled:
            raise HTTPException(503, MAIL_DISABLED_TEXT)
        ip = request.client.host if request.client else "unknown"
        throttle(db, "email-register-ip:" + ip, limit=10)
        throttle(db, "email-register-address:" + body.email, limit=3)
        now = time.time()
        recent = db.scalar(select(func.count()).select_from(Pending).where(
            Pending.email == body.email, Pending.created_at > now - 3600,
        ))
        latest = db.scalar(select(func.max(Pending.created_at)).where(Pending.email == body.email))
        if recent >= 5 or (latest is not None and latest > now - 60):
            raise HTTPException(429, "Подождите перед новым письмом", headers={"Retry-After": "60"})
        if db.scalar(select(func.count()).select_from(Pending).where(Pending.created_at > now - 86400)) >= 500:
            raise HTTPException(429, "Отправка кодов временно ограничена. Попробуйте позже")
        db.execute(delete(Pending).where(Pending.created_at < now - 86400))
        db.execute(update(Pending).where(Pending.email == body.email, Pending.used_at.is_(None)).values(
            used_at=now, password_hash="", binding_hash="", code_hash="",
        ))
        binding = secrets.token_urlsafe(32)
        code = f"{secrets.randbelow(1_000_000):06d}"
        row = Pending(
            email=body.email, password_hash=hash_password(body.password), binding_hash=hash_token(binding),
            code_hash=digest(binding, code), policy_version=POLICY_VERSION, expires_at=now + LIFETIME,
        )
        db.add(row)
        db.commit()
        # Same outward response for an existing email; never overwrite or attach that account.
        exists = db.scalar(select(User.id).where(User.email == body.email)) is not None
        if exists:
            retire(row, now)
            db.commit()
        else:
            try:
                mailer.send(body.email, "Код регистрации в beresta", (
                    f"Ваш код регистрации: {code}\n\nОн действует 10 минут. "
                    "Введите его в том браузере, где начали регистрацию.\n\n"
                    "Если вы не регистрировались в beresta, проигнорируйте письмо."
                ))
            except MailError:
                retire(row, time.time())
                db.commit()
                raise HTTPException(503, "Не удалось отправить код. Попробуйте позже") from None
        response.set_cookie(COOKIE, binding, httponly=True, secure=settings.secure_cookies,
                            samesite="lax", path=PATH, max_age=LIFETIME)
        return {"registration_id": row.id, "expires_at": datetime.fromtimestamp(row.expires_at, UTC).isoformat()}

    @router.post(PATH + "/registration/confirm", status_code=201)
    def confirm(body: EmailRegistrationConfirm, request: Request, response: Response, db=Depends(database)):
        check_origin(request)
        enabled()
        if not settings.allow_registration:
            raise HTTPException(403, "Регистрация закрыта")
        throttle(db, "email-code-ip:" + (request.client.host if request.client else "unknown"), limit=30)
        binding = request.cookies.get(COOKIE, "")
        row = db.scalar(select(Pending).where(Pending.id == body.registration_id).with_for_update())
        now = time.time()
        if (not binding or len(binding) > 100 or row is None or row.used_at is not None or row.expires_at <= now
                or row.attempts >= 5 or not hmac.compare_digest(hash_token(binding), row.binding_hash)):
            raise HTTPException(400, INVALID_CODE)
        if not hmac.compare_digest(digest(binding, body.code), row.code_hash):
            row.attempts += 1
            if row.attempts == 5:
                retire(row, now)
            db.commit()
            raise HTTPException(400, INVALID_CODE)
        if row.policy_version != POLICY_VERSION:
            raise HTTPException(409, "Политика обновилась. Начните регистрацию заново")
        user = User(username=available_name(db, row.email), email=row.email, email_verified_at=now,
                    password_hash=row.password_hash, policy_version=row.policy_version, policy_accepted_at=now)
        retire(row, now)
        db.add(user)
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            raise HTTPException(409, "Не удалось создать аккаунт. Попробуйте войти") from None
        record_event(db, user.id, "registered", user.id, outcome="email")
        result = create_session(db, response, user, request, settings, method="email")
        response.delete_cookie(COOKIE, path=PATH)
        return result

    @router.post(PATH + "/login")
    def login(body: EmailCredentials, request: Request, response: Response, db=Depends(database)):
        check_origin(request)
        enabled()
        throttle(db, "email-login-ip:" + (request.client.host if request.client else "unknown"), limit=30)
        throttle(db, "email-login-address:" + body.email)
        user = db.scalar(select(User).where(User.email == body.email, User.email_verified_at.is_not(None)))
        valid = verify_password(body.password, user.password_hash if user else DUMMY_PASSWORD_HASH)
        if user is None or not valid:
            raise HTTPException(401, "Неверная почта или пароль")
        return create_session(db, response, user, request, settings, method="email")

    return router
