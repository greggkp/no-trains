# Monitoring

The refresh schedule is external to GitHub. A systemd timer on the driver
machine dispatches `update-calendar.yml` every six hours; GitHub Actions runs
the tests and generator and deploys the result to GitHub Pages.

## What is covered

Every dispatched workflow reports its own health:

- a hard failure in tests, generation, deployment, or the heartbeat request opens a
  `calendar-pipeline` issue;
- soft degradation from parser fallbacks, detail-page failures, or PTV API
  drift opens the same issue;
- the next clean run closes the issue.

The systemd service fails locally if GitHub rejects the dispatch because of a
network problem, expired token, disabled workflow, or permission change. Its
status and logs are available with:

```bash
systemctl status no-trains-refresh.timer no-trains-refresh.service
journalctl -u no-trains-refresh.service
```

## Remaining dead-man risk

The machine cannot report its own total failure. If it is powered off, its
timer stops, or nobody notices a failed local service, GitHub receives no
dispatch and therefore cannot open a tracking issue.

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

The URL is a credential and must not be committed or placed in the local
systemd environment file.

## Driver configuration

The source-controlled systemd units and installation procedure are under
`ops/`. The live machine stores its Actions-only GitHub token in
`/etc/no-trains-refresh.env`; the PTV credentials and optional heartbeat URL
remain GitHub Actions secrets.
