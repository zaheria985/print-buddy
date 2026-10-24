"""Native #117 checks. Copied payload is NOT proof of Docker COPY or OCI runtime."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from unittest import mock

import pytest

from print_buddy import settings
from print_buddy.core import orca_adapter

ROOT = settings.ROOT


def test_payload_setup_outside_checkout_without_source_masking(tmp_path):
    payload = tmp_path / "payload"
    payload.mkdir()
    for name in ("print_buddy", "plugins", "config"):
        shutil.copytree(ROOT / name, payload / name,
                        ignore=shutil.ignore_patterns("__pycache__", "local.*", "after-update.sh"))
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("PRINT_BUDDY", "HERMES", "PYTHONPATH", "ORCA_"))}
    env.update(PYTHONPATH=str(payload), PYTHONDONTWRITEBYTECODE="1",
               PRINT_BUDDY_CONFIG=str(payload / "config/docker.yaml"),
               PRINT_BUDDY__BOT__MODE="standalone", DISCORD_BOT_TOKEN="offline-dummy",
               PRINT_BUDDY__DISCORD__CHAT_ID="100000000000000001",
               PRINT_BUDDY__DISCORD__ALLOWED_USER_IDS="200000000000000002",
               PRINT_BUDDY__PATHS__HERMES_HOME=str(tmp_path / "state"))
    code = """
import importlib.abc, json, sys
class Offline(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name.split('.')[0] in ('tools', 'gateway', 'hermes_cli', 'hermes_state'):
            raise ImportError('Hermes forbidden: ' + name)
sys.meta_path.insert(0, Offline())
def audit(event, args):
    if event in ('socket.connect', 'socket.bind'):
        raise AssertionError('network forbidden')
sys.addaudithook(audit)
from print_buddy.bot.__main__ import check
from print_buddy.bot.router import PLUGIN
from print_buddy import settings
assert check() == 0
print('PAYLOAD ' + json.dumps({'root': str(settings.ROOT), 'plugin': str(PLUGIN)}))
"""
    result = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, env=env,
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(next(line[8:] for line in result.stdout.splitlines() if line.startswith("PAYLOAD ")))
    assert report["root"] == str(payload)
    assert report["plugin"].startswith(str(payload))
    assert "ok    handlers: 13" in result.stdout


def test_no_orca_fit_refuses_without_an_artifact(tmp_path):
    missing = str(tmp_path / "no-orca-binary")
    with mock.patch.object(orca_adapter, "ORCA_COMMAND", [missing]), \
            mock.patch("urllib.request.urlopen", side_effect=AssertionError("network fallback")):
        with pytest.raises(FileNotFoundError, match="no-orca-binary"):
            orca_adapter.orca_plate_capacity(ROOT / "tests/slicer/fixtures/cube.stl", {}, copy_limit=1)
    assert not list(tmp_path.glob("*.gcode"))


def test_docker_scalar_environment_parity_for_every_schema_key():
    # The loader's supported scalar environment surface; lists and nested maps use files.
    base = settings.load(local=ROOT / "config/docker.yaml", environ={})
    env = {}
    keys = []
    for section, values in base.items():
        for key, value in values.items():
            if isinstance(value, (list, dict)):
                continue
            env[f"PRINT_BUDDY__{section.upper()}__{key.upper()}"] = (
                str(value).lower() if isinstance(value, bool) else str(int(value)) if isinstance(value, float) and value.is_integer() else str(value))
            keys.append((section, key))
    loaded = settings.load(local=ROOT / "config/docker.yaml", environ=env)
    for section, key in keys:
        assert loaded[section][key] == base[section][key], (section, key)


def test_example_requires_explicit_standalone_inputs_and_external_spoolman():
    text = (ROOT / ".env.example").read_text()
    assignments = dict(line.split("=", 1) for line in text.splitlines()
                       if line and not line.startswith("#") and "=" in line)
    assert assignments["PRINT_BUDDY__SERVICES__SPOOLMAN_URL"] == ""
    for key in ("DISCORD_BOT_TOKEN", "PRINT_BUDDY_IMAGE", "PRINT_BUDDY__DISCORD__CHAT_ID",
                "PRINT_BUDDY__DISCORD__ALLOWED_USER_IDS"):
        assert assignments[key] == ""
    assert assignments["PRINT_BUDDY__BOT__MODE"] == "standalone"
    assert "192.168." not in text


def test_application_and_slicer_channels_are_independent_build_arguments():
    text = (ROOT / "Dockerfile").read_text()
    assert 'case "$APP_CHANNEL" in stable|development)' in text
    assert 'case "$ORCA_CHANNEL" in stable|alpha)' in text
    assert 'case "$ORCA_CHANNEL" in stable) ref="$ORCA_STABLE_REF"' in text
    assert 'org.print-buddy.app-channel=$APP_CHANNEL' in text
    assert '$APP_CHANNEL' not in text.split('FROM debian:13-slim AS runtime')[0]


def test_optional_octoprint_and_nozzle_settings_file_environment_parity(tmp_path):
    configs = {
        "yaml": "services:\n  octoprint_url: http://octoprint.example:5000\nprinter:\n  nozzle_type: hardened_steel\n",
        "toml": '[services]\noctoprint_url = "http://octoprint.example:5000"\n[printer]\nnozzle_type = "hardened_steel"\n',
    }
    expected = settings.load(local=ROOT / "config/docker.yaml", environ={
        "PRINT_BUDDY__SERVICES__OCTOPRINT_URL": "http://octoprint.example:5000",
        "PRINT_BUDDY__PRINTER__NOZZLE_TYPE": "hardened_steel"})
    for suffix, text in configs.items():
        path = tmp_path / f"optional.{suffix}"
        path.write_text(text)
        actual = settings.load(local=path, environ={})
        assert actual["services"]["octoprint_url"] == expected["services"]["octoprint_url"]
        assert actual["printer"]["nozzle_type"] == expected["printer"]["nozzle_type"]
