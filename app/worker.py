"""Durable at-most-once dispatch. Ambiguous failures never trigger an automatic retry."""

import time

from pydantic import ValidationError
from sqlalchemy import select, update

from app.analytics import record_event
from app.config import Settings
from app.db import make_database
from app.error_logging import log_error
from app.models import Capture, Job, Note, ProviderBudget, User, new_id
from app.providers import ProviderError, make_provider, validate_result
from app.services import save_revision


class Worker:
    def __init__(self, sessions, settings: Settings, provider=None):
        self.sessions = sessions
        self.settings = settings
        self.provider = provider if provider is not None else make_provider(settings)

    def claim(self):
        now = time.time()
        with self.sessions() as db:
            expired = db.execute(
                update(Job)
                .where(Job.status == "running", Job.lease_until < now)
                .values(status="failed", error_code="execution_unknown", finished_at=now)
                .returning(Job.id, Job.user_id, Job.capture_id)
            ).all()
            if expired:
                log_error(
                    "job_lease_expired",
                    "execution_unknown",
                    log_file=self.settings.error_log_file,
                )
                for expired_id, user_id, capture_id in expired:
                    capture = db.get(Capture, capture_id)
                    record_event(db, user_id, "processing_failed", expired_id, capture.channel)
            job_id = db.scalar(
                select(Job.id)
                .where(Job.status == "queued", Job.provider == self.settings.provider)
                .order_by(Job.created_at, Job.id)
                .limit(1)
            )
            if job_id is None:
                db.commit()
                return None
            claimed = db.execute(
                update(Job)
                .where(Job.id == job_id, Job.status == "queued")
                .values(status="running", lease_until=now + self.settings.lease_seconds)
            ).rowcount
            db.commit()
            return job_id if claimed else None

    def reserve_budget(self, db, user_id):
        global_ok = db.execute(
            update(ProviderBudget)
            .where(
                ProviderBudget.id == "cloudru",
                ProviderBudget.reserved_calls < self.settings.live_call_limit,
            )
            .values(reserved_calls=ProviderBudget.reserved_calls + 1)
        ).rowcount
        user_ok = db.execute(
            update(User)
            .where(
                User.id == user_id,
                User.live_calls < self.settings.live_user_call_limit,
            )
            .values(live_calls=User.live_calls + 1)
        ).rowcount
        if not (global_ok and user_ok):
            db.rollback()
            raise ProviderError("budget_exhausted")
        db.commit()  # Reservation survives crashes; an uncertain call still consumes a slot.

    def fail(self, job_id, code, *, exception=None, http_status=None):
        log_error(
            "job_failed",
            code,
            log_file=self.settings.error_log_file,
            job_id=job_id,
            http_status=http_status,
            exception=exception,
        )
        with self.sessions() as db:
            changed = db.execute(
                update(Job)
                .where(Job.id == job_id, Job.status == "running")
                .values(status="failed", error_code=code, finished_at=time.time())
            ).rowcount
            if changed:
                job = db.get(Job, job_id)
                capture = db.get(Capture, job.capture_id)
                record_event(db, job.user_id, "processing_failed", job_id, capture.channel)
            db.commit()

    def run_once(self):
        job_id = self.claim()
        if job_id is None:
            return False
        try:
            with self.sessions() as db:
                job = db.get(Job, job_id)
                original = db.get(Capture, job.capture_id).original_text
                capture_id, user_id = job.capture_id, job.user_id
                if self.settings.provider == "cloudru":
                    self.reserve_budget(db, user_id)
            result = validate_result(self.provider.structure(original), original)
            now = time.time()
            with self.sessions() as db:
                completed = db.execute(
                    update(Job)
                    .where(Job.id == job_id, Job.status == "running", Job.lease_until >= now)
                    .values(status="succeeded", finished_at=now)
                ).rowcount
                if not completed:
                    db.rollback()
                    self.fail(job_id, "execution_unknown")
                    return True
                note = Note(
                    id=new_id(),
                    capture_id=capture_id,
                    user_id=user_id,
                    title=result.title,
                    markdown=result.markdown,
                    version=1,
                    provider=self.settings.provider,
                    conclusions=[
                        {"id": new_id(), **c.model_dump(), "status": "proposed"} for c in result.conclusions
                    ],
                )
                db.add(note)
                db.flush()
                save_revision(db, note)
                capture = db.get(Capture, capture_id)
                record_event(db, user_id, "processing_completed", job_id, capture.channel)
                db.commit()
        except ProviderError as exc:
            self.fail(job_id, exc.code, exception=exc, http_status=exc.http_status)
        except ValidationError as exc:
            self.fail(job_id, "provider_invalid_response", exception=exc)
        except Exception as exc:
            self.fail(job_id, "internal_error", exception=exc)
        return True


def main():
    settings = Settings.from_env()
    engine, sessions = make_database(settings.database_url)
    worker = Worker(sessions, settings)
    try:
        while True:
            if not worker.run_once():
                time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
