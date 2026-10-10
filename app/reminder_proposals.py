"""Deterministic Russian deadline parser. It turns the quoted `due_text` of a task into a proposed
reminder time. No AI is involved, nothing is stored and nothing is sent: the user confirms the time
through the existing reminder form.

Rules in short.
- A date without time means 09:00. Dayparts: утром 09:00, днём 13:00, вечером 19:00.
- An explicit day (сегодня, завтра, weekday, date) that is already past gives None.
- A bare time or daypart ("в 10", "вечером") that is already past rolls to tomorrow.
- A weekday means the nearest such day after today, never today itself.
- A date without year that passed more than 60 days ago means next year, a recent one gives None.
"""

import calendar
import re
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

DEFAULT_HOUR = 9
DAYPART_HOUR = {"утром": 9, "утра": 9, "утро": 9, "днем": 13, "дня": 13, "вечером": 19, "вечера": 19, "вечер": 19}
MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}
MONTH_NAMES = {number: name for name, number in MONTHS.items()}
WEEKDAYS = {}
for _index, _forms in enumerate([
    "понедельник понедельнику понедельника пн", "вторник вторнику вторника вт",
    "среда среду среде среды ср", "четверг четвергу четверга чт", "пятница пятницу пятнице пятницы пт",
    "суббота субботу субботе субботы сб", "воскресенье воскресенью воскресенья вс",
]):
    for _form in _forms.split():
        WEEKDAYS[_form] = _index
WEEKDAY_LABEL = [
    "в понедельник", "во вторник", "в среду", "в четверг", "в пятницу", "в субботу", "в воскресенье",
]
NUMBER_WORDS = {
    "один": 1, "одну": 1, "два": 2, "две": 2, "три": 3, "четыре": 4, "пять": 5, "шесть": 6, "семь": 7,
    "восемь": 8, "девять": 9, "десять": 10, "одиннадцать": 11, "двенадцать": 12, "пятнадцать": 15,
    "двадцать": 20, "тридцать": 30,
}
MAX_AHEAD = timedelta(days=366 * 2)

RELATIVE = re.compile(
    r"\bчерез\s+(?:(?P<n>\d{1,3})\s*)?"
    r"(?P<unit>полчаса|пару\s+[а-я]+|мин[а-я]*|час[а-я]*|ден[а-я]*|дн[а-я]*|сутки|суток|недел[а-я]*|месяц[а-я]*)\b"
)
ISO_DATE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
DOT_DATE = re.compile(r"\b(\d{1,2})\.(\d{1,2})(?:\.(\d{4}|\d{2}))?(?![\d:])")
SLASH_DATE = re.compile(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{4}|\d{2}))?\b")
NAMED_DATE = re.compile(r"\b(\d{1,2})\s+(" + "|".join(MONTHS) + r")(?:\s+(\d{4}))?\b")
MONTH_DAY = re.compile(r"\b(\d{1,2})(?:\s*-?\s*го)?\s+числа\b")
CLOCK = re.compile(r"\b(\d{1,2}):(\d{2})\b")
HOUR = re.compile(r"\b(?:в|к|до|около|ровно)\s+(\d{1,2})(?:\s*(?:часов|часа|час|ч))?\b(?!\s*\d)")
DAYPART = re.compile(r"\b(утром|утра|утро|днем|дня|вечером|вечера|вечер|ночью|ночи|ночь)\b")
NOON_MIDNIGHT = re.compile(r"\b(?:в|к)\s+(полдень|полночь)\b")
DAY_WORD = re.compile(r"\b(послезавтра|завтра|сегодня|" + "|".join(sorted(WEEKDAYS, key=len, reverse=True)) + r")\b")


def normalize(value):
    text = value.lower().replace("ё", "е")
    text = re.sub(r"[^a-zа-я0-9:./\- ]+", " ", text)
    text = re.sub(r"(?<!\d)[:./-]|[:./-](?!\d)", " ", text)
    words = [str(NUMBER_WORDS.get(word, word)) for word in text.split()]
    return " ".join(words)


def add_months(day, months):
    index = day.month - 1 + months
    year, month = day.year + index // 12, index % 12 + 1
    return day.replace(year=year, month=month, day=min(day.day, calendar.monthrange(year, month)[1]))


def safe_date(year, month, day):
    try:
        return datetime(year, month, day).date()
    except ValueError:
        return None


def full_year(value):
    if value is None:
        return None
    year = int(value)
    return year + 2000 if year < 100 else year


def clock_time(hour, minute, daypart):
    """Apply a daypart word to a spoken hour. Returns (hour, minute) or None when out of range."""
    if daypart in {"вечером", "вечера", "вечер", "днем", "дня"} and 1 <= hour < 12:
        hour += 12
    elif daypart in {"ночью", "ночи", "ночь"}:
        if hour == 12:
            hour = 0
        elif 6 <= hour < 12:
            hour += 12
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour, minute


def parse_relative(text, now):
    match = RELATIVE.search(text)
    if not match:
        return None, text
    unit, count = match["unit"], int(match["n"]) if match["n"] else 1
    if unit == "полчаса":
        count, unit = 30, "мин"
    elif unit.startswith("пару"):
        count, unit = 2, unit.split()[1]
    if not 1 <= count <= 400:
        return None, text
    rest = (text[:match.start()] + " " + text[match.end():]).strip()
    if unit.startswith("мин"):
        return now + timedelta(minutes=count), rest
    if unit.startswith("час"):
        return now + timedelta(hours=count), rest
    if unit.startswith(("ден", "дн", "сут")):
        day = now.date() + timedelta(days=count)
    elif unit.startswith("недел"):
        day = now.date() + timedelta(weeks=count)
    elif unit.startswith("месяц"):
        day = add_months(now.date(), count)
    else:
        return None, text
    return day, rest


def find_date(text, today):
    """Return (date or None, found, rest). found is True when a date phrase was present."""
    for pattern, order in ((ISO_DATE, "ymd"), (NAMED_DATE, "named"), (DOT_DATE, "dmy"), (SLASH_DATE, "dmy")):
        match = pattern.search(text)
        if not match:
            continue
        rest = (text[:match.start()] + " " + text[match.end():]).strip()
        if order == "ymd":
            return safe_date(int(match[1]), int(match[2]), int(match[3])), True, rest
        if order == "named":
            day, month, year = int(match[1]), MONTHS[match[2]], full_year(match[3])
        else:
            day, month, year = int(match[1]), int(match[2]), full_year(match[3])
        if year is None:
            date = safe_date(today.year, month, day)
            if date is not None and date < today and (today - date).days > 60:
                date = safe_date(today.year + 1, month, day)
            return date, True, rest
        return safe_date(year, month, day), True, rest
    match = MONTH_DAY.search(text)
    if match:
        rest = (text[:match.start()] + " " + text[match.end():]).strip()
        return int(match[1]), "month_day", rest
    return None, False, text


def find_time(text):
    """Return (hour, minute, found, daypart, is_midnight). found with hour None means an unusable time."""
    daypart_match = DAYPART.search(text)
    daypart = daypart_match[1] if daypart_match else None
    special = NOON_MIDNIGHT.search(text)
    if special:
        return (12 if special[1] == "полдень" else 0), 0, True, daypart, special[1] == "полночь"
    clock = CLOCK.search(text)
    if clock:
        hour, minute = clock_time(int(clock[1]), int(clock[2]), daypart) or (None, None)
        return hour, minute, True, daypart, False
    spoken = HOUR.search(text)
    if spoken:
        hour, minute = clock_time(int(spoken[1]), 0, daypart) or (None, None)
        return hour, minute, True, daypart, False
    if daypart in DAYPART_HOUR:
        return DAYPART_HOUR[daypart], 0, True, daypart, False
    if daypart:
        return None, None, True, daypart, False  # "ночью" without an hour is too vague
    return None, None, False, daypart, False


def month_day_candidate(number, hour, minute, now):
    first = now.date().replace(day=1)
    for offset in range(13):
        month = add_months(first, offset)
        date = safe_date(month.year, month.month, number)
        if date is not None and datetime.combine(date, time(hour, minute)) > now:
            return date
    return None


def parse_due_text(due_text, now):
    """Parse a quoted deadline against a naive local wall clock `now`. Returns a naive datetime or None."""
    if not isinstance(due_text, str) or not due_text.strip() or "\x00" in due_text:
        return None
    text = normalize(due_text)
    today = now.date()
    base, rest = parse_relative(text, now)
    if isinstance(base, datetime):
        result = base.replace(second=0, microsecond=0)
        return result if now < result <= now + MAX_AHEAD else None
    if base is not None:
        hour, minute, found, _, _ = find_time(rest)
        if found and hour is None:
            return None
        result = datetime.combine(base, time(hour if found else DEFAULT_HOUR, minute if found else 0))
        return result if now < result <= now + MAX_AHEAD else None
    date_value, found_date, rest = find_date(text, today)
    if found_date and date_value is None:
        return None
    hour, minute, found_time, _, _ = find_time(rest)
    if found_time and hour is None:
        return None
    if not found_time:
        hour, minute = DEFAULT_HOUR, 0
    day, explicit = None, True
    if isinstance(date_value, int):
        day = month_day_candidate(date_value, hour, minute, now)
        if day is None:
            return None
    elif date_value is not None:
        day = date_value
    else:
        word = DAY_WORD.search(rest)
        if word is None:
            if not found_time:
                return None
            day, explicit = today, False
        elif word[1] == "сегодня":
            day = today
        elif word[1] == "завтра":
            day = today + timedelta(days=1)
        elif word[1] == "послезавтра":
            day = today + timedelta(days=2)
        else:
            day = today + timedelta(days=(WEEKDAYS[word[1]] - today.weekday()) % 7 or 7)
    result = datetime.combine(day, time(hour, minute))
    if result <= now:
        if explicit:
            return None
        result += timedelta(days=1)
    return result if result <= now + MAX_AHEAD else None


def label_for(result, now):
    days = (result.date() - now.date()).days
    if days == 0:
        day = "сегодня"
    elif days == 1:
        day = "завтра"
    elif days == 2:
        day = "послезавтра"
    elif 3 <= days <= 6:
        day = WEEKDAY_LABEL[result.weekday()]
    else:
        day = f"{result.day} {MONTH_NAMES[result.month]}" + (f" {result.year}" if result.year != now.year else "")
    return f"{day} в {result:%H:%M}"


def propose_reminder(due_text, now, timezone):
    """Proposal for the confirmation form or None. `now` is an aware datetime, `timezone` an IANA name."""
    zone = ZoneInfo(timezone)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    local_now = now.astimezone(zone)
    result = parse_due_text(due_text, local_now.replace(tzinfo=None))
    if result is None:
        return None
    instant = result.replace(tzinfo=zone).astimezone(UTC)
    # A wall time inside a DST gap does not exist, so it is not proposed.
    if instant.astimezone(zone).replace(tzinfo=None) != result or instant <= now:
        return None
    label = label_for(result, local_now.replace(tzinfo=None))
    return {"local_time": f"{result:%Y-%m-%dT%H:%M}:00", "timezone": timezone, "label": label}
