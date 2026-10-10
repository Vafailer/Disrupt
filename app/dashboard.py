"""Dashboard numbers without AI. Plain counts over the user's own rows, no note text.

Days are local days in NOTES_LIMIT_TIMEZONE, the same zone the daily AI limit uses. The account has no
time zone of its own yet; reminders carry a zone each, but that is a per-reminder choice, not a profile.
"""

import time
from datetime import UTC, datetime, timedelta
from datetime import time as day_start
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from app.limits import daily_state
from app.models import Capture, Category, Item, Note, ProductEvent, Reminder

STREAK_LOOKBACK_DAYS = 400
UPCOMING_DAYS = 7


def local_day(stamp, zone):
    return datetime.fromtimestamp(stamp, zone).date()


def day_floor(day, zone):
    return datetime.combine(day, day_start.min, tzinfo=zone).timestamp()


def streak_length(days_with_captures, today):
    """Consecutive local days with a capture. A quiet today does not break a streak that ends yesterday."""
    day = today if today in days_with_captures else today - timedelta(days=1)
    length = 0
    while day in days_with_captures:
        length += 1
        day -= timedelta(days=1)
    return length


def build_dashboard(db, settings, user_id, days, now=None):
    now = time.time() if now is None else now
    zone = ZoneInfo(settings.limit_timezone)
    today = local_day(now, zone)
    first_day = today - timedelta(days=days - 1)
    period_start = day_floor(first_day, zone)
    lookback = day_floor(today - timedelta(days=max(days, STREAK_LOOKBACK_DAYS)), zone)

    captures = db.execute(select(
        Capture.created_at, Capture.channel, Capture.input_kind, Capture.processing_mode,
    ).where(Capture.user_id == user_id, Capture.created_at >= lookback)).all()
    in_period = [row for row in captures if row.created_at >= period_start]
    by_channel = {"web": 0, "telegram": 0}
    by_kind = {"text": 0, "audio": 0}
    for row in in_period:
        by_channel[row.channel] = by_channel.get(row.channel, 0) + 1
        by_kind[row.input_kind] = by_kind.get(row.input_kind, 0) + 1
    ai = sum(1 for row in in_period if row.processing_mode == "ai")

    note_stamps = db.scalars(select(Note.created_at).where(
        Note.user_id == user_id, Note.created_at >= period_start,
    )).all()
    per_day = {first_day + timedelta(days=offset): 0 for offset in range(days)}
    for stamp in note_stamps:
        day = local_day(stamp, zone)
        if day in per_day:
            per_day[day] += 1
    total_notes = db.scalar(select(func.count()).select_from(Note).where(Note.user_id == user_id))

    kinds = db.execute(
        select(Note.category_id, Item.kind, Item.status, func.count())
        .select_from(Item)
        .join(Note, (Note.id == Item.note_id) & (Note.user_id == Item.user_id))
        .where(Item.user_id == user_id, Item.kind.in_(["task", "idea"]))
        .group_by(Note.category_id, Item.kind, Item.status)
    ).all()
    task_open = sum(n for _, kind, status, n in kinds if kind == "task" and status == "open")
    task_done = sum(n for _, kind, status, n in kinds if kind == "task" and status == "completed")
    ideas = sum(n for _, kind, _, n in kinds if kind == "idea")
    done_events = db.scalars(select(ProductEvent.operation_id).where(
        ProductEvent.user_id == user_id, ProductEvent.name == "task_completed",
        ProductEvent.occurred_at >= period_start,
    )).all()

    note_counts = dict(db.execute(
        select(Note.category_id, func.count()).where(Note.user_id == user_id).group_by(Note.category_id)
    ).all())
    categories = []
    for category in db.scalars(select(Category).where(Category.user_id == user_id)):
        tasks = [(status, n) for cid, kind, status, n in kinds if cid == category.id and kind == "task"]
        categories.append({
            "category_id": category.id, "name": category.name, "notes": note_counts.get(category.id, 0),
            "tasks_total": sum(n for _, n in tasks),
            "tasks_done": sum(n for status, n in tasks if status == "completed"),
        })
    categories.sort(key=lambda row: (-row["notes"], row["name"].casefold(), row["category_id"]))

    horizon = now + UPCOMING_DAYS * 86400
    pending = (Reminder.user_id == user_id, Reminder.status == "confirmed", Reminder.scheduled_at > now)
    upcoming = db.scalar(
        select(func.count()).select_from(Reminder).where(*pending, Reminder.scheduled_at <= horizon)
    )
    following = db.execute(
        select(Reminder.scheduled_at, Reminder.timezone)
        .where(*pending).order_by(Reminder.scheduled_at, Reminder.id).limit(1)
    ).first()
    usage = daily_state(db, settings, user_id, now)
    total_tasks = task_open + task_done
    return {
        "days": days,
        "timezone": settings.limit_timezone,
        "period": {"from": first_day.isoformat(), "to": today.isoformat()},
        "totals": {"notes": total_notes, "notes_in_period": len(note_stamps)},
        "captures": {
            "total": len(in_period), "by_channel": by_channel, "by_input_kind": by_kind,
            "ai": ai, "manual": len(in_period) - ai,
        },
        "notes_per_day": [{"date": day.isoformat(), "notes": count} for day, count in per_day.items()],
        "tasks": {
            "open": task_open, "completed": task_done,
            "completed_in_period": len({operation.split(":")[0] for operation in done_events}),
            "completion_rate": round(task_done / total_tasks, 2) if total_tasks else None,
        },
        "ideas": ideas,
        "categories": categories,
        "streak_days": streak_length({local_day(row.created_at, zone) for row in captures}, today),
        "reminders": {
            "upcoming_7d": upcoming,
            "next_at": datetime.fromtimestamp(following.scheduled_at, UTC).isoformat() if following else None,
            "next_timezone": following.timezone if following else None,
        },
        "ai": {
            "units_used": usage["daily_units_used"], "units_limit": usage["daily_unit_limit"],
            "units_remaining": usage["daily_units_remaining"], "resets_at": usage["limit_resets_at"],
        },
    }
