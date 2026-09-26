# External refresh driver

The repository has no GitHub-owned refresh schedule. This machine dispatches
the `Update calendar` workflow every six hours; GitHub Actions remains the
worker and GitHub Pages remains the publisher.

## Authentication

Create a fine-grained personal access token restricted to `greggkp/no-trains`
with **Actions: write** repository permission. Store it outside the repository:

```bash
sudo install -m 600 -o root -g root /dev/null /etc/no-trains-refresh.env
sudoedit /etc/no-trains-refresh.env
```

The file must contain:

```ini
GH_TOKEN=github_pat_REPLACE_ME
```

Optionally add the Healthchecks.io ping URL as well. With it present, a
dispatch that still fails after all retries is reported to Healthchecks.io
immediately instead of waiting for the missed-ping grace period:

```ini
HEALTHCHECK_URL=https://hc-ping.com/REPLACE_ME
```

The PTV credentials stay in GitHub Actions secrets; this token can only request
a workflow run.

## Install and verify

```bash
sudo install -m 755 ops/no-trains-refresh.sh /usr/local/bin/no-trains-refresh
sudo install -m 644 ops/no-trains-refresh.service /etc/systemd/system/
sudo install -m 644 ops/no-trains-refresh.timer /etc/systemd/system/
sudo install -m 644 -D ops/journald-persistent.conf \
    /etc/systemd/journald.conf.d/50-no-trains-persistent.conf
sudo systemctl restart systemd-journald
sudo journalctl --flush
sudo systemctl daemon-reload
sudo systemctl start no-trains-refresh.service
sudo systemctl status no-trains-refresh.service
sudo systemctl enable --now no-trains-refresh.timer
systemctl list-timers no-trains-refresh.timer
```

The first manual start verifies authentication before the timer is enabled.
Inspect failures with:

```bash
journalctl -u no-trains-refresh.service
```

`no-trains-refresh.sh` makes up to five dispatch attempts five minutes apart
(`REFRESH_ATTEMPTS` / `REFRESH_RETRY_DELAY` in the service unit) before the
service fails. `test_ops_refresh.py` in the repository root pins that
behaviour with stub `gh` and `curl` binaries.

## Updating the installed copy

The driver runs the installed copies under `/usr/local/bin` and `/etc/systemd/`,
not this checkout, so a merged change to `ops/` does nothing until it is
reinstalled on the driver. From a checkout on the development machine, after
the change is merged and pulled:

```bash
ops/deploy.sh            # which installed files differ from ops/? (read-only)
ops/deploy.sh --apply    # install those, reload/restart what needs it, re-check
```

`deploy.sh` reaches the driver over SSH (`NO_TRAINS_HOST`, default
`raspberrypi`) and needs passwordless `sudo` there. It installs only the files
that differ; a changed unit triggers `daemon-reload` (plus a timer restart for
the timer), and a changed journald drop-in restarts `systemd-journald`.
`--apply` refuses uncommitted or unmerged `ops/` changes, so the driver only
runs what is on `main`. `test_ops_deploy.py` pins this with a stub `ssh`.

Changes to `generate_ics.py` or the workflow need no step on the driver:
GitHub Actions runs them from `main`.

`Persistent=true` causes one catch-up dispatch after boot if a scheduled time
was missed while the machine was off.

The journald drop-in overrides Raspberry Pi OS's default RAM-only journal so
`journalctl` still has the story after a reboot. Confirm it took effect with
`journalctl --list-boots`, which should show more than the current boot after
the next restart.
