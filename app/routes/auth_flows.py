"""Sign-in through Telegram, password recovery, privacy consent, email scaffold and deletion requests.

The bot half of the Telegram flow lives in app/routes/internal.py (login-confirm). Everything here
is browser-facing. Tokens are random, shown once and stored only as hashes.
"""

import hmac
import re
import secrets
import time
from datetime import UTC, datetime

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError

from app.analytics import record_event
from app.error_logging import log_error
from app.mailer import MAIL_DISABLED_TEXT, MailDisabled, MailError
from app.models import (
    EmailVerification,
    LoginSession,
    Outbox,
    TelegramIdentity,
    TelegramLogin,
    User,
    new_id,
)
from app.policy import POLICY_VERSION, require_consent
from app.schemas import (
    AcceptPolicy,
    DeletionRequest,
    EmailBody,
    PasswordReset,
    RecoveryRequest,
    TelegramLoginStart,
    TokenBody,
)
from app.security import (
    COOKIE_NAME,
    check_origin,
    get_login_session,
    hash_password,
    hash_token,
    throttle,
    verify_password,
)
from app.sessions import create_session

LOGIN_SECONDS = 300
RESET_SECONDS = 900
VERIFY_SECONDS = 86400
RESETS_PER_HOUR = 5  # Stops someone from filling a stranger's chat or mailbox with reset messages.
BINDING_COOKIE = "notes_tg_login"
BINDING_PATH = "/api/v1/auth/telegram"

RESET_TELEGRAM_TEXT = (
    "Сброс пароля в beresta.\n\n"
    "Откройте ссылку и задайте новый пароль. Она работает 15 минут и только один раз.\n\n"
    "{link}\n\n"
    "Если вы ничего не запрашивали, просто проигнорируйте это сообщение."
)
RESET_MAIL_SUBJECT = "Сброс пароля в beresta"
RESET_MAIL_BODY = (
    "Здравствуйте!\n\nЧтобы задать новый пароль в beresta, откройте ссылку. "
    "Она работает 15 минут и только один раз.\n\n{link}\n\n"
    "Если вы ничего не запрашивали, просто проигнорируйте это письмо."
)
VERIFY_MAIL_SUBJECT = "Подтвердите почту в beresta"
VERIFY_MAIL_BODY = (
    "Здравствуйте!\n\nЧтобы привязать этот адрес к аккаунту beresta, откройте ссылку в том браузере, "
    "где вы вошли. Она работает сутки.\n\n{link}\n\n"
    "Если вы ничего не запрашивали, просто проигнорируйте это письмо."
)
RECOVERY_TELEGRAM_ANSWER = {
    "status": "ok",
    "message": "Если у аккаунта подключён Telegram, мы отправили туда ссылку для нового пароля. Она действует 15 минут.",
}
RECOVERY_EMAIL_ANSWER = {
    "status": "ok",
    "message": "Если этот адрес подтверждён в аккаунте, мы отправили на него письмо со ссылкой. Она действует 15 минут.",
}
DEAD_LINK = "Ссылка недействительна или истекла. Запросите новую"
USED_LOGIN = "Эта ссылка входа уже использована. Начните заново"


def client_ip(request):
    return request.client.host if request.client else "unknown"


def iso(timestamp):
    return datetime.fromtimestamp(timestamp, UTC).isoformat()


def new_login(db, purpose, *, user_id=None, policy_version=None):
    now = time.time()
    db.execute(delete(TelegramLogin).where(TelegramLogin.expires_at < now - 86400))
    token, binding = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    row = TelegramLogin(
        id=new_id(), purpose=purpose, token_hash=hash_token(token), binding_hash=hash_token(binding),
        user_id=user_id, policy_version=policy_version, status="pending",
        expires_at=now + LOGIN_SECONDS, created_at=now,
    )
    db.add(row)
    db.commit()
    return row, token, binding


def free_username(db, row):
    """A name from the Telegram handle when it fits the username rules, otherwise tg<id>, made unique."""
    base = (row.telegram_username or "").lower()
    if not re.fullmatch(r"[a-z0-9_]{3,40}", base):
        base = f"tg{row.telegram_user_id}"
    names = [base, *(f"{base}_{secrets.token_hex(2)}" for _ in range(8)), f"tg{row.telegram_user_id}_{secrets.token_hex(4)}"]
    for name in names:
        if len(name) <= 64 and db.scalar(select(User.id).where(func.lower(User.username) == name)) is None:
            return name
    raise HTTPException(409, "Не удалось подобрать имя. Попробуйте ещё раз")


def issue_reset_token(db, user, channel, email=None):
    """Retire older reset links of this account and add a new one. The caller commits."""
    now = time.time()
    db.execute(
        update(EmailVerification)
        .where(
            EmailVerification.user_id == user.id, EmailVerification.purpose == "reset",
            EmailVerification.used_at.is_(None),
        )
        .values(used_at=now)
    )
    token = secrets.token_urlsafe(32)
    db.add(EmailVerification(
        user_id=user.id, purpose="reset", channel=channel, email=email,
        token_hash=hash_token(token), expires_at=now + RESET_SECONDS, created_at=now,
    ))
    return token


def too_many_resets(db, user):
    recent = db.scalar(
        select(func.count()).select_from(EmailVerification).where(
            EmailVerification.user_id == user.id, EmailVerification.purpose == "reset",
            EmailVerification.created_at > time.time() - 3600,
        )
    )
    return recent >= RESETS_PER_HOUR


def request_deletion(db, user, response):
    """Mark the account for removal by the operator and end every session. Nothing is purged here."""
    now = time.time()
    if user.deletion_requested_at is None:
        user.deletion_requested_at = now
    db.execute(delete(LoginSession).where(LoginSession.user_id == user.id))
    record_event(db, user.id, "deletion_requested", new_id())
    db.commit()
    response.delete_cookie(COOKIE_NAME, path="/")


def deep_link(settings, token):
    return f"https://t.me/{settings.telegram_bot_username}?start=login_{token}"


def send_logged(settings, mailer, to, subject, body):
    """Background mail: a failure is logged without the address and never shown to the visitor."""
    try:
        mailer.send(to, subject, body)
    except MailError as exc:
        log_error("mail_send_failed", "mail_error", log_file=settings.error_log_file, exception=exc)


def build_router(database, settings):
    router = APIRouter(tags=["auth"])

    def require_mailer(request):
        mailer = request.app.state.mailer
        if not mailer.enabled:
            raise HTTPException(503, MAIL_DISABLED_TEXT)
        return mailer

    # Sign in through the bot

    @router.post("/api/v1/auth/telegram/start", status_code=201)
    def telegram_start(body: TelegramLoginStart, request: Request, response: Response, db=Depends(database)):
        check_origin(request)
        throttle(db, "tg-login-start:" + client_ip(request), limit=10)
        require_consent(body.accept_policy, body.policy_version)
        row, token, binding = new_login(db, "login", policy_version=body.policy_version)
        response.set_cookie(
            BINDING_COOKIE, binding, httponly=True, secure=settings.secure_cookies, samesite="lax",
            max_age=LOGIN_SECONDS, path=BINDING_PATH,
        )
        return {"login_id": row.id, "deep_link": deep_link(settings, token), "expires_at": iso(row.expires_at)}

    @router.get("/api/v1/auth/telegram/status/{login_id}")
    def telegram_status(login_id: str, request: Request, response: Response, db=Depends(database)):
        throttle(db, "tg-login-status:" + client_ip(request), limit=120)
        binding = request.cookies.get(BINDING_COOKIE, "")
        row = db.get(TelegramLogin, login_id) if len(login_id) <= 36 else None
        # Only the browser that started the sign-in may finish it. Anyone else sees "not found".
        if (
            row is None or row.purpose != "login" or not binding or len(binding) > 200
            or not hmac.compare_digest(hash_token(binding).encode(), row.binding_hash.encode())
        ):
            raise HTTPException(404, "Вход не найден. Начните заново")
        now = time.time()
        if row.status == "consumed":
            raise HTTPException(409, USED_LOGIN)
        if row.expires_at <= now:
            return {"status": "expired"}
        if row.status == "pending":
            return {"status": "pending", "expires_at": iso(row.expires_at)}
        changed = db.execute(
            update(TelegramLogin)
            .where(TelegramLogin.id == row.id, TelegramLogin.status == "confirmed", TelegramLogin.expires_at > now)
            .values(status="consumed", consumed_at=now)
        ).rowcount
        if not changed:
            db.rollback()
            raise HTTPException(409, USED_LOGIN)
        identity = db.scalar(select(TelegramIdentity).where(
            TelegramIdentity.bot_id == row.bot_id, TelegramIdentity.telegram_user_id == row.telegram_user_id,
        ))
        created = identity is None
        try:
            if created:
                user = User(
                    username=free_username(db, row),
                    # Nobody knows this password. The owner of the Telegram account sets a real one through recovery.
                    password_hash=hash_password(secrets.token_urlsafe(48)),
                )
                db.add(user)
                db.flush()
                db.add(TelegramIdentity(
                    user_id=user.id, bot_id=row.bot_id, telegram_user_id=row.telegram_user_id, chat_id=row.chat_id,
                ))
                db.flush()
                record_event(db, user.id, "registered", user.id, outcome="telegram")
                record_event(db, user.id, "telegram_linked", row.id)
            else:
                user = db.get(User, identity.user_id)
        except IntegrityError:
            db.rollback()
            raise HTTPException(409, "Не удалось создать аккаунт. Попробуйте ещё раз") from None
        if user.policy_version != row.policy_version:
            user.policy_version, user.policy_accepted_at = row.policy_version, now
        payload = create_session(db, response, user, request, settings, method="telegram")
        response.delete_cookie(BINDING_COOKIE, path=BINDING_PATH)
        return {**payload, "status": "ok", "new_account": created}

    # Password recovery

    @router.post("/api/v1/auth/recovery/telegram")
    def recover_through_telegram(body: RecoveryRequest, request: Request, db=Depends(database)):
        check_origin(request)
        username = body.username.strip().lower()
        throttle(db, "recovery-ip:" + client_ip(request), limit=10)
        throttle(db, "recovery-user:" + username, limit=3)
        user = db.scalar(select(User).where(User.username == username))
        identity = None
        if user is not None and not too_many_resets(db, user):
            identity = db.scalar(
                select(TelegramIdentity)
                .where(TelegramIdentity.user_id == user.id, TelegramIdentity.delivery_status != "blocked")
                .order_by(TelegramIdentity.created_at, TelegramIdentity.id)
            )
        if identity is not None:
            token = issue_reset_token(db, user, "telegram")
            db.add(Outbox(
                user_id=user.id, bot_id=identity.bot_id, chat_id=identity.chat_id, generation=1, status="pending",
                message_kind="password_reset",
                message_text=RESET_TELEGRAM_TEXT.format(link=settings.public_origin + "/#reset=" + token),
            ))
            db.commit()
        return RECOVERY_TELEGRAM_ANSWER

    @router.post("/api/v1/auth/recovery/email")
    def recover_through_email(
        body: EmailBody, request: Request, background: BackgroundTasks, db=Depends(database),
    ):
        check_origin(request)
        mailer = require_mailer(request)
        throttle(db, "recovery-ip:" + client_ip(request), limit=10)
        throttle(db, "recovery-email:" + body.email, limit=3)
        user = db.scalar(select(User).where(User.email == body.email, User.email_verified_at.is_not(None)))
        if user is not None and not too_many_resets(db, user):
            token = issue_reset_token(db, user, "email", body.email)
            db.commit()
            background.add_task(
                send_logged, settings, mailer, body.email, RESET_MAIL_SUBJECT,
                RESET_MAIL_BODY.format(link=settings.public_origin + "/#reset=" + token),
            )
        return RECOVERY_EMAIL_ANSWER

    @router.post("/api/v1/auth/password-reset")
    def reset_password(body: PasswordReset, request: Request, db=Depends(database)):
        check_origin(request)
        throttle(db, "reset-ip:" + client_ip(request), limit=10)
        now = time.time()
        row = db.scalar(select(EmailVerification).where(
            EmailVerification.token_hash == hash_token(body.token), EmailVerification.purpose == "reset",
        ))
        if row is None or row.used_at is not None or row.expires_at <= now:
            raise HTTPException(400, DEAD_LINK)
        changed = db.execute(
            update(EmailVerification)
            .where(
                EmailVerification.id == row.id, EmailVerification.used_at.is_(None),
                EmailVerification.expires_at > now,
            )
            .values(used_at=now)
        ).rowcount
        if not changed:
            db.rollback()
            raise HTTPException(400, DEAD_LINK)
        user = db.get(User, row.user_id)
        user.password_hash = hash_password(body.password)
        if row.channel == "email" and row.email and user.email == row.email and user.email_verified_at is None:
            user.email_verified_at = now  # The link reached the mailbox, so the address is confirmed.
        # A reset ends every session, including one an intruder may hold.
        db.execute(delete(LoginSession).where(LoginSession.user_id == user.id))
        record_event(db, user.id, "password_reset", row.id, outcome=row.channel)
        db.commit()
        return {"status": "ok"}

    # Privacy policy

    @router.post("/api/v1/account/accept-policy")
    def accept_policy(body: AcceptPolicy, request: Request, db=Depends(database)):
        session = get_login_session(request, db, write=True)
        if body.policy_version != POLICY_VERSION:
            raise HTTPException(409, "Политика обновилась. Обновите страницу и примите её ещё раз")
        user = db.get(User, session.user_id)
        user.policy_version, user.policy_accepted_at = POLICY_VERSION, time.time()
        db.commit()
        return {"status": "ok", "policy_version": POLICY_VERSION}

    # Email scaffold. Sending stays off until the owner configures SMTP.

    @router.post("/api/v1/account/email")
    def set_email(body: EmailBody, request: Request, db=Depends(database)):
        session = get_login_session(request, db, write=True)
        mailer = require_mailer(request)
        throttle(db, "email-set:" + session.user_id, limit=3)
        user = db.get(User, session.user_id)
        if user.email == body.email and user.email_verified_at is not None:
            raise HTTPException(409, "Этот адрес уже подтверждён")
        now = time.time()
        db.execute(
            update(EmailVerification)
            .where(
                EmailVerification.user_id == user.id, EmailVerification.purpose == "verify",
                EmailVerification.used_at.is_(None),
            )
            .values(used_at=now)
        )
        token = secrets.token_urlsafe(32)
        db.add(EmailVerification(
            user_id=user.id, purpose="verify", channel="email", email=body.email,
            token_hash=hash_token(token), expires_at=now + VERIFY_SECONDS, created_at=now,
        ))
        db.commit()
        try:
            mailer.send(
                body.email, VERIFY_MAIL_SUBJECT,
                VERIFY_MAIL_BODY.format(link=settings.public_origin + "/#verify-email=" + token),
            )
        except MailDisabled:
            raise HTTPException(503, MAIL_DISABLED_TEXT) from None
        except MailError:
            raise HTTPException(503, "Не удалось отправить письмо. Попробуйте позже") from None
        return {"status": "sent"}

    @router.post("/api/v1/account/email/verify")
    def verify_email(body: TokenBody, request: Request, db=Depends(database)):
        session = get_login_session(request, db, write=True)
        throttle(db, "email-verify:" + session.user_id, limit=10)
        now = time.time()
        row = db.scalar(select(EmailVerification).where(
            EmailVerification.token_hash == hash_token(body.token), EmailVerification.purpose == "verify",
            EmailVerification.user_id == session.user_id,
        ))
        if row is None or row.used_at is not None or row.expires_at <= now or not row.email:
            raise HTTPException(400, DEAD_LINK)
        changed = db.execute(
            update(EmailVerification)
            .where(
                EmailVerification.id == row.id, EmailVerification.used_at.is_(None),
                EmailVerification.expires_at > now,
            )
            .values(used_at=now)
        ).rowcount
        if not changed:
            db.rollback()
            raise HTTPException(400, DEAD_LINK)
        user = db.get(User, session.user_id)
        user.email, user.email_verified_at = row.email, now
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise HTTPException(409, "Этот адрес уже привязан к другому аккаунту") from None
        return {"status": "verified", "email": user.email}

    # Deletion request (152-FZ). The operator removes the data; nothing is purged automatically.

    @router.post("/api/v1/account/delete-request")
    def delete_with_password(body: DeletionRequest, request: Request, response: Response, db=Depends(database)):
        session = get_login_session(request, db, write=True)
        throttle(db, "delete-password:" + session.user_id, limit=5)
        user = db.get(User, session.user_id)
        if not verify_password(body.password, user.password_hash):
            raise HTTPException(403, "Неверный пароль")
        request_deletion(db, user, response)
        return {"status": "requested"}

    @router.post("/api/v1/account/delete-request/telegram", status_code=201)
    def delete_telegram_start(request: Request, db=Depends(database)):
        session = get_login_session(request, db, write=True)
        throttle(db, "delete-telegram:" + session.user_id, limit=5)
        linked = db.scalar(select(TelegramIdentity.id).where(TelegramIdentity.user_id == session.user_id).limit(1))
        if linked is None:
            raise HTTPException(409, "Telegram не подключён. Подтвердите удаление паролем")
        row, token, _ = new_login(db, "delete", user_id=session.user_id)
        return {"login_id": row.id, "deep_link": deep_link(settings, token), "expires_at": iso(row.expires_at)}

    @router.post("/api/v1/account/delete-request/telegram/{login_id}")
    def delete_telegram_check(login_id: str, request: Request, response: Response, db=Depends(database)):
        session = get_login_session(request, db, write=True)
        row = db.get(TelegramLogin, login_id) if len(login_id) <= 36 else None
        if row is None or row.purpose != "delete" or row.user_id != session.user_id:
            raise HTTPException(404, "Запрос не найден. Начните заново")
        now = time.time()
        if row.status == "consumed":
            raise HTTPException(409, "Это подтверждение уже использовано. Начните заново")
        if row.expires_at <= now:
            return {"status": "expired"}
        if row.status == "pending":
            return {"status": "pending", "expires_at": iso(row.expires_at)}
        changed = db.execute(
            update(TelegramLogin)
            .where(TelegramLogin.id == row.id, TelegramLogin.status == "confirmed", TelegramLogin.expires_at > now)
            .values(status="consumed", consumed_at=now)
        ).rowcount
        if not changed:
            db.rollback()
            raise HTTPException(409, "Это подтверждение уже использовано. Начните заново")
        request_deletion(db, db.get(User, session.user_id), response)
        return {"status": "requested"}

    @router.post("/api/v1/account/delete-request/cancel")
    def delete_cancel(request: Request, db=Depends(database)):
        session = get_login_session(request, db, write=True)
        db.get(User, session.user_id).deletion_requested_at = None
        db.commit()
        return {"status": "cancelled"}

    return router
