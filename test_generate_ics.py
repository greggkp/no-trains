"""Tests for generate_ics.

Pure-logic tests only: nothing here touches the network. Functions that
normally fetch a page (build_event via fetch_detail) are exercised either
with link-less entries or by monkeypatching fetch_detail.

Run with: python -m unittest
"""

import datetime
import unittest
from zoneinfo import ZoneInfo

import generate_ics as g

MELBOURNE = ZoneInfo("Australia/Melbourne")


class ParseTimeTests(unittest.TestCase):
    def test_named_times(self):
        self.assertEqual(g.parse_time("midnight"), datetime.time(0, 0))
        self.assertEqual(g.parse_time("midday"), datetime.time(12, 0))
        self.assertEqual(g.parse_time("noon"), datetime.time(12, 0))

    def test_am_pm(self):
        self.assertEqual(g.parse_time("8pm"), datetime.time(20, 0))
        self.assertEqual(g.parse_time("8.30pm"), datetime.time(20, 30))
        self.assertEqual(g.parse_time("11:45am"), datetime.time(11, 45))

    def test_twelve_hour_edges(self):
        self.assertEqual(g.parse_time("12am"), datetime.time(0, 0))
        self.assertEqual(g.parse_time("12pm"), datetime.time(12, 0))

    def test_whitespace_and_case(self):
        self.assertEqual(g.parse_time("  8 PM "), datetime.time(20, 0))

    def test_unrecognised(self):
        with self.assertRaises(ValueError):
            g.parse_time("half past eight")


class ParseDatetimeTextTests(unittest.TestCase):
    def test_basic_range(self):
        start, end = g.parse_datetime_text(
            "8pm Friday 26 June to 11pm Sunday 28 June 2026"
        )
        self.assertEqual(
            start, datetime.datetime(2026, 6, 26, 20, 0, tzinfo=MELBOURNE)
        )
        self.assertEqual(
            end, datetime.datetime(2026, 6, 28, 23, 0, tzinfo=MELBOURNE)
        )

    def test_year_rollback_across_new_year(self):
        # Start has no year and inheriting the end's year would put it after
        # the end, so it should roll back to the previous year.
        start, end = g.parse_datetime_text(
            "11pm Tuesday 31 December to 5am Wednesday 1 January 2026"
        )
        self.assertEqual(
            start, datetime.datetime(2025, 12, 31, 23, 0, tzinfo=MELBOURNE)
        )
        self.assertEqual(
            end, datetime.datetime(2026, 1, 1, 5, 0, tzinfo=MELBOURNE)
        )

    def test_missing_end_year(self):
        with self.assertRaises(ValueError):
            g.parse_datetime_text("8pm Friday 26 June to 11pm Sunday 28 June")

    def test_wrong_number_of_datetimes(self):
        with self.assertRaises(ValueError):
            g.parse_datetime_text("8pm Friday 26 June 2026")


class ParseHeadlineTimesTests(unittest.TestCase):
    def test_last_service(self):
        start, end = g.parse_headline_times(
            "8.30pm to last service each night, Monday 22 June to Wednesday 24 June"
        )
        self.assertEqual(start, datetime.time(20, 30))
        self.assertIsNone(end)

    def test_explicit_end(self):
        start, end = g.parse_headline_times("8pm to 11pm each night")
        self.assertEqual(start, datetime.time(20, 0))
        self.assertEqual(end, datetime.time(23, 0))

    def test_no_times(self):
        self.assertEqual(g.parse_headline_times("buses replace trains"), (None, None))


class FormatTimeTests(unittest.TestCase):
    def test_no_leading_zero(self):
        moment = datetime.datetime(2026, 6, 26, 8, 0, tzinfo=MELBOURNE)
        self.assertEqual(g.format_time(moment), "8am")

    def test_on_the_hour_and_minutes(self):
        self.assertEqual(
            g.format_time(datetime.datetime(2026, 6, 26, 20, 0, tzinfo=MELBOURNE)),
            "8pm",
        )
        self.assertEqual(
            g.format_time(datetime.datetime(2026, 6, 26, 20, 30, tzinfo=MELBOURNE)),
            "8.30pm",
        )
        self.assertEqual(
            g.format_time(datetime.datetime(2026, 6, 26, 12, 0, tzinfo=MELBOURNE)),
            "12pm",
        )


class EscapeIcsTests(unittest.TestCase):
    def test_special_characters(self):
        self.assertEqual(
            g.escape_ics("a; b, c\\d\ne"), "a\\; b\\, c\\\\d\\ne"
        )


class FoldTests(unittest.TestCase):
    def test_short_line_unchanged(self):
        line = "SUMMARY:short"
        self.assertEqual(g.fold(line), line)

    def test_long_line_folded(self):
        line = "DESCRIPTION:" + "x" * 200
        folded = g.fold(line)
        self.assertIn("\r\n ", folded)
        for piece in folded.split("\r\n "):
            self.assertLessEqual(len(piece.encode("utf-8")), 75)
        # Unfolding (strip CRLF + leading space) restores the original.
        self.assertEqual(folded.replace("\r\n ", ""), line)

    def test_multibyte_not_split(self):
        # Each char is 4 bytes; folding must not cut mid-sequence.
        line = "X" * 70 + "😀" * 5
        folded = g.fold(line)
        for piece in folded.split("\r\n "):
            piece.encode("utf-8").decode("utf-8")  # would raise if split
        self.assertEqual(folded.replace("\r\n ", ""), line)


class SelectEntriesTests(unittest.TestCase):
    def _entry(self, id, type="bus-replacement", line="frankston", start=None, end=None):
        return {
            "id": id,
            "type": type,
            "classNames": [line, "metro"],
            "start": start,
            "end": end,
        }

    def test_filters_and_sorts(self):
        today = datetime.datetime.now(MELBOURNE).date()
        future = (today + datetime.timedelta(days=10)).isoformat()
        future2 = (today + datetime.timedelta(days=20)).isoformat()
        old = (today - datetime.timedelta(days=30)).isoformat()
        entries = [
            self._entry("b", start=future2, end=future2),
            self._entry("a", start=future, end=future),
            self._entry("wrong-type", type="works", start=future, end=future),
            self._entry("wrong-line", line="belgrave", start=future, end=future),
            self._entry("too-old", start=old, end=old),
        ]
        selected = g.select_entries(entries, "frankston")
        self.assertEqual([e["id"] for e in selected], ["a", "b"])

    def test_recently_finished_kept(self):
        today = datetime.datetime.now(MELBOURNE).date()
        recent = (today - datetime.timedelta(days=3)).isoformat()
        entries = [self._entry("recent", start=recent, end=recent)]
        self.assertEqual(len(g.select_entries(entries, "frankston")), 1)


class BuildEventTests(unittest.TestCase):
    def _entry(self, **overrides):
        entry = {
            "id": "42",
            "titleHTML": "Frankston Line",
            "classNames": ["frankston"],
            "dateTimeText": "8pm Friday 26 June to 11pm Sunday 28 June 2026",
            "type": "bus-replacement",
            "extendedProps": {},
            "start": "2026-06-26",
            "end": "2026-06-29",
        }
        entry.update(overrides)
        return entry

    def _joined(self, entry):
        return "\n".join(g.build_event(entry))

    def test_continuous_event(self):
        text = self._joined(self._entry())
        self.assertIn("UID:pw-42@metrotrains-planned-works", text)
        self.assertIn("SUMMARY:🚌 Frankston Line (8pm Fri – 11pm Sun)", text)
        self.assertIn("DTSTART;TZID=Australia/Melbourne:20260626T200000", text)
        self.assertIn("DTEND;TZID=Australia/Melbourne:20260628T230000", text)
        self.assertNotIn("RRULE", text)

    def test_nightly_event_recurs(self):
        entry = self._entry(classNames=["frankston", "at-night"])
        lines = g.build_event(entry)
        text = "\n".join(lines)
        self.assertIn("RRULE:FREQ=DAILY;COUNT=3", text)
        self.assertIn("each night", text)

    def test_unparseable_falls_back_to_all_day(self):
        entry = self._entry(dateTimeText="check the website for times")
        text = self._joined(entry)
        self.assertIn("DTSTART;VALUE=DATE:20260626", text)
        self.assertIn("DTEND;VALUE=DATE:20260629", text)
        self.assertIn("SUMMARY:🚌 Frankston Line", text)
        self.assertIn("whole days", text)

    def test_headline_overrides_feed_time(self):
        # The detail page is more precise (8.30pm) than the feed (8pm).
        def fake_detail(link):
            return "8.30pm to last service each night", []

        entry = self._entry(extendedProps={"link": "https://example.test/pw"})
        original = g.fetch_detail
        g.fetch_detail = fake_detail
        try:
            text = self._joined(entry)
        finally:
            g.fetch_detail = original
        self.assertIn("DTSTART;TZID=Australia/Melbourne:20260626T203000", text)
        self.assertIn("last service", text)
        self.assertIn("URL:https://example.test/pw", text)


class BuildCalendarTests(unittest.TestCase):
    def test_wraps_events(self):
        entry = {
            "id": "42",
            "titleHTML": "Frankston Line",
            "classNames": ["frankston"],
            "dateTimeText": "8pm Friday 26 June to 11pm Sunday 28 June 2026",
            "type": "bus-replacement",
            "extendedProps": {},
            "start": "2026-06-26",
            "end": "2026-06-29",
        }
        cal = g.build_calendar([entry], "frankston")
        self.assertTrue(cal.startswith("BEGIN:VCALENDAR\r\n"))
        self.assertTrue(cal.endswith("END:VCALENDAR\r\n"))
        self.assertIn("BEGIN:VTIMEZONE", cal)
        self.assertIn("X-WR-CALNAME:Frankston line bus replacements", cal)
        self.assertIn("BEGIN:VEVENT", cal)
        # Every line must be CRLF-terminated.
        self.assertNotIn("\r\r", cal)


if __name__ == "__main__":
    unittest.main()
