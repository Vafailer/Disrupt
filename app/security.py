import base64
import hashlib
import hmac
import secrets
import time

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.models import LoginSession, LoginThrottle

COOKIE_NAME = "notes_session"
SERVICE_BEARER = HTTPBearer(auto_error=False, scheme_name="InternalServiceToken")


def require_internal_service(
    request: Request, credentials: HTTPAuthorizationCredentials | None = Depends(SERVICE_BEARER)
):
    expected = request.app.state.settings.internal_api_token
    if not expected:
        raise HTTPException(503, "Внутренний API не настроен")
    if (
        credentials is None
        or credentials.scheme.lower() != "bearer"
        or not hmac.compare_digest(credentials.credentials.encode(), expected.encode())
    ):
        raise HTTPException(401, "Недопустимый сервисный секрет", headers={"WWW-Authenticate": "Bearer"})


def hash_token(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(digest).decode()


def verify_password(password: str, encoded: str) -> bool:
    _, salt, expected = encoded.split("$")
    digest = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=16384, r=8, p=1)
    return hmac.compare_digest(digest, base64.b64decode(expected))


DUMMY_PASSWORD_HASH = hash_password("not-an-account-password")


def throttle(db, identifier: str, limit: int = 10):
    now = time.time()
    key = hash_token(identifier)
    insert = sqlite_insert if db.bind.dialect.name == "sqlite" else pg_insert
    db.execute(insert(LoginThrottle).values(key=key, window_start=now, attempts=0).on_conflict_do_nothing())
    db.execute(
        update(LoginThrottle)
        .where(LoginThrottle.key == key, LoginThrottle.window_start <= now - 60)
        .values(window_start=now, attempts=0)
    )
    accepted = db.execute(
        update(LoginThrottle)
        .where(LoginThrottle.key == key, LoginThrottle.attempts < limit)
        .values(attempts=LoginThrottle.attempts + 1)
    ).rowcount
    # Commit attempts even if the subsequent password verification fails.
    db.execute(delete(LoginThrottle).where(LoginThrottle.window_start < now - 86400))
    db.commit()
    if not accepted:
        raise HTTPException(429, "Слишком много попыток. Подождите минуту.", headers={"Retry-After": "60"})


def check_origin(request: Request):
    origin = request.headers.get("origin")
    if origin and origin != request.app.state.settings.public_origin:
        raise HTTPException(403, "Недопустимый источник запроса")


def get_login_session(request: Request, db, *, write=False) -> LoginSession:
    token = request.cookies.get(COOKIE_NAME, "")
    if not token or len(token) > 200:
        raise HTTPException(401, "Войдите в приложение")
    session = db.scalar(
        select(LoginSession).where(
            LoginSession.token_hash == hash_token(token), LoginSession.expires_at > time.time()
        )
    )
    if session is None:
        raise HTTPException(401, "Сессия истекла. Войдите снова")
    if write:
        check_origin(request)
        if not hmac.compare_digest(request.headers.get("x-csrf-token", "").encode(), session.csrf_token.encode()):
            raise HTTPException(403, "Неверный CSRF-токен")
    return session
