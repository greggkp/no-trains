#!/bin/sh
# Check or update the installed copies of ops/ on the driver.
#
#   ops/deploy.sh            report which installed files differ from ops/
#   ops/deploy.sh --apply    install the ones that differ, reload what needs it
#
# Runs on the development machine, not the driver: it reaches the driver over
# SSH (NO_TRAINS_HOST, default raspberrypi) and needs passwordless sudo there.
# --apply refuses uncommitted or unmerged ops/ changes, so the driver only
# ever runs what is on main. Exits non-zero while anything still differs.
set -u

host="${NO_TRAINS_HOST:-raspberrypi}"
ops_dir=$(cd "$(dirname "$0")" && pwd)

# source|installed path|mode, one per line.
FILES="no-trains-refresh.sh|/usr/local/bin/no-trains-refresh|755
no-trains-refresh.service|/etc/systemd/system/no-trains-refresh.service|644
no-trains-refresh.timer|/etc/systemd/system/no-trains-refresh.timer|644
journald-persistent.conf|/etc/systemd/journald.conf.d/50-no-trains-persistent.conf|644"

remote() {
    ssh -o BatchMode=yes -o ConnectTimeout=10 "$host" "$@"
}

case "${1:---check}" in
    --check) apply=false ;;
    --apply) apply=true ;;
    *) echo "usage: $0 [--check|--apply]" >&2; exit 2 ;;
esac

if $apply; then
    cd "$ops_dir" || exit 1
    if [ -n "$(git status --porcelain -- .)" ]; then
        echo "refusing to deploy: uncommitted changes in ops/" >&2
        exit 1
    fi
    git fetch -q origin main 2>/dev/null \
        || echo "warning: could not fetch origin/main; using the last fetched copy" >&2
    if ! git diff --quiet origin/main HEAD -- .; then
        echo "refusing to deploy: ops/ differs from origin/main; merge first" >&2
        exit 1
    fi
fi

if ! remote true </dev/null; then
    echo "cannot reach $host over SSH" >&2
    exit 1
fi

tmp=$(mktemp)
trap 'rm -f "$tmp"' EXIT

changed=""
old_ifs=$IFS
IFS='
'
for entry in $FILES; do
    IFS='|' read -r src dest mode <<EOF
$entry
EOF
    if remote "cat '$dest'" >"$tmp" 2>/dev/null </dev/null \
        && cmp -s "$tmp" "$ops_dir/$src"; then
        echo "ok       $dest"
    else
        echo "differs  $dest"
        changed="$changed$entry
"
    fi
done
IFS=$old_ifs

if [ -z "$changed" ]; then
    echo "$host is up to date"
    exit 0
fi
if ! $apply; then
    echo "run $0 --apply to install" >&2
    exit 1
fi

reload=false
restart_timer=false
restart_journald=false
IFS='
'
for entry in $changed; do
    IFS='|' read -r src dest mode <<EOF
$entry
EOF
    # Stream the file over SSH into a remote temp file, then install it.
    if ! remote "t=\$(mktemp) && cat >\"\$t\" && sudo -n install -D -m $mode \"\$t\" '$dest'; rc=\$?; rm -f \"\$t\"; exit \$rc" \
        <"$ops_dir/$src"; then
        echo "failed to install $dest" >&2
        exit 1
    fi
    echo "installed $dest"
    case "$src" in
        *.service) reload=true ;;
        *.timer) reload=true; restart_timer=true ;;
        journald-*) restart_journald=true ;;
    esac
done
IFS=$old_ifs

if $reload; then
    remote "sudo -n systemctl daemon-reload" </dev/null || exit 1
    echo "reloaded systemd units"
fi
if $restart_timer; then
    remote "sudo -n systemctl restart no-trains-refresh.timer" </dev/null || exit 1
    echo "restarted no-trains-refresh.timer"
fi
if $restart_journald; then
    remote "sudo -n systemctl restart systemd-journald" </dev/null || exit 1
    echo "restarted systemd-journald"
fi

echo "verifying..."
exec sh "$0" --check
