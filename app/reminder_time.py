"""Resolve a wall clock time without silently picking a DST fold or shifting a gap."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from fastapi import HTTPException


def resolve_reminder_time(body, *, now=None):
    try:
        wall = datetime.fromisoformat(body.local_time)
        zone = ZoneInfo(body.timezone)
        choices = {}
        now = now or datetime.now(UTC)
        for fold in (0, 1):
            local = wall.replace(tzinfo=zone, fold=fold)
            utc = local.astimezone(UTC)
            if utc.astimezone(zone).replace(tzinfo=None) != wall:
                continue
            offset = local.strftime("%z")
            choices[utc] = {
                "scheduled_at": utc.isoformat(), "local_at": local.isoformat(),
                "utc_offset": offset[:3] + ":" + offset[3:], "is_future": utc > now,
            }
    except (ValueError, OverflowError):
        raise HTTPException(422, "Проверьте дату и время") from None
    if not choices:
        raise HTTPException(422, "Такого местного времени нет из-за перевода часов. Выберите другое")
    if not any(choice["is_future"] for choice in choices.values()):
        raise HTTPException(422, "Выберите точное время в будущем")
    return {
        "local_time": body.local_time, "timezone": body.timezone, "ambiguous": len(choices) > 1,
        "choices": [choices[key] for key in sorted(choices)],
    }
