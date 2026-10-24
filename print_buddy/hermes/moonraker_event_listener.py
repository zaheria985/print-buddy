#!/usr/bin/env python3
"""Event driven replacement for the old 5 minute Moonraker cron job. No AI agent involved.

Holds a websocket subscription to Moonraker print_stats. On every print state change
(and on every (re)connect) it runs the existing announcer logic in
moonraker_discord_announcements.main(), captures what it would have printed, and
posts that to #3d-printer through `hermes send` (bot token only, no LLM).

A light local tick (default 10 min) re-runs the same logic so the 2 hour post print
quiet reset and the 4 AM ET backup reset fire without any external scheduler. The
tick makes no model calls and posts nothing unless a transition occurred.
"""

import asyncio
import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime
from pathlib import Path

import websockets

from .. import healthcheck, printers
from . import _config
from . import moonraker_discord_announcements as announcer
from . import discord_print_summary as print_summary
from . import plan_timelapse
from . import print_control
INTERRUPTED_HINT = print_control.INTERRUPTED_HINT
try:
    from . import maintenance as maintenance_state
except ImportError:
    maintenance_state = None

_LISTENER = _config.cfg()["listener"]
WS_URL = str(_config.cfg()["services"]["moonraker_ws_url"])
DISCORD_TARGET = f"discord:{announcer.DISCORD_CHAT_ID}"
TICK_SECONDS = float(_LISTENER["tick_s"])
HERMES_BIN = _config.hermes_bin()
LOG = announcer.PROFILE_HOME / "logs" / "moonraker_event_listener.log"
TERMINAL = {"cancelled": "Print cancelled", "error": "Print stopped with an error"}


def log(line):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as fh:
        fh.write(f"{datetime.now().astimezone().isoformat(timespec='seconds')} {line}\n")


def post(message):
    """Deliver physical print lifecycle text to #3d-printer only, with no agent."""
    target = DISCORD_TARGET
    if target == "discord:":
        log("post skipped: discord.chat_id is not configured")
        return False
    if _config.standalone():
        return _post_rest(message)
    try:
        result = subprocess.run(
            [HERMES_BIN, "-p", "3dprinting", "send", "--quiet", "--to", target, message],
            capture_output=True, text=True, timeout=120,
        )
        log(f"post profile=3dprinting target={target} rc={result.returncode} {message.splitlines()[0][:120]!r} {result.stderr.strip()[:200]}")
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired) as exc:
        log(f"post failed profile=3dprinting target={target}: {exc!r}")
        return False


def _post_rest(message):
    """Standalone mode: Discord REST with the bot token. Works with no gateway connected."""
    from print_buddy.bot import rest
    ok, detail = rest.send(_bot_token(), announcer.DISCORD_CHAT_ID, message)
    log(f"post rest target={DISCORD_TARGET} ok={ok} {message.splitlines()[0][:120] if message else ''!r}"
        f"{'' if ok else ' ' + str(detail)[:200]}")
    return ok


def post_alert(message, kind, filename):
    """Post with Resume/Cancel buttons; fall back to plain text if Discord refuses."""
    try:
        from . import print_alert_buttons as pab
        if pab.post_with_buttons(_bot_token(), announcer.DISCORD_CHAT_ID,
                                 message, kind, filename):
            log(f"post buttons kind={kind} {message.splitlines()[0][:120]!r}")
            return True
        log("button post failed; falling back to plain text")
    except Exception as exc:
        log(f"button post error: {exc!r}")
    return post(message)


def post_plate_picker(plan_id):
    """Post the interactive remaining-plate picker; name the command only when it cannot be delivered."""
    try:
        from . import print_alert_buttons as pab
        from . import print_next
        from .print_records import remaining_plate_choices
        plan = print_next.load_plan(plan_id)
        parts = remaining_plate_choices(plan)
        if not parts:
            return False
        options = [{"label": print_next.plate_choice_label(part), "value": str(part["id"])} for part in parts]
        if pab.post_plate_picker(_bot_token(), announcer.DISCORD_CHAT_ID,
                                 "Which plate is next? Clear the bed, then choose a plate.", plan_id, options):
            log(f"post plate picker plan={plan_id} plates={len(options)}")
            return True
        log(f"plate picker post failed plan={plan_id}; falling back to text")
    except Exception as exc:
        log(f"plate picker post error plan={plan_id}: {exc!r}")
    return post(f"To start the next plate, type `/print-next {plan_id}`.")


def post_insertion_confirmation(slot):
    """Offer an answer-only IFS insertion UI, then retain the command-text fallback."""
    try:
        from . import print_alert_buttons as pab
        text = insertion_confirmation_text(slot)
        if pab.post_insertion_confirmation(_bot_token(), announcer.DISCORD_CHAT_ID,
                                           text, slot):
            log(f"post insertion confirmation slot={slot}")
            return True
        log(f"insertion confirmation button post failed slot={slot}; falling back to text")
    except Exception as exc:
        log(f"insertion confirmation button post error slot={slot}: {exc!r}")
    return post(insertion_prompt(slot))


def post_spool_question(record, nag=False):
    """Post once or refresh a paused-spool question without stacking nag messages."""
    try:
        from . import print_alert_buttons as pab
        previous = str(record.get("discord_message_id") or "") if nag else ""
        if nag and not previous:
            log("spool question nag skipped: no Discord message id to refresh")
            return False
        message_id = pab.post_spool_question(_bot_token(), announcer.DISCORD_CHAT_ID,
                                             spool_question_text(record, nag=nag), record, previous or None)
        if message_id:
            record["discord_message_id"] = message_id
            atomic_write_json(RUNOUT_PAUSE_STATE, record)
            log(f"spool question {'refreshed' if previous else 'posted'} message={message_id}")
            return True
        log("spool question component post failed")
    except Exception as exc:
        log(f"spool question component post error: {exc!r}")
    if nag:
        log("spool question nag left unchanged to avoid stacking messages")
        return False
    log("spool question component post failed; falling back to text")
    return post(spool_question_text(record))


def shutdown_alert_text(filename, state_message=None):
    from . import print_alert_buttons as pab
    if state_message is None:
        state_message = pab.shutdown_message()
    return ("**Printer firmware stopped.**\n" + print_summary.format_event("error", filename)
            + "\n**Why:** " + pab.explain(state_message)
            + "\nAn emergency stop can't be resumed. Clear the bed, then tap Cancel print "
              "(it clears the stop and ends the job, no motion), or start over with /print-again.")


def post_cancel_report(filename):
    """Full Klipper report on any cancel; falls back to the short summary if the report fails."""
    try:
        from . import klipper_report
        text = print_summary.format_event("cancelled", filename) + "\n" + klipper_report.report(filename)
    except Exception as exc:
        log(f"klipper report failed: {exc!r}")
        text = print_summary.format_event("cancelled", filename)
    return post(text)


SCREEN_CANCEL_NATIVE = {"cancel", "cancelled"}
SCREEN_CANCEL_BUSY = {"printing", "paused", "pausing", "error", "busy"}


def native_screen_cancel(prev, now):
    """Stock firmware left a job (printing/paused/error) for cancel or ready."""
    return prev in SCREEN_CANCEL_BUSY and now in SCREEN_CANCEL_NATIVE | {"ready"}


def screen_cancel_reboot(filename, *, status_reader=None, rebooter=None, clearer=None, poster=None):
    """House rule: a cancel from the printer screen force restarts Klipper. Never while printing."""
    from . import print_alert_buttons as pab
    read = status_reader or print_control.status
    say = poster or post
    st = read()
    if st.get("state") == "printing":
        log("screen cancel reboot skipped: printer is printing again")
        return False, "skipped"
    label = print_summary.safe_label(filename) if filename else "the print"
    if st.get("klippy") == "shutdown":
        say(f"Screen cancel on {label} during an emergency stop. Clearing it with one firmware restart (no motion or heat).")
        ok, msg = (clearer or pab.clear_shutdown)()
    else:
        say(f"Screen cancel on {label}. Force restarting the printer now, about two minutes.")
        ok, msg = (rebooter or print_control.force_reboot)()
    log(f"screen cancel reboot ok={ok}: {msg}")
    say(msg)
    return ok, msg


def post_shutdown_alert(filename):
    return post_alert(shutdown_alert_text(filename), "shutdown", filename)


def _profile_env_value(name):
    """Read one profile env value. The listener unit loads no EnvironmentFile."""
    return _config.profile_env_value(name)


def _bot_token():
    """The Discord bot token, from the variable [discord] token_env names (DISCORD_BOT_TOKEN by default)."""
    return _profile_env_value(str(_config.cfg()["discord"].get("token_env") or "DISCORD_BOT_TOKEN"))


def generate_roast(title, summary=""):
    """Generate a quick 1-sentence roast via Luna LLM for the bot persona. Best-effort."""
    title = str(title or "").strip()
    if not title:
        return ""
    key = _profile_env_value("CLIPROXY_API_KEY")
    if not key:
        return ""
    chat_url = _config.service_url("vision_chat_url")
    if not chat_url:
        log("roast skipped: services.vision_chat_url is not configured")
        return ""
    prompt = f"Model: {title}"
    if summary:
        prompt += f" ({summary})"
    req = urllib.request.Request(
        chat_url,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        data=json.dumps({
            "model": str(_config.cfg()["vision"]["roast_model"]),
            "messages": [
                {"role": "system", "content": f"You are {_config.bot_name()}, a witty 3D printer assistant who gently roasts 3D prints. Give ONE short funny sentence roasting this model. Keep it under 20 words. No quotes, no hashtags, no introductory filler."},
                {"role": "user", "content": prompt}
            ],
            "max_tokens": 50,
            "temperature": 0.8
        }).encode()
    )
    try:
        with urllib.request.urlopen(req, timeout=4) as resp:
            data = json.loads(resp.read())
            res = data["choices"][0]["message"]["content"].strip().strip("\"'")
            return res if res else ""
    except Exception:
        return ""


def start_message_with_roast(filename, archive=None, context_path=None):
    """Format the canonical start message and append a best-effort model roast."""
    message = print_summary.format_event("started", filename, archive=archive, context_path=context_path)
    job, _plate = print_summary.find_job(filename, archive=archive)
    title = (job or {}).get("title") or Path(str(filename or "")).stem
    roast = generate_roast(title)
    return f"{message}\n\n{roast}" if roast else message


def paused_message(filename, runout=False):
    """One alert when a print pauses. Never pauses, resumes, or cancels anything itself."""
    where = ""
    try:
        from . import print_stop
        where = print_stop.where_text(print_stop.status())
    except Exception as exc:
        log(f"paused status read failed: {exc!r}")
    if runout:
        extra = "Filament runout. Load the replacement at the printer and resume there. I will not resume it."
    else:
        extra = (f"Paused at {where}. " if where else "") + "Fix it at the printer, then tap Resume or Cancel below (or type /print-resume or /print-stop)."
    return print_summary.format_event("paused", filename, extra=extra)


def recovery_slot(filename):
    """The slot and spool this job was using, from the hardware map. No printer call."""
    try:
        state = json.loads(HARDWARE_STATE.read_text())
    except (OSError, ValueError):
        return None, None
    slots = state.get("ifs_slots") or {}
    active = state.get("active_slot")
    slot = active if active in (1, 2, 3, 4) else None
    record = slots.get(str(slot)) if slot else None
    return slot, (record or {}).get("spoolman_id")


def list_spools():
    """Read Spoolman inventory. A failure returns nothing rather than inventing a spool."""
    import urllib.request
    base = _config.service_url("spoolman_url")
    if not base:
        log("spool inventory read skipped: services.spoolman_url is not configured")
        return []
    url = base + "/api/v1/spool"
    try:
        with urllib.request.urlopen(url, timeout=8) as response:
            payload = json.loads(response.read().decode())
    except Exception as exc:
        log(f"spool inventory read failed: {exc!r}")
        return []
    return payload if isinstance(payload, list) else []


def spool_matches_recovery(existing_material, candidate_material, existing_color, candidate_color):
    """PLA and PLA+ can match, but color/finish must match exactly."""
    def normalized(value):
        return " ".join(str(value or "").casefold().split())

    materials = {normalized(existing_material), normalized(candidate_material)}
    materials_compatible = materials <= {"pla", "pla+"}
    if not materials_compatible and normalized(existing_material) != normalized(candidate_material):
        return False
    return bool(normalized(existing_color)) and normalized(existing_color) == normalized(candidate_color)


def classify_pause(message, where=""):
    """A pause is a filament runout only when the alert text says so.

    Moonraker's print_stats payload has no pause cause. Ordinary pauses stay ordinary.
    """
    text = f"{message or ''} {where or ''}".casefold()
    return "runout" if "runout" in text or "filament sensor" in text else "other"


def persist_runout_pause(filename, slot, prior_spool_id, path=None):
    """Remember the depleted slot until the operator answers. No printer action."""
    record = {
        "filename": filename or "",
        "slot": slot if slot in (1, 2, 3, 4) else None,
        "prior_spool_id": prior_spool_id,
        "cause": "runout",
        "recorded_at": time.time(),
    }
    atomic_write_json(Path(path or RUNOUT_PAUSE_STATE), record)
    return record


def load_runout_pause(path=None, max_age_s=6 * 60 * 60):
    record = read_json_object(Path(path or RUNOUT_PAUSE_STATE))
    recorded = record.get("recorded_at")
    if record.get("cause") not in ("runout", "swap") or not isinstance(recorded, (int, float)):
        return None
    # Suspended usage never expires: nobody is being charged until a human answers.
    if not record.get("usage_suspended") and time.time() - recorded > max_age_s:
        return None
    return record


def pause_is_runout(message, slot, slot_presence, head_has_filament):
    """Runout = the alert says so, the paused job's slot reads empty, or the head sensor is empty."""
    if classify_pause(message) == "runout":
        return True
    if slot in (1, 2, 3, 4) and isinstance(slot_presence, dict) and slot_presence.get(slot) is False:
        return True
    return head_has_filament is False


def open_spool_question(filename, slot, prior_spool_id, cause, suspended_spool, path=None):
    """Persist an unanswered spool question. Usage is already suspended by the caller."""
    record = {
        "filename": filename or "",
        "slot": slot if slot in (1, 2, 3, 4) else None,
        "prior_spool_id": prior_spool_id,
        "cause": cause,
        "usage_suspended": True,
        "suspended_spool_id": suspended_spool,
        "recorded_at": time.time(),
        "last_nag_at": time.time(),
        "nags": 0,
    }
    atomic_write_json(Path(path or RUNOUT_PAUSE_STATE), record)
    return record


def spool_question_text(record, nag=False):
    slot = record.get("slot")
    where = f"IFS slot {slot}" if slot in (1, 2, 3, 4) else "the active IFS slot"
    prior = record.get("prior_spool_id")
    why = ("ran out" if record.get("cause") == "runout"
           else "was unloaded and reloaded mid-print")
    head = (f"**Still waiting (reminder {record.get('nags', 0)})**: which spool is in {where}?" if nag
            else f"**Spool check:** the filament in {where} {why}.")
    same = ("\n• `same` if you put spool #%s back" % prior) if record.get("cause") == "swap" and prior else ""
    return (f"{head}\nFilament usage is **paused for every spool** until you answer, "
            f"so neither the old nor the new spool is charged.\nReply with:\n"
            f"• a spool number from Spoolman, e.g. `12`{same}\n"
            f"• or a new spool: `brand, material, color, grams` e.g. `Sunlu, PLA+, White, 1000`\n"
            "`/spool list` shows what is on the shelf. I will not touch the printer.")


def clear_runout_pause(path=None):
    target = Path(path or RUNOUT_PAUSE_STATE)
    if target.exists():
        target.unlink()


def compatible_offers(spools, material, color):
    """Offer same color/finish PLA or PLA+ spools. Never pick one."""
    offers = []
    for item in spools or []:
        filament = item.get("filament") or {}
        if item.get("archived") is True:
            continue
        if spool_matches_recovery(material, filament.get("material") or item.get("material"),
                                  color, filament.get("name") or item.get("color") or item.get("name")):
            offers.append({"id": item.get("id"), "remaining_weight": item.get("remaining_weight")})
    return offers


EXISTING_CHOICE = "existing spool"
NEW_CHOICE = "new spool"


def _grams(value):
    text = str(value or "").strip()
    # Discord replies commonly end in punctuation; accept a grams suffix but not stray prose.
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(?:g)?\s*[.!?]*", text, re.I)
    if not match:
        return None
    try:
        grams = float(match.group(1))
    except (TypeError, ValueError):
        return None
    if grams <= 0:
        return None
    return round(grams, 2)


def parse_runout_reply(text):
    """Turn one Discord reply into an existing-spool or new-spool choice. No writes."""
    raw = " ".join(str(text or "").split())
    folded = raw.casefold()
    if not raw:
        return {"status": "need_choice"}
    if folded in {"a", "existing", EXISTING_CHOICE}:
        return {"status": "need_spool_number"}
    if folded in {"b", "new", NEW_CHOICE}:
        return {"status": "need_new_fields"}
    spool_number = None
    if folded.startswith("#") and folded[1:].isdigit():
        spool_number = int(folded[1:])
    elif folded.isdigit():
        spool_number = int(folded)
    if spool_number:
        return {"status": "existing", "spool_id": spool_number}
    parts = [part.strip() for part in raw.split(",")]
    if len(parts) < 3:
        return {"status": "unparsed", "text": raw}
    brand = parts[0] or None
    material, color = parts[1], parts[2]
    grams = _grams(parts[3]) if len(parts) > 3 else None
    lot = parts[4] if len(parts) > 4 else None
    if not material or not color or grams is None:
        return {"status": "unparsed", "text": raw}
    return {"status": "new", "brand": brand, "material": material, "color": color,
            "grams": grams, "lot": lot}


def apply_runout_choice(state, context, choice, *, client, filaments=None):
    """Bind the paused slot. Inventory writes only. Never calls the printer.

    client(method, path, body) stands in for Spoolman. filaments is a list of
    existing filament records used to avoid a duplicate type.
    """
    slot = context.get("slot")
    if slot not in (1, 2, 3, 4):
        raise ValueError("runout pause has no IFS slot")
    prior_id = context.get("prior_spool_id")
    if choice.get("status") == "existing":
        spool_id = int(choice["spool_id"])
        if prior_id is not None and spool_id == int(prior_id):
            raise ValueError(f"spool #{spool_id} is the depleted spool; pick another or say new")
        record = client("GET", f"/api/v1/spool/{spool_id}", None)
        if record.get("archived") is True:
            raise ValueError(f"spool #{spool_id} is archived")
        filament = record.get("filament") or {}
        client("PATCH", f"/api/v1/spool/{spool_id}", {"location": f"IFS Slot {slot}"})
        if prior_id and int(prior_id) != spool_id:
            client("PATCH", f"/api/v1/spool/{int(prior_id)}",
                   {"archived": True, "remaining_weight": 0, "location": "depleted"})
        return {"action": "existing", "slot": slot, "spool_id": spool_id,
                "material": filament.get("material"), "color": filament.get("name"),
                "prior_spool_id": prior_id, "printer_commands": []}
    if choice.get("status") != "new":
        raise ValueError("choice is not an existing or new spool")
    grams = _grams(choice.get("grams"))
    material = str(choice.get("material") or "").strip()
    color = str(choice.get("color") or "").strip()
    if grams is None or not material or not color:
        raise ValueError("new spool needs material, color/finish, and full starting grams")
    match = None
    for filament in filaments or []:
        if (str(filament.get("material") or "").casefold() == material.casefold()
                and str(filament.get("name") or "").casefold() == color.casefold()
                and str((filament.get("vendor") or {}).get("name") or "").casefold()
                == str(choice.get("brand") or "").casefold()):
            match = filament
            break
    if match is None:
        match = client("POST", "/api/v1/filament", {
            "name": color, "material": material, "vendor_name": choice.get("brand") or None,
            "diameter": 1.75, "density": 1.24, "weight": grams,
        })
    created = client("POST", "/api/v1/spool", {
        "filament_id": match["id"], "initial_weight": grams, "remaining_weight": grams,
        "used_weight": 0, "location": f"IFS Slot {slot}", "lot_nr": choice.get("lot"),
        "comment": "runout replacement; starting weight supplied by operator",
    })
    if prior_id:
        client("PATCH", f"/api/v1/spool/{int(prior_id)}",
               {"archived": True, "remaining_weight": 0, "location": "depleted"})
    return {"action": "new", "slot": slot, "spool_id": created.get("id"),
            "filament_id": match.get("id"), "material": material, "color": color,
            "starting_grams": grams, "prior_spool_id": prior_id, "printer_commands": []}


def manual_resume_tracking_message(filename, slot=None, compatible_spools=None):
    """Prompt inventory reconciliation after a manual resume; never control the printer."""
    slot_text = f"IFS slot {slot}" if slot in (1, 2, 3, 4) else "the affected IFS slot"
    choices = []
    for item in compatible_spools or []:
        if isinstance(item, dict) and item.get("id") is not None:
            choices.append(f"#{item['id']} ({item.get('remaining_weight', 'unknown')} g remaining)")
    offer = (" Compatible same-color PLA/PLA+ spool found: " + ", ".join(choices) + ". You may choose its number; I will not select it automatically." if choices else "")
    return (
        f"Filament runout recovery for {print_summary.safe_label(filename)}, {slot_text}. "
        "Choose how to continue tracking: A) existing Spoolman spool, or B) new spool."
        + offer +
        " If existing, reply with its spool number or confirm the offered number. I will update assignment/tracking only. "
        "If new, load it and resume manually at the printer first. Once the live printer state shows the job resumed, "
        "send a clear label photo (preferred), or give optional brand, material, color/finish, and full starting weight in grams. "
        "I will then create a distinct spool at that full starting weight, mark the replaced exhausted spool depleted when confirmed, "
        "map this IFS slot to the new spool, and reconcile tracking sources. Similar-color records remain untouched. "
        "Inventory only: I will never start, resume, pause, heat, or move the printer."
    )


def _post_completion(message, completion_message):
    """Acknowledge only a matching pending notice after transport success."""
    delivered = post(message)
    if delivered:
        state = announcer.load_state()
        if state.get("pending_completion_message") == completion_message:
            state.pop("pending_completion_message", None)
            state["announced_completion"] = True
            announcer.save_state(state)
    return delivered


def run_announcer(reason):
    """Run announcer once; wait to make completion and its video one Discord message."""
    out = io.StringIO()
    pending = announcer.load_state().get("pending_completion_message")
    if pending:
        delivered = _post_completion(pending, pending)
        if not delivered or announcer.load_state().get("pending_completion_message"):
            # Avoid a second identical delivery attempt from main() in this pass.
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    announcer.main()
            except Exception as exc:  # never let one bad pass kill the listener
                log(f"announcer error ({reason}): {exc!r}")
            return
    try:
        with contextlib.redirect_stdout(out):
            announcer.main()
    except Exception as exc:  # never let one bad pass kill the listener
        log(f"announcer error ({reason}): {exc!r}")
        return
    lines = out.getvalue().splitlines()
    marker = next((line for line in lines if line.startswith("TIMELAPSE_POSTPROCESS:")), None)
    picker = next((line for line in lines if line.startswith(announcer.PLATE_PICKER_MARKER)), None)
    plan_id = picker.split(":", 1)[1].strip() if picker else None
    tl_line = next((line for line in lines if line.startswith(plan_timelapse.PLAN_MARKER)), None)
    message = "\n".join(line for line in lines
                        if not line.startswith(("TIMELAPSE_POSTPROCESS:", announcer.PLATE_PICKER_MARKER,
                                                plan_timelapse.PLAN_MARKER))).strip()
    if not message:
        return
    tl_plan = None
    if tl_line:
        try:
            tl_id, tl_stage, tl_order = tl_line[len(plan_timelapse.PLAN_MARKER):].strip().split(":")
            tl_plan = (tl_id, tl_stage, int(tl_order))
        except ValueError:
            log(f"ignoring malformed timelapse plan marker {tl_line!r}")
    if tl_plan and tl_plan[1] == "partial":
        # Plan stopped midway: post what exists so far, labeled partial. No picker.
        threading.Thread(target=_plan_timelapse_post, args=(None, message, tl_plan), daemon=True,
                         name="timelapse-partial").start()
        return
    if not marker:
        if tl_plan and tl_plan[1] == "final":
            _plan_timelapse_post(None, message, tl_plan)
        else:
            _post_completion(message, message)
        if plan_id:
            post_plate_picker(plan_id)
        return
    job_key = announcer._timelapse_job_key(marker.split(":", 1)[1].strip())
    if not job_key:
        log(f"timelapse postprocess has no matching active job for {marker!r}")
        if tl_plan and tl_plan[1] == "final":
            _plan_timelapse_post(None, message, tl_plan)
        else:
            _post_completion(message, message)
        if plan_id:
            post_plate_picker(plan_id)
        return
    # Rendering can take minutes; leave the websocket loop alone and post once when done.
    threading.Thread(target=_timelapse_postprocess, args=(job_key, message, plan_id, tl_plan), daemon=True,
                     name="timelapse-postprocess").start()


def _timelapse_postprocess(job_key, completion_message, plan_id=None, tl_plan=None):
    try:
        if tl_plan:
            _plan_timelapse_post(job_key, completion_message, tl_plan)
        else:
            _timelapse_post(job_key, completion_message)
    finally:
        # The picker follows the completion/timelapse post, whatever happened to the video.
        if plan_id:
            post_plate_picker(plan_id)


def _plan_timelapse_post(job_key, completion_message, tl_plan):
    """A plate of a multi-plate plan: keep its clip, post video only once, after the last plate or on cancel.

    Non-final plates post the completion text alone (the caller adds the picker). The final plate,
    or a stopped plan ("partial"), posts one video of every stored clip in plate order.
    """
    plan, stage, order = tl_plan
    clip = None
    if job_key and stage in ("more", "final"):
        try:
            archived = announcer.archive_completed_timelapse(job_key)
            if archived:
                clip = Path(str(announcer.materialize_archived_timelapse(archived)))
                plan_timelapse.add_clip(plan, order, clip)
            else:
                log(f"timelapse not rendered for {job_key!r} within the wait")
        except Exception as exc:
            log(f"plan timelapse clip for {job_key!r} unavailable: {exc!r}")
            clip = None
    if stage == "more":
        _post_completion(completion_message, completion_message)
        with contextlib.suppress(Exception):
            plan_timelapse.prune()  # abandoned plans never reach a final post
        return
    stored = plan_timelapse.clips(plan)
    expected = plan_timelapse.expected_clips(plan)
    # Partial when any plate's clip is missing, not only the final plate's.
    partial = stage == "partial" or clip is None or (expected is not None and len(stored) < expected)
    try:
        if not stored:
            _post_completion(completion_message, completion_message)
            return
        note = f"{completion_message}\nPartial timelapse: {len(stored)} plate(s) so far." if partial else completion_message
        try:
            combined = plan_timelapse.combine(plan)
        except Exception as exc:
            log(f"timelapse combine failed for plan {plan!r}: {exc!r}")
            _post_completion(completion_message, completion_message)
            return
        try:
            announcer.archive_local_timelapse(plan, combined)
        except Exception as exc:
            log(f"combined timelapse archive failed for plan {plan!r}: {exc!r}")
        video = combined
        try:
            video = discord_sized_video(combined)
        except Exception as exc:
            log(f"combined timelapse shrink failed for plan {plan!r}: {exc!r}")
        if not _post_completion(f"{note}\nMEDIA:{video}", completion_message):
            log(f"combined timelapse attach failed for plan {plan!r}; posting text only")
            _post_completion(f"{note}\nCombined timelapse too big for Discord.", completion_message)
    finally:
        plan_timelapse.clear(plan)
        with contextlib.suppress(Exception):
            plan_timelapse.prune()


def _timelapse_post(job_key, completion_message):
    try:
        archived = announcer.archive_completed_timelapse(job_key)
        if not archived:
            log(f"timelapse not rendered for {job_key!r} within the wait")
            _post_completion(completion_message, completion_message)
            return
        local_video = announcer.materialize_archived_timelapse(archived)
        try:
            local_video = discord_sized_video(local_video)
        except Exception as exc:  # oversize source still gets a try; the text fallback covers a 413
            log(f"timelapse shrink failed for {job_key!r}: {exc!r}")
        if not _post_completion(f"{completion_message}\nMEDIA:{local_video}", completion_message):
            # Never lose the completion notice because Discord refused the attachment.
            log(f"timelapse attach failed for {job_key!r}; posting text only")
            _post_completion(f"{completion_message}\nTimelapse archived but too big for Discord: {Path(str(archived)).name}", completion_message)
    except Exception as exc:
        log(f"timelapse postprocess failed for {job_key!r}: {exc!r}")
        _post_completion(completion_message, completion_message)


# Discord rejects bot uploads above the server limit (10 MiB unboosted) with HTTP 413.
DISCORD_UPLOAD_MAX_BYTES = int(_config.cfg()["discord"]["upload_max_bytes"])
# paths.ffmpeg: a bare name is looked up on PATH, a path is used as is.
FFMPEG_BIN = (shutil.which(_config.ffmpeg_bin()) or _config.ffmpeg_bin()) if "/" not in _config.ffmpeg_bin() else _config.ffmpeg_bin()


def _video_duration(path):
    probe = str(Path(FFMPEG_BIN).with_name("ffprobe"))
    out = subprocess.run([probe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True, timeout=60, check=True).stdout.strip()
    return float(out)


def discord_sized_video(path, max_bytes=None):
    """Return a copy of path re-encoded under the Discord upload cap, or path if it already fits."""
    max_bytes = max_bytes or DISCORD_UPLOAD_MAX_BYTES
    source = Path(str(path))
    if source.stat().st_size <= max_bytes:
        return source
    target = source.with_name(f"{source.stem}.discord.mp4")
    if target.is_file() and 0 < target.stat().st_size <= max_bytes:
        return target
    duration = max(1.0, _video_duration(source))
    # 90% of the budget for video; timelapses carry no audio.
    kbps = max(200, int(max_bytes * 8 * 0.90 / duration / 1000))
    for attempt_kbps in (kbps, int(kbps * 0.7)):
        partial = target.with_name(f".{target.name}.partial.mp4")
        subprocess.run([FFMPEG_BIN, "-y", "-v", "error", "-i", str(source), "-an",
                        "-vf", "scale='min(1280,iw)':-2", "-c:v", "libx264", "-preset", "veryfast",
                        "-b:v", f"{attempt_kbps}k", "-maxrate", f"{attempt_kbps}k", "-bufsize", f"{attempt_kbps * 2}k",
                        "-movflags", "+faststart", str(partial)],
                       capture_output=True, timeout=600, check=True)
        if partial.stat().st_size <= max_bytes:
            os.replace(partial, target)
            log(f"timelapse shrunk {source.stat().st_size} -> {target.stat().st_size} bytes at {attempt_kbps}k")
            return target
        partial.unlink(missing_ok=True)
    raise RuntimeError(f"could not shrink {source.name} under {max_bytes} bytes")



NOTIFICATION_STATE = _config.state_file("PRINT_BUDDY_NOTIFICATION_STATE", "lifecycle_notifications.json")


def _notification_identity(stats):
    """Stable per-job identity; prefer Moonraker history id, then filename plus start time."""
    job_id = stats.get("job_id") or stats.get("history_job_id")
    if job_id is not None:
        return f"job:{job_id}"
    filename = stats.get("filename")
    started = stats.get("print_start_time") or stats.get("start_time")
    if filename and started is not None:
        return f"file:{filename}|start:{started}"
    return None


def _claim_notification(identity, phase, path=None):
    """Persist an at-most-once lifecycle notification claim, atomically."""
    if not identity:
        return True
    target = Path(path or NOTIFICATION_STATE)
    state = read_json_object(target)
    key = f"{identity}|{phase}"
    if key in state.get("sent", []):
        return False
    sent = list(state.get("sent", []))
    sent.append(key)
    state["sent"] = sent[-500:]
    atomic_write_json(target, state)
    return True

def clean(filename):
    return print_summary.safe_label(filename, "print")


START_CLAIMS = _config.state_file("PRINT_BUDDY_START_CLAIMS", "print_buddy_start_claims.json")
START_CLAIM_TTL_S = float(_LISTENER["start_claim_ttl_s"])
NATIVE_POLL_SECONDS = float(_LISTENER["ifs_presence_poll_s"])
IFS_EDGE_STATE = _config.state_file("IFS_EDGE_STATE", "ifs_presence_edges.json")
IFS_PENDING_STATE = _config.state_file("IFS_PENDING_STATE", "ifs_pending_prompts.json")
RUNOUT_PAUSE_STATE = _config.state_file("RUNOUT_PAUSE_STATE", "runout_pause.json")
HARDWARE_STATE = _config.state_file("PRINTER_HARDWARE_STATE", "printer_hardware.json")


def read_json_object(path):
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def atomic_write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    os.replace(temporary, path)


def insertion_prompt(slot):
    return (f"Filament loaded in IFS slot {slot}. If it is the same spool, ignore this. "
            f"If it is a different one: `/spool change {slot} <spool#>` for a spool already in Spoolman, "
            f"or `/spool new brand, material, color, grams` then `/spool change {slot} <new#>`. "
            "`/spool list` shows the shelf. Nothing has been changed yet.")


def insertion_confirmation_text(slot):
    """Short human confirmation prompt for a newly detected idle IFS insertion."""
    return (f"**IFS slot {slot} has filament.** Is this the same spool already mapped to that slot? "
            "Tap Same spool, Existing spool, or New spool. No inventory or printer change happens until you answer.")


class FilamentInsertionMonitor:
    """Stable native slot presence detector. Sends text only, never edits inventory."""
    def __init__(self, edge_path=IFS_EDGE_STATE, pending_path=IFS_PENDING_STATE):
        self.edge_path = Path(edge_path)
        self.pending_path = Path(pending_path)
        self.baselined = False
        self.last_sample = {}
        self.stable_counts = {}
        self.transition_from = {}

    def observe(self, slots, *, idle, connected=True):
        """Return newly confirmed slots; startup/reconnect samples only establish baseline."""
        if not connected or not idle or not isinstance(slots, dict):
            self.last_sample = {}
            self.stable_counts = {}
            self.transition_from = {}
            self.baselined = False
            return []
        current = {str(slot): value for slot, value in slots.items() if str(slot) in {"1", "2", "3", "4"} and isinstance(value, bool)}
        if not self.baselined:
            self.last_sample = current
            self.stable_counts = {slot: 1 for slot in current}
            edges = read_json_object(self.edge_path)
            updated = False
            for slot, present in current.items():
                if edges.get(slot) != present:
                    edges[slot] = present
                    updated = True
            if updated:
                atomic_write_json(self.edge_path, edges)
            self.baselined = True
            return []
        stable = []
        edges = read_json_object(self.edge_path)
        pending = read_json_object(self.pending_path)
        changed = False
        for slot, present in current.items():
            prior = self.last_sample.get(slot)
            self.stable_counts[slot] = self.stable_counts.get(slot, 0) + 1 if prior == present else 1
            if prior != present:
                self.transition_from[slot] = prior
            # A quick pull/reinsert can produce only one absent sample: edges remains True,
            # but the physical change is still worth an inventory-only confirmation prompt.
            if (self.transition_from.get(slot) is False and present is True and edges.get(slot) is True):
                stable.append(int(slot))
                pending[slot] = {"event": f"slot-{slot}-present", "status": "awaiting_user_dispatch"}
                self.transition_from[slot] = None
            elif self.stable_counts[slot] >= 2 and edges.get(slot) != present:
                edges[slot] = present
                changed = True
                if self.transition_from.get(slot) is False and present is True:
                    stable.append(int(slot))
                    pending[slot] = {"event": f"slot-{slot}-present", "status": "awaiting_user_dispatch"}
                    self.transition_from[slot] = None
        self.last_sample = current
        if changed:
            atomic_write_json(self.edge_path, edges)
        if stable:
            atomic_write_json(self.pending_path, pending)
        return stable


def start_claim_time(filename, now=None):
    """When print-now claimed this job's start announcement, or None when unclaimed/expired."""
    now = time.time() if now is None else now
    try:
        claims = json.loads(START_CLAIMS.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(claims, dict):
        return None
    ts = claims.get(Path(str(filename or "")).name)
    return ts if isinstance(ts, (int, float)) and 0 <= now - ts < START_CLAIM_TTL_S else None


def start_claimed(filename, now=None):
    """True when model_job.py print-now already owns this job's start announcement."""
    return start_claim_time(filename, now) is not None


def announce_warmup_done(filename):
    """A claimed start already has its one "Print started" post: edit it, never post another.

    Edits the intake's message in place when its id was recorded; otherwise (or if Discord
    refuses the edit) posts one short, distinct line that is not a second "Print started".
    """
    text = print_summary.format_event("started", filename)
    text += "\nWarmup complete, now printing."
    message_id = None
    try:
        from . import print_next
        message_id = print_next.take_start_message(filename)
    except Exception as exc:
        log(f"start message lookup failed file={filename!r}: {exc!r}")
    if message_id:
        try:
            from . import print_alert_buttons as pab
            if pab.edit_message(_bot_token(), announcer.DISCORD_CHAT_ID,
                                message_id, text):
                log(f"start message edited id={message_id} file={filename!r}")
                return True
        except Exception as exc:
            log(f"start message edit error id={message_id}: {exc!r}")
        log(f"start message edit failed id={message_id}; posting short line")
    title = print_summary.safe_label(((print_summary.find_job(filename)[0] or {}).get("title")) or filename, "print")
    return post(f"Warmup done, now printing: {title}")


SUBSCRIBE = json.dumps({
    "jsonrpc": "2.0", "id": 1, "method": "printer.objects.subscribe",
    "params": {"objects": {"print_stats": ["state", "filename", "message", "info"],
                             "gcode_move": ["gcode_position"], "toolhead": ["position"]}},
})

# ZMod START_PRINT logs `Printing with bed mesh: <profile>`. A blank profile means no mesh is
# applied; that is what made the 2026-10-03 first layers print too high.
NO_MESH_ALERT = ("**Heads up: this print started with NO bed mesh.** The first layer will likely be too high or "
                 "uneven, like the cancelled one on Oct 3. Watch it, and cancel if it is not sticking. Fix: run "
                 "full bed leveling on the printer screen before the next print.")


def no_mesh_line(line):
    """True when a gcode response line reports a print starting with no bed mesh profile."""
    marker = "Printing with bed mesh:"
    if not isinstance(line, str) or marker not in line:
        return False
    return line.split(marker, 1)[1].strip() == ""


SPOOL_NAG_SECONDS = float(_LISTENER["spool_nag_s"])


def _camera_url():
    return _config.require(_config.service_url("camera_snapshot_url"), "services.camera_snapshot_url")


def _vision_chat_url():
    return _config.require(_config.service_url("vision_chat_url"), "services.vision_chat_url")


VISUAL_VERDICTS = {"ok", "air", "spaghetti", "detached", "empty_idle", "uncertain"}
VISUAL_ALERT_VERDICTS = {"air", "spaghetti", "detached", "uncertain"}
_VISION_BASE = _config.service_url("vision_chat_url")
_VISION_MODEL = str(_config.cfg()["vision"]["model"])
_VISION_CONTEXT = (
    "Printer: Flashforge AD5X, enclosed, smooth PEI plate. "
    "Reply with JSON only, no markdown: {\"verdict\":\"ok|air|spaghetti|detached|empty_idle|uncertain\",\"reason\":\"short\"}. "
    "ok means plastic is on the bed and printing normally. "
    "air means the bed is empty while a print should be there. "
    "spaghetti means a tangled failed extrusion. "
    "detached means a printed part has come off the plate. "
    "empty_idle means the printer is idle and the bed is empty. "
    "uncertain means the frame is not clear enough to judge."
)


_VISION_CONTEXT = "Printer: Flashforge AD5X, enclosed, smooth PEI plate. Reply JSON verdict ok|air|spaghetti|detached|empty_idle|uncertain. "


def _camera_snapshot_bytes():
    request = urllib.request.Request(_camera_url())
    with urllib.request.urlopen(request, timeout=8) as response:
        image = response.read(8 * 1024 * 1024 + 1)
    if len(image) > 8 * 1024 * 1024 or not image.startswith(bytes((0xff, 0xd8))):
        raise ValueError("camera response is not a bounded JPEG snapshot")
    return image


# Camera stall alert presets for [visual_watch] sensitivity, 1 (quietest) to 5 (loudest).
# Level 3 is the original hard-coded behavior. diff is the mean 64x48 grayscale difference a
# frame needs to count as changed; stall_layers is how many layers an unchanged frame may span;
# vision_veto lets an "ok" or "safe" vision verdict cancel a pixel-diff stall alert.
VISUAL_SENSITIVITY = {
    1: {"diff": 1.5, "stall_layers": 8, "periodic_s": 1800, "vision_veto": True},
    2: {"diff": 2.0, "stall_layers": 5, "periodic_s": 1200, "vision_veto": True},
    3: {"diff": 3.0, "stall_layers": 3, "periodic_s": 900, "vision_veto": False},
    4: {"diff": 4.0, "stall_layers": 3, "periodic_s": 600, "vision_veto": False},
    5: {"diff": 5.0, "stall_layers": 2, "periodic_s": 450, "vision_veto": False},
}
VISUAL_HEALTHY_VERDICTS = {"ok", "safe"}


def _visual_level(level=None):
    """The configured sensitivity level, 1 to 5 (3 when unset)."""
    if level is None:
        level = _config.cfg().get("visual_watch", {}).get("sensitivity", 3)
    level = int(level)
    if level not in VISUAL_SENSITIVITY:
        raise ValueError(f"visual_watch sensitivity must be 1 to 5, got {level!r}")
    return level


def _visual_profile(level=None):
    return VISUAL_SENSITIVITY[_visual_level(level)]


def _frame_changed(before, after, threshold=None):
    """Cheap whole-frame perceptual difference; false means visually unchanged."""
    from PIL import Image, ImageChops, ImageStat
    import io as _io
    a = Image.open(_io.BytesIO(before)).convert("L").resize((64, 48))
    b = Image.open(_io.BytesIO(after)).convert("L").resize((64, 48))
    if threshold is None:
        threshold = _visual_profile()["diff"]
    return ImageStat.Stat(ImageChops.difference(a, b)).mean[0] >= threshold


_VISION_CONTEXT = "Printer: Flashforge AD5X, enclosed, smooth PEI plate. Reply JSON verdict ok|air|spaghetti|detached|empty_idle|uncertain. "


def _strip_json_fence(text):
    raw = str(text or "").strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0]
    return raw.strip()


def _visual_verdict(image, prompt):
    endpoint = os.environ.get("PRINT_VISION_URL")
    try:
        if endpoint:
            payload = json.dumps({"image": __import__("base64").b64encode(image).decode(), "prompt": prompt}).encode()
            req = urllib.request.Request(endpoint, data=payload, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as response:
                result = json.loads(response.read())
            return str(result.get("verdict", "uncertain")).casefold(), str(result.get("reason", ""))[:300]
        key = _profile_env_value("CLIPROXY_API_KEY")
        if not key: return "uncertain", "vision key is not configured"
        encoded = __import__("base64").b64encode(image).decode()
        body = {"model": _VISION_MODEL, "temperature": 0, "max_tokens": 200,
                "messages": [{"role": "user", "content": [{"type": "text", "text": _VISION_CONTEXT + str(prompt)},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{encoded}"}}]}]}
        req = urllib.request.Request(_vision_chat_url(),
            data=json.dumps(body).encode(), headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as response: result = json.loads(response.read())
        parsed = json.loads(_strip_json_fence(result["choices"][0]["message"]["content"]))
        return str(parsed.get("verdict", "uncertain")).casefold(), str(parsed.get("reason", ""))[:300]
    except Exception as exc: return "uncertain", f"vision check failed: {exc!r}"


def _visual_layer(stats):
    info = stats.get("info") if isinstance(stats, dict) else None
    if not isinstance(info, dict):
        return None
    value = info.get("current_layer") or info.get("layer")
    return value if isinstance(value, int) else None


def _visual_alert(filename, image, reason):
    target = Path(os.environ.get("PRINT_WATCH_FRAME_DIR") or _config.cache_dir() / "print-watch")
    target.mkdir(parents=True, exist_ok=True)
    path = target / f"{int(time.time())}-{Path(filename or 'print').stem}.jpg"
    path.write_bytes(image)
    text = f"Print camera check: {clean(filename)}\n{reason}"
    try:
        from . import print_alert_buttons as pab
        if pab.post_visual_alert(_bot_token(), announcer.DISCORD_CHAT_ID,
                                 text, filename, path):
            log(f"post visual alert {clean(filename)!r}")
            return path
        log("visual button post failed; falling back to text")
    except Exception as exc:
        log(f"visual button post error: {exc!r}")
    post(text + f"\n{path}\nMEDIA:{path}")
    return path


def _slot_bools(slots):
    out = {}
    for key, value in (slots or {}).items():
        try:
            slot = int(key)
        except (TypeError, ValueError):
            continue
        if slot in (1, 2, 3, 4) and isinstance(value, bool):
            out[slot] = value
    return out


class VisualPrintWatch:
    """Pure trigger policy for periodic visual checks; never controls the printer."""
    def __init__(self, sensitivity=None):
        self.sensitivity = _visual_level(sensitivity)
        self.profile = VISUAL_SENSITIVITY[self.sensitivity]
        self.reset()

    def reset(self):
        self.last_layer = None
        self.last_check_at = 0.0
        self.last_snapshot = None
        self.last_snapshot_layer = 0
        self.progress_seen_layer = 0
        self.alerted_stall_layer = None
        self.layer2_checked = False
        self.layer5_checked = False
        self.last_alert_at = 0.0

    def due(self, layer, now, *, snapshot_changed=None):
        if not isinstance(layer, int) or layer < 1:
            return None
        if layer >= 2 and not self.layer2_checked:
            self.layer2_checked = True
            self.last_check_at = now
            return "layer2"
        if layer >= 5 and not self.layer5_checked:
            self.layer5_checked = True
            self.last_check_at = now
            return "layer5"
        stall_layers = self.profile["stall_layers"]
        if now - self.last_check_at >= self.profile["periodic_s"]:
            self.last_check_at = now
            reason = "stalled_layers" if snapshot_changed is False and self.last_layer is not None and layer - self.last_layer >= stall_layers else "periodic"
            return reason
        if snapshot_changed is False and self.last_snapshot_layer and layer - self.last_snapshot_layer >= stall_layers and self.alerted_stall_layer is None:
            self.alerted_stall_layer = layer
            self.last_layer = layer
            self.last_check_at = now
            return "stalled_layers"
        if snapshot_changed is None and layer >= 5 and self.alerted_stall_layer is None and self.last_snapshot_layer and layer - self.last_snapshot_layer >= stall_layers:
            self.last_layer = layer
            return "layer_progress"
        return None

    def record(self, layer, now, snapshot=None):
        if isinstance(layer, int):
            self.last_layer = layer
        self.last_check_at = now
        self.last_snapshot = snapshot
        if isinstance(layer, int): self.last_snapshot_layer = layer


class MidPrintSlotTracker:
    """Debounced per-slot presence during a print. Reports emptied and refilled slots."""

    def __init__(self, stable_samples=2):
        self.stable_samples = stable_samples
        self.reset()

    def reset(self):
        self.confirmed = {}
        self.candidate = {}
        self.counts = {}
        self.emptied = set()

    def observe(self, slots):
        events = []
        for slot, present in _slot_bools(slots).items():
            if self.candidate.get(slot) == present:
                self.counts[slot] = self.counts.get(slot, 0) + 1
            else:
                self.candidate[slot], self.counts[slot] = present, 1
            if self.counts[slot] < self.stable_samples or self.confirmed.get(slot) == present:
                continue
            before = self.confirmed.get(slot)
            self.confirmed[slot] = present
            if before is True and present is False:
                self.emptied.add(slot)
                events.append(("emptied", slot))
            elif before is False and present is True and slot in self.emptied:
                self.emptied.discard(slot)
                events.append(("refilled", slot))
        return events

    def empty_slots(self, slots=None):
        live = {s for s, p in _slot_bools(slots).items() if p is False and self.confirmed.get(s) is not False}
        return sorted(self.emptied | live)


def _mapped_spool(slot):
    try:
        state = json.loads(HARDWARE_STATE.read_text())
    except (OSError, ValueError):
        return None
    return ((state.get("ifs_slots") or {}).get(str(slot)) or {}).get("spoolman_id")


def _maintenance_history_totals():
    payload = print_control._call('/server/history/totals')
    result = payload.get('result') or {}
    return float(result.get('total_time') or result.get('print_time') or 0.0)


async def maintenance_reminder_tick(listener):
    """Post due maintenance only when an explicit idle state is known."""
    from . import maintenance
    try:
        st = await asyncio.to_thread(print_control.status)
        if st.get('state') != 'standby':
            return
        hours = await asyncio.to_thread(_maintenance_history_totals)
        state = maintenance.load()
        message = maintenance.reminder_text(state, hours, idle=True)
        if message:
            signature = message.partition('(')[0]
            if state.get('last_reminder') != signature:
                if await asyncio.to_thread(post, message):
                    state['last_reminder'] = signature
                    maintenance.save(state)
    except Exception as exc:
        log(f"maintenance reminder check failed: {exc!r}")


async def maintenance_cancel_reminder():
    try:
        st = await asyncio.to_thread(print_control.status)
        if st.get('state') == 'standby':
            await asyncio.to_thread(post, 'A print was cancelled. When the printer is idle, check the filament path and consider a maintenance quick check.')
    except Exception as exc:
        log(f"cancel maintenance reminder failed: {exc!r}")


async def maintenance_airprint_reminder():
    # TODO: t_16bad84c signal contract is not available here.
    return None


NATIVE_ERROR_HINTS = {
    "E0102": "the filament changer could not feed that slot to the head (tangle, chewed or bent tip, or tube not seated)",
}


def native_error_key(detail):
    """(code, slot) when the stock screen reports an error, else None. The screen can halt a
    print while Klipper still says printing, so Moonraker alone never notices."""
    code = str((detail or {}).get("errorCode") or "").strip()
    if not code and (detail or {}).get("status") != "error":
        return None
    slot = ((detail or {}).get("matlStationInfo") or {}).get("currentLoadSlot")
    return (code or "unknown", slot)


def native_error_text(filename, key):
    code, slot = key
    where = f" (slot {slot})" if slot else ""
    hint = NATIVE_ERROR_HINTS.get(code, "see the printer screen for details")
    link = f"https://wiki.flashforge.com/en/ad5x/error_code_list_ad5x" if code.startswith("E") else ""
    return (f"**Print halted by the printer: {code}{where}.**\n" + print_summary.format_event("error", filename)
            + f"\n**Why:** {hint}.\nKlipper still shows printing, so nothing else will alert. Fix it, then press "
              "retry on the touchscreen." + (f"\n{link}" if link else ""))


class Listener:
    def __init__(self):
        self.state = None
        self.filename = ""
        self.pause_message = ""
        self.ifs_monitor = FilamentInsertionMonitor()
        self.slot_tracker = MidPrintSlotTracker()
        self.visual_watch = VisualPrintWatch()
        self.last_stats = {}
        self.last_visual_task = None
        self.visual_frames = {}
        self.last_slots = {}
        self.last_native_poll = 0.0
        self.native_poll_inflight = False
        self.prev_native = None
        self.screen_reboot_task = None
        self.background_tasks = set()  # strong refs: an unreferenced task can be collected mid-run
        self.maintenance_last_check = 0.0
        self.last_pause_was_runout = False
        self.pause_context = None
        self.native_error_alerted = None

    async def read_slots(self):
        from print_buddy.core import printer_state
        from print_buddy.core import model_job
        detail = await asyncio.to_thread(model_job.read_stock_detail, printer_state.load())
        return detail, model_job.read_stock_ifs_slots_from_detail(detail)

    async def open_question(self, slot, cause):
        """Stop charging every spool, remember the question, and ask once. Nag loop repeats it."""
        if load_runout_pause() is not None:
            return None
        from . import spool_sync
        try:
            suspended = await asyncio.to_thread(spool_sync.suspend_usage)
            note = ""
        except Exception as exc:
            suspended, note = None, f"\n⚠️ Could not pause filament usage ({exc}); the old spool may keep being charged."
            log(f"usage suspend failed: {exc!r}")
        record = open_spool_question(self.filename, slot, _mapped_spool(slot) if slot else None,
                                     cause, suspended)
        log(f"spool question opened: cause={cause} slot={slot} suspended_spool={suspended}")
        await asyncio.to_thread(post_spool_question, record)
        return record

    async def nag_if_due(self):
        record = load_runout_pause()
        if not record or not record.get("usage_suspended"):
            return
        if time.time() - float(record.get("last_nag_at") or 0) < SPOOL_NAG_SECONDS:
            return
        record["nags"] = int(record.get("nags") or 0) + 1
        record["last_nag_at"] = time.time()
        atomic_write_json(RUNOUT_PAUSE_STATE, record)
        await asyncio.to_thread(post_spool_question, record, True)

    async def poll_native_presence(self):
        """Read authenticated stock detail while idle and send only a one-way prompt."""
        if not printers.current().has_native_screen:
            return  # no vendor status API, no IFS presence to watch
        if self.native_poll_inflight:
            return
        self.native_poll_inflight = True
        try:
            detail, slots = await self.read_slots()
            self.last_slots = slots
            native = detail.get("status")
            if native_screen_cancel(self.prev_native, native):
                # Klipper shut down mid print, so Moonraker never sees the screen cancel. The stock firmware does.
                if (await asyncio.to_thread(print_control.status)).get("klippy") == "shutdown":
                    await asyncio.to_thread(post_cancel_report, self.filename)
                    self.start_screen_cancel_reboot(f"native {self.prev_native}->{native} during emergency stop")
            self.prev_native = native
            if self.state in ("printing", "paused"):
                key = native_error_key(detail)
                if key and key != self.native_error_alerted:
                    self.native_error_alerted = key
                    log(f"native screen error {key} file={self.filename!r}")
                    await asyncio.to_thread(post, native_error_text(self.filename, key))
                elif not key:
                    self.native_error_alerted = None
                for event, slot in self.slot_tracker.observe(slots):
                    log(f"mid-print slot {slot} {event}")
                    if event == "refilled":
                        # Unloaded and reloaded mid print: unknown spool. Runout already asked.
                        await self.open_question(slot, "swap")
            # Only exact idle state qualifies. Unknown/preparing/printing all suppress prompts.
            idle = detail.get("status") == "ready" and self.state == "standby"
            found = self.ifs_monitor.observe(slots, idle=idle, connected=True)
            for slot in found:
                await asyncio.to_thread(post_insertion_confirmation, slot)
                log(f"native filament presence transition prompted for slot {slot}")
        except Exception as exc:
            log(f"native filament presence poll failed: {exc!r}")
            self.ifs_monitor.observe({}, idle=False, connected=False)
        finally:
            self.native_poll_inflight = False

    async def classify_runout(self):
        """Pause cause from the alert text plus a fresh slot read. Returns (is_runout, slot)."""
        if not printers.current().has_native_screen:
            return False, None  # no slot telemetry: a pause is a plain pause
        slots = self.last_slots
        try:
            _detail, slots = await self.read_slots()
            self.last_slots = slots
            self.slot_tracker.observe(slots)
        except Exception as exc:
            log(f"pause slot read failed: {exc!r}")
        empty = self.slot_tracker.empty_slots(slots)
        slot = empty[0] if len(empty) == 1 else recovery_slot(self.filename)[0]
        present = _slot_bools(slots)
        is_runout = pause_is_runout(self.pause_message, slot, present, None) or bool(empty)
        return is_runout, slot

    async def visual_check(self, reason, layer):
        """Capture and classify a print frame; notify humans only, never actuate.

        A configured vision endpoint that returns uncertain, or any snapshot or
        vision failure, alerts. Missing plastic, spaghetti, and a detached part
        alert. Layers that advanced with an unchanged frame are promoted to a
        stall here, because the scheduler cannot see the camera before capture.
        At [visual_watch] sensitivity 1 or 2 a stall alerts only when the vision
        verdict is not ok/safe; at 3 to 5 every stall alerts.
        """
        image = None
        try:
            image = await asyncio.to_thread(_camera_snapshot_bytes)
            prior = self.visual_frames.get(self.filename)
            changed = None if prior is None else await asyncio.to_thread(_frame_changed, prior, image)
            snapshot_before = self.visual_watch.last_snapshot_layer
            stall = (reason in {"layer_progress", "periodic"} and changed is False
                     and isinstance(layer, int) and layer >= 5
                     and snapshot_before and layer - snapshot_before >= self.visual_watch.profile["stall_layers"]
                     and self.visual_watch.alerted_stall_layer is None)
            if stall:
                reason = "stalled_layers"
                self.visual_watch.alerted_stall_layer = layer
            # A layer-progress probe that is not a stall must not move the
            # periodic clock or replace the baseline frame.
            if reason != "layer_progress":
                self.visual_frames[self.filename] = image
                self.visual_watch.record(layer, time.time(), image)
            # A layer-progress probe that is not a stall adopts the new frame
            # as the baseline without moving the periodic clock.
            if reason == "layer_progress":
                self.visual_frames[self.filename] = image
                self.visual_watch.last_snapshot = image
                if isinstance(layer, int):
                    self.visual_watch.last_snapshot_layer = max(self.visual_watch.last_snapshot_layer, layer)
                    self.visual_watch.progress_seen_layer = layer
                self.visual_watch.last_snapshot_layer = max(self.visual_watch.last_snapshot_layer, layer or 0)
                self.visual_watch.progress_seen_layer = layer or 0
                log(f"visual check file={self.filename!r} reason={reason} layer={layer} changed={changed} verdict=skipped")
                return
            prompt = "Is there plastic on the bed under the nozzle, or is there spaghetti/detachment?"
            verdict, detail = await asyncio.to_thread(_visual_verdict, image, prompt)
            suspicious = verdict in {"air", "spaghetti", "detached", "uncertain", "no", "unsafe"}
            if reason == "stalled_layers" and changed is False:
                # Levels 1 and 2 trust a healthy vision verdict over the pixel diff.
                suspicious = not (self.visual_watch.profile["vision_veto"] and verdict in VISUAL_HEALTHY_VERDICTS)
            if verdict == "uncertain":
                suspicious = True
            log(f"visual check file={self.filename!r} reason={reason} layer={layer} changed={changed} verdict={verdict} detail={detail!r}")
            if suspicious and image is not None:
                await asyncio.to_thread(_visual_alert, self.filename, image,
                                        f"{reason} at layer {layer or 'unknown'}: {verdict}; {detail or 'visual check needs operator review'}")
        except Exception as exc:
            log(f"visual check failed file={self.filename!r} reason={reason}: {exc!r}")
            if image is not None:
                try:
                    await asyncio.to_thread(
                        _visual_alert, self.filename, image,
                        f"{reason} at layer {layer or 'unknown'}: vision check failed: {exc!r}")
                except Exception as alert_exc:
                    log(f"visual alert failed file={self.filename!r}: {alert_exc!r}")

    async def on_status(self, print_stats, reason):
        self.last_stats.update(print_stats)
        new_state = print_stats.get("state", self.state)
        if "filename" in print_stats:
            filename = print_stats.get("filename")
            if filename and filename != self.filename:
                self.filename = filename
                self.visual_watch.reset()
                self.visual_frames.pop(filename, None)
        if "message" in print_stats:
            self.pause_message = print_stats.get("message") or ""
        if new_state is None or new_state == self.state:
            return
        old = self.state
        identity = _notification_identity({"start_time": start_claim_time(self.filename),
                                           **self.last_stats, "filename": self.filename})
        phase = "start" if new_state == "printing" and old != "paused" else ("terminal" if old in ("printing", "paused") and new_state in TERMINAL else None)
        if phase and not _claim_notification(identity, phase):
            log(f"suppressed duplicate {phase} notice identity={identity!r}")
            self.state = new_state
            return
        self.state = new_state
        log(f"state {old} -> {new_state} file={self.filename!r} ({reason})")
        if new_state == "printing" and _config.cfg()["visual_watch"]["enabled"]:
            layer = _visual_layer(self.last_stats)
            reason_due = self.visual_watch.due(layer, time.time(), snapshot_changed=None)
            if reason_due:
                self.last_visual_task = asyncio.create_task(self.visual_check(reason_due, layer))
        if old is not None and old != "printing" and new_state == "printing":
            # Claimed starts already have the intake's one post; unclaimed starts (printer screen,
            # other tools) get exactly one canonical post here.
            if old == "paused":
                log(f"resume, no start post: {self.filename!r}")
                self.last_pause_was_runout = False
            else:
                self.slot_tracker.reset()
                self.visual_watch.reset()
                if start_claimed(self.filename):
                    log(f"start claimed by print-now; updating its message, no second start post: {self.filename!r}")
                    await asyncio.to_thread(announce_warmup_done, self.filename)
                else:
                    await asyncio.to_thread(post, start_message_with_roast(self.filename))
        elif old == "printing" and new_state == "paused":
            self.last_pause_was_runout, slot = await self.classify_runout()
            if self.last_pause_was_runout:
                await asyncio.to_thread(post, paused_message(self.filename, runout=True))
                self.pause_context = await self.open_question(slot, "runout")
            else:
                self.pause_context = None
                await asyncio.to_thread(post_alert, paused_message(self.filename), "paused", self.filename)
        elif old in ("printing", "paused") and new_state in TERMINAL:
            self.last_pause_was_runout = False
            if new_state == "cancelled":
                await maintenance_cancel_reminder()
            # Completion, records, snapshot, and session reset stay in the announcer.
            # The announcer also owns the sole completion Discord post.
            if new_state == "cancelled":
                # Acknowledge promptly; collecting the full Klipper report may block.
                await asyncio.to_thread(post, print_summary.format_event("cancelled", self.filename))
                task = asyncio.create_task(asyncio.to_thread(post_cancel_report, self.filename))
                self.background_tasks.add(task)
                task.add_done_callback(self.background_tasks.discard)
                from . import klipper_report
                if not klipper_report.cancelled_by_bot():
                    self.start_screen_cancel_reboot("moonraker cancelled")
            elif new_state != "complete":
                await asyncio.to_thread(post, print_summary.format_event(new_state, self.filename))
        await asyncio.to_thread(run_announcer, f"{old}->{new_state}")
        if new_state in ("complete", "cancelled", "error", "standby") and old in ("printing", "paused"):
            self.visual_watch.reset()
            self.visual_frames.pop(self.filename, None)
            await self.after_print(new_state)

    def start_screen_cancel_reboot(self, why):
        """Fire once per cancel, in the background, and only when the bot did not cancel it."""
        from . import klipper_report
        if not printers.current().has_native_screen:
            return False  # the screen-cancel firmware restart is a stock-screen workaround
        if klipper_report.cancelled_by_bot():
            log(f"cancel was the bot's ({why}); no auto restart")
            return False
        if self.screen_reboot_task is not None and not self.screen_reboot_task.done():
            log(f"screen cancel restart already running ({why})")
            return False
        log(f"screen cancel detected ({why}); auto restart")
        self.screen_reboot_task = asyncio.create_task(asyncio.to_thread(screen_cancel_reboot, self.filename))
        return True

    async def after_print(self, new_state):
        """Screen updates deferred during the print, then the next queued copy prompt."""
        self.slot_tracker.reset()
        try:
            from . import spool_sync
            flushed = await asyncio.to_thread(spool_sync.flush_pending_screen)
            if flushed:
                log(f"deferred screen sync applied to slots {flushed}")
        except Exception as exc:
            log(f"deferred screen sync failed: {exc!r}")
        if new_state == "complete":
            from . import print_control
            prompt = print_control.next_copy_prompt(self.filename)
            if prompt:
                await asyncio.to_thread(post, prompt)

    async def session(self):
        self.ifs_monitor.baselined = False
        self.ifs_monitor.last_sample = {}
        self.ifs_monitor.stable_counts = {}
        self.ifs_monitor.transition_from = {}
        async with websockets.connect(WS_URL, ping_interval=20, ping_timeout=20, max_size=2**22) as ws:
            self.moonraker_connected = True
            await ws.send(SUBSCRIBE)
            last_tick = time.monotonic()
            while True:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=30)
                except asyncio.TimeoutError:
                    raw = None
                if raw:
                    msg = json.loads(raw)
                    method = msg.get("method")
                    if msg.get("id") == 1:
                        if "error" in msg:  # klippy not ready yet; notify_klippy_ready resubscribes
                            log(f"subscribe refused: {msg['error']}")
                        else:
                            status = ((msg.get("result") or {}).get("status") or {}).get("print_stats") or {}
                            await self.on_status(status, "subscribe")
                    elif method == "notify_status_update":
                        ps = (msg.get("params") or [{}])[0].get("print_stats")
                        if ps:
                            await self.on_status(ps, "event")
                            if self.state == "printing" and _config.cfg()["visual_watch"]["enabled"]:
                                layer = _visual_layer(self.last_stats)
                                reason_due = self.visual_watch.due(layer, time.time())
                                if reason_due:
                                    self.last_visual_task = asyncio.create_task(self.visual_check(reason_due, layer))
                    elif method == "notify_gcode_response":
                        for line in msg.get("params") or []:
                            if no_mesh_line(line):
                                log(f"no bed mesh at print start: {self.filename!r}")
                                await asyncio.to_thread(post, f"{NO_MESH_ALERT}\nPrint: {print_summary.safe_label(self.filename)}")
                    elif method in ("notify_klippy_disconnected", "notify_klippy_shutdown"):
                        log(f"{method} while state={self.state}")
                        if self.state in ("printing", "paused"):
                            await asyncio.to_thread(post_shutdown_alert, self.filename)
                            self.state = "error"
                    elif method == "notify_klippy_ready":
                        log("klippy ready; resubscribing")
                        await ws.send(SUBSCRIBE)
                if time.monotonic() - self.last_native_poll >= NATIVE_POLL_SECONDS:
                    self.last_native_poll = time.monotonic()
                    await self.poll_native_presence()
                    try:
                        await self.nag_if_due()
                    except Exception as exc:
                        log(f"spool nag failed: {exc!r}")
                if time.monotonic() - self.maintenance_last_check >= 3600:
                    self.maintenance_last_check = time.monotonic()
                    await maintenance_reminder_tick(self)
                if time.monotonic() - last_tick >= TICK_SECONDS:
                    last_tick = time.monotonic()
                    await asyncio.to_thread(run_announcer, "tick")

    async def heartbeat(self):
        """Adapted from contributor Docker commit 3bb0984; no printer probe."""
        while True:
            try:
                await asyncio.to_thread(healthcheck.beat,
                                        moonraker_connected=self.moonraker_connected,
                                        state=self.state)
            except Exception as exc:
                log(f"heartbeat write failed: {exc!r}")
            await asyncio.sleep(healthcheck.HEARTBEAT_S)

    async def forever(self):
        delay = 5
        self.moonraker_connected = False
        beating = asyncio.create_task(self.heartbeat())
        try:
            while True:
                try:
                    await self.session()
                except Exception as exc:
                    log(f"connection lost: {exc!r}; retry in {delay}s")
                finally:
                    self.moonraker_connected = False
                await asyncio.sleep(delay)
                delay = min(delay * 2, 120)
        finally:
            beating.cancel()
            await asyncio.gather(beating, return_exceptions=True)


def main():
    if not WS_URL:
        raise SystemExit("services.moonraker_url is not configured (or services.moonraker_ws_url). "
                         "Set it in config/local.yaml or config/local.toml; the listener has nothing to connect to.")
    route = "PRINT_VISION_URL" if os.environ.get("PRINT_VISION_URL") else (_VISION_BASE or "not configured")
    log(f"listener starting; camera watchdog initialized route={route} model={_VISION_MODEL}")
    asyncio.run(Listener().forever())


if __name__ == "__main__":
    main()
