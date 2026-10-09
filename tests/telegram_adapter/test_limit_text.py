from datetime import datetime, timedelta, timezone

import pytest

from telegram_adapter.limit_text import format_wait, saved_reply, wait_until

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)
SAVED = {"capture_id": "x", "job_id": None, "status": "saved", "note_url": "https://example.test/"}
NORMAL = "Запись сохранена. Результат и статус обработки доступны в Beresta."
OLD = "Лимит ИИ на сегодня исчерпан. Запись сохранена без ИИ, разобрать её можно завтра."


@pytest.mark.parametrize(("seconds", "text"), [
    (-5, "меньше минуты"),
    (0, "меньше минуты"),
    (59, "меньше минуты"),
    (60, "1 мин"),
    (61, "2 мин"),
    (40 * 60, "40 мин"),
    (3600, "1 ч 0 мин"),
    (5 * 3600 + 12 * 60, "5 ч 12 мин"),
    (5 * 3600 + 11 * 60 + 1, "5 ч 12 мин"),
])
def test_format_wait(seconds, text):
    assert format_wait(seconds) == text


@pytest.mark.parametrize("value", [None, 5, "", "вчера", "2026-10-11T00:00:00"])
def test_wait_until_ignores_missing_or_invalid_values(value):
    assert wait_until(value, NOW) is None


def test_wait_until_counts_across_offsets():
    moscow_midnight = "2026-10-11T00:00:00+03:00"  # 21:00 UTC on the 10th
    assert wait_until(moscow_midnight, NOW) == 9 * 3600


def test_downgrade_reply_names_the_wait():
    reset = (NOW + timedelta(hours=5, minutes=12)).isoformat()
    text = saved_reply({**SAVED, "ai_limit_exceeded": True, "ai_limit_resets_at": reset}, NOW)
    assert text == "Лимит ИИ на сегодня исчерпан. Запись сохранена без ИИ. Лимит обновится через 5 ч 12 мин."


@pytest.mark.parametrize("reset", [None, "мусор", "2026-10-11T00:00:00"])
def test_downgrade_reply_falls_back_without_a_valid_time(reset):
    assert saved_reply({**SAVED, "ai_limit_exceeded": True, "ai_limit_resets_at": reset}, NOW) == OLD
    assert saved_reply({**SAVED, "ai_limit_exceeded": True}, NOW) == OLD


@pytest.mark.parametrize(("left", "tail"), [
    (30, ""), (6, ""), (5, "\nИИ на сегодня: осталось 5."), (1, "\nИИ на сегодня: осталось 1."),
    (0, "\nИИ на сегодня: осталось 0."), (None, ""), (True, ""), ("3", ""),
])
def test_normal_reply_adds_the_counter_only_when_low(left, tail):
    assert saved_reply({**SAVED, "ai_units_remaining": left}, NOW) == NORMAL + tail


def test_reply_without_new_keys_is_unchanged():
    assert saved_reply(SAVED, NOW) == NORMAL
