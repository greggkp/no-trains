"""Generate an ICS calendar of Metro Trains bus replacement works.

Fetches the JSON feed that powers https://www.metrotrains.com.au/planned-works/
and writes one .ics file per configured line into docs/, for hosting on
GitHub Pages and subscribing to from Google/Apple Calendar.
"""

import datetime
import hashlib
import hmac
import html
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

FEED_URL = (
    "https://www.metrotrains.com.au/wp-admin/admin-ajax.php"
    "?action=mt_get_planned_works"
)
USER_AGENT = "no-trains-calendar/1.0 (personal bus-replacement calendar)"
LINES = ["frankston", "sandringham"]
WORK_TYPE = "bus-replacement"
OUTPUT_DIR = Path(__file__).parent / "docs"
MELBOURNE = ZoneInfo("Australia/Melbourne")

# How far back to keep finished works in the feed, so recently ended events
# don't vanish from the calendar the moment they finish.
KEEP_PAST_DAYS = 7

# Optional enrichment: the official PTV Timetable API v3 publishes planned
# disruptions with machine-readable UTC timestamps (including a concrete end
# for "last service"), which beat parsing the English wording. Enabled only
# when PTV_DEV_ID/PTV_API_KEY are set; without them the text parsing below
# remains the sole time source, so local runs and forks work unchanged.
PTV_BASE_URL = "https://timetableapi.ptv.vic.gov.au"
PTV_ROUTE_IDS = {"frankston": 6, "sandringham": 12}
PTV_BUS_REPLACEMENT_RE = re.compile(r"buses replace trains", re.IGNORECASE)
# The feed rounds times (8pm vs PTV's 8.30pm); disagreement beyond this is
# drift worth flagging, not rounding.
PTV_MISMATCH_TOLERANCE = datetime.timedelta(hours=3)

MONTHS = {
    name: number
    for number, name in enumerate(
        "January February March April May June July August "
        "September October November December".split(),
        start=1,
    )
}

DATETIME_TEXT_RE = re.compile(
    r"(?P<time>midnight|midday|noon|\d{1,2}(?:[.:]\d{2})?\s*[ap]m)\s+"
    r"(?:Mon|Tues|Wednes|Thurs|Fri|Satur|Sun)day\s+"
    r"(?P<day>\d{1,2})\s+(?P<month>[A-Z][a-z]+)\s*(?P<year>\d{4})?",
    re.IGNORECASE,
)

TIME_RE = re.compile(r"midnight|midday|noon|\d{1,2}(?:[.:]\d{2})?\s*[ap]m", re.IGNORECASE)
HEADLINE_END_RE = re.compile(
    r"\bto\s+(?P<end>last service|" + TIME_RE.pattern + ")", re.IGNORECASE
)
PATTERNS_ATTR_RE = re.compile(r"data-patterns='([^']+)'")
HEADLINE_DIV_RE = re.compile(r'class="pw__single-top">(.*?)</div>', re.DOTALL)

VTIMEZONE = """\
BEGIN:VTIMEZONE
TZID:Australia/Melbourne
BEGIN:STANDARD
DTSTART:19700405T030000
RRULE:FREQ=YEARLY;BYMONTH=4;BYDAY=1SU
TZOFFSETFROM:+1100
TZOFFSETTO:+1000
TZNAME:AEST
END:STANDARD
BEGIN:DAYLIGHT
DTSTART:19701004T020000
RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=1SU
TZOFFSETFROM:+1000
TZOFFSETTO:+1100
TZNAME:AEDT
END:DAYLIGHT
END:VTIMEZONE"""


@dataclass
class Stats:
    """Counts of how generation went, for failure/degradation alerting.

    Generation can "succeed" (write a valid feed) while quietly degrading
    because the unofficial upstream changed its wording or markup: entries
    stop parsing and fall back to all-day events, or detail pages stop
    yielding headlines/stations. Those are the early-warning signs the
    scraper is drifting, so we count them and surface them to CI.
    """

    total_events: int = 0
    events_with_link: int = 0
    fallback_events: int = 0
    detail_failures: int = 0
    # PTV enrichment health. ptv_unmatched (an entry with no PTV disruption)
    # is reported but not degradation: PTV often publishes works later than
    # the Metro site lists them, so far-future entries legitimately lack a
    # match. Errors (API configured but failing) and mismatches (the two
    # sources disagree on when a work starts) are drift and do degrade.
    ptv_matched: int = 0
    ptv_unmatched: int = 0
    ptv_mismatches: int = 0
    ptv_errors: int = 0

    @property
    def degraded(self) -> bool:
        return (
            self.fallback_events > 0
            or self.detail_failures > 0
            or self.ptv_errors > 0
            or self.ptv_mismatches > 0
        )


def fetch_feed():
    request = urllib.request.Request(FEED_URL, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        entries = json.load(response)
    if not isinstance(entries, list) or not entries:
        raise ValueError(f"Planned works feed returned no entries: {entries!r}")
    return entries


def ptv_signed_url(path, params, dev_id, api_key):
    """Build a PTV Timetable API URL with the required HMAC-SHA1 signature."""
    query = urllib.parse.urlencode({**params, "devid": dev_id})
    raw = f"{path}?{query}"
    signature = hmac.new(
        api_key.encode(), raw.encode(), hashlib.sha1
    ).hexdigest().upper()
    return f"{PTV_BASE_URL}{raw}&signature={signature}"


def parse_ptv_spans(payload):
    """Extract bus-replacement (start, end) spans from a PTV disruptions
    response, as aware Melbourne datetimes sorted by start.

    PTV's disruption_type is too coarse to identify bus replacements
    ("Planned Works" also covers e.g. City Loop closures), so filter on the
    title wording. Disruptions without both timestamps are unusable here.
    """
    spans = []
    for disruption in payload.get("disruptions", {}).get("metro_train", []):
        if not PTV_BUS_REPLACEMENT_RE.search(disruption.get("title") or ""):
            continue
        if not disruption.get("from_date") or not disruption.get("to_date"):
            continue
        spans.append(tuple(
            datetime.datetime.fromisoformat(
                disruption[key].replace("Z", "+00:00")
            ).astimezone(MELBOURNE)
            for key in ("from_date", "to_date")
        ))
    return sorted(spans)


def fetch_ptv_spans(stats=None):
    """Fetch planned bus-replacement spans per line from the PTV API.

    Returns {line: [(start, end), ...]} for each line fetched successfully.
    A missing key means PTV times are unavailable for that line (keys not
    configured, no route mapping, or the request failed) and the caller
    should fall back to text parsing without counting unmatched entries.
    """
    dev_id = os.environ.get("PTV_DEV_ID")
    api_key = os.environ.get("PTV_API_KEY")
    if not dev_id or not api_key:
        print("note: PTV_DEV_ID/PTV_API_KEY not set; using feed text times only",
              file=sys.stderr)
        return {}

    spans = {}
    for line in LINES:
        route_id = PTV_ROUTE_IDS.get(line)
        if route_id is None:
            print(f"warning: no PTV route id for {line}; add it to PTV_ROUTE_IDS",
                  file=sys.stderr)
            continue
        url = ptv_signed_url(
            f"/v3/disruptions/route/{route_id}",
            {"disruption_status": "planned"}, dev_id, api_key,
        )
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                spans[line] = parse_ptv_spans(json.load(response))
        except (OSError, ValueError, KeyError) as error:
            if stats is not None:
                stats.ptv_errors += 1
            print(f"warning: PTV disruptions fetch failed for {line}: {error}",
                  file=sys.stderr)
    return spans


def match_ptv_span(entry, spans):
    """Pick the PTV span for a feed entry by date-range overlap.

    Returns (start, end) or None. Two concurrent works on the same line
    (e.g. different sections) can both overlap an entry; identically-timed
    matches are interchangeable, differing ones are ambiguous — use neither.
    """
    start_date = datetime.date.fromisoformat(entry["start"])
    end_date = datetime.date.fromisoformat(entry["end"])  # exclusive
    matches = {
        span for span in spans
        if span[0].date() < end_date and span[1].date() >= start_date
    }
    return matches.pop() if len(matches) == 1 else None


def parse_time(text):
    text = text.lower().strip()
    if text == "midnight":
        return datetime.time(0, 0)
    if text in ("midday", "noon"):
        return datetime.time(12, 0)
    match = re.fullmatch(r"(\d{1,2})(?:[.:](\d{2}))?\s*([ap]m)", text)
    if not match:
        raise ValueError(f"Unrecognised time: {text!r}")
    hour = int(match.group(1)) % 12
    if match.group(3) == "pm":
        hour += 12
    return datetime.time(hour, int(match.group(2) or 0))


def parse_datetime_text(text):
    """Parse e.g. '8pm Friday 26 June to 11pm Sunday 28 June 2026'.

    Returns (start, end) as aware datetimes in Melbourne time. The year is
    usually only present on the end date; a start without a year takes the
    end's year, rolled back one if that would place it after the end.
    """
    matches = list(DATETIME_TEXT_RE.finditer(text))
    if len(matches) != 2:
        raise ValueError(f"Expected two datetimes in {text!r}, found {len(matches)}")

    end_match = matches[1]
    if not end_match.group("year"):
        raise ValueError(f"No year on end date in {text!r}")

    def build(match, year):
        return datetime.datetime.combine(
            datetime.date(year, MONTHS[match.group("month").capitalize()],
                          int(match.group("day"))),
            parse_time(match.group("time")),
            tzinfo=MELBOURNE,
        )

    end = build(end_match, int(end_match.group("year")))
    start_year = int(matches[0].group("year") or end_match.group("year"))
    start = build(matches[0], start_year)
    if start > end:
        start = build(matches[0], start_year - 1)
    if start > end:
        raise ValueError(f"Start after end in {text!r}")
    return start, end


def strip_html(text):
    return html.unescape(re.sub(r"<[^>]+>", "", text)).strip()


def fetch_detail(link):
    """Scrape a planned-works detail page for precise times and stations.

    Returns (headline, station_groups): the headline is wording like
    '8.30pm to last service each night, Monday 22 June to Wednesday 24 June',
    and station_groups is a list of (label, [station, ...]) tuples.
    """
    request = urllib.request.Request(link, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        page = response.read().decode("utf-8", errors="replace")

    headline = None
    headline_match = HEADLINE_DIV_RE.search(page)
    if headline_match:
        text = strip_html(re.sub(r"<[^>]+>", " ", headline_match.group(1)))
        headline = re.sub(r"\s+", " ", text).strip() or None

    station_groups = []
    patterns_match = PATTERNS_ATTR_RE.search(page)
    if patterns_match:
        patterns = json.loads(html.unescape(patterns_match.group(1)))
        for line_block in patterns:
            for line_name, groups in line_block.items():
                for group in groups:
                    label = (
                        f"{line_name} — {group['title']}"
                        if len(patterns) > 1 or len(groups) > 1
                        else ""
                    )
                    station_groups.append((label, group.get("stations", [])))

    return headline, station_groups


def parse_headline_times(headline):
    """Pull (start_time, end_time) out of a detail-page headline.

    Either may be None: the start when no time is present, the end when the
    page says 'last service' (or nothing parseable).
    """
    start_match = TIME_RE.search(headline)
    start = parse_time(start_match.group(0)) if start_match else None
    end = None
    end_match = HEADLINE_END_RE.search(headline)
    if end_match and end_match.group("end").lower() != "last service":
        end = parse_time(end_match.group("end"))
    return start, end


def format_time(moment):
    text = moment.strftime("%I.%M%p" if moment.minute else "%I%p").lower()
    return text.lstrip("0")


def escape_ics(text):
    return (
        text.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\n", "\\n")
    )


def fold(line):
    """Fold a content line to the 75-octet limit required by RFC 5545."""
    encoded = line.encode("utf-8")
    if len(encoded) <= 75:
        return line
    parts = []
    while encoded:
        cut = min(75 if not parts else 74, len(encoded))
        # Don't split inside a multi-byte UTF-8 sequence.
        while cut < len(encoded) and (encoded[cut] & 0xC0) == 0x80:
            cut -= 1
        parts.append(encoded[:cut].decode("utf-8"))
        encoded = encoded[cut:]
    return ("\r\n ").join(parts)


def format_local(moment):
    return moment.strftime("%Y%m%dT%H%M%S")


def build_event(entry, stats=None, ptv_span=None):
    if stats is not None:
        stats.total_events += 1
    title = strip_html(entry["titleHTML"])
    link = entry.get("extendedProps", {}).get("link", "")
    date_text = entry["dateTimeText"].strip()
    nightly = "at-night" in entry["classNames"]

    headline, station_groups = None, []
    if link:
        if stats is not None:
            stats.events_with_link += 1
        try:
            headline, station_groups = fetch_detail(link)
        except (OSError, ValueError, KeyError) as error:
            if stats is not None:
                stats.detail_failures += 1
            print(f"warning: pw-{entry['id']}: detail page failed: {error}",
                  file=sys.stderr)

    description_parts = [headline or date_text]
    for label, stations in station_groups:
        name = f"Affected stations ({label})" if label else "Affected stations"
        description_parts.append(f"{name}: {', '.join(stations)}")
    if link:
        description_parts.append(f"Details: {link}")

    rrule = None
    try:
        try:
            start, end = parse_datetime_text(date_text)
        except ValueError:
            # A PTV span can still produce a timed event when the feed's
            # wording defeats the text parser.
            if ptv_span is None:
                raise
            start = end = None
        head_start, head_end = (
            parse_headline_times(headline) if headline else (None, None)
        )
        ends_last_service = bool(
            headline and not head_end and "last service" in headline.lower()
        )
        if ptv_span:
            if stats is not None:
                stats.ptv_matched += 1
                if start is not None and abs(ptv_span[0] - start) > PTV_MISMATCH_TOLERANCE:
                    stats.ptv_mismatches += 1
                    print(f"warning: pw-{entry['id']}: PTV start {ptv_span[0]} "
                          f"disagrees with feed start {start}", file=sys.stderr)
            # PTV timestamps are official and complete (they resolve "last
            # service" to a real end time), so they supersede both the feed
            # wording and the detail-page headline.
            start, end = ptv_span
            head_start = head_end = None
            ends_last_service = False
        elif head_start:
            # The detail page is more precise than the feed (8.30pm vs 8pm).
            start = start.replace(hour=head_start.hour, minute=head_start.minute)

        if nightly:
            # Trains still run during the day: one event per night instead of
            # a single block spanning the whole period.
            night_end = head_end or end.timetz().replace(tzinfo=None)
            wraps = night_end <= start.time()
            first_night_end = datetime.datetime.combine(
                start.date() + datetime.timedelta(days=1 if wraps else 0),
                night_end,
                tzinfo=MELBOURNE,
            )
            nights = max(1, (end.date() - start.date()).days + (0 if wraps else 1))
            if nights > 1:
                rrule = f"RRULE:FREQ=DAILY;COUNT={nights}"
            end = first_night_end
            end_label = "last service" if ends_last_service else format_time(end)
            time_suffix = f"{format_time(start)}–{end_label} each night"
        else:
            if head_end:
                end = end.replace(hour=head_end.hour, minute=head_end.minute)
            end_label = "last service" if ends_last_service else format_time(end)
            time_suffix = f"{format_time(start)} {start:%a} – {end_label} {end:%a}"

        dtstart = f"DTSTART;TZID=Australia/Melbourne:{format_local(start)}"
        dtend = f"DTEND;TZID=Australia/Melbourne:{format_local(end)}"
        stamp = start.astimezone(datetime.timezone.utc)
        summary = f"🚌 {title} ({time_suffix})"
    except ValueError as error:
        # Keep the entry rather than dropping it: fall back to the all-day
        # range from the feed's start/end fields (end is exclusive).
        if stats is not None:
            stats.fallback_events += 1
        print(f"warning: pw-{entry['id']}: {error}; using all-day fallback",
              file=sys.stderr)
        dtstart = f"DTSTART;VALUE=DATE:{entry['start'].replace('-', '')}"
        dtend = f"DTEND;VALUE=DATE:{entry['end'].replace('-', '')}"
        stamp = datetime.datetime.strptime(entry["start"], "%Y-%m-%d").replace(
            tzinfo=datetime.timezone.utc
        )
        description_parts.append("(Exact times unavailable; showing whole days.)")
        summary = f"🚌 {title}"

    lines = [
        "BEGIN:VEVENT",
        f"UID:pw-{entry['id']}@metrotrains-planned-works",
        # Deterministic stamp so unchanged feed data produces an identical file.
        f"DTSTAMP:{stamp.strftime('%Y%m%dT%H%M%SZ')}",
        dtstart,
        dtend,
    ]
    if rrule:
        lines.append(rrule)
    lines += [
        f"SUMMARY:{escape_ics(summary)}",
        f"DESCRIPTION:{escape_ics(chr(10).join(description_parts))}",
    ]
    if link:
        lines.append(f"URL:{escape_ics(link)}")
    lines.append("END:VEVENT")
    return lines


def select_entries(entries, line):
    cutoff = (
        datetime.datetime.now(MELBOURNE) - datetime.timedelta(days=KEEP_PAST_DAYS)
    ).date().isoformat()
    selected = {}
    for entry in entries:
        if entry["type"] != WORK_TYPE:
            continue
        if line not in entry["classNames"]:
            continue
        if entry["end"] < cutoff:
            continue
        selected[entry["id"]] = entry
    return sorted(selected.values(), key=lambda e: (e["start"], e["id"]))


def build_calendar(entries, line, stats=None, ptv_spans=None):
    line_name = line.replace("-", " ").title()
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//no-trains//metro-planned-works//EN",
        "CALSCALE:GREGORIAN",
        f"X-WR-CALNAME:{escape_ics(f'{line_name} line bus replacements')}",
        "X-WR-TIMEZONE:Australia/Melbourne",
        *VTIMEZONE.splitlines(),
    ]
    for entry in entries:
        # ptv_spans is None when PTV times are unavailable for this line;
        # then entries aren't "unmatched", there was just nothing to match.
        span = match_ptv_span(entry, ptv_spans) if ptv_spans is not None else None
        if ptv_spans is not None and span is None and stats is not None:
            stats.ptv_unmatched += 1
        lines.extend(build_event(entry, stats, span))
    lines.append("END:VCALENDAR")
    return "\r\n".join(fold(l) for l in lines) + "\r\n"


def report(stats):
    """Surface generation health for alerting.

    Always logs a summary. When running under GitHub Actions, writes
    machine-readable outputs (consumed by the workflow to open/close a
    tracking issue) and a step-summary line, and emits a `::warning::`
    annotation if degraded. Does not affect the generated feeds, so feed
    output stays deterministic regardless of environment.
    """
    summary = (
        f"events={stats.total_events} fallback={stats.fallback_events} "
        f"detail_failures={stats.detail_failures}/{stats.events_with_link} "
        f"ptv_matched={stats.ptv_matched} ptv_unmatched={stats.ptv_unmatched} "
        f"ptv_mismatches={stats.ptv_mismatches} ptv_errors={stats.ptv_errors} "
        f"degraded={stats.degraded}"
    )
    print(summary, file=sys.stderr)

    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a", encoding="utf-8") as handle:
            handle.write(f"total_events={stats.total_events}\n")
            handle.write(f"fallback_events={stats.fallback_events}\n")
            handle.write(f"detail_failures={stats.detail_failures}\n")
            handle.write(f"ptv_matched={stats.ptv_matched}\n")
            handle.write(f"ptv_unmatched={stats.ptv_unmatched}\n")
            handle.write(f"ptv_mismatches={stats.ptv_mismatches}\n")
            handle.write(f"ptv_errors={stats.ptv_errors}\n")
            handle.write(f"degraded={'true' if stats.degraded else 'false'}\n")

    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a", encoding="utf-8") as handle:
            handle.write(f"### Calendar generation\n\n- {summary}\n")

    if stats.degraded:
        print(f"::warning::Calendar generation degraded: {summary}")


def main():
    feed = fetch_feed()
    OUTPUT_DIR.mkdir(exist_ok=True)
    stats = Stats()
    ptv_spans = fetch_ptv_spans(stats)
    for line in LINES:
        selected = select_entries(feed, line)
        output_path = OUTPUT_DIR / f"{line}.ics"
        output_path.write_text(
            build_calendar(selected, line, stats, ptv_spans.get(line)),
            encoding="utf-8",
        )
        print(f"{output_path.name}: {len(selected)} events")
    report(stats)
    return stats


if __name__ == "__main__":
    main()
