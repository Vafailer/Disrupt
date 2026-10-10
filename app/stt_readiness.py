"""Explicit one-shot owner probe. Original, key and transcript never enter stdout."""
import argparse
import io
import os
import time

from sqlalchemy import select

from app import crypto
from app.audio_storage import AudioStorage
from app.config import Settings, read_secret_file
from app.db import make_database
from app.models import Capture, ProviderUsage, User, new_id
from app.providers import MockProvider, ProviderError
from app.speech import CloudRuSpeechProvider, normalized_wav
from app.worker import Worker


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture-id", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--allow-live-probe", action="store_true", required=True)
    args = parser.parse_args()
    settings = Settings.from_env()
    crypto.configure_from_settings(settings)
    if settings.provider != "cloudru" or not settings.allow_live_requests:
        raise SystemExit("Live owner permission required")
    engine, sessions = make_database(settings.database_url)
    request_id = "stt-readiness:" + args.capture_id + ":wav-ru-v1"
    with sessions() as db:
        capture = db.get(Capture, args.capture_id)
        if capture is None or capture.input_kind != "audio":
            raise SystemExit("Owner audio capture required")
        if db.scalar(select(ProviderUsage.id).where(ProviderUsage.request_id == request_id)):
            raise SystemExit("Probe already recorded; automatic repeat refused")
        user_id, key, seconds, channel = capture.user_id, capture.audio_key, capture.audio_seconds, capture.channel
        is_test = db.get(User, user_id).is_test
    with AudioStorage(settings.audio_storage_path, read_only=True).open_original(key) as source:
        audio = normalized_wav(source.read(10 * 1024 * 1024 + 1), settings.audio_ffmpeg_path)
    receipt_id = new_id()
    with sessions() as db:
        db.add(ProviderUsage(id=receipt_id, user_id=user_id, operation_id=request_id, request_id=request_id,
                            is_test=is_test, channel=channel, kind="stt", model=args.model,
                            status="unknown", stt_seconds=seconds))
        # Receipt and reservation commit together before dispatch. A crash cannot cause a replay.
        Worker(sessions, settings, provider=MockProvider()).reserve_budget(db, user_id)
    provider = CloudRuSpeechProvider(read_secret_file(os.environ["NOTES_CLOUDRU_API_KEY_FILE"]),
                                    args.model, base_url=settings.cloudru_base_url)
    started, status = time.monotonic(), "unknown"
    try:
        text = provider.transcribe(io.BytesIO(audio), media_type="audio/wav")
        status = "succeeded"
        print("STT succeeded; transcript_characters", len(text))
    except ProviderError as exc:
        status = "unknown" if exc.code.endswith("_unknown") else "failed"
        print("STT error", exc.code, "http_status", exc.http_status,
              "stage", getattr(exc, "diagnostic_stage", "unclassified"))
    finally:
        latency_ms = round((time.monotonic() - started) * 1000)
        with sessions.begin() as db:
            receipt = db.get(ProviderUsage, receipt_id)
            receipt.status, receipt.latency_ms = status, latency_ms
        engine.dispose()
        print("latency_ms", latency_ms)
    return 0 if status == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
