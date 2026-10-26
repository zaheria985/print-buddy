"""Stop the current print with one Moonraker cancel, or one Bambu LAN stop. No agent, no retries.

Used by the ``/print-stop`` Discord command in the print-feedback plugin and by the
listener's paused alert. Reads print_stats, sends at most one /printer/print/cancel,
then reports the state Moonraker actually shows afterwards.
"""

from __future__ import annotations

import json
import time
import urllib.request
from typing import Any

from . import _config

BASE = _config.moonraker_url()
ACTIVE = ("printing", "paused")
STOP_LABEL = "Stop print now"
KEEP_LABEL = "Keep printing"


def _call(path: str, method: str = "GET", timeout: float = 10) -> dict[str, Any]:
    req = urllib.request.Request(_config.require(BASE, "services.moonraker_url") + path, method=method, data=b"" if method == "POST" else None)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read() or b"{}")


def _lan_adapter():
    """The configured printer's adapter when it talks Bambu Lab LAN MQTT; None for Moonraker printers (#276)."""
    from print_buddy import printers
    adapter = printers.current()
    return adapter if getattr(adapter, "transport", None) == "bambulab_lan" else None


def status() -> dict[str, Any]:
    """state, filename, layer, total_layer, progress (0..1), print_duration."""
    adapter = _lan_adapter()
    if adapter is not None:
        if not adapter.lan_configured():
            raise RuntimeError("Bambu LAN status is not configured ([bambu] lan_host and lan_serial plus the access code)")
        live = adapter.normalize_status(adapter.client().snapshot())
        return {"state": str(live.get("state") or "unknown"), "filename": live.get("filename") or "",
                "layer": live.get("layer"), "total_layer": live.get("total_layer"),
                "progress": live.get("progress"), "print_duration": None}
    data = _call("/printer/objects/query?print_stats&virtual_sdcard=progress")
    s = (data.get("result") or {}).get("status") or {}
    ps = s.get("print_stats") or {}
    info = ps.get("info") or {}
    return {"state": str(ps.get("state") or "unknown"), "filename": ps.get("filename") or "",
            "layer": info.get("current_layer"), "total_layer": info.get("total_layer"),
            "progress": (s.get("virtual_sdcard") or {}).get("progress"),
            "print_duration": ps.get("print_duration")}


def where_text(st: dict[str, Any]) -> str:
    bits = []
    if st.get("layer") and st.get("total_layer"):
        bits.append(f"layer {st['layer']}/{st['total_layer']}")
    if isinstance(st.get("progress"), (int, float)):
        bits.append(f"{st['progress'] * 100:.0f}% done")
    return ", ".join(bits)


def cancel(expected_filename: str, wait_s: float = 20, poll_s: float = 1.0) -> tuple[bool, str]:
    """One cancel, only if the same file is still printing or paused. Returns (sent, truthful text)."""
    before = status()
    if before["state"] not in ACTIVE:
        return False, f"Nothing to stop: printer is {before['state']}. Nothing sent."
    if expected_filename and before["filename"] != expected_filename:
        return False, "A different print is running now than the one you confirmed. Nothing sent."
    adapter = _lan_adapter()
    if adapter is not None:
        adapter.control("stop")  # one LAN MQTT stop; the printer answers on the next snapshot
    else:
        try:
            from . import klipper_report
            klipper_report.mark_bot_cancel()
        except Exception:
            pass
        _call("/printer/print/cancel", method="POST", timeout=15)
    deadline = time.monotonic() + wait_s
    after = before
    while time.monotonic() < deadline:
        time.sleep(poll_s)
        try:
            after = status()
        except Exception:
            continue
        if after["state"] not in ACTIVE:
            break
    if after["state"] in ACTIVE:
        return True, (f"Cancel sent, but the printer still reports {after['state']} after {wait_s:.0f}s. "
                      "Check the printer screen. Not retrying.")
    if adapter is not None and after["state"] == "error":
        # A Bambu printer reports a stopped job as FAILED until the operator clears it on the screen.
        return True, "Stopped. The printer shows the cancelled job as an error until you acknowledge it on its screen."
    return True, f"Stopped. Printer now reports {after['state']}."
