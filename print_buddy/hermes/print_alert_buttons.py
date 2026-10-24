"""Stop/pause alerts with Resume and Cancel buttons for #3d-printer.

The listener posts the alert (Discord REST, bot token) and records a short alert id.
The print-feedback gateway plugin receives the button click and calls ``handle_click``.
Every action re-reads the printer first and refuses when the state no longer matches.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

from . import _config

STATE = _config.state_file("PRINT_ALERT_STATE", "print_alert_buttons.json")
BASE = _config.moonraker_url()
PREFIX = "pbalert"
INSERTION_PREFIX = "pbinsert"
SPOOL_QUESTION_PREFIX = "pbspool"
PLATE_PREFIX = "pbplate"
CONFIRM_LABEL = "Confirm bed is clear & print"
TTL = 24 * 3600
_LOCK = threading.Lock()


def _call(path: str, method: str = "GET", timeout: float = 10) -> dict[str, Any]:
    req = urllib.request.Request(_config.require(BASE, "services.moonraker_url") + path, method=method, data=b"" if method == "POST" else None)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read() or b"{}")


def shutdown_message() -> str:
    """Klipper's own reason for the stop. Empty when it can't be read."""
    try:
        s = (_call("/printer/objects/query?webhooks=state_message").get("result") or {}).get("status") or {}
        return str((s.get("webhooks") or {}).get("state_message") or "")
    except Exception:
        return ""


def explain(state_message: str) -> str:
    """Plain English cause. Unknown text is passed through, never guessed at."""
    text = (state_message or "").strip()
    if not text:
        return "Klipper didn't say why. Check the printer screen."
    weight = re.search(r"Weight\s*(\d+(?:\.\d+)?)\s*g?\s*>\s*(?:max\s*)?(\d+(?:\.\d+)?)", text)
    if weight or re.search(r"nozzle hit|part detachment", text, re.I):
        z = re.search(r"\bZ\s*=\s*(-?\d+(?:\.\d+)?)", text)
        bits = []
        if weight:
            bits.append(f"the nozzle load cell read {float(weight.group(1)):g} g against a "
                        f"{float(weight.group(2)):g} g limit")
        if z:
            bits.append(f"at Z={float(z.group(1)):g} mm")
        detail = (" (" + ", ".join(bits) + ")") if bits else ""
        return ("Nozzle collision stop: the nozzle hit something" + detail + ". Usually a blob on the bed, "
                "a curled edge, or a part that came loose. Clear the bed and check the nozzle before anything else.")
    first = next((ln.strip() for ln in text.splitlines() if ln.strip()), text)
    return f"Klipper says: {first}"


def _load() -> dict[str, Any]:
    try:
        data = json.loads(STATE.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(data: dict[str, Any]) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    os.replace(tmp, STATE)


def record(kind: str, filename: str, now: float | None = None, **extra: Any) -> str:
    """Remember one alert; returns its short id. Extra fields support inventory-only UI flows."""
    now = time.time() if now is None else now
    aid = secrets.token_hex(4)
    with _LOCK:
        data = {k: v for k, v in _load().items() if now - float(v.get("at", 0)) < TTL}
        data[aid] = {"kind": kind, "filename": filename, "at": now, **extra}
        _save(data)
    return aid


def peek(aid: str) -> dict[str, Any] | None:
    """Read an alert without claiming it, so a bad choice can be refused and the prompt stays usable."""
    alert = _load().get(aid)
    if alert and time.time() - float(alert.get("at", 0)) < TTL:
        return alert
    return None


def take(aid: str) -> dict[str, Any] | None:
    """Claim an alert once. A second click on the same alert gets None."""
    with _LOCK:
        data = _load()
        alert = data.pop(aid, None)
        _save(data)
    if alert and time.time() - float(alert.get("at", 0)) < TTL:
        return alert
    return None


def components(aid: str, kind: str) -> list[dict[str, Any]]:
    if kind == "visual":
        return [{"type": 1, "components": [
            {"type": 2, "style": 2, "label": "Pause", "custom_id": f"{PREFIX}:pause:{aid}"},
            {"type": 2, "style": 3, "label": "It's fine", "custom_id": f"{PREFIX}:acknowledge:{aid}"},
        ]}]
    buttons = []
    if kind == "paused":
        buttons.append({"type": 2, "style": 3, "label": "Resume print", "custom_id": f"{PREFIX}:resume:{aid}"})
    buttons.append({"type": 2, "style": 4, "label": "Cancel print", "custom_id": f"{PREFIX}:cancel:{aid}"})
    return [{"type": 1, "components": buttons}]


def insertion_components(aid: str) -> list[dict[str, Any]]:
    """Three non-actuating choices for one idle IFS insertion prompt."""
    return [{"type": 1, "components": [
        {"type": 2, "style": 2, "label": "Same spool", "custom_id": f"{INSERTION_PREFIX}:same:{aid}"},
        {"type": 2, "style": 1, "label": "Existing spool", "custom_id": f"{INSERTION_PREFIX}:existing:{aid}"},
        {"type": 2, "style": 1, "label": "New spool", "custom_id": f"{INSERTION_PREFIX}:new:{aid}"},
    ]}]


def spool_question_components(aid: str, prior_spool_id: int | None = None) -> list[dict[str, Any]]:
    """Inventory-only answers for a paused runout or swap question."""
    buttons = []
    if prior_spool_id is not None:
        buttons.append({"type": 2, "style": 2, "label": "Same spool",
                        "custom_id": f"{SPOOL_QUESTION_PREFIX}:same:{aid}"})
    buttons.extend([
        {"type": 2, "style": 1, "label": "Existing spool",
         "custom_id": f"{SPOOL_QUESTION_PREFIX}:existing:{aid}"},
        {"type": 2, "style": 1, "label": "New spool",
         "custom_id": f"{SPOOL_QUESTION_PREFIX}:new:{aid}"},
    ])
    return [{"type": 1, "components": buttons}]


def plate_picker_components(aid: str, options: list[dict[str, str]]) -> list[dict[str, Any]]:
    """One select menu listing the remaining plates; choosing one only prepares a Confirm button."""
    return [{"type": 1, "components": [
        {"type": 3, "custom_id": f"{PLATE_PREFIX}:pick:{aid}", "placeholder": "Choose the next plate",
         "min_values": 1, "max_values": 1,
         "options": [{"label": o["label"][:100], "value": o["value"][:100]} for o in options[:25]]}]}]


def parse_plate_custom_id(custom_id: str) -> tuple[str, str] | None:
    """Return ('pick' | 'go' | 'spool', alert id) for plate picker controls, or None for unrelated ones."""
    parts = str(custom_id or "").split(":")
    if len(parts) == 3 and parts[0] == PLATE_PREFIX and parts[1] in ("pick", "go", "spool") and parts[2]:
        return parts[1], parts[2]
    return None


def post_plate_picker(token: str, channel_id: str, content: str, plan_id: str,
                      options: list[dict[str, str]]) -> bool:
    """Post the remaining-plate picker. False means the caller should fall back to a plain-text line."""
    if not token or not channel_id or not plan_id or not options:
        return False
    aid = record("plate-picker", "", plan_id=plan_id)
    body = json.dumps({"content": content[:1990], "components": plate_picker_components(aid, options),
                       "allowed_mentions": {"parse": []}}).encode()
    req = urllib.request.Request(f"https://discord.com/api/v10/channels/{channel_id}/messages", data=body,
                                 method="POST", headers={"Authorization": f"Bot {token}",
                                                         "Content-Type": "application/json",
                                                         "User-Agent": "DiscordBot (hermes-3dprinting, 1.0)"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return 200 <= resp.status < 300
    except Exception:
        take(aid)
        return False


def post_spool_question(token: str, channel_id: str, content: str, context: dict[str, Any],
                        message_id: str | None = None) -> str | None:
    """Post or edit one paused-spool question. Returns its Discord message id."""
    if not token or not channel_id:
        return None
    aid = record("spool-question", "", context=dict(context or {}), message_id=message_id or "")
    body = json.dumps({"content": content[:1990],
                       "components": spool_question_components(aid, context.get("prior_spool_id")),
                       "allowed_mentions": {"parse": []}}).encode()
    url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
    method = "POST"
    if message_id:
        url += f"/{message_id}"
        method = "PATCH"
    req = urllib.request.Request(url, data=body, method=method,
                                 headers={"Authorization": f"Bot {token}",
                                          "Content-Type": "application/json",
                                          "User-Agent": "DiscordBot (hermes-3dprinting, 1.0)"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read() or b"{}")
            return str(data.get("id") or message_id or "") or None
    except Exception:
        take(aid)
        return None


def edit_message(token: str, channel_id: str, message_id: str, content: str) -> bool:
    """Replace the text of one bot message in place. False on any failure."""
    if not (token and channel_id and message_id):
        return False
    req = urllib.request.Request(
        f"https://discord.com/api/v10/channels/{channel_id}/messages/{message_id}",
        data=json.dumps({"content": content[:1990], "allowed_mentions": {"parse": []}}).encode(),
        method="PATCH", headers={"Authorization": f"Bot {token}", "Content-Type": "application/json",
                                 "User-Agent": "DiscordBot (hermes-3dprinting, 1.0)"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False


def post_insertion_confirmation(token: str, channel_id: str, content: str, slot: int) -> bool:
    """Post an answer-only IFS insertion UI. It has no printer or inventory side effect."""
    if not token or not channel_id or slot not in (1, 2, 3, 4):
        return False
    aid = record("insertion", "", slot=slot)
    body = json.dumps({"content": content[:1990], "components": insertion_components(aid),
                       "allowed_mentions": {"parse": []}}).encode()
    req = urllib.request.Request(f"https://discord.com/api/v10/channels/{channel_id}/messages", data=body,
                                 method="POST", headers={"Authorization": f"Bot {token}",
                                                         "Content-Type": "application/json",
                                                         "User-Agent": "DiscordBot (hermes-3dprinting, 1.0)"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return 200 <= resp.status < 300
    except Exception:
        take(aid)
        return False


def insertion_select_options(spools: list[dict[str, Any]], mapped: dict[str, Any] | None = None,
                             excluded_ids: set[int] | None = None) -> list[dict[str, str]]:
    """Discord select options, closest material/color to the old slot record first."""
    mapped = mapped or {}
    excluded_ids = excluded_ids or set()
    material = str(mapped.get("material") or "").casefold()
    color = str(mapped.get("color") or "").casefold()

    def score(spool):
        filament = spool.get("filament") or {}
        candidate_material = str(filament.get("material") or "").casefold()
        candidate_color = str(filament.get("name") or "").casefold()
        return (candidate_material != material, candidate_color != color, int(spool.get("id") or 0))

    options = []
    for spool in sorted((s for s in spools if isinstance(s, dict) and not s.get("archived")
                         and int(s.get("id") or 0) not in excluded_ids), key=score)[:25]:
        filament = spool.get("filament") or {}
        grams = spool.get("remaining_weight")
        detail = " ".join(part for part in (str(filament.get("name") or "").strip(),
                                               str(filament.get("material") or "").strip(),
                                               f"{grams:g} g" if isinstance(grams, (int, float)) else "") if part)
        options.append({"label": f"#{spool.get('id')} {detail}"[:100], "value": str(spool.get("id")),
                        "description": "Select this shelf spool"})
    return options


def new_insertion_fields() -> list[dict[str, Any]]:
    """Modal contract kept data-only so it can be covered without a gateway."""
    return [
        {"key": "brand", "label": "Brand (optional)", "required": False, "max_length": 100},
        {"key": "material", "label": "Material", "required": True, "max_length": 50},
        {"key": "color", "label": "Color / finish", "required": True, "max_length": 100},
        {"key": "grams", "label": "Full starting grams", "required": True, "max_length": 12},
        {"key": "lot", "label": "Lot (optional)", "required": False, "max_length": 100},
    ]


def bind_insertion_existing(slot: int, spool_id: int, spool_sync) -> str:
    """Reuse the one slot-binding path; insertion UI never issues a printer command."""
    result = spool_sync.bind_slot(slot, spool_id, note="Bound from Discord IFS insertion", push_screen_update=False)
    record = result["record"]
    grams = f", {record['remaining_grams']:g} g left" if record.get("remaining_grams") is not None else ""
    return f"Slot {slot} → spool #{spool_id} ({record['name']}{grams}). Inventory updated; printer untouched."


def create_and_bind_insertion(slot: int, values: dict[str, Any], spool_sync) -> str:
    """Create the operator-described spool and bind it in one inventory-only step."""
    spec = spool_sync.parse_new(", ".join(str(values.get(key) or "").strip() for key in ("brand", "material", "color", "grams")))
    spec["lot"] = str(values.get("lot") or "").strip() or None
    made = spool_sync.create_spool(spec)
    result = spool_sync.bind_slot(slot, int(made["spool_id"]), note="Created and bound from Discord IFS insertion",
                                  push_screen_update=False)
    record = result["record"]
    return f"Created spool #{made['spool_id']}; slot {slot} → #{made['spool_id']} ({record['name']}). Inventory updated; printer untouched."


def post_with_buttons(token: str, channel_id: str, content: str, kind: str, filename: str) -> bool:
    """Post the alert with buttons. False means the caller should fall back to plain text."""
    if not token or not channel_id:
        return False
    aid = record(kind, filename)
    body = json.dumps({"content": content[:1990], "components": components(aid, kind),
                       "allowed_mentions": {"parse": []}}).encode()
    req = urllib.request.Request(f"https://discord.com/api/v10/channels/{channel_id}/messages", data=body,
                                 method="POST", headers={"Authorization": f"Bot {token}",
                                                         "Content-Type": "application/json",
                                                         "User-Agent": "DiscordBot (hermes-3dprinting, 1.0)"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            ok = 200 <= resp.status < 300
            if ok:
                with contextlib.suppress(Exception):
                    mid = json.loads(resp.read() or b"{}").get("id")
                    if mid:
                        with _LOCK:
                            data = _load()
                            if aid in data:
                                data[aid].update(message_id=str(mid), channel_id=str(channel_id))
                                _save(data)
            return ok
    except Exception:
        take(aid)
        return False


def retire_alerts(token: str, filename: str | None, note: str, kinds: tuple[str, ...] = ("shutdown", "error", "pause")) -> int:
    """Strip the buttons from stale alerts for this job and say why, so a dead Cancel button is not left behind."""
    retired = 0
    with _LOCK:
        data = _load()
        stale = {aid: a for aid, a in data.items() if a.get("kind") in kinds
                 and (filename is None or a.get("filename") == filename)}
        for aid in stale:
            data.pop(aid, None)
        _save(data)
    for alert in stale.values():
        mid, chan = alert.get("message_id"), alert.get("channel_id")
        if not (token and mid and chan):
            continue
        req = urllib.request.Request(
            f"https://discord.com/api/v10/channels/{chan}/messages/{mid}",
            data=json.dumps({"components": [], "content": f"~~Printer firmware stopped.~~ {note}"[:1990],
                             "allowed_mentions": {"parse": []}}).encode(),
            method="PATCH", headers={"Authorization": f"Bot {token}", "Content-Type": "application/json",
                                     "User-Agent": "DiscordBot (hermes-3dprinting, 1.0)"})
        with contextlib.suppress(Exception):
            with urllib.request.urlopen(req, timeout=15) as resp:
                retired += 200 <= resp.status < 300
    return retired


def post_visual_alert(token: str, channel_id: str, content: str, filename: str, image: Path) -> bool:
    """Post one camera image and its safe visual-review buttons in a single Discord message."""
    if not token or not channel_id or not image.is_file():
        return False
    aid = record("visual", filename)
    boundary = f"----hermes-print-{secrets.token_hex(12)}"
    payload = json.dumps({"content": content[:1990], "components": components(aid, "visual"),
                          "allowed_mentions": {"parse": []}}).encode()
    try:
        frame = image.read_bytes()
        if not frame:
            raise ValueError("empty camera frame")
        body = b"".join((
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"payload_json\"\r\n"
            "Content-Type: application/json\r\n\r\n".encode() + payload + b"\r\n",
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"files[0]\"; filename=\"{image.name}\"\r\n"
            "Content-Type: image/jpeg\r\n\r\n".encode() + frame + b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ))
        req = urllib.request.Request(f"https://discord.com/api/v10/channels/{channel_id}/messages", data=body,
                                     method="POST", headers={"Authorization": f"Bot {token}",
                                                              "Content-Type": f"multipart/form-data; boundary={boundary}",
                                                              "User-Agent": "DiscordBot (hermes-3dprinting, 1.0)"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            return 200 <= resp.status < 300
    except Exception:
        take(aid)
        return False

def parse_custom_id(custom_id: str) -> tuple[str, str] | None:
    parts = str(custom_id or "").split(":")
    if len(parts) == 3 and parts[0] == PREFIX and parts[1] in ("resume", "cancel", "pause", "acknowledge"):
        return parts[1], parts[2]
    return None


def parse_insertion_custom_id(custom_id: str) -> tuple[str, str] | None:
    """Return the inventory-only insertion choice and alert id, or None for unrelated controls."""
    parts = str(custom_id or "").split(":")
    if len(parts) == 3 and parts[0] == INSERTION_PREFIX and parts[1] in ("same", "existing", "new") and parts[2]:
        return parts[1], parts[2]
    return None


def parse_spool_question_custom_id(custom_id: str) -> tuple[str, str] | None:
    """Return a paused-spool choice and alert id, or None for unrelated controls."""
    parts = str(custom_id or "").split(":")
    if len(parts) == 3 and parts[0] == SPOOL_QUESTION_PREFIX and parts[1] in ("same", "existing", "new") and parts[2]:
        return parts[1], parts[2]
    return None


def clear_shutdown(wait_s: float = 60, poll_s: float = 2.0, *, caller=None, reader=None) -> tuple[bool, str]:
    """FIRMWARE_RESTART once after an emergency stop, then wait for Klipper ready. No motion, no heat."""
    from . import print_control as pc
    call = caller or _call
    read = reader or pc.status
    try:
        call("/printer/firmware_restart", method="POST", timeout=15)
    except Exception:
        pass  # the restart drops the connection; readback decides
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        time.sleep(poll_s)
        try:
            st = read()
        except Exception:
            continue
        if st.get("klippy") == "ready":
            return True, "Cleared the emergency stop. Klipper is ready, heaters off."
    return False, f"Sent a firmware restart, but Klipper wasn't ready after {wait_s:.0f}s. Check the printer screen. Not retrying."


def handle_click(action: str, alert: dict[str, Any] | None) -> str:
    """Run one button action. Always re-reads state first; never retries."""
    from . import print_control as pc
    from . import print_stop
    if not alert:
        return "That alert is old or was already answered. Nothing sent."
    filename = alert.get("filename") or ""
    if alert.get("kind") == "visual":
        if action == "acknowledge":
            return "Visual check acknowledged. Monitoring continues; nothing sent to the printer."
        if action == "pause":
            st = pc.status()
            if st.get("klippy") != "ready":
                return f"The printer's controller is {st.get('klippy')}. Nothing sent."
            if st.get("state") != "printing":
                return f"Nothing to pause: printer is {st.get('state')}. Nothing sent."
            if filename and st.get("filename") and st.get("filename") != filename:
                return "A different print is loaded now than the one in this alert. Nothing sent."
            pc._call("/printer/print/pause", method="POST", timeout=15)
            return "Pause sent. Check the printer screen for the current state."
        return "That visual alert action is not recognized. Nothing sent."
    st = pc.status()
    if action == "resume":
        return pc.resume(filename)[1]
    # cancel
    notes = []
    if st.get("klippy") == "shutdown":
        ok, msg = clear_shutdown()
        notes.append(msg)
        if not ok:
            return "\n".join(notes)
        st = pc.status()
    if st.get("state") in print_stop.ACTIVE:
        if filename and st.get("filename") and st["filename"] != filename:
            notes.append("A different print is loaded now than the one in this alert. Not cancelling it.")
        else:
            notes.append(print_stop.cancel(st.get("filename") or filename)[1])
    else:
        notes.append(f"The job is already gone (printer is {st.get('state')}). Nothing to cancel.")
    return "\n".join(notes)
