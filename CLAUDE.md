# CLAUDE.md

Guidance for working in this repository.

## What this is

`no-trains` generates subscribable `.ics` calendar feeds of planned **bus
replacement works** on Melbourne Metro train lines. A GitHub Actions cron job
runs `generate_ics.py` every six hours, which scrapes the (unofficial) JSON feed
behind metrotrains.com.au/planned-works, writes one `.ics` per line into
`docs/`, and commits only when the output changed. `docs/` is published with
GitHub Pages.

## Layout

- `generate_ics.py` — the entire generator. Pure **standard library only**
  (`urllib`, `re`, `datetime`, `zoneinfo`, `json`, `html`). Keep it that way:
  there is no `requirements.txt` and CI installs nothing.
- `test_generate_ics.py` — `unittest` suite covering the pure parsing/formatting
  logic. No network access; the one detail-page test monkeypatches
  `fetch_detail`.
- `docs/` — generated `*.ics` feeds plus `index.html`. The `.ics` files are
  build artifacts committed by CI; don't hand-edit them.
- `.github/workflows/update-calendar.yml` — the cron generator job (and tests).

## Conventions

- **No third-party dependencies.** Tests use stdlib `unittest`, not pytest.
- Output must be **deterministic**: identical upstream data must produce a
  byte-identical file, otherwise CI commits noise every six hours. `DTSTAMP` is
  derived from event data, not `now()`, for this reason. Preserve that property.
- ICS output follows RFC 5545: CRLF line endings, 75-octet line folding
  (`fold()`), and value escaping (`escape_ics()`).
- All event times are Melbourne-local (`Australia/Melbourne`); a hand-written
  `VTIMEZONE` is embedded so clients render times correctly.
- Times come from two sources: the feed's `dateTimeText` and the more precise
  detail-page headline. The headline overrides the feed when both are present.
- Parsing is best-effort: an entry whose times can't be parsed falls back to an
  all-day event (logged to stderr) rather than being dropped.

## Running

```bash
python generate_ics.py        # regenerate docs/*.ics
python -m unittest            # run the test suite
```

## When changing parsing logic

The upstream endpoint is unofficial and its wording varies ("8pm Friday 26 June
to 11pm Sunday 28 June 2026", "last service", "each night", etc.). When you
touch `parse_*`, `build_event`, or the regexes, **add a test case** capturing
the new wording so the behaviour is pinned. Run `python -m unittest` before
committing.

## Adding a line

Add the slug to `LINES` in `generate_ics.py` and a link in `docs/index.html`.
Valid slugs are the lowercase hyphenated Metro line names (see README).
