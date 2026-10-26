#!/usr/bin/env python3
"""Container readiness across the bot, Moonraker listener and local Spoolman."""
import os
import sys
import urllib.request

# Installed as /usr/local/bin/print-buddy-healthcheck: for a script invocation Python
# puts the SCRIPT's directory on sys.path, not /app, so without this the probe dies
# with ModuleNotFoundError (the combined container could never report healthy).
# Same pattern as tests/docker/image_probe.py and docker/slicer-smoke.py.
sys.path.insert(0, "/app")

from print_buddy import settings
from print_buddy.bot.health import check as bot_check
from print_buddy.healthcheck import check as listener_check
from print_buddy.healthcheck import listener_required


def main():
    healthy = True
    if os.environ.get("DISCORD_BOT_TOKEN"):
        healthy &= bot_check() == 0
    else:
        print("ok  Discord bot disabled by empty token")
    # The listener is required exactly when the supervisor started it: the
    # configured model's connection decides (#256), mirroring the disabled-bot
    # branch above. An unresolved configuration fails instead of passing.
    try:
        required = listener_required()
    except settings.SettingsError as exc:
        print(f"FAIL  listener: configuration unresolved: {exc}")
        required, healthy = None, False
    if required:
        ok, reason = listener_check()
        print(f"{'ok' if ok else 'FAIL'}  listener: {reason}")
        healthy &= ok
    elif required is not None:
        print("ok  listener: not required for this printer connection")
    try:
        url = ("http://127.0.0.1:" + os.environ.get("SPOOLMAN_PORT", "8000")
               if os.environ.get("PRINT_BUDDY_BUNDLED_SPOOLMAN", "true") == "true"
               else os.environ["PRINT_BUDDY__SERVICES__SPOOLMAN_URL"].rstrip("/"))
        with urllib.request.urlopen(url + "/api/v1/health", timeout=3) as response:
            healthy &= response.status == 200
            print(f"{'ok' if response.status == 200 else 'FAIL'}  Spoolman: HTTP {response.status}")
    except Exception as exc:
        print(f"FAIL  Spoolman: {exc}")
        healthy = False
    return 0 if healthy else 1


if __name__ == "__main__":
    sys.exit(main())
