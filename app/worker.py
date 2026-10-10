"""Durable at-most-once dispatch. Ambiguous failures never trigger an automatic retry."""

import signal
import threading
import time

from pydantic import ValidationError
from sqlalchemy import select, update

from app.analytics import record_event
from app.assistant import run_assistant_once
from app.audio_contracts import AUDIO_MEDIA_TYPES, AudioReader, SpeechProvider
from app.audio_storage import AudioStorage
from app.config import Settings
from app.db import make_database
from app.error_logging import log_error
from app.models import (
    Capture,
    Category,
    Item,
    Job,
    Note,
    Outbox,
    ProviderBudget,
    TranscriptRevision,
    User,
    new_id,
)
from app.providers import ProviderError, make_provider, validate_result
from app.services import ensure_category, lock_account, save_revision
from app.speech import CloudRuSpeechProvider, make_speech_provider
from app.usage import UsageRecorder


class Worker:
    def __init__(
        self, sessions, settings: Settings, provider=None, *,
        audio_storage: AudioReader | None = None, speech_provider: SpeechProvider | None = None,
    ):
        self.sessions = sessions
        self.settings = settings
        self.provider = provider if provider is not None else make_provider(settings)
        self.audio_storage = audio_storage
        self.speech_provider = speech_provider
        self.usage = UsageRecorder(sessions, settings)

    def original_for_job(self, job_id):
        with self.sessions() as db:
            job = db.get(Job, job_id)
            if job.status != "running" or job.lease_until < time.time():
                raise ProviderError("execution_unknown")
            capture = db.get(Capture, job.capture_id)
            if capture.input_kind == "text":
                return capture.original_text
            if capture.input_kind != "audio":
                raise ProviderError("input_kind_invalid")
            if capture.transcript is not None:
                return self.validate_transcript(capture.transcript)
            key, media_type = capture.audio_key, capture.audio_media_type
        if self.speech_provider is None:
            raise ProviderError("stt_not_configured")
        if self.audio_storage is None:
            raise ProviderError("audio_storage_unavailable")
        if not key or media_type not in AUDIO_MEDIA_TYPES:
            raise ProviderError("audio_invalid")
        if isinstance(self.speech_provider, CloudRuSpeechProvider):
            if self.settings.provider != "cloudru" or not self.settings.allow_live_requests:
                raise ProviderError("stt_not_configured")
            with self.sessions() as db:
                # STT and LLM each consume a durable slot before their own dispatch.
                self.reserve_budget(db, db.get(Job, job_id).user_id)
        try:
            with self.audio_storage.open_original(key) as source:
                transcript = self.usage.call(
                    job_id, "stt", self.speech_provider,
                    lambda _: self.validate_transcript(self.speech_provider.transcribe(source, media_type=media_type)),
                )
        except OSError:
            raise ProviderError("audio_storage_unavailable") from None
        transcript = self.validate_transcript(transcript)
        with self.sessions() as db:
            # Serialize with lease expiration. A late STT result cannot start the LLM.
            active = db.execute(
                update(Job)
                .where(Job.id == job_id, Job.status == "running", Job.lease_until >= time.time())
                .values(status="running", lease_until=time.time() + self.settings.lease_seconds)
            ).rowcount
            if not active:
                raise ProviderError("execution_unknown")
            capture = db.get(Capture, job.capture_id)
            if capture.transcript is None:
                capture.transcript = transcript
                capture.original_text = transcript
                db.add(TranscriptRevision(capture_id=capture.id, version=capture.transcript_version,
                                          text=transcript, origin="stt", created_at=time.time()))
            else:
                transcript = self.validate_transcript(capture.transcript)
            # Keep the transcript even if the next stage fails or the process stops.
            db.commit()
        return transcript

    @staticmethod
    def validate_transcript(text):
        if not isinstance(text, str) or not text.strip() or "\x00" in text or len(text) > 12000:
            raise ProviderError("stt_invalid_response")
        return text

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
                    db.execute(update(Outbox).where(
                        Outbox.job_id == expired_id, Outbox.status.in_({"pending", "leased", "retryable"}),
                    ).values(status="cancelled", error_code="processing_failed"))
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
                db.execute(update(Outbox).where(
                    Outbox.job_id == job_id, Outbox.status.in_({"pending", "leased", "retryable"}),
                ).values(status="cancelled", error_code="processing_failed"))
            db.commit()

    def run_once(self):
        job_id = self.claim()
        if job_id is None:
            return run_assistant_once(self)  # Capture jobs always go first.
        try:
            original = self.original_for_job(job_id)
            with self.sessions() as db:
                job = db.get(Job, job_id)
                capture_id, user_id = job.capture_id, job.user_id
                category_context = {
                    category.name: category.id
                    for category in db.scalars(
                        select(Category).where(Category.user_id == user_id).order_by(Category.id).limit(100)
                    )
                }
                if self.settings.provider == "cloudru":
                    self.reserve_budget(db, user_id)
            def structure(on_usage):
                measured = getattr(self.provider, "structure_with_usage", None)
                result = (
                    measured(original, categories=tuple(category_context), on_usage=on_usage)
                    if measured else self.provider.structure(original, categories=tuple(category_context))
                )
                return validate_result(result, original)

            result = self.usage.call(job_id, "llm", self.provider, structure)
            now = time.time()
            with self.sessions() as db:
                lock_account(db, user_id)
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
                if result.category_name:
                    existing_id = next(
                        (
                            category_id
                            for name, category_id in category_context.items()
                            if name.casefold() == result.category_name.casefold()
                        ),
                        None,
                    )
                    category = (
                        db.scalar(
                            select(Category).where(Category.id == existing_id, Category.user_id == user_id)
                        )
                        if existing_id
                        else ensure_category(db, user_id, result.category_name, best_effort=True)
                    )
                    note.category_id = category.id if category else None
                db.add(note)
                db.flush()
                for position, item in enumerate(result.items):
                    db.add(
                        Item(
                            user_id=user_id,
                            note_id=note.id,
                            kind=item.kind,
                            text=item.text,
                            source_quote=item.source_quote,
                            due_text=item.due_text,
                            due_at=None,
                            status="open",
                            version=1,
                            position=position,
                        )
                    )
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

    def budget_available(self):
        """Keep queued originals intact when the deployment's total call cap is spent."""
        if self.settings.provider != "cloudru":
            return True
        with self.sessions() as db:
            budget = db.get(ProviderBudget, "cloudru")
            return budget is not None and budget.reserved_calls < self.settings.live_call_limit


def main():
    settings = Settings.from_env()
    engine, sessions = make_database(settings.database_url)
    stopping = threading.Event()
    def request_stop(signum, frame):
        stopping.set()
    previous_handlers = {sig: signal.signal(sig, request_stop) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        worker = Worker(sessions, settings, audio_storage=AudioStorage(settings.audio_storage_path, read_only=True),
                        speech_provider=make_speech_provider(settings))
        while not stopping.is_set():
            if not worker.budget_available():
                time.sleep(1)
                continue
            if not worker.run_once():
                time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)
        engine.dispose()


if __name__ == "__main__":
    main()
