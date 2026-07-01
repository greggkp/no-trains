# CLAUDE.md

Guidance for working in this repository.

## What this is

`no-trains` generates subscribable `.ics` calendar feeds of planned **bus
replacement works** on Melbourne Metro train lines. A GitHub Actions cron job
runs `generate_ics.py` every six hours, which scrapes the (unofficial) JSON feed
behind metrotrains.com.au/planned-works, writes one `.ics` per line into
`docs/`, and deploys `docs/` straight to GitHub Pages as a build artifact.
Nothing is committed back to `main` (its branch protection stays strict); the
`.ics` files are gitignored.

## Layout

- `generate_ics.py` — the entire generator. Pure **standard library only**
  (`urllib`, `re`, `datetime`, `zoneinfo`, `json`, `html`). Keep it that way:
  there is no `requirements.txt` and CI installs nothing.
- `test_generate_ics.py` — `unittest` suite covering the pure parsing/formatting
  logic. No network access; the one detail-page test monkeypatches
  `fetch_detail`.
- `docs/` — `index.html` plus the generated `*.ics` feeds. The `.ics` files
  are build artifacts (gitignored, deployed to Pages by CI); don't hand-edit
  or commit them.
- `.github/workflows/update-calendar.yml` — the cron generator job (and tests).

## Conventions

- **No third-party dependencies.** Tests use stdlib `unittest`, not pytest.
- Output must be **deterministic**: identical upstream data must produce a
  byte-identical file, so calendar clients don't see spurious updates and
  feed diffs stay meaningful. `DTSTAMP` is derived from event data, not
  `now()`, for this reason. Preserve that property.
- ICS output follows RFC 5545: CRLF line endings, 75-octet line folding
  (`fold()`), and value escaping (`escape_ics()`).
- All event times are Melbourne-local (`Australia/Melbourne`); a hand-written
  `VTIMEZONE` is embedded so clients render times correctly.
- Times come from two sources: the feed's `dateTimeText` and the more precise
  detail-page headline. The headline overrides the feed when both are present.
- Parsing is best-effort: an entry whose times can't be parsed falls back to an
  all-day event (logged to stderr) rather than being dropped.
- Generation health is tracked in a `Stats` object and surfaced by `report()`:
  it counts fallback events and detail-page failures, writes `GITHUB_OUTPUT`
  (`degraded`, counts) under Actions, and emits a `::warning::` when degraded.
  `Stats`/`report()` must not influence feed bytes — keep them side-channel only.

## Failure notifications

The `update` workflow opens/updates a tracking GitHub Issue (label
`calendar-pipeline`) on **hard failure** (any step fails — generation crash or
Pages deploy failure) or **soft degradation** (`degraded=true` — entries fell
back or detail scrapes failed), and **auto-closes** it on the next clean run.
Soft degradation does not block publishing: the feed still deploys, the issue
just flags drift. This needs `issues: write` permission (already set). No
external services or secrets.

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
