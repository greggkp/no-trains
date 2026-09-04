"""Tests for generate_ics.

Pure-logic tests only: nothing here touches the network. Functions that
normally fetch a page (build_event via fetch_detail) are exercised either
with link-less entries or by monkeypatching fetch_detail.

Run with: python -m unittest
"""

import datetime
import os
import tempfile
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


def ptv_disruption(**overrides):
    """A PTV /v3/disruptions disruption, shaped like the real API response."""
    disruption = {
        "disruption_id": 366096,
        "title": (
            "Frankston Line: Buses replace trains from 8.30pm Friday 24 July "
            "to last service Sunday 26 July 2026"
        ),
        "description": "From 8.30pm Friday 24 July to last service Sunday 26 July",
        "disruption_type": "Planned Works",
        "disruption_status": "Planned",
        "from_date": "2026-07-24T10:30:00Z",
        "to_date": "2026-07-26T17:00:00Z",
    }
    disruption.update(overrides)
    return disruption


class PtvSignedUrlTests(unittest.TestCase):
    def test_signature_is_stable(self):
        # Pins the exact signing construction (path?params&devid, HMAC-SHA1
        # uppercase hex) that has been verified against the live API.
        url = g.ptv_signed_url(
            "/v3/disruptions/route/6",
            {"disruption_status": "planned"},
            "300001",
            "fake-key",
        )
        self.assertEqual(
            url,
            "https://timetableapi.ptv.vic.gov.au/v3/disruptions/route/6"
            "?disruption_status=planned&devid=300001"
            "&signature=776C023C5E85E9E87B3F4F29E5D141D07E2C0A58",
        )


class ParsePtvSpansTests(unittest.TestCase):
    def test_bus_replacement_converted_to_melbourne(self):
        payload = {"disruptions": {"metro_train": [ptv_disruption()]}}
        spans = g.parse_ptv_spans(payload)
        # July is AEST (UTC+10): 10:30Z is 8.30pm, and the "last service"
        # end 17:00Z resolves to 3am the following morning.
        self.assertEqual(spans, [(
            datetime.datetime(2026, 7, 24, 20, 30, tzinfo=MELBOURNE),
            datetime.datetime(2026, 7, 27, 3, 0, tzinfo=MELBOURNE),
        )])

    def test_non_bus_replacement_filtered_out(self):
        payload = {"disruptions": {"metro_train": [ptv_disruption(
            title="Frankston Line: No City Loop trains from 9pm each night"
        )]}}
        self.assertEqual(g.parse_ptv_spans(payload), [])

    def test_missing_to_date_skipped(self):
        payload = {"disruptions": {"metro_train": [ptv_disruption(to_date=None)]}}
        self.assertEqual(g.parse_ptv_spans(payload), [])

    def test_no_metro_train_key(self):
        self.assertEqual(g.parse_ptv_spans({"disruptions": {}}), [])


class MatchPtvSpanTests(unittest.TestCase):
    SPAN = (
        datetime.datetime(2026, 7, 24, 20, 30, tzinfo=MELBOURNE),
        datetime.datetime(2026, 7, 27, 3, 0, tzinfo=MELBOURNE),
    )

    def _entry(self, start, end):
        return {"start": start, "end": end}

    def test_overlap_matches(self):
        # Feed end date is exclusive; the span ending 3am Monday 27th still
        # overlaps an entry listed as 24th–27th.
        entry = self._entry("2026-07-24", "2026-07-27")
        self.assertEqual(g.match_ptv_span(entry, [self.SPAN]), self.SPAN)

    def test_no_overlap(self):
        entry = self._entry("2026-09-01", "2026-09-03")
        self.assertIsNone(g.match_ptv_span(entry, [self.SPAN]))

    def test_ambiguous_overlap_matches_nothing(self):
        other = (
            datetime.datetime(2026, 7, 25, 20, 0, tzinfo=MELBOURNE),
            datetime.datetime(2026, 7, 26, 23, 0, tzinfo=MELBOURNE),
        )
        entry = self._entry("2026-07-24", "2026-07-27")
        self.assertIsNone(g.match_ptv_span(entry, [self.SPAN, other]))

    def test_duplicate_spans_still_match(self):
        entry = self._entry("2026-07-24", "2026-07-27")
        self.assertEqual(
            g.match_ptv_span(entry, [self.SPAN, self.SPAN]), self.SPAN
        )


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


class BuildEventPtvTests(unittest.TestCase):
    """PTV API timestamps, when matched, supersede all text-derived times."""

    # 8.30pm Friday 26 June to 3am Monday 29 June 2026, Melbourne time —
    # the shape PTV publishes for "8pm Friday to last service Sunday".
    SPAN = (
        datetime.datetime(2026, 6, 26, 20, 30, tzinfo=MELBOURNE),
        datetime.datetime(2026, 6, 29, 3, 0, tzinfo=MELBOURNE),
    )

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

    def test_ptv_times_used_for_continuous_event(self):
        stats = g.Stats()
        text = "\n".join(g.build_event(self._entry(), stats, self.SPAN))
        self.assertIn("DTSTART;TZID=Australia/Melbourne:20260626T203000", text)
        self.assertIn("DTEND;TZID=Australia/Melbourne:20260629T030000", text)
        # "Last service" becomes a concrete end time in the summary.
        self.assertIn("SUMMARY:🚌 Frankston Line (8.30pm Fri – 3am Mon)", text)
        self.assertEqual(stats.ptv_matched, 1)
        self.assertEqual(stats.ptv_mismatches, 0)
        self.assertFalse(stats.degraded)

    def test_ptv_times_used_for_nightly_event(self):
        span = (
            datetime.datetime(2026, 6, 26, 21, 0, tzinfo=MELBOURNE),
            datetime.datetime(2026, 6, 29, 3, 0, tzinfo=MELBOURNE),
        )
        entry = self._entry(classNames=["frankston", "at-night"])
        text = "\n".join(g.build_event(entry, None, span))
        self.assertIn("DTSTART;TZID=Australia/Melbourne:20260626T210000", text)
        self.assertIn("DTEND;TZID=Australia/Melbourne:20260627T030000", text)
        self.assertIn("RRULE:FREQ=DAILY;COUNT=3", text)
        self.assertIn("9pm–3am each night", text)

    def test_ptv_rescues_unparseable_text(self):
        stats = g.Stats()
        entry = self._entry(dateTimeText="check the website for times")
        text = "\n".join(g.build_event(entry, stats, self.SPAN))
        self.assertIn("DTSTART;TZID=Australia/Melbourne:20260626T203000", text)
        self.assertNotIn("whole days", text)
        self.assertEqual(stats.fallback_events, 0)
        self.assertFalse(stats.degraded)

    def test_ptv_disagreement_flagged_but_ptv_wins(self):
        # A start a day away from the feed's is drift, not rounding.
        span = (
            datetime.datetime(2026, 6, 27, 20, 30, tzinfo=MELBOURNE),
            datetime.datetime(2026, 6, 29, 3, 0, tzinfo=MELBOURNE),
        )
        stats = g.Stats()
        text = "\n".join(g.build_event(self._entry(), stats, span))
        self.assertIn("DTSTART;TZID=Australia/Melbourne:20260627T203000", text)
        self.assertEqual(stats.ptv_mismatches, 1)
        self.assertTrue(stats.degraded)

    def test_rounding_difference_is_not_a_mismatch(self):
        # Feed says 8pm, PTV says 8.30pm: within tolerance.
        stats = g.Stats()
        g.build_event(self._entry(), stats, self.SPAN)
        self.assertEqual(stats.ptv_mismatches, 0)


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

    def test_ptv_unmatched_counted_only_when_ptv_available(self):
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
        # PTV available but no disruption overlaps: unmatched (reported,
        # not degraded — PTV publishes works later than the Metro site).
        stats = g.Stats()
        g.build_calendar([entry], "frankston", stats, ptv_spans=[])
        self.assertEqual(stats.ptv_unmatched, 1)
        self.assertFalse(stats.degraded)
        # PTV unavailable entirely: nothing to match against.
        stats = g.Stats()
        g.build_calendar([entry], "frankston", stats, ptv_spans=None)
        self.assertEqual(stats.ptv_unmatched, 0)

    def test_matched_span_flows_through_to_event(self):
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
        span = (
            datetime.datetime(2026, 6, 26, 20, 30, tzinfo=MELBOURNE),
            datetime.datetime(2026, 6, 29, 3, 0, tzinfo=MELBOURNE),
        )
        stats = g.Stats()
        cal = g.build_calendar([entry], "frankston", stats, ptv_spans=[span])
        self.assertIn("DTSTART;TZID=Australia/Melbourne:20260626T203000", cal)
        self.assertEqual(stats.ptv_matched, 1)
        self.assertEqual(stats.ptv_unmatched, 0)


class StatsTests(unittest.TestCase):
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

    def test_healthy_run_not_degraded(self):
        stats = g.Stats()
        g.build_event(self._entry(), stats)
        self.assertEqual(stats.total_events, 1)
        self.assertEqual(stats.fallback_events, 0)
        self.assertEqual(stats.detail_failures, 0)
        self.assertFalse(stats.degraded)

    def test_fallback_counts_as_degraded(self):
        stats = g.Stats()
        g.build_event(self._entry(dateTimeText="see website"), stats)
        self.assertEqual(stats.fallback_events, 1)
        self.assertTrue(stats.degraded)

    def test_detail_failure_counts_as_degraded(self):
        def boom(link):
            raise OSError("upstream changed")

        stats = g.Stats()
        entry = self._entry(extendedProps={"link": "https://example.test/pw"})
        original = g.fetch_detail
        g.fetch_detail = boom
        try:
            g.build_event(entry, stats)
        finally:
            g.fetch_detail = original
        self.assertEqual(stats.events_with_link, 1)
        self.assertEqual(stats.detail_failures, 1)
        self.assertTrue(stats.degraded)

    def test_ptv_errors_count_as_degraded(self):
        # total_events=1 isolates the PTV signal: a zero-event run is
        # degraded on its own (see test_zero_events_counts_as_degraded).
        self.assertTrue(g.Stats(total_events=1, ptv_errors=1).degraded)
        self.assertTrue(g.Stats(total_events=1, ptv_mismatches=1).degraded)
        self.assertFalse(g.Stats(total_events=1, ptv_unmatched=3).degraded)

    def test_zero_events_counts_as_degraded(self):
        # An upstream shape change that yields no entries would otherwise
        # write empty calendars and report a clean run.
        self.assertTrue(g.Stats().degraded)
        self.assertFalse(g.Stats(total_events=1).degraded)

    def test_empty_feed_degrades_a_whole_run(self):
        stats = g.Stats()
        cal = g.build_calendar([], "frankston", stats)
        self.assertEqual(stats.total_events, 0)
        self.assertTrue(stats.degraded)
        # Still a valid, deployable calendar — degradation does not block
        # publishing, it just flags drift.
        self.assertIn("BEGIN:VCALENDAR", cal)
        self.assertNotIn("BEGIN:VEVENT", cal)

    def test_report_writes_github_output(self):
        stats = g.Stats(
            total_events=5, events_with_link=5, fallback_events=2, detail_failures=1,
            ptv_matched=3, ptv_unmatched=2, ptv_errors=1,
        )
        with tempfile.TemporaryDirectory() as tmp:
            out_path = os.path.join(tmp, "out")
            old = os.environ.get("GITHUB_OUTPUT")
            os.environ["GITHUB_OUTPUT"] = out_path
            try:
                g.report(stats)
            finally:
                if old is None:
                    del os.environ["GITHUB_OUTPUT"]
                else:
                    os.environ["GITHUB_OUTPUT"] = old
            with open(out_path, encoding="utf-8") as handle:
                written = handle.read()
        self.assertIn("fallback_events=2", written)
        self.assertIn("detail_failures=1", written)
        self.assertIn("ptv_matched=3", written)
        self.assertIn("ptv_unmatched=2", written)
        self.assertIn("ptv_errors=1", written)
        self.assertIn("degraded=true", written)


if __name__ == "__main__":
    unittest.main()
