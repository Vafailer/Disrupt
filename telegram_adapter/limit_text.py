"""Plain texts about the daily AI limit. Pure functions, no network."""

from datetime import datetime, timezone

LOW_UNITS = 5


def format_wait(seconds):
    """'5 ч 12 мин', '40 мин' or 'меньше минуты'. Minutes round up so the user never waits longer than said."""
    minutes = -(-max(0, int(seconds)) // 60)
    if minutes <= 0 or seconds < 60:
        return "меньше минуты"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} ч {minutes} мин" if hours else f"{minutes} мин"


def wait_until(resets_at, now=None):
    """Seconds until an ISO time with offset, or None when the value is missing or invalid."""
    if not isinstance(resets_at, str):
        return None
    try:
        moment = datetime.fromisoformat(resets_at)
    except ValueError:
        return None
    if moment.tzinfo is None:
        return None
    return (moment - (now or datetime.now(timezone.utc))).total_seconds()


def saved_reply(result, now=None):
    """Reply text for a saved capture, from the core response."""
    if result.get("ai_limit_exceeded") is True:
        seconds = wait_until(result.get("ai_limit_resets_at"), now)
        if seconds is None:
            return "Лимит ИИ на сегодня исчерпан. Запись сохранена без ИИ, разобрать её можно завтра."
        return (
            "Лимит ИИ на сегодня исчерпан. Запись сохранена без ИИ. "
            f"Лимит обновится через {format_wait(seconds)}."
        )
    text = "Запись сохранена. Результат и статус обработки доступны в Beresta."
    left = result.get("ai_units_remaining")
    if isinstance(left, int) and not isinstance(left, bool) and 0 <= left <= LOW_UNITS:
        text += f"\nИИ на сегодня: осталось {left}."
    return text
