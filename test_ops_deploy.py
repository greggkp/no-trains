"""Pin the check/apply behaviour of ops/deploy.sh.

The script runs from a throwaway git repo holding a copy of ops/, against a
stub ``ssh`` that executes the remote command locally with absolute paths
redirected into a fake driver root. ``sudo``, ``install`` (macOS's lacks -D)
and ``systemctl`` are stubbed on the "remote" PATH, so nothing touches a real
host.
"""

import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

OPS = pathlib.Path(__file__).resolve().parent / "ops"
INSTALLED = {
    "no-trains-refresh.sh": "usr/local/bin/no-trains-refresh",
    "no-trains-refresh.service": "etc/systemd/system/no-trains-refresh.service",
    "no-trains-refresh.timer": "etc/systemd/system/no-trains-refresh.timer",
    "journald-persistent.conf":
        "etc/systemd/journald.conf.d/50-no-trains-persistent.conf",
}


def git(repo, *args):
    subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=test@example.test",
         *args],
        cwd=repo, check=True, capture_output=True,
    )


class DeployScriptTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = pathlib.Path(tmp.name)
        self.calls = base / "calls.log"
        self.root = base / "driver"
        self.repo = base / "repo"
        shutil.copytree(OPS, self.repo / "ops")
        git(self.repo, "init", "-q")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-q", "-m", "ops")
        git(self.repo, "update-ref", "refs/remotes/origin/main", "HEAD")

        self.bin = base / "bin"
        self.remote_bin = base / "remote-bin"
        self.bin.mkdir()
        self.remote_bin.mkdir()
        self.stub(self.bin / "ssh",
                  'while [ "${1#-}" != "$1" ]; do\n'
                  '  case "$1" in -o) shift 2 ;; *) shift ;; esac\n'
                  'done\n'
                  'host=$1; shift\n'
                  'echo "ssh $host $*" >> "$CALLS"\n'
                  '[ -n "${SSH_DOWN:-}" ] && exit 255\n'
                  'cmd=$(printf %s "$*" | sed "s#\'/#\'$ROOT/#g")\n'
                  'PATH="$REMOTE_BIN:$PATH" exec sh -c "$cmd"\n')
        self.stub(self.remote_bin / "sudo",
                  '[ "$1" = -n ] && shift\nexec "$@"\n')
        self.stub(self.remote_bin / "systemctl",
                  'echo "systemctl $*" >> "$CALLS"\n')
        self.stub(self.remote_bin / "install",
                  'while [ "${1#-}" != "$1" ]; do\n'
                  '  case "$1" in -m) mode=$2; shift 2 ;; *) shift ;; esac\n'
                  'done\n'
                  'mkdir -p "$(dirname "$2")" && cp "$1" "$2" && chmod "$mode" "$2"\n')

    def stub(self, path, body):
        path.write_text("#!/bin/sh\n" + body)
        path.chmod(0o755)

    def install_all(self):
        for src, dest in INSTALLED.items():
            target = self.root / dest
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(OPS / src, target)

    def run_deploy(self, *args, **env):
        full = {
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "CALLS": str(self.calls),
            "ROOT": str(self.root),
            "REMOTE_BIN": str(self.remote_bin),
            "HOME": os.environ.get("HOME", "/tmp"),
        }
        full.update(env)
        return subprocess.run(
            ["/bin/sh", str(self.repo / "ops" / "deploy.sh"), *args],
            env=full, capture_output=True, text=True,
        )

    def logged(self, prefix):
        if not self.calls.exists():
            return []
        return [l for l in self.calls.read_text().splitlines() if l.startswith(prefix)]

    def test_up_to_date(self):
        self.install_all()
        result = self.run_deploy()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("raspberrypi is up to date", result.stdout)
        self.assertEqual(result.stdout.count("ok "), 4)

    def test_check_reports_drift_without_changing_anything(self):
        self.install_all()
        (self.root / INSTALLED["no-trains-refresh.sh"]).write_text("old\n")
        result = self.run_deploy("--check")
        self.assertEqual(result.returncode, 1)
        self.assertIn("differs  /usr/local/bin/no-trains-refresh", result.stdout)
        self.assertIn("--apply", result.stderr)
        self.assertEqual(
            (self.root / INSTALLED["no-trains-refresh.sh"]).read_text(), "old\n"
        )

    def test_apply_script_only_needs_no_reload(self):
        self.install_all()
        (self.root / INSTALLED["no-trains-refresh.sh"]).write_text("old\n")
        result = self.run_deploy("--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        installed = self.root / INSTALLED["no-trains-refresh.sh"]
        self.assertEqual(installed.read_bytes(),
                         (OPS / "no-trains-refresh.sh").read_bytes())
        self.assertEqual(installed.stat().st_mode & 0o777, 0o755)
        self.assertEqual(self.logged("systemctl "), [])
        self.assertIn("raspberrypi is up to date", result.stdout)

    def test_apply_timer_reloads_and_restarts_timer(self):
        self.install_all()
        (self.root / INSTALLED["no-trains-refresh.timer"]).write_text("old\n")
        result = self.run_deploy("--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.logged("systemctl "), [
            "systemctl daemon-reload",
            "systemctl restart no-trains-refresh.timer",
        ])

    def test_apply_service_reloads_only(self):
        self.install_all()
        (self.root / INSTALLED["no-trains-refresh.service"]).write_text("old\n")
        result = self.run_deploy("--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.logged("systemctl "), ["systemctl daemon-reload"])

    def test_apply_journald_restarts_journald(self):
        self.install_all()
        (self.root / INSTALLED["journald-persistent.conf"]).unlink()
        result = self.run_deploy("--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.root / INSTALLED["journald-persistent.conf"]).exists())
        self.assertEqual(self.logged("systemctl "),
                         ["systemctl restart systemd-journald"])

    def test_fresh_driver_gets_everything(self):
        result = self.run_deploy("--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        for src, dest in INSTALLED.items():
            self.assertEqual((self.root / dest).read_bytes(),
                             (OPS / src).read_bytes())

    def test_apply_refuses_uncommitted_changes(self):
        (self.repo / "ops" / "no-trains-refresh.sh").write_text("wip\n")
        result = self.run_deploy("--apply")
        self.assertEqual(result.returncode, 1)
        self.assertIn("uncommitted", result.stderr)
        self.assertEqual(self.logged("ssh "), [])

    def test_apply_refuses_unmerged_changes(self):
        (self.repo / "ops" / "no-trains-refresh.sh").write_text("branch\n")
        git(self.repo, "commit", "-qam", "unmerged")
        result = self.run_deploy("--apply")
        self.assertEqual(result.returncode, 1)
        self.assertIn("differs from origin/main", result.stderr)
        self.assertEqual(self.logged("ssh "), [])

    def test_unreachable_host(self):
        result = self.run_deploy(SSH_DOWN="1", NO_TRAINS_HOST="pi2")
        self.assertEqual(result.returncode, 1)
        self.assertIn("cannot reach pi2", result.stderr)

    def test_bad_argument(self):
        self.assertEqual(self.run_deploy("--yolo").returncode, 2)


if __name__ == "__main__":
    unittest.main()
