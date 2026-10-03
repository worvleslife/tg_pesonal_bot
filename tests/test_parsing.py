import unittest
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from assistant_bot.parsing import next_occurrence, parse_reminder


MOSCOW = ZoneInfo("Europe/Moscow")


class ReminderParsingTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 29, 18, 0, tzinfo=MOSCOW)

    def parse(self, text, now=None, zone="Europe/Moscow"):
        return parse_reminder(text, timezone=zone, now=now or self.now)

    def local_time(self, parsed):
        return datetime.fromtimestamp(parsed.due_at, MOSCOW)

    def test_relative_cyrillic_prefix_and_original_message(self):
        result = self.parse("  Напомни мне через 10 минут Купить хлеб  ")
        self.assertEqual(result.text, "Купить хлеб")
        self.assertEqual(result.due_at, int(self.now.timestamp()) + 600)
        self.assertIsNone(result.repeat)

    def test_relative_hours_and_crossing_year(self):
        now = datetime(2026, 12, 31, 23, 10, tzinfo=MOSCOW)
        result = self.parse("через 2 часа Новый год", now)
        self.assertEqual(self.local_time(result), datetime(2027, 1, 1, 1, 10, tzinfo=MOSCOW))

    def test_today_and_tomorrow(self):
        self.assertEqual(self.local_time(self.parse("сегодня в 18:30 чай")), datetime(2026, 9, 29, 18, 30, tzinfo=MOSCOW))
        result = self.parse("завтра в 09:00 зарядка")
        self.assertEqual(self.local_time(result), datetime(2026, 9, 30, 9, tzinfo=MOSCOW))

    def test_absolute_date(self):
        result = self.parse("30.09.2026 12:00 встреча")
        self.assertEqual(self.local_time(result), datetime(2026, 9, 30, 12, tzinfo=MOSCOW))

    def test_naive_reference_is_local(self):
        result = self.parse("через 1 час чай", datetime(2026, 9, 29, 18))
        self.assertEqual(self.local_time(result).hour, 19)

    def test_reference_in_other_zone(self):
        result = self.parse("сегодня в 19:00 чай", datetime(2026, 9, 29, 15, tzinfo=timezone.utc))
        self.assertEqual(self.local_time(result).hour, 19)

    def test_repeat_first_occurrence_is_next_clock_time(self):
        for raw, expected in [("каждый день в 09:00 вода", "daily"), ("каждую неделю в 09:00 обзор", "weekly")]:
            with self.subTest(raw=raw):
                result = self.parse(raw)
                self.assertEqual(result.repeat, expected)
                self.assertEqual(self.local_time(result), datetime(2026, 9, 30, 9, tzinfo=MOSCOW))
        self.assertEqual(self.local_time(self.parse("каждый день в 20:00 вода")).day, 29)

    def test_recurrence_skips_missed_dates(self):
        due = int(datetime(2026, 9, 27, 9, tzinfo=MOSCOW).timestamp())
        result = next_occurrence(due, "daily", "Europe/Moscow", int(self.now.timestamp()))
        self.assertEqual(datetime.fromtimestamp(result, MOSCOW), datetime(2026, 9, 30, 9, tzinfo=MOSCOW))
        result = next_occurrence(due, "weekly", "Europe/Moscow", int(self.now.timestamp()))
        self.assertEqual(datetime.fromtimestamp(result, MOSCOW), datetime(2026, 10, 4, 9, tzinfo=MOSCOW))

    def test_equal_timestamp_advances_and_future_stays(self):
        timestamp = int(self.now.timestamp())
        self.assertEqual(next_occurrence(timestamp, "daily", "Europe/Moscow", timestamp), timestamp + 86400)
        self.assertEqual(next_occurrence(timestamp + 10, "daily", "Europe/Moscow", timestamp), timestamp + 10)

    def test_recurrence_preserves_wall_clock_across_dst(self):
        berlin = ZoneInfo("Europe/Berlin")
        original = datetime(2026, 3, 28, 9, tzinfo=berlin)
        timestamp = int(original.timestamp())
        result = next_occurrence(timestamp, "daily", "Europe/Berlin", timestamp)
        self.assertEqual(datetime.fromtimestamp(result, berlin), datetime(2026, 3, 29, 9, tzinfo=berlin))
        self.assertEqual(result - timestamp, 23 * 3600)

    def test_recurrence_skips_nonexistent_local_time(self):
        berlin = ZoneInfo("Europe/Berlin")
        cases = [
            ("daily", datetime(2026, 3, 28, 2, 30, tzinfo=berlin), datetime(2026, 3, 30, 2, 30, tzinfo=berlin)),
            ("weekly", datetime(2026, 3, 22, 2, 30, tzinfo=berlin), datetime(2026, 4, 5, 2, 30, tzinfo=berlin)),
        ]
        for repeat, original, expected in cases:
            with self.subTest(repeat=repeat):
                timestamp = int(original.timestamp())
                result = next_occurrence(timestamp, repeat, "Europe/Berlin", timestamp)
                self.assertEqual(result, int(expected.timestamp()))

    def test_recurrence_uses_first_ambiguous_time(self):
        berlin = ZoneInfo("Europe/Berlin")
        for repeat, day in [("daily", 24), ("weekly", 18)]:
            with self.subTest(repeat=repeat):
                timestamp = int(datetime(2026, 10, day, 2, 30, tzinfo=berlin).timestamp())
                result = next_occurrence(timestamp, repeat, "Europe/Berlin", timestamp)
                expected = datetime(2026, 10, 25, 2, 30, tzinfo=berlin, fold=0)
                self.assertEqual(result, int(expected.timestamp()))
                self.assertEqual(datetime.fromtimestamp(result, berlin).fold, 0)

    def test_recurrence_does_not_send_twice_during_clock_overlap(self):
        berlin = ZoneInfo("Europe/Berlin")
        due = int(datetime(2026, 10, 24, 2, 30, tzinfo=berlin).timestamp())
        now = int(datetime(2026, 10, 25, 2, 30, tzinfo=berlin, fold=0).timestamp())
        result = next_occurrence(due, "daily", "Europe/Berlin", now)
        expected = datetime(2026, 10, 26, 2, 30, tzinfo=berlin)
        self.assertEqual(result, int(expected.timestamp()))

    def test_dst_gap_and_ambiguous_time_rejected(self):
        for value, current in [
            ("29.03.2026 02:30 встреча", datetime(2026, 3, 28, tzinfo=timezone.utc)),
            ("25.10.2026 02:30 встреча", datetime(2026, 10, 24, tzinfo=timezone.utc)),
        ]:
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "перевод"):
                self.parse(value, current, "Europe/Berlin")

    def test_relative_time_is_elapsed_time_across_dst(self):
        berlin = ZoneInfo("Europe/Berlin")
        now = datetime(2026, 3, 29, 1, 30, tzinfo=berlin)
        result = self.parse("через 2 часа встреча", now, "Europe/Berlin")
        self.assertEqual(result.due_at - int(now.timestamp()), 7200)
        self.assertEqual(datetime.fromtimestamp(result.due_at, berlin).hour, 4)

    def test_invalid_requests_rejected(self):
        values = [
            "", "завтра", "через 10 минут", "через 10 минут —", "через 0 минут чай",
            "сегодня в 17:00 чай", "сегодня в 18:00 чай", "30.09.2026 25:00 чай",
            "31.09.2026 12:00 встреча", "29.02.2027 12:00 встреча", "в 9 чай",
            "через -1 час чай", "когда-нибудь чай",
        ]
        for value in values:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.parse(value)

    def test_invalid_timezone_and_repeat_rejected(self):
        with self.assertRaisesRegex(ValueError, "пояс"):
            self.parse("через 1 час чай", zone="Not/AZone")
        with self.assertRaises(ValueError):
            next_occurrence(0, "monthly", "Europe/Moscow", 10)

    def test_emoji_reminder_text_is_preserved(self):
        self.assertEqual(self.parse("через 1 час 💊").text, "💊")


if __name__ == "__main__":
    unittest.main()
