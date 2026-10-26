"""#256: the Moonraker listener runs only for connections that use one.

The decision comes from the resolved printer model (model -> vendor
connection), never from whether a Moonraker URL is filled in. Offline: the
registry and fake settings only, no printer or container contact.
"""
import contextlib
import importlib.util
import io
import os
import unittest
from pathlib import Path
from unittest import mock

from print_buddy import healthcheck, listener_gate, printers, settings

ROOT = Path(__file__).resolve().parents[1]


def data(model):
    return {"printer": {"model": model}}


class ListenerGateDecisionTests(unittest.TestCase):
    def test_moonraker_models_require_the_listener(self):
        for model in ("flashforge.ad5x", "klipper.generic", "sovol.sv06"):
            with self.subTest(model=model):
                self.assertTrue(healthcheck.listener_required(data(model)))

    def test_non_moonraker_models_do_not(self):
        # bambulab_lan and flashforge_stock forbid a Moonraker URL in settings;
        # octoprint never populates the listener's websocket URL. All three
        # SystemExited the listener before #256.
        for model in ("bambulab.a1", "bambulab.p1s", "flashforge_stock.ad5x",
                      "octoprint.marlin"):
            with self.subTest(model=model):
                self.assertFalse(healthcheck.listener_required(data(model)))

    def test_every_registered_model_is_classified_by_its_connection(self):
        for key, model in printers.MODELS.items():
            with self.subTest(model=key):
                self.assertEqual(healthcheck.listener_required(data(key)),
                                 model.vendor.connection == "moonraker")


class ListenerGateCliTests(unittest.TestCase):
    def run_gate(self, connection):
        with mock.patch.object(healthcheck, "listener_connection",
                               return_value=connection):
            return listener_gate.main()

    def test_moonraker_connection_starts_the_listener(self):
        self.assertEqual(self.run_gate("moonraker"), 0)

    def test_other_connections_skip_the_listener(self):
        for connection in ("bambulab_lan", "flashforge_stock", "octoprint", "offline"):
            with self.subTest(connection=connection):
                self.assertEqual(self.run_gate(connection), 3)

    def test_unresolved_configuration_is_a_distinct_failure(self):
        with mock.patch.object(healthcheck, "listener_connection",
                               side_effect=settings.SettingsError("bad model")):
            self.assertEqual(listener_gate.main(), 2)


def load_docker_healthcheck():
    spec = importlib.util.spec_from_file_location(
        "pb_docker_healthcheck", ROOT / "docker" / "healthcheck.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DockerHealthcheckTests(unittest.TestCase):
    """docker/healthcheck.py mirrors the supervisor's gate decision."""

    def run_main(self, module):
        response = mock.MagicMock()
        response.__enter__.return_value.status = 200
        env = {"DISCORD_BOT_TOKEN": "", "PRINT_BUDDY_BUNDLED_SPOOLMAN": "true"}
        out = io.StringIO()
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(module.urllib.request, "urlopen",
                                  return_value=response), \
                contextlib.redirect_stdout(out):
            code = module.main()
        return code, out.getvalue()

    def test_not_required_listener_is_ok_without_a_heartbeat(self):
        check = mock.Mock(side_effect=AssertionError("heartbeat must not be read"))
        module = load_docker_healthcheck()
        with mock.patch.object(module, "listener_required", return_value=False), \
                mock.patch.object(module, "listener_check", check):
            code, out = self.run_main(module)
        self.assertEqual(code, 0)
        self.assertIn("not required for this printer connection", out)

    def test_required_listener_states(self):
        for heartbeat_ok, expected in ((True, 0), (False, 1)):
            with self.subTest(heartbeat_ok=heartbeat_ok):
                module = load_docker_healthcheck()
                with mock.patch.object(module, "listener_required", return_value=True), \
                        mock.patch.object(module, "listener_check",
                                          return_value=(heartbeat_ok, "reason")):
                    code, _ = self.run_main(module)
                self.assertEqual(code, expected)

    def test_unresolved_configuration_fails(self):
        module = load_docker_healthcheck()
        with mock.patch.object(module, "listener_required",
                               side_effect=settings.SettingsError("bad model")):
            code, out = self.run_main(module)
        self.assertEqual(code, 1)
        self.assertIn("configuration unresolved", out)


if __name__ == "__main__":
    unittest.main()
