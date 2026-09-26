"""Pin the retry/report behaviour of ops/no-trains-refresh.sh.

The script is exercised with stub ``gh``, ``curl`` and ``timeout`` binaries on
PATH, so no network access or real credentials are involved (and the suite
runs on macOS, which has no ``timeout``).
"""

import os
import pathlib
import subprocess
import tempfile
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parent / "ops" / "no-trains-refresh.sh"
DISPATCH_ARGS = "workflow run update-calendar.yml --repo greggkp/no-trains --ref main"


class RefreshScriptTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.bin = pathlib.Path(tmp.name) / "bin"
        self.bin.mkdir()
        self.calls = pathlib.Path(tmp.name) / "calls.log"
        self.stub_timeout()

    def stub_timeout(self, hang_first=0):
        """timeout that logs its args, reports the first ``hang_first`` runs
        as timed out (exit 124, gh never runs), then runs the command."""
        self.stub(
            "timeout",
            f'echo "timeout $*" >> "$CALLS"\n'
            f'n=$(grep -c "^timeout " "$CALLS")\n'
            f'if [ "$n" -le {hang_first} ]; then exit 124; fi\n'
            f'while [ "${{1#-}}" != "$1" ]; do shift 2; done\n'
            f'shift\n'
            f'exec "$@"\n',
        )

    def stub(self, name, body):
        path = self.bin / name
        path.write_text("#!/bin/sh\n" + body)
        path.chmod(0o755)

    def stub_gh(self, failures_before_success):
        """gh that fails ``failures_before_success`` times, then succeeds."""
        self.stub(
            "gh",
            f'echo "gh $*" >> "$CALLS"\n'
            f'n=$(grep -c "^gh " "$CALLS")\n'
            f'if [ "$n" -gt {failures_before_success} ]; then\n'
            f'  echo "Created workflow_dispatch event"; exit 0\n'
            f'fi\n'
            f'echo "error: connect: no route to host" >&2; exit 1\n',
        )

    def stub_curl(self):
        self.stub("curl", 'echo "curl $*" >> "$CALLS"\nexit 0\n')

    def run_script(self, **env):
        full = {
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "CALLS": str(self.calls),
            "REFRESH_ATTEMPTS": "3",
            "REFRESH_RETRY_DELAY": "0",
            "GH_TOKEN": "stub",
        }
        full.update(env)
        return subprocess.run(
            ["/bin/sh", str(SCRIPT)], env=full, capture_output=True, text=True
        )

    def logged(self, prefix):
        if not self.calls.exists():
            return []
        return [l for l in self.calls.read_text().splitlines() if l.startswith(prefix)]

    def test_first_attempt_succeeds(self):
        self.stub_gh(0)
        self.stub_curl()
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.logged("gh "), [f"gh {DISPATCH_ARGS}"])
        self.assertEqual(self.logged("curl "), [])
        self.assertIn("dispatched on attempt 1 of 3", result.stdout)

    def test_transient_failure_is_retried(self):
        self.stub_gh(2)
        self.stub_curl()
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.logged("gh ")), 3)
        self.assertEqual(self.logged("curl "), [])
        self.assertIn("attempt 1 of 3 failed", result.stderr)
        self.assertIn("dispatched on attempt 3 of 3", result.stdout)

    def test_exhausted_retries_fail_without_report_when_unconfigured(self):
        self.stub_gh(99)
        self.stub_curl()
        result = self.run_script()
        self.assertEqual(result.returncode, 1)
        self.assertEqual(len(self.logged("gh ")), 3)
        self.assertEqual(self.logged("curl "), [])
        self.assertIn("failed after 3 attempts", result.stderr)
        self.assertIn("HEALTHCHECK_URL not set", result.stderr)

    def test_exhausted_retries_report_to_healthchecks(self):
        self.stub_gh(99)
        self.stub_curl()
        result = self.run_script(HEALTHCHECK_URL="https://hc.example/abc123/")
        self.assertEqual(result.returncode, 1)
        curl_calls = self.logged("curl ")
        self.assertEqual(len(curl_calls), 1)
        self.assertIn("https://hc.example/abc123/fail", curl_calls[0])
        self.assertNotIn("abc123//fail", curl_calls[0])
        # The last gh error travels in the ping body for diagnosis.
        self.assertIn("no route to host", curl_calls[0])
        self.assertIn("failure reported to Healthchecks.io", result.stderr)

    def test_dispatch_is_capped_by_timeout(self):
        self.stub_gh(0)
        self.stub_curl()
        result = self.run_script(REFRESH_DISPATCH_TIMEOUT="45")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.logged("timeout "), [f"timeout -k 10 45 gh {DISPATCH_ARGS}"])

    def test_hung_dispatch_is_retried(self):
        self.stub_timeout(hang_first=1)
        self.stub_gh(0)
        self.stub_curl()
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("gh timed out after 60s", result.stderr)
        self.assertIn("dispatched on attempt 2 of 3", result.stdout)

    def test_repeated_hangs_are_reported(self):
        self.stub_timeout(hang_first=99)
        self.stub_gh(0)
        self.stub_curl()
        result = self.run_script(HEALTHCHECK_URL="https://hc.example/abc123")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.logged("gh "), [])
        curl_calls = self.logged("curl ")
        self.assertEqual(len(curl_calls), 1)
        self.assertIn("gh timed out after 60s", curl_calls[0])

    def test_report_failure_is_logged_not_fatal_to_diagnosis(self):
        self.stub_gh(99)
        self.stub("curl", 'echo "curl $*" >> "$CALLS"\nexit 22\n')
        result = self.run_script(HEALTHCHECK_URL="https://hc.example/abc123")
        self.assertEqual(result.returncode, 1)
        self.assertIn("also failed", result.stderr)


if __name__ == "__main__":
    unittest.main()
