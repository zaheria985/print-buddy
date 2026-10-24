"""Issue #117 packaging regressions, runnable without a container engine."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class DockerReleaseContract(unittest.TestCase):
    def test_standalone_is_default_runtime(self):
        dockerfile = (ROOT / "Dockerfile").read_text()
        self.assertIn('"print_buddy.bot"', dockerfile)
        self.assertIn("PRINT_BUDDY__BOT__MODE=standalone", dockerfile)

    def test_docker_examples_use_source_installed_slicer(self):
        for suffix in ("yaml", "toml"):
            with self.subTest(format=suffix):
                config = (ROOT / "config" / f"docker.{suffix}").read_text()
                self.assertIn("/usr/bin/orca-slicer", config)
                self.assertNotIn("/opt/orcaslicer/AppRun", config)

    def test_separate_services_and_external_spoolman_default(self):
        import yaml

        compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
        services = compose["services"]
        self.assertIn("bot", services)
        self.assertIn("listener", services)
        self.assertNotIn("spoolman", services)
        for name, module in (("bot", "print_buddy.bot"),
                             ("listener", "print_buddy.hermes.moonraker_event_listener")):
            with self.subTest(service=name):
                service = services[name]
                self.assertEqual(service["command"], ["python3", "-m", module])
                self.assertIn("healthcheck", service)
                self.assertFalse(service.get("depends_on"))
                self.assertFalse(any("/app" in v for v in service["volumes"]))

    def test_configuration_parity_channels_and_packaged_plugins(self):
        import yaml
        import tomllib
        from print_buddy import settings

        configs = [settings.load(local=ROOT / "config" / f"docker.{suffix}", environ={})
                   for suffix in ("yaml", "toml")]
        self.assertEqual(configs[0], configs[1])
        self.assertEqual(configs[0]["services"]["spoolman_url"], "")
        dockerfile = (ROOT / "Dockerfile").read_text()
        self.assertIn("ARG APP_CHANNEL=development", dockerfile)
        ignored = (ROOT / ".dockerignore").read_text().splitlines()
        self.assertNotIn("plugins", ignored)
        self.assertIn(".env", ignored)
        self.assertTrue((ROOT / "plugins" / "print-feedback").is_dir())
        extra = yaml.safe_load((ROOT / "docker-compose.spoolman.yml").read_text())
        self.assertIn("spoolman", extra["services"])
        self.assertNotIn("ports", extra["services"]["spoolman"])

    def test_orca_is_optional_and_built_from_source(self):
        dockerfile = (ROOT / "Dockerfile").read_text()
        self.assertIn("ARG WITH_ORCA=", dockerfile)
        self.assertIn("ARG ORCA_CHANNEL=", dockerfile)
        self.assertNotIn(".AppImage", dockerfile)
        self.assertIn("cmake --build", dockerfile)


if __name__ == "__main__":
    unittest.main()
