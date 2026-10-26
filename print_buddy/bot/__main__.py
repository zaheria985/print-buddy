"""python3 -m print_buddy.bot [run | check | health]

run     start the standalone bot on the [bot] platform (the default). Refuses unless [bot] mode is standalone.
check   validate settings, token, channel, users and handlers offline. Exit 1 on a problem.
health  exit 0 only while a running bot reports a fresh heartbeat with no Hermes loaded.
"""
import logging
import sys

from .. import logs, settings


def checks(cfg):
    """[(label, ok, detail)]. Offline: nothing here contacts the chat platform."""
    from ..hermes import _config
    from . import health, llm, platform
    from .runtime import allowed_users
    rows = []

    def add(label, ok, detail):
        rows.append((label, ok, detail))

    mode = cfg["bot"]["mode"]
    add("mode", mode == "standalone", mode if mode == "standalone" else
        f"{mode}; set PRINT_BUDDY__BOT__MODE=standalone (or [bot] mode) to run without Hermes")
    name = str(cfg["bot"].get("platform") or "discord")
    implemented = platform.platform_module(name) is not None
    add("platform", implemented, name if implemented else
        f"{name}; platform {name!r} is not implemented yet (#164) — discord, telegram and matrix are available")
    if name == "discord":
        token_env = cfg["discord"]["token_env"]
        add("token", bool(_config.discord_token()), f"${token_env} is set" if _config.discord_token()
            else f"${token_env} is empty; put the Discord bot token in that environment variable")
        chat = str(cfg["discord"]["chat_id"] or "")
        add("print channel", chat.isdigit(), chat or "discord.chat_id is empty")
        users = allowed_users(cfg)
        add("allowed users", bool(users), f"{len(users)} user(s)" if users
            else "none: set discord.allowed_user_ids (or approval_user_id); everyone is refused")
    elif name == "telegram":
        from .telegram import telegram_token
        token_env = cfg["telegram"]["token_env"]
        add("token", bool(telegram_token(cfg)), f"${token_env} is set" if telegram_token(cfg)
            else f"${token_env} is empty; put the Telegram bot token in that environment variable")
        chat = str(cfg["telegram"]["chat_id"] or "")
        add("print channel", bool(chat) and chat.lstrip("-").isdigit() and chat != "-",
            chat or "telegram.chat_id is empty")
        users = allowed_users(cfg, "telegram")
        add("allowed users", bool(users), f"{len(users)} user(s)" if users
            else "none: set telegram.allowed_user_ids (or approval_user_id); everyone is refused")
    elif name == "matrix":
        from .matrix import matrix_token
        token_env = cfg["matrix"]["access_token_env"]
        add("token", bool(matrix_token(cfg)), f"${token_env} is set" if matrix_token(cfg)
            else f"${token_env} is empty; put the Matrix access token in that environment variable")
        homeserver = str(cfg["matrix"]["homeserver_url"] or "")
        add("homeserver", homeserver.startswith(("http://", "https://")),
            homeserver or "matrix.homeserver_url is empty")
        room = str(cfg["matrix"]["room_id"] or "")
        add("print room", room.startswith("!"), room or "matrix.room_id is empty")
        users = allowed_users(cfg, "matrix")
        add("allowed users", bool(users), f"{len(users)} user(s)" if users
            else "none: set matrix.allowed_user_ids (or approval_user_id); everyone is refused")
    label, available, detail = platform.library_status(name)
    add(label, available, detail)
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
    if name in ("telegram", "matrix"):
        # Telegram and Matrix are pure bots (#164 stages 2-3, #73): the LLM
        # fallback is never wired for them, whatever [llm] says elsewhere.
        add("llm", True, f"off ({name.capitalize()} is a pure bot: no LLM; unmatched messages get the command list)")
    else:
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
    cfg = settings.current()
    name = str(cfg["bot"].get("platform") or "discord")
    if name == "discord":
        from .client import run_bot
        return run_bot(cfg)
    if name == "telegram":
        from .telegram import run_bot
        return run_bot(cfg)
    if name == "matrix":
        from .matrix import run_bot
        return run_bot(cfg)
    print(f"Platform {name!r} is not implemented yet (#164).", file=sys.stderr)  # unreachable while checks report it, kept as a guard
    return 2


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
