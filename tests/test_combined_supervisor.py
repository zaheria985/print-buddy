"""Native lifecycle regressions: fake children only, no service or printer contact."""
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'docker/supervise.sh'


class CombinedSupervisorTests(unittest.TestCase):
    def environment(self, directory, child):
        fake = Path(directory, 'python3')
        # The supervisor probes the listener gate (#256) before starting
        # children; the fake answers it from PRINT_BUDDY_TEST_LISTENER_GATE
        # (default 0 = a Moonraker model, listener required) and runs the
        # child body for every other python3 invocation.
        gate = ('case "$*" in *print_buddy.listener_gate*) '
                'exit "${PRINT_BUDDY_TEST_LISTENER_GATE:-0}";; esac\n')
        fake.write_text('#!/bin/bash\n' + gate + child)
        fake.chmod(0o755)
        return {**os.environ, 'PATH': directory + ':' + os.environ['PATH'],
                'PRINT_BUDDY_BUNDLED_SPOOLMAN': 'false',
                'PRINT_BUDDY__SERVICES__SPOOLMAN_URL': 'http://external.invalid:8000',
                'DISCORD_BOT_TOKEN': ''}

    def test_explicit_command_is_executed_without_services(self):
        result = subprocess.run(['bash', str(SCRIPT), 'printf', 'offline-probe'],
                                capture_output=True, text=True, timeout=3)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, 'offline-probe')

    def test_even_clean_required_child_exit_is_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            env = self.environment(directory, 'exit 0\n')
            result = subprocess.run(['bash', str(SCRIPT)], env=env,
                                    capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('bot disabled', result.stdout)

    def test_child_failure_status_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            env = self.environment(directory, 'exit 7\n')
            result = subprocess.run(['bash', str(SCRIPT)], env=env,
                                    capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 7, result.stderr)

    def test_sigterm_exits_and_stops_child(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory, 'child-pid')
            env = self.environment(directory, f'echo $$ > {marker}\nexec sleep 60\n')
            process = subprocess.Popen(['bash', str(SCRIPT)], env=env,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                for _ in range(100):
                    if marker.exists():
                        break
                    time.sleep(0.02)
                self.assertTrue(marker.exists())
                pid = int(marker.read_text())
                process.send_signal(signal.SIGTERM)
                process.communicate(timeout=15)
                self.assertEqual(process.returncode, 0)
                with self.assertRaises(ProcessLookupError):
                    os.kill(pid, 0)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate()

    def test_listener_skipped_for_non_moonraker_connection(self):
        """Gate says no listener (#256): only the bot child runs, and its exit rules."""
        with tempfile.TemporaryDirectory() as directory:
            env = self.environment(directory, 'exit 5\n')
            env['PRINT_BUDDY_TEST_LISTENER_GATE'] = '3'
            env['DISCORD_BOT_TOKEN'] = 'dummy'
            result = subprocess.run(['bash', str(SCRIPT)], env=env,
                                    capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 5, result.stderr)
        self.assertIn('listener not started', result.stdout)

    def test_listener_gate_failure_stops_container(self):
        """An unresolved configuration stops the supervisor before any child starts."""
        with tempfile.TemporaryDirectory() as directory:
            env = self.environment(directory, 'exit 9\n')
            env['PRINT_BUDDY_TEST_LISTENER_GATE'] = '2'
            result = subprocess.run(['bash', str(SCRIPT)], env=env,
                                    capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn('Listener gate failed', result.stderr)

    def test_no_children_configuration_idles_until_terminated(self):
        """No listener, no bot token, external Spoolman: stay up, exit 0 on SIGTERM."""
        with tempfile.TemporaryDirectory() as directory:
            env = self.environment(directory, 'exit 9\n')
            env['PRINT_BUDDY_TEST_LISTENER_GATE'] = '3'
            process = subprocess.Popen(['bash', str(SCRIPT)], env=env,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                time.sleep(1.0)
                self.assertIsNone(process.poll())
                process.send_signal(signal.SIGTERM)
                stdout, _ = process.communicate(timeout=15)
                self.assertEqual(process.returncode, 0)
                self.assertIn('idling', stdout)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate()

    def test_disabled_backend_requires_external_url(self):
        with tempfile.TemporaryDirectory() as directory:
            env = self.environment(directory, 'exit 0\n')
            env['PRINT_BUDDY__SERVICES__SPOOLMAN_URL'] = 'http://127.0.0.1:8000'
            result = subprocess.run(['bash', str(SCRIPT)], env=env,
                                    capture_output=True, text=True, timeout=3)
        self.assertEqual(result.returncode, 2)
        self.assertIn('External Spoolman URL required', result.stderr)
