#!/bin/sh
# Dispatch the "Update calendar" workflow, retrying transient failures, and
# report a final failure straight to Healthchecks.io when HEALTHCHECK_URL is
# set. Run by no-trains-refresh.service; GH_TOKEN (and optionally
# HEALTHCHECK_URL) arrive from /etc/no-trains-refresh.env.
#
# Retries live here rather than in systemd's Restart= because OnFailure=
# fires on every failed attempt, not only once retries are exhausted, so a
# unit-level handler would page on the first transient blip.
set -u

attempts="${REFRESH_ATTEMPTS:-5}"
delay="${REFRESH_RETRY_DELAY:-300}"

attempt=1
while :; do
    if output=$(gh workflow run update-calendar.yml --repo greggkp/no-trains --ref main 2>&1); then
        printf '%s\n' "$output"
        echo "dispatched on attempt $attempt of $attempts"
        exit 0
    fi
    printf '%s\n' "$output" >&2
    if [ "$attempt" -ge "$attempts" ]; then
        break
    fi
    echo "dispatch attempt $attempt of $attempts failed; retrying in ${delay}s" >&2
    attempt=$((attempt + 1))
    sleep "$delay"
done

echo "dispatch failed after $attempts attempts" >&2
if [ -n "${HEALTHCHECK_URL:-}" ]; then
    if curl --fail --silent --show-error --max-time 10 --retry 3 \
        --data "no-trains-refresh on $(hostname): dispatch failed after $attempts attempts. Last error: $output" \
        "${HEALTHCHECK_URL%/}/fail"; then
        echo "failure reported to Healthchecks.io" >&2
    else
        echo "failure report to Healthchecks.io also failed" >&2
    fi
else
    echo "HEALTHCHECK_URL not set; failure not reported to Healthchecks.io" >&2
fi
exit 1
