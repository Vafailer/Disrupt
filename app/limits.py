"""Daily AI allowance in units. Computed from stored captures, so no counter can drift."""

import math
import time
from datetime import datetime, timedelta
from datetime import time as day_start
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from app.models import Capture

# Stored in the capture_saved event, so a replay of the same request knows it was saved without AI.
LIMIT_OUTCOME = "ai_limit"


def capture_cost(settings, input_kind, audio_seconds=None):
    """Text costs a flat amount. Audio costs a base plus one step for every full minute."""
    if input_kind == "audio":
        minutes = math.floor(max(0.0, audio_seconds or 0.0) / 60)
        return settings.audio_unit_base + settings.audio_unit_per_minute * minutes
    return settings.text_unit_cost


def day_window(settings, now=None):
    """Start of the current and of the next calendar day in the limit time zone."""
    zone = ZoneInfo(settings.limit_timezone)
    today = datetime.fromtimestamp(time.time() if now is None else now, zone).date()
    return (
        datetime.combine(today, day_start.min, tzinfo=zone),
        datetime.combine(today + timedelta(days=1), day_start.min, tzinfo=zone),
    )


def units_used(db, settings, user_id, now=None):
    """Units of the user's AI captures created today. Manual captures are free."""
    start, end = day_window(settings, now)
    today = (
        Capture.user_id == user_id, Capture.processing_mode == "ai",
        Capture.created_at >= start.timestamp(), Capture.created_at < end.timestamp(),
    )
    texts = db.scalar(select(func.count()).select_from(Capture).where(*today, Capture.input_kind != "audio"))
    audio = db.scalars(select(Capture.audio_seconds).where(*today, Capture.input_kind == "audio")).all()
    return texts * capture_cost(settings, "text") + sum(capture_cost(settings, "audio", s) for s in audio)


def within_daily_limit(db, settings, user_id, cost, now=None):
    if settings.daily_unit_limit == 0:
        return True
    return units_used(db, settings, user_id, now) + cost <= settings.daily_unit_limit


def daily_state(db, settings, user_id, now=None):
    """Keys of GET /api/v1/provider/usage that belong to the daily limit."""
    used = units_used(db, settings, user_id, now)
    limit = settings.daily_unit_limit
    return {
        "daily_unit_limit": limit,
        "daily_units_used": used,
        "daily_units_remaining": max(0, limit - used) if limit else None,
        "text_unit_cost": settings.text_unit_cost,
        "audio_unit_base": settings.audio_unit_base,
        "audio_unit_per_minute": settings.audio_unit_per_minute,
        "limit_resets_at": day_window(settings, now)[1].isoformat(),
    }


def telegram_state(db, settings, user_id, now=None):
    """Remaining units and next reset for the bot reply. Both are null when the daily limit is off."""
    if settings.daily_unit_limit == 0:
        return {"ai_units_remaining": None, "ai_limit_resets_at": None}
    daily = daily_state(db, settings, user_id, now)
    return {"ai_units_remaining": daily["daily_units_remaining"], "ai_limit_resets_at": daily["limit_resets_at"]}
