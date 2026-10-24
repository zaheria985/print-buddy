"""Offline process-identity and stale-state regressions; no network or printer."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from print_buddy import healthcheck as health


class ListenerHealthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "listener_health.json"
        patch = mock.patch.object(health, "heartbeat_path", return_value=self.path)
        patch.start()
        self.addCleanup(patch.stop)

    def test_own_process_healthy_without_orca_or_printer(self):
        health.beat(moonraker_connected=False)
        self.assertTrue(health.check()[0])

    def test_missing_stale_malformed_foreign_and_reused_pid_fail(self):
        self.assertFalse(health.check()[0])
        record = health.beat()
        for changes in ({"time": 0}, {"time": float("nan")}, {"pid": 0},
                        {"pid": "invalid"}, {"process_start": "reused"},
                        {"runtime": "bot"}, {"host": "other-container"}):
            with self.subTest(changes=changes):
                self.path.write_text(json.dumps({**record, **changes}))
                self.assertFalse(health.check()[0])
        for value in ("[]", "null", "broken"):
            self.path.write_text(value)
            self.assertFalse(health.check()[0])

    def test_listener_cancels_heartbeat_on_shutdown(self):
        import asyncio
        from print_buddy.hermes import moonraker_event_listener as mel

        async def flow():
            listener = mel.Listener.__new__(mel.Listener)
            listener.state = "offline"
            started = asyncio.Event()
            stopped = asyncio.Event()

            async def heartbeat():
                started.set()
                try:
                    await asyncio.Future()
                finally:
                    stopped.set()

            async def session():
                await asyncio.Future()

            listener.heartbeat = heartbeat
            listener.session = session
            task = asyncio.create_task(listener.forever())
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(stopped.is_set())
            self.assertFalse(listener.moonraker_connected)

        asyncio.run(flow())

    def test_current_linux_process_identity(self):
        self.assertTrue(health.process_identity(os.getpid()).isdigit())
