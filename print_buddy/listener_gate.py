"""Shell-facing probe: does the configured printer use the Moonraker listener?

docker/supervise.sh runs ``python3 -m print_buddy.listener_gate`` before
starting children (#256). The decision comes from the resolved configuration
(the printer model and its vendor connection, via print_buddy.healthcheck),
never from whether services.moonraker_url happens to be filled in, so a
misconfiguration still fails loudly in settings validation instead of
silently changing which children run.

Exit codes: 0 = start the listener, 3 = the configured connection has no
listener, 2 = the configuration could not be resolved (the supervisor stops
with the printed error rather than guessing).
"""
from __future__ import annotations

import sys

from . import healthcheck, settings


def main():
    try:
        connection = healthcheck.listener_connection()
    except settings.SettingsError as exc:
        print(f"listener gate: configuration unresolved: {exc}", file=sys.stderr)
        return 2
    if connection in healthcheck.LISTENER_CONNECTIONS:
        print(f"listener gate: connection {connection!r} uses the Moonraker listener")
        return 0
    print(f"listener gate: connection {connection!r} has no Moonraker listener; not starting it")
    return 3


if __name__ == "__main__":
    sys.exit(main())
