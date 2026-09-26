#!/bin/sh
# Dispatch the "Update calendar" workflow, retrying transient failures, and
# report a final failure straight to Healthchecks.io when HEALTHCHECK_URL is
# set. Run by no-trains-refresh.service; GH_TOKEN (and optionally
# HEALTHCHECK_URL) arrive from /etc/no-trains-refresh.env.
#
# Retries live here rather than in systemd's Restart= because OnFailure=
# fires on every failed attempt, not only once retries are exhausted, so a
# unit-level handler would page on the first transient blip.
#
# Each dispatch is capped by `timeout`: a hung gh would otherwise sit until
# systemd's TimeoutStartSec killed the whole script, before it could report
# the failure. Worst case (5 x 60s hangs + 4 x 300s waits) stays well inside
# the service's 30-minute limit.
set -u

attempts="${REFRESH_ATTEMPTS:-5}"
delay="${REFRESH_RETRY_DELAY:-300}"
dispatch_timeout="${REFRESH_DISPATCH_TIMEOUT:-60}"

attempt=1
while :; do
    output=$(timeout -k 10 "$dispatch_timeout" \
        gh workflow run update-calendar.yml --repo greggkp/no-trains --ref main 2>&1)
    status=$?
    if [ "$status" -eq 0 ]; then
        printf '%s\n' "$output"
        echo "dispatched on attempt $attempt of $attempts"
        exit 0
    fi
    if [ "$status" -eq 124 ] || [ "$status" -eq 137 ]; then
        output="gh timed out after ${dispatch_timeout}s${output:+: $output}"
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
