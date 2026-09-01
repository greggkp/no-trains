# Monitoring: options and findings

Status: **decision pending, nothing implemented.** Written 2026-09-01 as a
handover for a future session. The goal is infrequent confirmation that the
pipeline is running, and fast notice when it is not.

## What exists today

`update-calendar.yml` runs on `schedule: "47 */6 * * *"`. On any failed step,
or when `generate_ics.py` reports `degraded=true`, the workflow opens or
updates a `calendar-pipeline` issue and auto-closes it on the next clean run.

This detects a run that **fails**. It cannot detect runs that **stop
happening**: no run means no failed step, so no issue is opened and no email
is sent. That is the gap.

## Findings (2026-09-01)

Last 30 scheduled runs, 23 Aug to 1 Sep, all successful:

| Metric                       | Value    |
|------------------------------|----------|
| Nominal interval             | 6 h      |
| Median actual gap            | 6.6 h    |
| Max actual gap               | 13.6 h   |
| Gaps over 8 h                | 10 of 29 |
| Runs delivered vs 6 h slots  | 30 of 36 |

GitHub's cron is best-effort and drops or delays runs under load. Any
"missing run" alarm below about 16 h will false-alarm.

Other facts that shape the options:

- The feed is deterministic by design (`DTSTAMP` from event data), so the
  deployed `.ics` carries no freshness signal. An external HTTP check on the
  feed cannot detect staleness. A side-channel file (e.g. `docs/health.json`
  with a generated-at time and the `Stats` counters) would be needed; it must
  not touch feed bytes.
- Degraded runs *succeed*, so GitHub sends no Actions failure email for them.
  The tracking issue is the only channel. Hard-failure emails for scheduled
  runs go to the run's actor, which is whoever last edited the cron line in
  the workflow file (currently the repo owner). A bot commit touching the
  schedule line would redirect them.
- CLAUDE.md records a "no external notification services" decision. A true
  dead man's switch needs something outside GitHub's scheduler; an in-GitHub
  watchdog workflow shares the scheduler and the 60-day rule below, so it
  fails in a correlated way.

## Problem 1: detecting absence (dead man's switch)

### Option 1a: Healthchecks.io ping (recommended)

Free tier. Store the ping URL as an Actions secret. After the Pages deploy
step, `curl -fsS -m 10 --retry 3 "$HC_URL"`; add a step with
`if: failure()` that hits `"$HC_URL/fail"` so hard failures alert
immediately instead of waiting out the grace period. Suggested settings:
period 6 h, grace 12 h, giving an alert about 18 h after the last success
with near-zero false positives given the jitter above. Its weekly or monthly
report email is the "infrequent confirmation". Alert channels include email,
Pushover, Signal, Telegram.

Requires relaxing the "no external notification services" note in CLAUDE.md.

### Option 1b: publish `docs/health.json`

Have `report()` write `docs/health.json` (generated-at UTC, counts,
`degraded`). Gives an end-to-end freshness signal on the deployed site,
independent of the ping, and something to eyeball. Keep it out of the `.ics`
bytes. Pairs with 1a; not sufficient alone because something external still
has to evaluate the timestamp.

### Option 1c: in-GitHub watchdog workflow

A second scheduled workflow queries the Actions API for the last successful
`Update calendar` run and fails (which emails the actor) if it is older than
~18 h. Weekly "confirmation" would need a comment on a pinned issue, since
GitHub never emails on success. Strictly weaker than 1a: same scheduler
jitter, same 60-day auto-disable, correlated failure. Only worth it if
external services stay off the table.

### Option 1d: Claude Routine

A Claude Code Routine every 6 h reads the Actions API and emails via Gmail
only if the last success is older than 18 h, plus a weekly summary.
Independent of GitHub's scheduler, but dependent on Claude infrastructure
and usage budget. Weaker than 1a for a hobby pipeline.

### Also do regardless

- Confirm the repo owner is watching the repo for issues, otherwise the
  degraded-run issue is silent.
- Confirm Actions notification settings send email on failed workflows.

## Problem 2: the 60-day scheduled-workflow cutoff

GitHub automatically disables the `schedule` trigger in a **public** repo
after 60 days with no repository activity. Last push was 2026-07-24, so the
cutoff lands around **2026-09-22** unless something resets it. Dependabot is
configured security-only (`open-pull-requests-limit: 0`), so it will not
produce routine commits. Only `schedule` is affected; `workflow_dispatch`
and `push` keep working.

Caveats: docs.github.com was unreachable from the session sandbox, so the
rule wording is from memory. GitHub does not define "activity"; empirically
a pushed commit or re-enabling the workflow resets the timer. Whether a push
to a non-default branch counts is unverified. Do not rely on a warning
email; none was confirmed.

### Option 2a: accept it, re-enable by hand

One click in the Actions tab, or:

```bash
gh workflow enable update-calendar.yml --repo greggkp/no-trains
```

Downtime is bounded by monitoring latency, so this only works with 1a in
place.

### Option 2b: workflow re-enables itself weekly

Add a job that calls the enable endpoint on its own workflow. No commits, no
external service. Scope the permission to the job, not the workflow, to
preserve the hardened permissions from #5:

```yaml
  keepalive:
    runs-on: ubuntu-latest
    permissions:
      actions: write
    steps:
      - run: gh workflow enable update-calendar.yml --repo "$GITHUB_REPOSITORY"
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
```

Caveat: the best-known action doing exactly this (keepalive-workflow) now
shows as disabled by GitHub Staff for a terms-of-service violation. The
reason is not visible. Treat this as a grey area, not a sanctioned technique.

### Option 2c: generate real commits

Periodically commit a small file (e.g. a monthly bump of `health.json`) to a
non-protected branch. Same grey area as 2b, and it is unverified that a
non-default-branch push counts, so this is worse than 2b.

### Option 2d: make the repo private

The rule does not apply to private repos. GitHub Pages from a private repo
needs a paid plan.

### Option 2e: drop `schedule`, dispatch externally (recommended)

Keep `workflow_dispatch` and fire it from an external scheduler via
`POST /repos/greggkp/no-trains/actions/workflows/update-calendar.yml/dispatches`
using a fine-grained PAT scoped to this repo with Actions: write only. The
60-day rule disappears, and so does the cron jitter: external cron services
fire on time, so the 4 to 14 h gaps become a real 6 h. Candidates:
cron-job.org (free) or a Claude Code Routine calling the same endpoint.

New failure mode: PAT expiry (fine-grained tokens expire within a year).
That is exactly what the dead man's switch (1a) catches.

## Recommended combination

1. **1a + 1b**: Healthchecks.io ping after deploy plus `docs/health.json`.
   About 20 lines of workflow YAML and 15 lines of Python plus a test.
2. **2e**: external dispatch replaces `schedule`. If external services stay
   off the table, fall back to **2b** with the terms-of-service caveat noted.
3. Update the "Failure notifications" section of CLAUDE.md to reflect the
   external service decision.

Needed from the repo owner before implementing: a decision on external
services, the Healthchecks.io ping URL (as secret `HEALTHCHECK_URL`), and if
2e is chosen, a fine-grained PAT held by the external scheduler, not by the
repo.
