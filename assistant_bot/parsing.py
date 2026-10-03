"""Predictable Russian reminder parsing without sending text to another service."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone as datetime_timezone
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


@dataclass(frozen=True, slots=True)
class ParsedReminder:
    text: str
    due_at: int
    repeat: str | None = None


_HELP = (
    "Напишите, например: «через 10 минут купить хлеб», «завтра в 09:00 зарядка», "
    "«30.09.2026 12:00 встреча» или «каждый день в 09:00 вода»."
)
_RELATIVE = re.compile(
    r"через\s+(\d+)\s+(секунд(?:а|ы|у)?|сек\.?|минут(?:а|ы|у)?|мин\.?|"
    r"час(?:а|ов)?|ч\.?|день|дня|дней|сутки|суток)\s+(.+)",
    re.IGNORECASE | re.DOTALL,
)
_NAMED = re.compile(
    r"(сегодня|завтра|послезавтра)\s+(?:в\s+)?(\d{1,2}):(\d{2})\s+(.+)",
    re.IGNORECASE | re.DOTALL,
)
_ABSOLUTE = re.compile(
    r"(\d{1,2})\.(\d{1,2})\.(\d{4})\s+(?:в\s+)?(\d{1,2}):(\d{2})\s+(.+)",
    re.DOTALL,
)
_REPEAT = re.compile(
    r"(каждый день|ежедневно|каждую неделю|еженедельно)\s+(?:в\s+)?"
    r"(\d{1,2}):(\d{2})\s+(.+)",
    re.IGNORECASE | re.DOTALL,
)


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(
            f"Неизвестный часовой пояс: {name}. Например: Europe/Moscow. "
            "Если пояс верный, установите пакет tzdata."
        ) from exc


def _localize(value: datetime, zone: ZoneInfo) -> datetime:
    """Reject wall-clock times that disappear or occur twice at a DST change."""
    variants = []
    for fold in (0, 1):
        candidate = value.replace(tzinfo=zone, fold=fold)
        roundtrip = candidate.astimezone(datetime_timezone.utc).astimezone(zone)
        if roundtrip.replace(tzinfo=None) == value:
            variants.append(candidate)
    if not variants:
        raise ValueError("Такого местного времени нет из-за перевода часов. Выберите другое время.")
    if len({item.utcoffset() for item in variants}) > 1:
        raise ValueError("Это время повторяется при переводе часов. Выберите другое время.")
    return variants[0]


def _message(text: str) -> str:
    cleaned = text.strip().lstrip("—–-:, ").strip()
    if not cleaned:
        raise ValueError("Добавьте текст: о чём вам напомнить?")
    return cleaned


def _wall_time(day: datetime, hour: str, minute: str, zone: ZoneInfo) -> datetime:
    try:
        value = datetime(day.year, day.month, day.day, int(hour), int(minute))
    except ValueError as exc:
        raise ValueError("Проверьте время: часы от 00 до 23, минуты от 00 до 59.") from exc
    return _localize(value, zone)


def parse_reminder(
    raw: str, timezone: str = "Europe/Moscow", now: datetime | None = None
) -> ParsedReminder:
    """Parse documented forms; refuse guessed dates and reminders in the past.

    ``now`` can be injected for tests. A naive ``now`` means local time in the
    selected zone. Stored timestamps always represent UTC instants.
    """
    zone = _zone(timezone)
    reference = now or datetime.now(datetime_timezone.utc)
    if reference.tzinfo is None:
        reference = _localize(reference, zone)
    reference = reference.astimezone(zone)
    raw = re.sub(r"^напомни(?:\s+мне)?\b[\s,:]*", "", raw.strip(), flags=re.IGNORECASE)
    repeat = None

    if match := _RELATIVE.fullmatch(raw):
        amount = int(match[1])
        unit = match[2].lower()
        if amount <= 0:
            raise ValueError("Интервал должен быть больше нуля.")
        multiplier = 1 if unit.startswith("сек") else 60 if unit.startswith("мин") else 3600 if unit.startswith("ч") else 86400
        if amount * multiplier > 3650 * 86400:
            raise ValueError("Выберите интервал не больше 10 лет.")
        # Relative durations mean elapsed real time even through a DST transition.
        due = reference.astimezone(datetime_timezone.utc) + timedelta(seconds=amount * multiplier)
        message = _message(match[3])
    elif match := _NAMED.fullmatch(raw):
        day = reference + timedelta(days={"сегодня": 0, "завтра": 1, "послезавтра": 2}[match[1].lower()])
        due = _wall_time(day, match[2], match[3], zone)
        message = _message(match[4])
    elif match := _ABSOLUTE.fullmatch(raw):
        try:
            day = datetime(int(match[3]), int(match[2]), int(match[1]))
        except ValueError as exc:
            raise ValueError("Такой даты нет. Используйте формат ДД.ММ.ГГГГ, например 30.09.2026.") from exc
        due = _wall_time(day, match[4], match[5], zone)
        message = _message(match[6])
    elif match := _REPEAT.fullmatch(raw):
        repeat = "weekly" if "недел" in match[1].lower() else "daily"
        due = _wall_time(reference, match[2], match[3], zone)
        if due.timestamp() <= reference.timestamp():
            # The first occurrence is the next chosen clock time, including for
            # weekly reminders. Subsequent occurrences retain that weekday.
            due = _wall_time(reference + timedelta(days=1), match[2], match[3], zone)
        message = _message(match[4])
    else:
        raise ValueError(_HELP + " После времени обязательно добавьте текст напоминания.")

    if due.timestamp() <= reference.timestamp():
        raise ValueError("Это время уже прошло. Укажите будущую дату или «завтра в …».")
    return ParsedReminder(text=message, due_at=int(due.timestamp()), repeat=repeat)


def next_occurrence(due_at: int, repeat: str, timezone: str, now: int) -> int:
    """Return a future recurrence, skipping missed slots and keeping local time.

    A DST gap skips that calendar occurrence; an overlapping time uses its first
    occurrence (fold=0). Initial user input remains strict in ``parse_reminder``.
    """
    if repeat not in {"daily", "weekly"}:
        raise ValueError("Повтор должен быть daily или weekly.")
    zone = _zone(timezone)
    if due_at > now:
        return due_at
    step = 1 if repeat == "daily" else 7
    original = datetime.fromtimestamp(due_at, zone)
    current = datetime.fromtimestamp(now, zone)
    increments = max(1, (current.date() - original.date()).days // step)
    naive_original = original.replace(tzinfo=None)
    while True:
        wall_clock = naive_original + timedelta(days=increments * step)
        candidate = wall_clock.replace(tzinfo=zone, fold=0)
        roundtrip = candidate.astimezone(datetime_timezone.utc).astimezone(zone)
        exists = roundtrip.replace(tzinfo=None) == wall_clock
        if exists and candidate.timestamp() > now:
            return int(candidate.timestamp())
        increments += 1
