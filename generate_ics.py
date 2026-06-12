"""Generate an ICS calendar of Metro Trains bus replacement works.

Fetches the JSON feed that powers https://www.metrotrains.com.au/planned-works/
and writes one .ics file per configured line into docs/, for hosting on
GitHub Pages and subscribing to from Google/Apple Calendar.
"""

import datetime
import html
import json
import re
import sys
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

FEED_URL = (
    "https://www.metrotrains.com.au/wp-admin/admin-ajax.php"
    "?action=mt_get_planned_works"
)
USER_AGENT = "no-trains-calendar/1.0 (personal bus-replacement calendar)"
LINES = ["frankston"]
WORK_TYPE = "bus-replacement"
OUTPUT_DIR = Path(__file__).parent / "docs"
MELBOURNE = ZoneInfo("Australia/Melbourne")

# How far back to keep finished works in the feed, so recently ended events
# don't vanish from the calendar the moment they finish.
KEEP_PAST_DAYS = 7

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


def fetch_feed():
    request = urllib.request.Request(FEED_URL, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        entries = json.load(response)
    if not isinstance(entries, list) or not entries:
        raise ValueError(f"Planned works feed returned no entries: {entries!r}")
    return entries


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


def build_event(entry):
    title = strip_html(entry["titleHTML"])
    link = entry.get("extendedProps", {}).get("link", "")
    date_text = entry["dateTimeText"].strip()

    description_parts = [date_text]
    if link:
        description_parts.append(f"Details: {link}")

    try:
        start, end = parse_datetime_text(date_text)
        dtstart = f"DTSTART;TZID=Australia/Melbourne:{format_local(start)}"
        dtend = f"DTEND;TZID=Australia/Melbourne:{format_local(end)}"
        stamp = start.astimezone(datetime.timezone.utc)
    except ValueError as error:
        # Keep the entry rather than dropping it: fall back to the all-day
        # range from the feed's start/end fields (end is exclusive).
        print(f"warning: pw-{entry['id']}: {error}; using all-day fallback",
              file=sys.stderr)
        start_date = entry["start"].replace("-", "")
        end_date = entry["end"].replace("-", "")
        dtstart = f"DTSTART;VALUE=DATE:{start_date}"
        dtend = f"DTEND;VALUE=DATE:{end_date}"
        stamp = datetime.datetime.strptime(entry["start"], "%Y-%m-%d").replace(
            tzinfo=datetime.timezone.utc
        )
        description_parts.append("(Exact times unavailable; showing whole days.)")

    lines = [
        "BEGIN:VEVENT",
        f"UID:pw-{entry['id']}@metrotrains-planned-works",
        # Deterministic stamp so unchanged feed data produces an identical file.
        f"DTSTAMP:{stamp.strftime('%Y%m%dT%H%M%SZ')}",
        dtstart,
        dtend,
        f"SUMMARY:{escape_ics('🚌 ' + title)}",
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


def build_calendar(entries, line):
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
        lines.extend(build_event(entry))
    lines.append("END:VCALENDAR")
    return "\r\n".join(fold(l) for l in lines) + "\r\n"


def main():
    feed = fetch_feed()
    OUTPUT_DIR.mkdir(exist_ok=True)
    for line in LINES:
        selected = select_entries(feed, line)
        output_path = OUTPUT_DIR / f"{line}.ics"
        output_path.write_text(build_calendar(selected, line), encoding="utf-8")
        print(f"{output_path.name}: {len(selected)} events")


if __name__ == "__main__":
    main()
