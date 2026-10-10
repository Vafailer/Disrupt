"""Administrator accounts from the server shell. Every action is written to the audit log.

    python -m app.admin_cli create <username>
    python -m app.admin_cli confirm <username> <code>
    python -m app.admin_cli disable|enable|reset-2fa <username>
    python -m app.admin_cli list
    python -m app.admin_cli mark-test-user|unmark-test-user <username>
"""

import argparse
import getpass
import re
import sys
from datetime import datetime, timezone

from sqlalchemy import delete, select

from app import totp
from app.admin_auth import audit
from app.admin_models import AdminAccount, AdminSession
from app.config import Settings
from app.db import make_database
from app.models import User
from app.security import hash_password

USERNAME = re.compile(r"^[a-z0-9_.-]{3,64}$")
MIN_PASSWORD = 12


def note(db, action, *, target=None, **details):
    audit(db, action, target=target, details={"via": "cli", **details})


def find_admin(db, parser, username):
    admin = db.scalar(select(AdminAccount).where(AdminAccount.username == username))
    if admin is None:
        parser.error("Администратор не найден")
    return admin


def show_secret(admin, secret):
    print("Добавьте аккаунт в приложение-аутентификатор. Эти данные показаны один раз.")
    print("Ссылка otpauth:", totp.otpauth_uri(secret, admin.username))
    print("Секрет base32:", secret)
    print(f"Затем выполните команду confirm {admin.username} с кодом из приложения.")


def create(db, args, parser):
    username = args.username.lower()
    if not USERNAME.fullmatch(username):
        parser.error("Имя от 3 до 64 символов, латиница, цифры, точка, дефис и подчёркивание")
    if db.scalar(select(AdminAccount.id).where(AdminAccount.username == username)):
        parser.error("Такой администратор уже есть")
    password = getpass.getpass("Пароль: ")
    if password != getpass.getpass("Повторите пароль: "):
        parser.error("Пароли не совпали")
    if len(password) < MIN_PASSWORD or len(password) > 128 or password.lower() == username:
        parser.error(f"Пароль должен быть от {MIN_PASSWORD} до 128 символов")
    secret = totp.new_secret()
    admin = AdminAccount(username=username, password_hash=hash_password(password), totp_secret=secret)
    db.add(admin)
    db.flush()
    note(db, "admin_created", target=username)
    db.commit()
    show_secret(admin, secret)


def confirm(db, args, parser):
    admin = find_admin(db, parser, args.username.lower())
    if admin.totp_enabled:
        parser.error("Этот администратор уже подтверждён")
    step = totp.verify(admin.totp_secret, args.code)
    if step is None:
        note(db, "admin_confirm_failed", target=admin.username)
        db.commit()
        parser.error("Код не подошёл. Проверьте время на телефоне и попробуйте с новым кодом")
    admin.totp_enabled, admin.last_totp_step = True, step
    note(db, "admin_confirmed", target=admin.username)
    db.commit()
    print("Готово. Вход в админку включён.")


def disable(db, args, parser):
    admin = find_admin(db, parser, args.username.lower())
    admin.disabled = True
    db.execute(delete(AdminSession).where(AdminSession.admin_id == admin.id))
    note(db, "admin_disabled", target=admin.username)
    db.commit()
    print("Вход отключён, открытые сессии закрыты.")


def enable(db, args, parser):
    admin = find_admin(db, parser, args.username.lower())
    admin.disabled, admin.failed_attempts, admin.locked_until = False, 0, None
    note(db, "admin_enabled", target=admin.username)
    db.commit()
    print("Вход включён.")


def reset_2fa(db, args, parser):
    admin = find_admin(db, parser, args.username.lower())
    secret = totp.new_secret()
    admin.totp_secret, admin.totp_enabled, admin.last_totp_step = secret, False, None
    db.execute(delete(AdminSession).where(AdminSession.admin_id == admin.id))
    note(db, "admin_2fa_reset", target=admin.username)
    db.commit()
    print("Старый код больше не работает, сессии закрыты.")
    show_secret(admin, secret)


def listing(db, args, parser):
    note(db, "admin_list")
    db.commit()
    rows = db.scalars(select(AdminAccount).order_by(AdminAccount.username)).all()
    if not rows:
        print("Администраторов пока нет.")
    for admin in rows:
        state = "отключён" if admin.disabled else "активен" if admin.totp_enabled else "ждёт подтверждения кода"
        last = (datetime.fromtimestamp(admin.last_login_at, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                if admin.last_login_at else "не входил")
        print(f"{admin.username}\t{state}\tпоследний вход {last}")


def mark(flag):
    def run(db, args, parser):
        user = db.scalar(select(User).where(User.username == args.username.lower()))
        if user is None:
            parser.error("Пользователь не найден")
        user.is_test = flag
        note(db, "user_marked_test" if flag else "user_unmarked_test", target=user.username, is_test=flag)
        db.commit()
        print("Аккаунт исключён из метрик." if flag else "Аккаунт снова учитывается в метриках.")
    return run


def build_parser():
    parser = argparse.ArgumentParser(prog="python -m app.admin_cli", description=__doc__.split("\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    for name, handler, arguments in [
        ("create", create, ["username"]), ("confirm", confirm, ["username", "code"]),
        ("disable", disable, ["username"]), ("enable", enable, ["username"]),
        ("reset-2fa", reset_2fa, ["username"]), ("list", listing, []),
        ("mark-test-user", mark(True), ["username"]), ("unmark-test-user", mark(False), ["username"]),
    ]:
        command = commands.add_parser(name)
        for argument in arguments:
            command.add_argument(argument)
        command.set_defaults(handler=handler)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    engine, sessions = make_database(Settings.from_env().database_url)
    try:
        with sessions() as db:
            args.handler(db, args, parser)
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(main())
