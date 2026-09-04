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

The PTV credentials stay in GitHub Actions secrets; this token can only request
a workflow run.

## Install and verify

```bash
sudo install -m 644 ops/no-trains-refresh.service /etc/systemd/system/
sudo install -m 644 ops/no-trains-refresh.timer /etc/systemd/system/
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

`Persistent=true` causes one catch-up dispatch after boot if a scheduled time
was missed while the machine was off.
