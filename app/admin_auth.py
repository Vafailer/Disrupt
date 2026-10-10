"""Administrator sign-in, sessions, network gate and audit trail.

An administrator is a separate identity with its own password, authenticator code and session.
It is never a regular user with extra rights, and no regular session grants any admin access.
"""

import base64
import hashlib
import hmac
import ipaddress
import secrets
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from sqlalchemy import delete, or_, select, update
from starlette.responses import JSONResponse

from app import totp
from app.admin_models import AdminAccount, AdminAudit, AdminSession
from app.security import DUMMY_PASSWORD_HASH, hash_token, throttle, verify_password

COOKIE_NAME = "beresta_admin"
MAX_FAILURES = 5
LOCK_SECONDS = 15 * 60
SESSION_SECONDS = 8 * 3600
IDLE_SECONDS = 30 * 60
TOUCH_SECONDS = 60
GENERIC_FAILURE = "Не удалось войти. Проверьте данные."
UNAUTHENTICATED = "Войдите в админку"
# A public reverse proxy always adds at least one of these. Admin is never served through one.
FORWARDING_HEADERS = {b"x-forwarded-for", b"forwarded", b"x-real-ip", b"x-forwarded-host", b"x-forwarded-proto"}


def admin_path(path: str) -> bool:
    if path in {"/admin", "/admin-api"} or path.startswith(("/admin/", "/admin-api/")):
        return True
    return path.startswith("/static/") and path.rsplit("/", 1)[-1].startswith("admin.")


def peer_allowed(scope, networks) -> bool:
    """True only for a direct connection from an allowed private network, never via a proxy."""
    if any(name in FORWARDING_HEADERS for name, _ in scope.get("headers", [])):
        return False
    client = scope.get("client")
    if not client:
        return False
    try:
        address = ipaddress.ip_address(client[0])
    except ValueError:
        return False
    if getattr(address, "ipv4_mapped", None):
        address = address.ipv4_mapped
    return any(address in network for network in networks)


class AdminNetworkGate:
    """Outside the private network the admin does not exist: every admin path answers 404."""

    def __init__(self, app, networks):
        self.app, self.networks = app, networks

    async def __call__(self, scope, receive, send):
        guarded = scope["type"] in {"http", "websocket"} and admin_path(scope["path"])
        if guarded and not peer_allowed(scope, self.networks):
            if scope["type"] == "websocket":
                return await send({"type": "websocket.close", "code": 1008})
            return await JSONResponse({"detail": "Not Found"}, status_code=404)(scope, receive, send)
        await self.app(scope, receive, send)


def client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


def csrf_for(token: str) -> str:
    """Derived from the session token, so the page can ask for it again after a reload."""
    digest = hmac.new(token.encode(), b"beresta-admin-csrf-v1", hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def audit(db, action, *, admin_id=None, target=None, ip=None, details=None):
    """Add an audit row to the caller's transaction. `details` must never carry user content."""
    db.add(AdminAudit(admin_id=admin_id, action=action, target=target, ip=ip, details=details or {}))


def check_same_origin(request: Request):
    origin = request.headers.get("origin")
    if origin is not None and (origin == "null" or urlsplit(origin).netloc != request.headers.get("host", "")):
        raise HTTPException(403, "Недопустимый источник запроса")


@dataclass
class AdminContext:
    admin: AdminAccount
    session: AdminSession
    token: str


def authenticate(request: Request, db, *, write=False) -> AdminContext:
    token = request.cookies.get(COOKIE_NAME, "")
    if not token or len(token) > 200:
        raise HTTPException(401, UNAUTHENTICATED)
    now = time.time()
    session = db.get(AdminSession, hash_token(token))
    if session is None:
        raise HTTPException(401, UNAUTHENTICATED)
    admin = db.get(AdminAccount, session.admin_id)
    if (session.expires_at <= now or session.last_seen_at + IDLE_SECONDS <= now
            or admin is None or admin.disabled or not admin.totp_enabled):
        db.delete(session)
        db.commit()
        raise HTTPException(401, UNAUTHENTICATED)
    if write:
        check_same_origin(request)
        if not hmac.compare_digest(hash_token(request.headers.get("x-csrf-token", "")), session.csrf_hash):
            raise HTTPException(403, "Неверный CSRF-токен")
    if now - session.last_seen_at >= TOUCH_SECONDS:
        session.last_seen_at = now
        db.commit()
    return AdminContext(admin, session, token)


def refuse(db, admin, ip, reason, *, count=True, now=None):
    """Record a failed sign-in, maybe lock the account, and answer with the one generic message."""
    now = time.time() if now is None else now
    locked = False
    if admin is not None and count:
        db.execute(update(AdminAccount).where(AdminAccount.id == admin.id)
                   .values(failed_attempts=AdminAccount.failed_attempts + 1))
        attempts = db.scalar(select(AdminAccount.failed_attempts).where(AdminAccount.id == admin.id))
        if attempts >= MAX_FAILURES:
            locked = True
            db.execute(update(AdminAccount).where(AdminAccount.id == admin.id)
                       .values(failed_attempts=0, locked_until=now + LOCK_SECONDS))
    admin_id = admin.id if admin is not None else None
    audit(db, "login_failed", admin_id=admin_id, ip=ip, details={"reason": reason})
    if locked:
        audit(db, "account_locked", admin_id=admin_id, ip=ip, details={"minutes": LOCK_SECONDS // 60})
    db.commit()
    raise HTTPException(401, GENERIC_FAILURE)


def sign_in(db, *, username, password, code, ip):
    """Check username, password and authenticator code. Returns (token, csrf, admin) or raises 401."""
    now = time.time()
    throttle(db, "admin-login:" + ip, limit=10)
    username = username.strip().lower()
    admin = db.scalar(select(AdminAccount).where(AdminAccount.username == username)) if 0 < len(username) <= 64 else None
    if admin is None:
        verify_password(password, DUMMY_PASSWORD_HASH)  # Same work as for a real account.
        refuse(db, None, ip, "unknown_account", now=now)
    if admin.locked_until is not None and admin.locked_until > now:
        verify_password(password, DUMMY_PASSWORD_HASH)
        refuse(db, admin, ip, "locked", count=False, now=now)
    password_ok = verify_password(password, admin.password_hash)
    step = totp.verify(admin.totp_secret, code, now=now, last_step=admin.last_totp_step)
    usable = admin.totp_enabled and not admin.disabled
    if not usable:
        refuse(db, admin, ip, "disabled", count=False, now=now)
    if not (password_ok and step is not None):
        refuse(db, admin, ip, "credentials", now=now)
    # Claim the step atomically, so two parallel requests cannot both use one code.
    claimed = db.execute(
        update(AdminAccount)
        .where(AdminAccount.id == admin.id,
               or_(AdminAccount.last_totp_step.is_(None), AdminAccount.last_totp_step < step))
        .values(last_totp_step=step, failed_attempts=0, locked_until=None, last_login_at=now)
    ).rowcount
    if not claimed:
        refuse(db, admin, ip, "replayed_code", now=now)
    db.execute(delete(AdminSession).where(AdminSession.expires_at < now))
    token = secrets.token_urlsafe(32)
    csrf = csrf_for(token)
    db.add(AdminSession(token_hash=hash_token(token), admin_id=admin.id, created_at=now,
                        expires_at=now + SESSION_SECONDS, last_seen_at=now, csrf_hash=hash_token(csrf), ip=ip))
    audit(db, "login_success", admin_id=admin.id, ip=ip)
    db.commit()
    return token, csrf, admin
