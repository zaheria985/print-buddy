"""Health check, listener posts without Hermes, settings, doctor, and a clean no-Hermes process."""
import asyncio
import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest import mock

import pytest

from print_buddy import doctor, settings
from print_buddy.bot import __main__ as bot_main
from print_buddy.bot import health
from print_buddy.hermes import _config
from print_buddy.hermes import moonraker_discord_announcements as announcer
from print_buddy.hermes import moonraker_event_listener as mel


def beat(**extra):
    return {"mode": "standalone", "pid": os.getpid(), "heartbeat_at": time.time(), "connected": True,
            "user": "Print Buddy#0001", "handlers": 13, "hermes_modules": [], **extra}


@pytest.mark.parametrize(("status", "ok", "reason"), [
    (None, False, "no heartbeat"),
    (beat(), True, "connected as Print Buddy#0001"),
    (beat(heartbeat_at=time.time() - 3600), False, "old"),
    (beat(connected=False), False, "not connected"),
    (beat(hermes_modules=["tools.clarify_gateway"]), False, "Hermes modules loaded"),
    (beat(mode="hermes"), False, "mode"),
    (beat(pid=2 ** 22 + 12345), False, "is gone"),
])
def test_health_verdicts(status, ok, reason):
    got, why = health.evaluate(status, stale_s=120)
    assert got is ok and reason in why


def test_health_command_reads_the_heartbeat_file(capsys):
    health.write(beat())
    assert health.check() == 0
    health.write(beat(connected=False))
    assert health.check() == 1
    assert "not connected" in capsys.readouterr().out


def test_health_http_endpoint():
    async def flow():
        server = await health.serve(0, lambda: beat(), host="127.0.0.1")
        port = server.sockets[0].getsockname()[1]
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"GET /healthz HTTP/1.1\r\nHost: x\r\n\r\n")
            await writer.drain()
            data = await reader.read()
            writer.close()
            return data
        finally:
            server.close()
            await server.wait_closed()

    data = asyncio.run(flow())
    head, body = data.split(b"\r\n\r\n", 1)
    assert head.startswith(b"HTTP/1.1 200") and json.loads(body)["ok"] is True


def test_listener_posts_over_rest_in_standalone_mode(monkeypatch):
    sent = []
    monkeypatch.setitem(_config.cfg()["bot"], "mode", "standalone")
    monkeypatch.setattr(mel, "_profile_env_value", lambda name: "tok" if name == "DISCORD_BOT_TOKEN" else "")
    monkeypatch.setattr("print_buddy.bot.rest.send", lambda token, chat, text: sent.append((token, chat, text)) or (True, "1"))
    with mock.patch.object(mel.subprocess, "run", side_effect=AssertionError("hermes send was called")):
        assert mel.post("Print started: cube") is True
    assert sent == [("tok", announcer.DISCORD_CHAT_ID, "Print started: cube")]


def test_listener_keeps_hermes_send_in_hermes_mode(monkeypatch):
    monkeypatch.setitem(_config.cfg()["bot"], "mode", "hermes")
    with mock.patch.object(mel.subprocess, "run", return_value=mock.Mock(returncode=0, stderr="")) as run, \
            mock.patch("print_buddy.bot.rest.send", side_effect=AssertionError("REST in hermes mode")):
        assert mel.post("Print started: cube") is True
    assert run.call_args[0][0][1:4] == ["-p", "3dprinting", "send"]


def test_button_posts_use_the_configured_token_variable(monkeypatch):
    monkeypatch.setitem(_config.cfg()["discord"], "token_env", "MY_BOT_TOKEN")
    monkeypatch.setattr(mel, "_profile_env_value", lambda name: {"MY_BOT_TOKEN": "mine"}.get(name, ""))
    assert mel._bot_token() == "mine"


def test_standalone_mode_has_no_agent_session_to_reset(monkeypatch):
    monkeypatch.setitem(_config.cfg()["bot"], "mode", "standalone")
    with mock.patch.object(announcer, "_live_channel_sessions", side_effect=AssertionError("state.db read")), \
            mock.patch.object(announcer, "_queued_jobs", return_value=[]), \
            mock.patch.object(announcer, "_open_multipart_plans", return_value=[]):
        assert announcer.check_and_reset_idle_discord_session(time.time()) == (
            False, "standalone mode: no agent session", True)
        assert announcer.silently_reset_discord_session("1")[0] is False
        state = announcer.process_pending_cleanups({"pending_cleanups": [{"cleanup_due_at": 0}]})
    assert state["pending_cleanups"] == []


def _load(text, tmp_path, environ=None):
    path = tmp_path / "local.yaml"
    path.write_text(text)
    return settings.load(local=path, environ=environ or {})


def test_bot_settings_defaults_and_validation(tmp_path):
    data = _load("", tmp_path)
    assert data["bot"] == {"mode": "hermes", "health_port": 0, "health_stale_s": 120, "sync_commands": True}
    assert data["discord"]["token_env"] == "DISCORD_BOT_TOKEN" and data["discord"]["allowed_user_ids"] == ""
    assert data["llm"]["chat_url"] == "" and data["llm"]["api_key_env"] == "LLM_API_KEY"
    data = _load("bot:\n  mode: standalone\ndiscord:\n  allowed_user_ids: '1, 2 3'\n", tmp_path,
                 {"PRINT_BUDDY__BOT__HEALTH_PORT": "8080"})
    assert data["bot"]["mode"] == "standalone" and data["bot"]["health_port"] == 8080
    assert settings.user_ids(data["discord"]["allowed_user_ids"]) == ["1", "2", "3"]
    for text, error in (("bot:\n  mode: chatty\n", "mode must be one of"),
                        ("discord:\n  allowed_user_ids: 'me'\n", "numeric Discord user IDs"),
                        ("llm:\n  chat_url: http://x\n", "model is required"),
                        ("discord:\n  token_env: 'not a name'\n", "environment variable name"),
                        ("bot:\n  health_port: 70000\n", "health_port")):
        with pytest.raises(settings.SettingsError, match=error):
            _load(text, tmp_path)


def test_check_command_names_every_problem(monkeypatch, capsys, tmp_path):
    cfg = settings.load(local=Path(os.devnull), environ={})
    cfg["paths"]["hermes_home"] = tmp_path / "profile"
    monkeypatch.setattr(settings, "current", lambda: cfg)
    monkeypatch.setattr(_config, "cfg", lambda: cfg)
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    monkeypatch.setattr(_config, "profile_env_value", lambda name, **kw: "")
    assert bot_main.check() == 1
    out = capsys.readouterr().out
    for line in ("FAIL  mode: hermes", "FAIL  token", "FAIL  print channel", "FAIL  allowed users",
                 "ok    handlers: 13", "ok    no hermes", f"ok    state dir: {tmp_path}"):
        assert line in out, out


def test_run_refuses_to_start_when_checks_fail(monkeypatch):
    monkeypatch.setattr(bot_main, "check", lambda out=None: 1)
    with mock.patch("print_buddy.bot.client.PrintBuddyBot") as bot:
        assert bot_main.run() == 2
    bot.assert_not_called()


def test_doctor_reports_mode_users_and_a_shared_token(monkeypatch, tmp_path):
    cfg = settings.load(local=Path(os.devnull), environ={})
    cfg["bot"]["mode"] = "standalone"
    cfg["discord"].update(chat_id="1", allowed_user_ids="5, 6")
    cfg["paths"]["hermes_home"] = tmp_path / "data"
    hermes = tmp_path / ".hermes"
    (hermes / "profiles" / "3dprinting").mkdir(parents=True)
    (hermes / "profiles" / "3dprinting" / ".env").write_text("DISCORD_BOT_TOKEN='same-token'\n")
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "same-token")
    assert doctor.check_bot(cfg) == "standalone, print channel 1, 2 allowed user(s)"
    assert "holds this bot token" in doctor.check_shared_token(cfg, hermes_root=hermes)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "other-token")
    assert doctor.check_shared_token(cfg, hermes_root=hermes) is None
    assert doctor.check_shared_token(cfg, hermes_root=tmp_path / "no-hermes") is None
    cfg["bot"]["mode"] = "hermes"
    assert doctor.check_bot(cfg) == "hermes (the Hermes gateway runs the print-feedback plugin)"
    assert doctor.check_discord_login(cfg) is None


def test_doctor_login_check_uses_the_token(monkeypatch):
    cfg = settings.load(local=Path(os.devnull), environ={})
    cfg["bot"]["mode"] = "standalone"
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "tok")
    with mock.patch("print_buddy.bot.rest.whoami", return_value={"id": "9", "username": "printbuddy"}) as who:
        assert doctor.check_discord_login(cfg) == "printbuddy (9)"
    assert who.call_args[0][0] == "tok"
    monkeypatch.delenv("DISCORD_BOT_TOKEN")
    monkeypatch.setattr(_config, "profile_env_value", lambda name, **kw: "")
    with pytest.raises(RuntimeError, match="DISCORD_BOT_TOKEN is empty"):
        doctor.check_discord_login(cfg)


BLOCK_HERMES = """
import importlib.abc, sys
class NoHermes(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name.split('.')[0] in ('tools', 'gateway', 'hermes_state', 'hermes_cli'):
            raise ImportError('Hermes is not installed: ' + name)
        return None
sys.meta_path.insert(0, NoHermes())
"""

CHILD = BLOCK_HERMES + """
import json, os, sys, tempfile
sys.path.insert(0, os.path.join(os.getcwd(), 'tests'))
import conftest  # the offline test settings: temp state, fake hosts
from unittest import mock
from print_buddy.core import printer_state
from corpus import harness
from print_buddy.bot import health
import print_buddy.bot.client  # the discord.py client imports without Hermes too
case = harness.load_case(harness.CASES / 'parts-one-plate')
with mock.patch.object(printer_state, 'save', lambda state: None):
    run = harness.run_case(case, tempfile.mkdtemp(), transport='standalone')
harness.check_expectations(case, run)
print('REPORT ' + json.dumps({'status': run.receipt.get('status'), 'starts': len(run.starts),
                              'hermes': health.hermes_modules_loaded()}))
"""


def test_a_clean_process_with_hermes_blocked_runs_the_whole_intake():
    """Acceptance (#73): core and bot import and run with no Hermes module; any Hermes import fails here."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PRINT_BUDDY", "HERMES_"))}
    env.update(PYTHONPATH=str(settings.ROOT), PYTHONDONTWRITEBYTECODE="1")
    proc = subprocess.run([sys.executable, "-c", CHILD], cwd=str(settings.ROOT), env=env,
                          capture_output=True, text=True, timeout=300)
    lines = [line for line in proc.stdout.splitlines() if line.startswith("REPORT ")]
    assert lines, f"child failed ({proc.returncode}):\n{proc.stdout[-2000:]}\n{proc.stderr[-3000:]}"
    report = json.loads(lines[-1][len("REPORT "):])
    assert report == {"status": "STARTED", "starts": 1, "hermes": []}


def test_install_standalone_renders_units_and_data_folder(tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PRINT_BUDDY", "HERMES_"))}
    env.update(SYSTEMD_USER_DIR=str(tmp_path / "units"), NO_SYSTEMCTL="1",
               PRINT_BUDDY__PATHS__HERMES_HOME=str(tmp_path / "data"), PYTHON=sys.executable,
               PRINT_BUDDY_CONFIG=str(tmp_path / "empty.yaml"))
    (tmp_path / "empty.yaml").write_text("")
    script = settings.ROOT / "scripts" / "install-standalone.sh"
    first = subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True, timeout=120)
    assert first.returncode == 0, first.stderr
    for name in ("print-buddy-bot", "print-buddy-listener"):
        unit = (tmp_path / "units" / f"{name}.service").read_text()
        assert "{{" not in unit and "PRINT_BUDDY__BOT__MODE=standalone" in unit
        assert f"EnvironmentFile=-{tmp_path / 'data'}/.env" in unit
        assert "HERMES_HOME" not in unit and "PartOf=" not in unit  # not tied to a Hermes gateway
        assert (tmp_path / "units" / f"{name}.service").stat().st_mode & 0o777 != 0o600
    assert "-m print_buddy.bot run" in (tmp_path / "units" / "print-buddy-bot.service").read_text()
    assert (tmp_path / "data" / ".env").stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "data" / "state").is_dir()
    again = subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True, timeout=120)
    assert again.stdout.count("unchanged") == 2


@pytest.mark.parametrize("changes", [
    {"host": "other-container"}, {"process_start": "reused"},
    {"pid": 0}, {"pid": -1}, {"pid": "invalid"}, {"pid": 10 ** 100},
    {"heartbeat_at": float("nan")}, {"heartbeat_at": time.time() + 3600},
])
def test_bot_health_command_rejects_foreign_or_invalid_identity(changes):
    health.write(beat())
    status = health.read()
    health.path().write_text(json.dumps({**status, **changes}))
    assert health.check(out=io.StringIO()) == 1


def test_bot_shutdown_awaits_owned_resources(cfg):
    from print_buddy.bot.client import PrintBuddyBot

    async def flow():
        bot = PrintBuddyBot(cfg)
        stopped = asyncio.Event()
        started = asyncio.Event()

        async def heartbeat():
            started.set()
            try:
                await asyncio.Future()
            finally:
                stopped.set()

        task = asyncio.create_task(heartbeat())
        await started.wait()
        server = mock.Mock()
        server.wait_closed = mock.AsyncMock()
        bot._background = [task, server]
        with mock.patch("discord.ext.commands.Bot.close", mock.AsyncMock()):
            await bot.close()
        assert stopped.is_set() and task.done()
        server.close.assert_called_once()
        server.wait_closed.assert_awaited_once()
        assert bot._background == [] and not bot.connected

    asyncio.run(flow())
