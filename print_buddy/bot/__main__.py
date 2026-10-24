"""python3 -m print_buddy.bot [run | check | health]

run     start the standalone Discord bot (the default). Refuses unless [bot] mode is standalone.
check   validate settings, token, channel, users and handlers offline. Exit 1 on a problem.
health  exit 0 only while a running bot reports a fresh heartbeat with no Hermes loaded.
"""
import logging
import sys

from .. import logs, settings


def checks(cfg):
    """[(label, ok, detail)]. Offline: nothing here contacts Discord."""
    from ..hermes import _config
    from . import health, llm
    from .runtime import allowed_users
    rows = []

    def add(label, ok, detail):
        rows.append((label, ok, detail))

    mode = cfg["bot"]["mode"]
    add("mode", mode == "standalone", mode if mode == "standalone" else
        f"{mode}; set PRINT_BUDDY__BOT__MODE=standalone (or [bot] mode) to run without Hermes")
    token_env = cfg["discord"]["token_env"]
    add("token", bool(_config.discord_token()), f"${token_env} is set" if _config.discord_token()
        else f"${token_env} is empty; put the Discord bot token in that environment variable")
    chat = str(cfg["discord"]["chat_id"] or "")
    add("print channel", chat.isdigit(), chat or "discord.chat_id is empty")
    users = allowed_users(cfg)
    add("allowed users", bool(users), f"{len(users)} user(s)" if users
        else "none: set discord.allowed_user_ids (or approval_user_id); everyone is refused")
    try:
        import discord
        add("discord.py", True, discord.__version__)
    except ImportError:
        add("discord.py", False, "not installed: pip install -r requirements.txt")
    try:
        from .router import Router
        names = Router().handler_names()
        add("handlers", bool(names), f"{len(names)}: {', '.join(names)}")
    except Exception as exc:
        add("handlers", False, f"plugin failed to load: {exc}")
    loaded = health.hermes_modules_loaded()
    add("no hermes", not loaded, "no Hermes module imported" if not loaded else ", ".join(loaded))
    try:
        state = _config.state_dir()
        state.mkdir(parents=True, exist_ok=True)
        probe = state / ".write-test"
        probe.write_text("")
        probe.unlink()
        add("state dir", True, str(state))
    except OSError as exc:
        add("state dir", False, f"not writable: {exc}")
    add("llm", True, f"{cfg['llm']['model']} at {cfg['llm']['chat_url']}" if llm.configured(cfg)
        else "off (unmatched messages get the command list)")
    return rows


def check(out=None):
    out = out or sys.stdout
    try:
        cfg = settings.current()
    except settings.SettingsError as exc:
        print(f"FAIL  settings: {exc}", file=out)
        return 1
    failed = False
    for label, ok, detail in checks(cfg):
        failed |= not ok
        print(f"{'ok' if ok else 'FAIL':<5} {label}: {detail}", file=out)
    return 1 if failed else 0


def run():
    logs.configure()
    if check(sys.stderr):
        print("Not starting: fix the FAIL lines above.", file=sys.stderr)
        return 2
    import discord
    from ..hermes import _config
    from .client import PrintBuddyBot
    try:
        PrintBuddyBot(settings.current()).run(_config.discord_token(), log_handler=None)
    except discord.LoginFailure as exc:
        print(f"Discord refused the bot token: {exc}", file=sys.stderr)
        return 3
    except discord.PrivilegedIntentsRequired:
        print("Turn on the Message Content intent for this bot in the Discord developer portal "
              "(Bot > Privileged Gateway Intents).", file=sys.stderr)
        return 3
    return 0


def main(argv):
    command = argv[0] if argv else "run"
    if command == "run":
        return run()
    if command == "check":
        return check()
    if command == "health":
        from . import health
        return health.check()
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
