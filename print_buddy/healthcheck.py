"""Listener process liveness, adapted from zaheria985's Docker contribution.

Source: 3bb0984250c20eb336d11ba1da06633950b45182 (MIT).
Separate files and host/process identity prevent another service's heartbeat
from satisfying this check. Printer connectivity is reported, not required:
restarting a healthy listener cannot fix an offline printer. No network probe.
"""
from __future__ import annotations

import json
import math
import os
import socket
import sys
import time
from pathlib import Path

from . import settings

HEARTBEAT_S = 30
RUNTIME = "moonraker_event_listener"


def heartbeat_path(data=None):
    return settings.state_dir(data) / "listener_health.json"


def process_identity(pid):
    # Linux start ticks disambiguate PID reuse, including container PID 1.
    text = Path(f"/proc/{int(pid)}/stat").read_text()
    return text.rsplit(")", 1)[1].split()[19]


def beat(runtime=RUNTIME, data=None, **details):
    path = heartbeat_path(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {**details, "runtime": runtime, "pid": os.getpid(),
              "host": socket.gethostname(), "time": time.time(),
              "process_start": process_identity(os.getpid())}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(record, sort_keys=True))
    os.replace(tmp, path)
    return record


def check(data=None, now=None):
    try:
        record = json.loads(heartbeat_path(data).read_text())
        age = (time.time() if now is None else now) - float(record["time"])
        if not math.isfinite(age) or age < -5 or age > 90:
            return False, "listener heartbeat stale or invalid"
        if record["runtime"] != RUNTIME or record["host"] != socket.gethostname():
            return False, "heartbeat belongs to another service or container"
        if int(record["pid"]) <= 0 or process_identity(record["pid"]) != record["process_start"]:
            return False, "listener process is gone or replaced"
        return True, "listener event loop alive; printer_connected=" + str(record.get("moonraker_connected", False))
    except (OSError, ValueError, TypeError, KeyError, IndexError, OverflowError, settings.SettingsError):
        return False, "listener heartbeat missing, unreadable or invalid"


def main():
    ok, reason = check()
    print(f"{'ok' if ok else 'FAIL'}  {reason}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
