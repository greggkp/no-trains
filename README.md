# no-trains

Subscribable calendar feeds of planned **bus replacement works** on Melbourne
Metro train lines.

A GitHub Actions workflow runs every six hours, fetches the JSON feed that
powers [metrotrains.com.au/planned-works](https://www.metrotrains.com.au/planned-works/),
and regenerates one `.ics` file per configured line in `docs/`, served via
GitHub Pages. Subscribe to a feed URL in Google/Apple Calendar and bus
replacements appear (and update, and disappear when cancelled) automatically.

## Feeds

- Frankston line: `https://<username>.github.io/no-trains/frankston.ics`

To subscribe in Google Calendar: **Settings → Add calendar → From URL** and
paste the feed URL. Note Google refreshes subscribed calendars on its own
schedule (typically every 12–24 hours).

## Adding a line

Edit `LINES` in `generate_ics.py`, e.g.:

```python
LINES = ["frankston", "belgrave"]
```

and add a link to `docs/index.html`. Valid line slugs are the lowercase,
hyphenated line names used by the Metro site: `alamein`, `belgrave`,
`craigieburn`, `cranbourne`, `flemington`, `frankston`, `glen-waverley`,
`hurstbridge`, `lilydale`, `mernda`, `pakenham`, `sandringham`, `showgrounds`,
`stony-point`, `sunbury`, `upfield`, `werribee`, `williamstown`.

## How it works

- `generate_ics.py` — fetches the planned-works JSON (no API key needed),
  filters to `bus-replacement` entries for the configured lines, parses the
  human-readable times (e.g. *"8pm Friday 26 June to 11pm Sunday 28 June
  2026"*) into Melbourne-local event times, and writes RFC 5545 ICS files.
  Entries whose times can't be parsed fall back to all-day events rather than
  being dropped.
- `.github/workflows/update-calendar.yml` — cron job that regenerates the
  feeds and commits only when something changed.
- `docs/` — the generated feeds plus a small index page, published with
  GitHub Pages (deploy from branch `main`, folder `/docs`).

The upstream endpoint is unofficial (it's what the Metro website itself
calls), so it may change without notice. If it breaks, the official fallback
is Transport Victoria's [GTFS-Realtime Service Alerts](https://opendata.transport.vic.gov.au/dataset/gtfs-realtime).
