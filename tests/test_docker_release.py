"""Issue #117 release residue on top of main's Docker contract (test_docker_release_contract.py).

Settings parity: every user-adjustable setting in the schema has a docs/SETTINGS.md row and, when it
is a scalar, an .env.example line; every PRINT_BUDDY__ name in a deployment file is a real setting.
Sanitization: public deployment files carry no LAN addresses, machine paths, IDs or credentials.
Build: bounded, validated Orca parallelism and a cache keyed so channels, revisions, architectures
and option profiles never mix. Unraid: both templates match the Compose runtime contract.
"""
import ipaddress
import json
import os
import re
import shlex
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

import yaml

from print_buddy import printers, settings

ROOT = settings.ROOT
DOCS = (ROOT / "docs" / "SETTINGS.md").read_text()
ENV_EXAMPLE = (ROOT / ".env.example").read_text()
DOCKERFILE = (ROOT / "Dockerfile").read_text()
COMPOSE = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
JOBS = ROOT / "docker" / "orca-build-jobs.sh"
UNRAID = {name: ET.parse(ROOT / "docker" / "unraid" / f"print-buddy-{name}.xml").getroot()
          for name in ("bot", "listener")}
# Public files a user copies or reads to deploy. All must be sanitized.
DEPLOYMENT_FILES = [".env.example", "docker-compose.yml", "docker-compose.spoolman.yml", "Dockerfile",
                    "docker/entrypoint.sh", "docker/orca-build-jobs.sh", "docker/unraid/print-buddy-bot.xml",
                    "docker/unraid/print-buddy-listener.xml", "config/docker.yaml", "config/docker.toml",
                    "docs/DOCKER.md", "docs/SETTINGS.md", "tests/docker/compose.acceptance.yml"]
SECRETS = {"DISCORD_BOT_TOKEN", "LLM_API_KEY", "OCTOPRINT_API_KEY", "MOONRAKER_API_KEY", "CLIPROXY_API_KEY",
           "OPENVIKING_API_KEY"}
COMPOSE_VARS = {"PRINT_BUDDY_IMAGE", "PRINT_BUDDY_DATA", "PRINT_BUDDY_CONFIG"}
DATA_TARGETS = {"/data/hermes", "/data/archive", "/data/records", "/data/cache"}


def inventory():
    """{(section, key): example value} for every user-adjustable setting; open sections use key None."""
    home = settings._read(settings.HOME)
    found = {}
    for model in printers.MODELS.values():
        for section, values in settings._merge(model.defaults(), home).items():
            for key, value in values.items():
                found[(section, None if section in settings.OPEN_SECTIONS else key)] = value
    for (section, key), kind in settings.OPTIONAL_KEYS.items():
        found[(section, key)] = kind()
    return found


def env_name(section, key):
    return f"PRINT_BUDDY__{section.upper()}__{key.upper()}"


def is_scalar(value):
    return not isinstance(value, (list, dict))


def table(heading):
    """{first column: second column} of the markdown table under a docs/SETTINGS.md heading."""
    text = DOCS.split(f"## {heading}\n", 1)[1].split("\n## ", 1)[0]
    return dict(re.findall(r"^\| `([A-Z0-9_]+)` \| ([^|]+) \|", text, re.M))


class SettingsParityTests(unittest.TestCase):
    def setUp(self):
        self.inventory = inventory()
        self.rows = dict(re.findall(r"^\| `([a-z_]+\.(?:[a-z_]+|<[a-zA-Z]+>))` \| ([^|]+) \|", DOCS, re.M))

    def test_schema_includes_the_standalone_and_vendor_settings(self):
        for setting in [("bot", "mode"), ("bot", "health_port"), ("llm", "chat_url"), ("discord", "token_env"),
                        ("discord", "allowed_user_ids"), ("printer", "vendor"), ("printer", "slicer"),
                        ("delivery", "methods")]:
            self.assertIn(setting, self.inventory)

    def test_every_setting_has_a_reference_row_with_its_env_name(self):
        for (section, key), value in self.inventory.items():
            with self.subTest(setting=f"{section}.{key}"):
                if key is None:
                    self.assertTrue(any(name.startswith(f"{section}.<") for name in self.rows), section)
                    continue
                self.assertIn(f"{section}.{key}", self.rows)
                expected = f"`{env_name(section, key)}`" if is_scalar(value) else "file only"
                self.assertEqual(self.rows[f"{section}.{key}"].strip(), expected)

    def test_every_reference_row_is_a_real_setting(self):
        for name in self.rows:
            section, key = name.split(".")
            with self.subTest(row=name):
                self.assertIn((section, None if key.startswith("<") else key), self.inventory)

    def test_env_example_lists_every_scalar_setting(self):
        for (section, key), value in self.inventory.items():
            if key is not None and is_scalar(value):
                with self.subTest(setting=f"{section}.{key}"):
                    self.assertRegex(ENV_EXAMPLE, rf"(?m)^#? ?{env_name(section, key)}=")

    def test_every_documented_env_name_is_accepted_by_the_loader(self):
        names = set()
        for path in DEPLOYMENT_FILES:
            names |= set(re.findall(r"PRINT_BUDDY__[A-Z0-9_]+__[A-Z0-9_]+", (ROOT / path).read_text()))
        names -= {"PRINT_BUDDY__SECTION__KEY", "PRINT_BUDDY__NOZZLE_TEMP_OFFSETS__TYPE"}
        self.assertIn("PRINT_BUDDY__SERVICES__MOONRAKER_URL", names)
        for name in sorted(names):
            section, key = name[len("PRINT_BUDDY__"):].lower().split("__")
            with self.subTest(env=name):
                self.assertTrue((section, key) in self.inventory or section in settings.OPEN_SECTIONS, name)

    def test_every_commented_example_value_loads_when_uncommented(self):
        """Integer spelling, booleans and URLs in .env.example are values the loader accepts."""
        environ = {}
        for line in ENV_EXAMPLE.splitlines():
            match = re.match(r"^#? ?(PRINT_BUDDY__[A-Z0-9_]+)=(.+)$", line)
            if match and "example" not in match.group(2) and match.group(1) != "PRINT_BUDDY__PRINTER__MODEL":
                environ[match.group(1)] = match.group(2)
        self.assertGreater(len(environ), 40)
        data = settings.load(local=ROOT / "config" / "docker.yaml", environ=environ)
        self.assertEqual(data["bot"]["mode"], "standalone")

    def test_compose_build_and_secret_variables_are_documented(self):
        self.assertEqual(set(table("Compose variables")), COMPOSE_VARS)
        self.assertEqual(set(table("Secrets")), SECRETS)
        args = set(re.findall(r"(?m)^ARG ([A-Z_]+)", DOCKERFILE)) - {"TARGETARCH"}
        self.assertEqual(set(table("Build arguments")), args)
        for name, default in re.findall(r"(?m)^ARG ([A-Z_]+)=(\S+)$", DOCKERFILE):
            with self.subTest(arg=name):
                self.assertEqual(table("Build arguments")[name].strip(), f"`{default}`")
        for name in SECRETS:
            with self.subTest(secret=name):
                self.assertRegex(ENV_EXAMPLE, rf"(?m)^#? ?{name}=$")


class SanitizedExamplesTests(unittest.TestCase):
    def test_no_lan_addresses_machine_paths_ids_or_credentials(self):
        for path in DEPLOYMENT_FILES:
            text = (ROOT / path).read_text()
            with self.subTest(file=path):
                for raw in re.findall(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?![\d.])", text):
                    address = ipaddress.ip_address(raw)
                    self.assertFalse(address.is_private and not address.is_loopback, f"{path}: LAN address {raw}")
                self.assertIsNone(re.search(r"/(home|Users)/(?!app/)[a-z]", text), f"{path}: machine path")
                self.assertIsNone(re.search(r"/mnt/user/(?!appdata/)", text), f"{path}: personal share")
                self.assertIsNone(re.search(r"(?<![0-9a-f])(?!100000000000000001|200000000000000002)\d{17,20}(?![0-9a-f])",
                                            text), f"{path}: Discord ID")
                self.assertIsNone(re.search(r"[MN][A-Za-z\d]{23,}\.[\w-]{6}\.[\w-]{27,}|gh[pousr]_[A-Za-z0-9]{20,}"
                                            r"|sk-[A-Za-z0-9]{20,}", text), f"{path}: credential")
                for host in re.findall(r"https?://([A-Za-z0-9.-]+)", text):
                    self.assertFalse(host.endswith((".lan", ".local", ".home", ".internal")), f"{path}: private host {host}")

    def test_required_secrets_are_empty_in_the_example(self):
        for line in ENV_EXAMPLE.splitlines():
            name = line.lstrip("# ").split("=", 1)[0]
            if name in SECRETS:
                with self.subTest(secret=name):
                    self.assertEqual(line.split("=", 1)[1], "")


class OrcaBuildJobsTests(unittest.TestCase):
    """docker/orca-build-jobs.sh: bounded, memory-aware, validated."""

    def run_jobs(self, request, nproc=8, mem_kib=None, cpu_max=None, memory_max=None, per_job=None):
        with tempfile.TemporaryDirectory() as root:
            proc = Path(root, "proc")
            proc.mkdir()
            (proc / "meminfo").write_text(f"MemTotal: 99999999 kB\nMemAvailable: {mem_kib or 64 * 1024 * 1024} kB\n")
            cgroup = Path(root, "sys", "fs", "cgroup")
            cgroup.mkdir(parents=True)
            if cpu_max:
                (cgroup / "cpu.max").write_text(cpu_max + "\n")
            if memory_max:
                (cgroup / "memory.max").write_text(memory_max + "\n")
            env = {"PATH": os.environ["PATH"], "ORCA_JOBS_ROOT": root, "ORCA_JOBS_NPROC": str(nproc)}
            if per_job:
                env["ORCA_BUILD_MEM_PER_JOB_MIB"] = str(per_job)
            return subprocess.run(["sh", str(JOBS), request], capture_output=True, text=True, env=env, timeout=30)

    def jobs(self, *args, **kwargs):
        result = self.run_jobs(*args, **kwargs)
        self.assertEqual(result.returncode, 0, result.stderr)
        return int(result.stdout)

    def test_auto_never_takes_every_cpu(self):
        self.assertEqual(self.jobs("auto", nproc=8), 7)
        self.assertEqual(self.jobs("auto", nproc=1), 1)

    def test_auto_is_bounded_by_memory(self):
        six_gib = 6 * 1024 * 1024
        self.assertEqual(self.jobs("auto", nproc=4, mem_kib=six_gib), 2)
        self.assertEqual(self.jobs("auto", nproc=4, memory_max=str(6 * 1024 ** 3)), 2)
        self.assertEqual(self.jobs("auto", nproc=4, mem_kib=1024 * 1024), 1)
        self.assertEqual(self.jobs("auto", nproc=16, mem_kib=six_gib, per_job=1024), 6)

    def test_auto_respects_a_cgroup_cpu_quota(self):
        self.assertEqual(self.jobs("auto", nproc=16, cpu_max="400000 100000"), 3)
        self.assertEqual(self.jobs("auto", nproc=4, cpu_max="max 100000"), 3)

    def test_explicit_override_and_validation(self):
        self.assertEqual(self.jobs("5", nproc=2, mem_kib=1024), 5)
        for bad in ("0", "65", "-1", "two", "", "1.5", "08"):
            with self.subTest(request=bad):
                result = self.run_jobs(bad)
                self.assertEqual(result.returncode, 2)
                self.assertIn("ORCA_BUILD_JOBS", result.stderr)
        self.assertEqual(self.run_jobs("auto", per_job=100).returncode, 2)

    def test_jobs_apply_to_dependencies_and_the_application(self):
        build = DOCKERFILE.split("FROM debian:13-slim AS runtime")[0]
        self.assertIn('orca-build-jobs "$ORCA_BUILD_JOBS" >/dev/null', build)  # validated even without Orca
        self.assertIn('export CMAKE_BUILD_PARALLEL_LEVEL="$jobs"', build)       # every dependency's own build
        self.assertIn("cmake --build deps/build --parallel 1", build)             # one dependency at a time
        self.assertIn('cmake --build build --parallel "$jobs"', build)            # the application
        self.assertNotIn("$(nproc)", build)


class OrcaCacheTests(unittest.TestCase):
    def test_cache_id_separates_channel_revisions_architecture_and_profile(self):
        mount = re.search(r"--mount=type=cache,id=(\S+?),target=/cache,sharing=locked", DOCKERFILE)
        self.assertIsNotNone(mount)
        for part in ("${TARGETARCH}", "${ORCA_CHANNEL}", "${ORCA_STABLE_REF}", "${ORCA_ALPHA_REF}",
                     "${ORCA_BUILD_PROFILE}"):
            self.assertIn(part, mount.group(1))

    def test_toolchain_change_resets_build_trees_and_source_is_unpatched(self):
        self.assertIn('"$(gcc -dumpfullversion)"', DOCKERFILE)
        self.assertIn('"$(cmake --version | head -n 1)" "$CC" "$CXX" "$ORCA_BUILD_PROFILE"', DOCKERFILE)
        self.assertIn("rm -rf /cache/orca/deps/build /cache/orca/build", DOCKERFILE)
        self.assertIn('test "$(git rev-parse HEAD)" = "$ref"', DOCKERFILE)
        self.assertIn('test -z "$(git status --porcelain --untracked-files=no)"', DOCKERFILE)
        self.assertIn("CC=/usr/lib/ccache/gcc CXX=/usr/lib/ccache/g++", DOCKERFILE)
        self.assertNotIn(".AppImage", DOCKERFILE)

    def test_build_records_its_identity(self):
        self.assertIn("> /out/orca-build-info", DOCKERFILE)
        self.assertIn("> /out/orca-source-revision", DOCKERFILE)


class RuntimeImageTests(unittest.TestCase):
    def test_every_stage_uses_debian_stable(self):
        """Debian 13 slim for every stage; it has no stock UID 1000 user, so buddy takes 1000 directly."""
        self.assertEqual(re.findall(r"^FROM (\S+)", DOCKERFILE, re.M), ["debian:13-slim"] * 3)
        runtime = DOCKERFILE.split("FROM debian:13-slim AS runtime", 1)[1]
        self.assertIn("useradd --uid 1000 --create-home buddy", runtime)
        self.assertNotIn("ubuntu", DOCKERFILE)


class UnraidTemplateTests(unittest.TestCase):
    def configs(self, root, kind):
        return {c.get("Target"): c for c in root.findall("Config") if c.get("Type") == kind}

    def test_each_template_runs_its_compose_service(self):
        for name, root in UNRAID.items():
            service = COMPOSE["services"][name]
            with self.subTest(service=name):
                self.assertEqual(shlex.split(root.findtext("PostArgs")), service["command"])
                params = shlex.split(root.findtext("ExtraParams"))
                health = params[params.index("--health-cmd") + 1]
                self.assertEqual(["CMD", *shlex.split(health)], service["healthcheck"]["test"])
                self.assertEqual(root.findtext("Repository"), "REPLACE_WITH_TESTED_IMAGE")
                self.assertEqual(root.findtext("Privileged"), "false")

    def test_hardening_and_limits_match_compose(self):
        runtime = COMPOSE["x-runtime"]
        for name, root in UNRAID.items():
            params = shlex.split(root.findtext("ExtraParams"))
            value = {flag: params[i + 1] for i, flag in enumerate(params[:-1]) if flag.startswith("--")}
            logs = [params[i + 1] for i, flag in enumerate(params[:-1]) if flag == "--log-opt"]
            with self.subTest(service=name):
                self.assertEqual(value["--cap-drop"], "ALL")
                self.assertIn(value["--security-opt"], runtime["security_opt"])
                self.assertEqual(int(value["--pids-limit"]), runtime["pids_limit"])
                self.assertEqual(value["--memory"], runtime["mem_limit"])
                self.assertEqual(float(value["--cpus"]), float(runtime["cpus"]))
                self.assertEqual(value["--shm-size"], runtime["shm_size"])
                self.assertEqual(value["--restart"], runtime["restart"])
                self.assertEqual(sorted(logs), sorted(f"{k}={v}" for k, v in runtime["logging"]["options"].items()))

    def test_paths_and_variables_match_the_runtime(self):
        compose_env = COMPOSE["x-runtime"]["environment"]
        for name, root in UNRAID.items():
            with self.subTest(service=name):
                paths = self.configs(root, "Path")
                self.assertEqual(set(paths), DATA_TARGETS)
                self.assertTrue(all(p.text.startswith("/mnt/user/appdata/print-buddy/") for p in paths.values()))
                variables = self.configs(root, "Variable")
                for key in ("PRINT_BUDDY_CONFIG", "PRINT_BUDDY__BOT__MODE"):
                    self.assertEqual(variables[key].text, compose_env[key].split(":-")[-1].rstrip("}"))
                for target, config in variables.items():
                    if target in SECRETS:
                        self.assertEqual(config.get("Mask"), "true")
                        self.assertFalse(config.text)
                    elif target.startswith("PRINT_BUDDY__"):
                        section, key = target[len("PRINT_BUDDY__"):].lower().split("__")
                        self.assertIn((section, key), inventory())
                    else:
                        self.assertIn(target, COMPOSE_VARS)
                for required in ("DISCORD_BOT_TOKEN", "PRINT_BUDDY__DISCORD__CHAT_ID",
                                 "PRINT_BUDDY__DISCORD__ALLOWED_USER_IDS"):
                    self.assertEqual(variables[required].get("Required"), "true")
        self.assertEqual({c.get("Target"): c.text for c in UNRAID["bot"].findall("Config")},
                         {c.get("Target"): c.text for c in UNRAID["listener"].findall("Config")})


class AcceptanceFixtureTests(unittest.TestCase):
    def test_overlay_isolates_services_and_gives_the_fake_printer_a_readiness_check(self):
        overlay = yaml.safe_load((ROOT / "tests" / "docker" / "compose.acceptance.yml").read_text())
        self.assertTrue(overlay["networks"]["isolated"]["internal"])
        for name in ("bot", "listener", "fake-moonraker"):
            with self.subTest(service=name):
                service = overlay["services"][name]
                self.assertEqual(service["networks"], ["isolated"])
                self.assertEqual(service["restart"], "no")
                self.assertNotIn("ports", service)
        fake = overlay["services"]["fake-moonraker"]
        self.assertIn("/server/info", json.dumps(fake["healthcheck"]["test"]))
        self.assertEqual(overlay["services"]["listener"]["depends_on"]["fake-moonraker"]["condition"], "service_healthy")
        spoolman = yaml.safe_load((ROOT / "tests" / "docker" / "compose.acceptance-spoolman.yml").read_text())
        self.assertEqual(spoolman["services"]["spoolman"]["networks"], ["isolated"])

    def test_harness_owns_its_names_and_publishes_no_port(self):
        script = (ROOT / "tests" / "docker" / "image_acceptance.sh").read_text()
        self.assertIn('project="pb-accept-${mode}-${run_id}"', script)
        self.assertNotIn("container_name", script)
        self.assertNotRegex(script, r"\s-p\s+\d|--publish")
        self.assertNotIn("prune", script)
        self.assertIn("not a version upgrade", script)


if __name__ == "__main__":
    unittest.main()
