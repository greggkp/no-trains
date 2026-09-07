# Monitoring

The refresh schedule is external to GitHub. A systemd timer on the driver
machine dispatches `update-calendar.yml` every six hours; GitHub Actions runs
the tests and generator and deploys the result to GitHub Pages.

## What is covered

Every dispatched workflow reports its own health:

- a hard failure in tests, generation, deployment, or the heartbeat request opens a
  `calendar-pipeline` issue;
- soft degradation from parser fallbacks, detail-page failures, PTV API
  drift, or a run that produced no events at all opens the same issue;
- the next clean run closes the issue.

The local dispatch retries transient failures: `ops/no-trains-refresh.sh`
makes up to five attempts, five minutes apart, before the service fails.
A dispatch that still fails (network down, expired token, disabled workflow,
permission change) is logged locally and, when `HEALTHCHECK_URL` is present
in `/etc/no-trains-refresh.env`, reported straight to Healthchecks.io as a
failure so the alert arrives within minutes instead of after the grace
period. Status and logs:

```bash
systemctl status no-trains-refresh.timer no-trains-refresh.service
journalctl -u no-trains-refresh.service
```

Raspberry Pi OS keeps the journal in RAM by default, so a reboot would erase
the evidence of why a dispatch failed. `ops/journald-persistent.conf`
overrides that; the install steps in `ops/README.md` apply it.

## Remaining dead-man risk

The machine cannot report its own total failure. If it is powered off, its
timer stops, or it has no network for the whole retry window and no local
`HEALTHCHECK_URL`, GitHub receives no dispatch and therefore cannot open a
tracking issue.

The workflow has optional Healthchecks.io support for this case. Its
`notify` job:

- pings the configured check after every clean refresh;
- sends a failure signal for a hard failure or degraded run;
- does nothing when `HEALTHCHECK_URL` is absent, so forks need no setup.

Because the ping happens only after an externally dispatched workflow reaches
the notification job, missing pings cover the complete path from the driver
timer through GitHub Pages deployment.

## Enabling the dead-man check

1. Create a Healthchecks.io check with a six-hour period and a twelve-hour
   grace period. This alerts about eighteen hours after the last completed
   refresh, allowing for a missed run or temporary outage.
2. Choose the desired alert channel and enable periodic status reports if
   wanted.
3. Add the check's ping URL as the GitHub Actions repository secret
   `HEALTHCHECK_URL`.
4. Dispatch `Update calendar` manually and confirm the check records a ping.
5. Optionally add the same URL to `/etc/no-trains-refresh.env` on the driver
   so a dispatch that fails after all retries is reported immediately.

The URL is a credential and must not be committed. Keeping a copy in the
root-only local environment file is a deliberate trade-off: faster alerting
on driver-side failure against a second copy of the secret outside GitHub.

## Driver configuration

The source-controlled systemd units and installation procedure are under
`ops/`. The live machine stores its Actions-only GitHub token (and,
optionally, the heartbeat URL) in `/etc/no-trains-refresh.env`; the PTV
credentials remain GitHub Actions secrets only.
