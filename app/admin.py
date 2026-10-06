"""Explicit server-side role assignment; there is no role field in registration."""

import argparse

from app.config import Settings
from app.db import make_database
from app.models import User


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("user_id")
    parser.add_argument("--role", choices=["user", "admin"], default="admin")
    args = parser.parse_args()
    engine, sessions = make_database(Settings.from_env().database_url)
    try:
        with sessions() as db:
            user = db.get(User, args.user_id)
            if user is None:
                parser.error("User not found")
            user.role = args.role
            db.commit()
            print(f"Role assigned: {user.id} -> {user.role}")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
