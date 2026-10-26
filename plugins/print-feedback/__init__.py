"""Profile-scoped truthful receipt feedback and print controls via supported gateway hooks.

The gateway loads this folder through a symlink into the Hermes profile. Everything it needs lives in
the Print Buddy package: print_buddy.hermes (listener helpers, print control) and print_buddy.core
(the slicing and print pipeline). Hosts, paths and temperatures come from the home configuration.
"""
import asyncio
import contextlib
import json
import logging
import os
import re
import sys
import time
import threading
from pathlib import Path
from collections import OrderedDict

logger = logging.getLogger(__name__)


def _make_package_importable():
    """The gateway does not run from the repo. When print_buddy is not installed, use the repo
    this plugin symlink points into (plugins/print-feedback/__init__.py -> repo root)."""
    try:
        import print_buddy  # noqa: F401
    except ImportError:
        root = str(Path(__file__).resolve().parents[2])
        if root not in sys.path:
            sys.path.insert(0, root)


_make_package_importable()
from print_buddy.hermes import _config  # noqa: E402


def _hermes_home():
    return _config.hermes_home()


def _state_dir():
    return _config.state_dir()


_seen = OrderedDict()
ACK_TIMEOUT = 1.5
SEEN_TTL = 3600
SEEN_LIMIT = 1024
# Upload-wait freeze length. Read at use time so a settings override in tests is honored.
_upload_waits = {}


def _wait_key(source):
    return (getattr(source, 'profile', None), str(getattr(source, 'chat_id', '')),
            str(getattr(source, 'thread_id', '') or ''))


# Platforms whose standalone sources these handlers serve (#164): Discord,
# Telegram (stage 2) and Matrix (stage 3). The gate was never really "is
# this Discord" but "is this our bot's print source"; Hermes gateway
# behaviour for the Discord profile is unchanged, and Telegram/Matrix
# sources only exist when an operator runs that standalone bot.
BOT_PLATFORMS = frozenset({'discord', 'telegram', 'matrix'})


def _platform_value(source):
    return getattr(getattr(source, 'platform', None), 'value', None)


def _is_bot_platform(source):
    return _platform_value(source) in BOT_PLATFORMS


def _is_print_discord_event(event):
    source = getattr(event, 'source', None)
    return (source is not None
            and _is_bot_platform(source)
            and getattr(source, 'profile', None) == '3dprinting'
            and not getattr(event, 'internal', False)
            and not getattr(source, 'is_bot', False)
            and not getattr(source, 'profile_route_rejected', False))


async def upload_wait_dispatch(event, gateway, **kwargs):
    """Freeze a Discord intake while uploads arrive; only an explicit resume releases it."""
    if not _is_print_discord_event(event):
        return None
    source = event.source
    if not getattr(source, 'user_id', None) or not gateway._is_user_authorized_for_source(source):
        return None
    text = str(getattr(event, 'text', '') or '').strip().casefold()
    key = _wait_key(source)
    now = time.monotonic()
    until = _upload_waits.get(key, 0)
    if until <= now:
        _upload_waits.pop(key, None)
        until = 0
    waiting = bool(re.search(r'\b(wait|hold|pause)\b.*\b(upload|attach|file)s?\b', text))
    resume = bool(re.search(r'\b(resume|continue|uploads? (?:are )?(?:done|complete|finished))\b', text))
    if waiting:
        _upload_waits[key] = now + (globals().get('_config').upload_wait_s() if globals().get('_config') else 3600)
        return {'action': 'skip', 'reason': 'upload wait active'}
    if until:
        if resume:
            _upload_waits.pop(key, None)
            return None
        return {'action': 'skip', 'reason': 'upload wait active; explicit resume required'}
    return None


async def guided_dispatch(event, gateway, **kwargs):
    """/print-buddy setup|debugging|status|cancel, plus plain replies to an open setup checklist (#162).

    One state machine (print_buddy.bot.guided) serves both runtimes. Setup never collects a
    token, never writes configuration without the explicit `apply` reply, and never touches a
    printer; debugging is read-only and redacted.
    """
    if not _is_print_discord_event(event):
        return None
    source = event.source
    if not getattr(source, 'user_id', None) or not gateway._is_user_authorized_for_source(source):
        return None
    text = str(getattr(event, 'text', '') or '').strip()
    from print_buddy.bot import guided
    key = f"{source.chat_id}:{source.thread_id or ''}:{source.user_id}"
    store = guided.SetupStore(_state_dir() / guided.STORE_NAME)
    if not text.startswith('/print-buddy'):
        session = store.get(key)
        if not session or session.get('status') not in ('in_progress', 'awaiting_consent'):
            return None
    adapter = gateway._delivery_adapter_for(source)
    if adapter is None:
        return {'action': 'skip', 'reason': 'guided UI delivery unavailable'}

    def report():
        cfg = _config.cfg()
        token = _config.discord_token()
        return guided.debugging_report(loader=lambda: cfg,
                                       token_getter=lambda _cfg: token,
                                       secrets=[token] if token else [])

    try:
        token_env = str(_config.cfg()['discord'].get('token_env') or 'DISCORD_BOT_TOKEN')
    except Exception:
        token_env = 'DISCORD_BOT_TOKEN'
    try:
        reply = await asyncio.to_thread(guided.handle_text, key, text, store=store,
                                        token_env=token_env, report=report)
    except Exception:
        logger.exception('guided setup/debugging failed')
        reply = 'That guided step failed and nothing was changed. Try `/print-buddy status`, or `/print-buddy cancel` to pause setup.'
    if reply is None:
        return None
    metadata = {'thread_id': source.thread_id} if source.thread_id else None
    await adapter.send(chat_id=source.chat_id, metadata=metadata, content=reply)
    return {'action': 'skip', 'reason': 'guided setup/debugging handled'}


def receipt_text(text):
    text = str(text or '').strip()
    lower = text.casefold()
    has_link = bool(re.search(r'https?://\S+', text))
    bed_clear = 'bed is clear' in lower or 'bed clear' in lower
    if has_link:
        return None  # the model preview and color picker follow; a receipt is noise
    if lower in {'run the routine', 'run routine'}:
        return 'Received. Preparing the requested routine and checking safety conditions. It is not running yet.'
    if bed_clear:
        return 'Received your bed-clear confirmation. Checking the requested action before execution. Nothing has started yet.'
    if re.search(r'\b(level|leveling|calibrate|calibration)\b', lower):
        return 'Received your calibration request. Checking the routine and safety conditions. No motion or heating has started.'
    return None


async def receive_ack(event, gateway, **kwargs):
    """Await delivery before model dispatch, never alter or execute the request."""
    source = getattr(event, 'source', None)
    if source is None:
        return None
    platform = getattr(getattr(source, 'platform', None), 'value', None)
    if (platform not in BOT_PLATFORMS or getattr(source, 'profile', None) != '3dprinting'
            or getattr(event, 'internal', False) or getattr(source, 'is_bot', False)
            or getattr(source, 'profile_route_rejected', False)):
        return None
    if getattr(event, 'metadata', {}).get('fresh_model_intake_handled'):
        return None
    text = receipt_text(getattr(event, 'text', ''))
    message_id = getattr(event, 'message_id', None)
    if not text or not message_id:
        return None
    # This hook is before core auth. Recheck the SAME core authorization first.
    try:
        if not getattr(source, 'user_id', None) or not gateway._is_user_authorized_for_source(source):
            return None
        adapter = gateway._delivery_adapter_for(source)
        if adapter is None:
            return None
    except Exception:
        logger.exception('Print receipt authorization/delivery lookup failed')
        return None
    await _deliver_receipt(source, adapter, message_id, text)
    return None


async def _deliver_receipt(source, adapter, message_id, text):
    """Send one receipt per Discord message; failures never block the request."""
    now = time.monotonic()
    while _seen and (now - next(iter(_seen.values())) > SEEN_TTL or len(_seen) >= SEEN_LIMIT):
        _seen.popitem(last=False)
    key = (source.profile, source.chat_id, source.thread_id, str(message_id))
    if key in _seen:
        return
    # Reserve before awaiting so duplicate events cannot race and double-send.
    _seen[key] = now
    try:
        result = await asyncio.wait_for(adapter.send(
            chat_id=source.chat_id, content=text, reply_to=message_id,
            metadata={'thread_id': source.thread_id} if source.thread_id else None,
        ), timeout=ACK_TIMEOUT)
        if not getattr(result, 'success', False):
            logger.warning('Print receipt delivery failed; continuing original request')
    except asyncio.TimeoutError:
        logger.warning('Print receipt delivery exceeded %.1fs; continuing original request', ACK_TIMEOUT)
    except Exception:
        logger.exception('Print receipt delivery failed; continuing original request')


def clarify_receipt(result):
    """Deterministic echo of a clarify button choice. None unless an answer was submitted."""
    try:
        data = json.loads(result) if isinstance(result, str) else result
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict) or data.get('outcome') != 'submitted':
        return None
    answers = [r.get('user_response') for r in data.get('responses') or []
               if isinstance(r, dict) and r.get('status') == 'answered' and r.get('user_response')]
    if not answers:
        return None
    lines = []
    for raw in answers:
        choice = ', '.join(map(str, raw)) if isinstance(raw, list) else str(raw)
        low = choice.casefold()
        if 'bed' in low and 'clear' in low and not re.search(r"\b(not|isn't|no)\b", low):
            lines.append('You confirmed the bed is clear. Starting the checks before slicing and printing; nothing has started yet.')
        else:
            lines.append(f'You chose: {choice}. Nothing has started.')
    return ' '.join(lines)


def _session(name):
    try:
        from gateway.session_context import get_session_env
        return get_session_env(name)
    except Exception:
        return os.getenv(name, '')


def workflow_receipt(result):
    """Report completed stages only from successful structured terminal evidence."""
    try:
        data = json.loads(result) if isinstance(result, str) else result
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict) or data.get('exit_code') != 0 or data.get('error'):
        return None
    output = data.get('output')
    if not isinstance(output, str):
        return None
    if re.search(r'^SLICED \(not uploaded\): /[^\n]+$', output, re.M):
        return 'Slicing completed. The workflow is checking the next step.'
    if re.search(r'^AUDIT: \{', output, re.M):
        return 'The model files were retrieved and inspected. No completed slice is reported yet.'
    return None


def cap_discord_clarifications(tool_name=None, args=None, **kwargs):
    """Keep 3dprinting Discord clarify cards actionable without truncating silently."""
    if (tool_name != 'clarify' or _session('HERMES_SESSION_PLATFORM') != 'discord'
            or _session('HERMES_SESSION_PROFILE') != '3dprinting' or not isinstance(args, dict)):
        return None
    questions = args.get('questions')
    if not isinstance(questions, list):
        return None
    capped = []
    changed = False
    for item in questions:
        if not isinstance(item, dict) or not isinstance(item.get('choices'), list) or len(item['choices']) <= 4:
            capped.append(item)
            continue
        question = str(item.get('question') or '').strip()
        notice = 'Additional options exist. Choose Other to name one or request the remainder.'
        if notice not in question:
            question = f'{question} {notice}'.strip()
        capped.append({**item, 'question': question, 'choices': item['choices'][:4]})
        changed = True
    return {'action': 'modify', 'args': {**args, 'questions': capped}} if changed else None


def clarify_ack(tool_name=None, result=None, tool_call_id=None, **kwargs):
    """post_tool_call: deterministic choices and factual completed milestones."""
    if tool_name not in {'clarify', 'terminal'}:
        return None
    if _session('HERMES_SESSION_PLATFORM') != 'discord' or _session('HERMES_SESSION_PROFILE') != '3dprinting':
        return None
    if tool_name == 'terminal':
        command = (kwargs.get('args') or {}).get('command', '')
        if 'model_job' not in command:
            return None
    text = clarify_receipt(result) if tool_name == 'clarify' else workflow_receipt(result)
    chat = _session('HERMES_SESSION_CHAT_ID')
    if not text or not chat:
        return None
    key = ('clarify', chat, str(tool_call_id or text))
    if key in _seen:
        return None
    _seen[key] = time.monotonic()
    thread = _session('HERMES_SESSION_THREAD_ID')
    target = f'discord:{chat}:{thread}' if thread else f'discord:{chat}'
    try:
        from tools.send_message_tool import send_message_tool
        reply = send_message_tool({'action': 'send', 'target': target, 'message': text})
        if '"error"' in str(reply):
            logger.warning('Clarify receipt delivery failed; continuing')
    except Exception:
        logger.exception('Clarify receipt delivery failed; continuing')
    return None


# A deterministic execution bound, not a larger model iteration allowance.
TERMINAL_LIMIT = 12
_tool_turns = OrderedDict()
_tool_lock = threading.Lock()


def guard_terminal_loop(tool_name=None, args=None, session_id=None, turn_id=None, **kwargs):
    """Reject repeats and terminal thrashing within one identified user turn."""
    search_tools = {'search_files', 'web_search', 'viking_search', 'session_search', 'tool_search'}
    read_tools = {'read_file', 'web_extract', 'viking_read', 'viking_browse', 'browser_exec'}
    exploration = tool_name in search_tools | read_tools
    if tool_name not in {'terminal', 'write_file', 'patch'} and not exploration:
        return None
    profile = _session('HERMES_SESSION_PROFILE')
    if not profile:
        profile = Path(os.getenv('HERMES_HOME', '')).name
    if profile != '3dprinting':
        return None
    values = args or {}
    # Printer ownership includes diagnosis and implementation maintenance.
    # Tool-count limits and source bans are not physical safety protections.
    if tool_name in {'write_file', 'patch'}:
        path = str(values.get('path', ''))
        archive = _config.archive_dir()
        if Path(path).suffix.lower() == '.gcode' and not Path(path).expanduser().resolve().is_relative_to(archive):
            return {'action': 'block', 'message': f'REFUSED: G-code edits belong under {archive}. Preserve the slicer original, record the diff, and verify before use.'}
    if tool_name != 'terminal':
        return None
    command = values.get('command', '')
    if not isinstance(command, str):
        return None
    # SET_EXTRUDER_SLOT is installed in mod/generic.cfg and only forwards
    # SLOT to _SET_EXTRUDER_SLOT. ZMod documents it as bookkeeping for which
    # spool is loaded. It is not a physical load. Do not name-ban it.
    if not session_id or not turn_id:
        return None
    key = (str(session_id), str(turn_id))
    now = time.monotonic()
    with _tool_lock:
        while _tool_turns and (now - next(iter(_tool_turns.values()))['at'] > SEEN_TTL or len(_tool_turns) >= SEEN_LIMIT):
            _tool_turns.popitem(last=False)
        state = _tool_turns.setdefault(key, {'at': now, 'commands': {}})
        repeats = state['commands'].get(command, 0)
        if repeats >= 2:
            return {'action': 'block', 'message': 'REFUSED: repeat terminal command already attempted twice this turn. Report the result or blocker; do not poll or rephrase the command to bypass this guard.'}
        state['commands'][command] = repeats + 1
    return None


async def color_selection_dispatch(event, gateway, **kwargs):
    """Explicit color command: real Discord UI, no physical or slicing actions."""
    source = getattr(event, 'source', None)
    text = str(getattr(event, 'text', '')).strip()
    if not text.startswith('/print-colors '):
        return None
    if (source is None or not _is_bot_platform(source)
            or getattr(source, 'profile', None) != '3dprinting'
            or getattr(event, 'internal', False) or getattr(source, 'is_bot', False)
            or getattr(source, 'profile_route_rejected', False)):
        return None
    if not getattr(source, 'user_id', None) or not gateway._is_user_authorized_for_source(source):
        return None
    adapter = gateway._delivery_adapter_for(source)
    if adapter is None:
        return {'action': 'skip', 'reason': 'color UI delivery unavailable'}
    from print_buddy.core import printer_state
    from print_buddy.core.discord_color_intake import collect
    from print_buddy.core.link_intake import PromptCancelled
    from print_buddy.core import model_job
    ARCHIVE = model_job.ARCHIVE
    path = Path(text[len('/print-colors '):]).expanduser().resolve()
    metadata = {'thread_id': source.thread_id} if source.thread_id else None
    key = ('color-ui', source.chat_id, source.thread_id)
    if key in _seen:
        return {'action': 'skip', 'reason': 'color selection already active'}
    _seen[key] = time.monotonic()
    try:
        if path.name != 'JOB.json' or not path.is_relative_to(ARCHIVE.resolve()):
            raise ValueError('color command requires an archived JOB.json')
        result = await collect(path, printer_state.load(), adapter, source.chat_id,
                               f'print-colors:{source.chat_id}:{source.thread_id}', metadata)
        message = json.dumps(result)
    except PromptCancelled:
        message = 'Cancelled. No colors changed, nothing sliced, nothing started.'
    except Exception as exc:
        message = f'Color selection failed: {exc}. No slicing, upload or print started.'
    finally:
        _seen.pop(key, None)
    await adapter.send(chat_id=source.chat_id, content=message, metadata=metadata)
    return {'action': 'skip', 'reason': 'deterministic color selection handled'}


USE_LOADED_SPOOL = 'Use the loaded spool · bed is clear'
SPOOL_CHANGED = 'spool changed since slicing'


def _spool_change_text(target, detail):
    found = re.search(r'slot (\d+) spool changed since slicing \(was (\d+), now (\d+)\)', str(detail))
    where = f"Slot {found.group(1)} now holds spool #{found.group(3)} (it was #{found.group(2)} at slicing). " if found else ''
    return (f"{where}Print {target['label']} with the spool that is loaded now? "
            "Same material family is still required.")


async def _offer_spool_change(adapter, source, metadata, print_next, target, detail):
    """A slot's spool was swapped after slicing (ran out, changed color). Ask once; on yes, start with it.

    Only the slice-time spool id is waived; material family and live slot checks still run.
    """
    import uuid
    from print_buddy.core.prompts import gateway as _prompts
    cm = _prompts()
    question = _spool_change_text(target, detail)
    choices = [USE_LOADED_SPOOL, print_next.CANCEL_LABEL]
    cid = uuid.uuid4().hex
    session_key = f'print-next-spool:{source.chat_id}:{source.thread_id}'
    cm.register(cid, session_key, question, choices)
    try:
        sent = await adapter.send_clarify(chat_id=str(source.chat_id), question=question, choices=choices,
                                          clarify_id=cid, session_key=session_key, metadata=metadata)
        if not getattr(sent, 'success', False):
            return False, detail
        answer = await asyncio.to_thread(cm.wait_for_response, cid, (globals().get('_config').question_timeout_s() if globals().get('_config') else 1800))
    finally:
        cm.resolve_gateway_clarify(cid, cm.CANCELLED)
    if answer != USE_LOADED_SPOOL:
        return False, 'Cancelled. Nothing uploaded or started.'
    return await print_next.start({**target, 'accept_spool_change': True})


async def print_next_dispatch(event, gateway, **kwargs):
    """/print-next <plan_id> [part_id]: one bed-clear button for the exact next (or named) plate. One start per tap."""
    source = getattr(event, 'source', None)
    text = str(getattr(event, 'text', '')).strip()
    if not (text == '/print-next' or text.startswith('/print-next ')):
        return None
    if (source is None or not _is_bot_platform(source)
            or getattr(source, 'profile', None) != '3dprinting'
            or getattr(event, 'internal', False) or getattr(source, 'is_bot', False)
            or getattr(source, 'profile_route_rejected', False)):
        return None
    if not getattr(source, 'user_id', None) or not gateway._is_user_authorized_for_source(source):
        return None
    adapter = gateway._delivery_adapter_for(source)
    if adapter is None:
        return {'action': 'skip', 'reason': 'next plate UI delivery unavailable'}
    import uuid
    from print_buddy.hermes import print_next
    metadata = {'thread_id': source.thread_id} if source.thread_id else None
    args = text[len('/print-next'):].split()
    plan_id = args[0] if args else ''
    part_id = args[1] if len(args) > 1 else None
    key = ('print-next', plan_id)
    if key in _seen:
        await adapter.send(chat_id=source.chat_id, metadata=metadata,
                           content='A start prompt for this plan is already open. Nothing started.')
        return {'action': 'skip', 'reason': 'next plate prompt already active'}
    _seen[key] = time.monotonic()
    try:
        if not plan_id:
            raise print_next.NextPlateError('usage: /print-next <plan_id> [part_id]')
        plan = print_next.load_plan(plan_id)
        target = print_next.resolve_next(plan) if part_id is None else print_next.resolve_next(plan, part_id=part_id)
        from print_buddy.hermes import discord_print_summary as print_summary
        job, plate = print_summary.find_job(target['gcode'])
        seconds = (plate or {}).get('seconds')
        number = print_summary._print_number({'_job_path': str(Path(target['job_json']).parent)})
        target['estimate_text'] = (f"Print #{number if number else 'unknown'}; estimated print time ~{print_summary.human(seconds)} "
                                   f"plus warmup ~{print_summary.human(seconds + print_summary.WARMUP_S)} total"
                                   if isinstance(seconds, (int, float)) else
                                   f"Print #{number if number else 'unknown'}; estimated print time unknown")
        from print_buddy.core.prompts import gateway as _prompts
        cm = _prompts()
        choices = [print_next.START_LABEL, print_next.CANCEL_LABEL]
        question = print_next.question_text(target)
        cid = uuid.uuid4().hex
        session_key = f'print-next:{source.chat_id}:{source.thread_id}'
        cm.register(cid, session_key, question, choices)
        try:
            sent = await adapter.send_clarify(chat_id=str(source.chat_id), question=question,
                                              choices=choices, clarify_id=cid,
                                              session_key=session_key, metadata=metadata)
            if not getattr(sent, 'success', False):
                raise RuntimeError('button delivery failed')
            answer = await asyncio.to_thread(cm.wait_for_response, cid, (globals().get('_config').question_timeout_s() if globals().get('_config') else 1800))
        finally:
            cm.resolve_gateway_clarify(cid, cm.CANCELLED)
        if answer != print_next.START_LABEL:
            message = 'Cancelled or expired. Nothing uploaded or started.'
        else:
            await adapter.send(chat_id=source.chat_id, metadata=metadata,
                               content=f"Preparing to start {target['label']}. {target.get('estimate_text', 'estimated print time unknown')}.")
            ok, detail = await print_next.start(target)
            if not ok and SPOOL_CHANGED in str(detail):
                ok, detail = await _offer_spool_change(adapter, source, metadata, print_next, target, detail)
            message = (None if ok
                       else f"Did not start {target['label']}. Not retrying.\n{detail}")
            if not ok and _start_refused_needs_restart(detail):
                from print_buddy.hermes import print_control as pc
                message += '\n' + await _offer_force_restart(adapter, source, metadata, pc, detail)
    except print_next.NextPlateError as exc:
        message = f'No next plate: {exc}. Nothing uploaded or started.'
    except Exception as exc:
        logger.exception('print-next failed')
        message = f'Next plate failed: {exc}. Check the printer screen before retrying.'
    finally:
        _seen.pop(key, None)
    if message:  # None: start accepted, printer preparing; the listener posts "Print started".
        await adapter.send(chat_id=source.chat_id, content=message, metadata=metadata)
    return {'action': 'skip', 'reason': 'deterministic next plate handled'}


def _discord_operator(event, gateway):
    """True for an authorized human Discord message in this profile."""
    source = getattr(event, 'source', None)
    if (source is None or not _is_bot_platform(source)
            or getattr(source, 'profile', None) != '3dprinting'
            or getattr(event, 'internal', False) or getattr(source, 'is_bot', False)
            or getattr(source, 'profile_route_rejected', False)):
        return None
    if not getattr(source, 'user_id', None) or not gateway._is_user_authorized_for_source(source):
        return None
    return source


def _maintenance_hours_now():
    from print_buddy.hermes import print_control
    payload = print_control._call('/server/history/totals')
    result = payload.get('result') or {}
    seconds = result.get('total_time', result.get('print_time'))
    if seconds is None:
        raise RuntimeError('Moonraker history totals did not include total_time or print_time')
    return float(seconds) / 3600.0


def _cold_pull_temps():
    """(pla_hot, pla_cold, petg_hot, petg_cold, pla_retry_cold) from [cold_pull] in the home configuration."""
    from print_buddy.hermes import print_control as pc
    pla_hot, pla_cold = pc.COLD_PULL_PROFILES['pla_trial']
    petg_hot, petg_cold = pc.COLD_PULL_PROFILES['petg']
    return pla_hot, pla_cold, petg_hot, petg_cold, pla_cold + 5


async def _coldpull_workflow(adapter, source, metadata, *, source_tag, initial_cold=None, selected_material=None):
    """Shared user interaction for the standalone and maintenance cold-pull paths."""
    from print_buddy.hermes import maintenance
    from print_buddy.hermes import print_control as pc

    pla_hot, pla_cold, petg_hot, petg_cold, retry_cold = _cold_pull_temps()
    answer = selected_material
    if answer is None:
        answer = await _confirm(adapter, source, metadata, f'{source_tag}-material',
            'Choose material for this cold pull.',
            [f'PLA (trial) {pla_hot}/{pla_cold}', f'PETG {petg_hot}/{petg_cold}'], (globals().get('_config').question_timeout_s() if globals().get('_config') else 1800))
    if not answer:
        return None, 'Cancelled or expired. Nothing sent.'
    is_pla = answer.startswith('PLA') or answer.startswith('Run PLA')
    temp, cold = (pla_hot, pla_cold) if is_pla else (petg_hot, petg_cold)
    if is_pla and (answer.endswith(f'{pla_hot}/{retry_cold}') or initial_cold == retry_cold):
        cold = retry_cold
    state = maintenance.load()
    pulls = [r.get('cold_pull', {}) for r in state.get('runs', []) if r.get('cold_pull')]
    prior = pulls[-1] if pulls else {}
    if is_pla and (initial_cold == retry_cold or prior.get('next_cold') == retry_cold):
        cold = retry_cold
        if initial_cold != retry_cold:
            retry_answer = await _confirm(adapter, source, metadata, f'{source_tag}-temp',
                f'Previous PLA tip snapped or did not release. Try COLD={retry_cold} once?',
                [f'Use COLD={retry_cold}', f'Keep COLD={pla_cold}'], (globals().get('_config').question_timeout_s() if globals().get('_config') else 1800))
            if retry_answer != f'Use COLD={retry_cold}':
                cold = pla_cold
    dirty_count = 0
    for pull in reversed(pulls):
        if pull.get('result') != 'dirty':
            break
        dirty_count += 1
    if dirty_count:
        repeat = await _confirm(adapter, source, metadata, f'{source_tag}-repeat',
            'A prior pull was dirty. Repeat once?', ['Run one repeat', 'Cancel'], (globals().get('_config').question_timeout_s() if globals().get('_config') else 1800))
        if repeat != 'Run one repeat':
            return None, 'Cancelled. Nothing sent.'
    await adapter.send(chat_id=source.chat_id, metadata=metadata,
        content='Bed must be clear; it homes. Screen may look frozen for a few minutes. Confirm only at the printer.')
    confirmed = await _confirm(adapter, source, metadata, f'{source_tag}-actuate',
        'I am present, the bed is clear, and authorize one cold pull.', ['Confirm cold pull', 'Cancel'], (globals().get('_config').safety_confirm_timeout_s() if globals().get('_config') else 600))
    if confirmed != 'Confirm cold pull':
        return None, 'Cancelled or expired. Nothing sent.'
    import time as _time
    # Capture authorization only after the successful button response.
    confirmed_at = _time.time()
    ok, detail = await asyncio.to_thread(pc.coldpull, temp, cold, confirmed_at=confirmed_at)
    record = {'kind': 'coldpull', 'source': source_tag,
              'cold_pull': {'temp': temp, 'cold': cold, 'result': 'awaiting_tip' if ok else 'failed', 'detail': detail}}
    if not ok:
        record['hours_total'] = await asyncio.to_thread(_maintenance_hours_now)
        maintenance.append_run(record)
        return record, detail
    tip = await _confirm(adapter, source, metadata, f'{source_tag}-tip', 'How did the pulled tip look?',
                         ['Clean', 'Dirty', 'Snapped', "Didn't release"], (globals().get('_config').cold_pull_outcome_s() if globals().get('_config') else 1800))
    result = {'Clean': 'clean', 'Dirty': 'dirty', 'Snapped': 'snapped', "Didn't release": 'didnt_release'}.get(tip)
    if not result:
        record['hours_total'] = await asyncio.to_thread(_maintenance_hours_now)
        maintenance.append_run(record)
        return record, 'Cold pull finished; heater target is 0, but the tip result was not recorded.'
    guidance = maintenance.result_guidance(result, dirty_count)
    record['cold_pull'].update({'result': result, **guidance})
    if guidance['offer_cold_95'] and temp == pla_hot:
        record['cold_pull']['next_cold'] = retry_cold
    record['hours_total'] = await asyncio.to_thread(_maintenance_hours_now)
    maintenance.append_run(record)
    message = detail
    if guidance['offer_repeat']:
        message += ' One dirty pull can be repeated.'
    if guidance['suggest_nozzle_swap']:
        message += ' Two dirty pulls in a row: consider a nozzle swap.'
    if guidance['offer_cold_95'] and temp == pla_hot:
        message += f' Next PLA attempt can use COLD={retry_cold}.'
    return record, message


async def coldpull_dispatch(event, gateway, **kwargs):
    source, adapter = _control_preamble(event, gateway, '/print-coldpull')
    if source is None:
        return None
    if adapter is None:
        return {'action': 'skip', 'reason': 'cold-pull UI delivery unavailable'}
    metadata = {'thread_id': source.thread_id} if source.thread_id else None
    key = ('coldpull',)
    if key in _seen:
        await adapter.send(chat_id=source.chat_id, metadata=metadata, content='A cold pull is already in progress. Nothing sent.')
        return {'action': 'skip', 'reason': 'cold-pull prompt already active'}
    _seen[key] = time.monotonic()
    try:
        _record, message = await _coldpull_workflow(adapter, source, metadata, source_tag='coldpull')
    except Exception as exc:
        logger.exception('/print-coldpull failed')
        message = f'Cold pull stopped: {exc}. Do not retry unless printer state is checked.'
    finally:
        _seen.pop(key, None)
    await adapter.send(chat_id=source.chat_id, metadata=metadata, content=message)
    return {'action': 'skip', 'reason': 'cold pull handled'}


async def maintenance_dispatch(event, gateway, **kwargs):
    """Guided preventive maintenance checklist using the shared cold-pull path."""
    source, adapter = _control_preamble(event, gateway, '/print-maintenance', with_args=True)
    if source is None:
        return None
    if adapter is None:
        return {'action': 'skip', 'reason': 'maintenance UI delivery unavailable'}
    import json as _json
    import uuid
    from print_buddy.hermes import maintenance
    metadata = {'thread_id': source.thread_id} if source.thread_id else None
    mode = str(getattr(event, 'text', '')).strip()[len('/print-maintenance'):].strip().casefold() or 'full'
    if adapter is None:
        return {'action': 'skip', 'reason': 'maintenance UI delivery unavailable'}
    if mode not in {'full', 'quick'}:
        await adapter.send(chat_id=source.chat_id, metadata=metadata,
                           content='Use `/print-maintenance full` or `/print-maintenance quick`.')
        return {'action': 'skip', 'reason': 'maintenance usage shown'}
    key = ('maintenance', source.chat_id, source.thread_id)
    if key in _seen:
        await adapter.send(chat_id=source.chat_id, metadata=metadata, content='A maintenance checklist is already open.')
        return {'action': 'skip', 'reason': 'maintenance already active'}
    _seen[key] = time.monotonic()
    try:
        from print_buddy.hermes import print_control as pc
        st = await asyncio.to_thread(pc.status)
        if st.get('state') in {'printing', 'paused'}:
            message = f"Maintenance is unavailable while the printer is {st['state']}. Nothing changed."
        else:
            state = maintenance.load()
            session = state.get('pending')
            count = 4 if mode == 'full' else 2
            if not isinstance(session, dict) or session.get('kind') != mode:
                session = maintenance.begin_session(mode, state)
            instructions = [
                'Extruder gears: open the tension lever, brush out filament dust, and check the last filament end for a ground notch.',
                '4-in-1 connector and slot tubes: pull them and clear any broken filament.',
                'IFS pinch rollers: clear shavings from each roller shaft and confirm the lever springs are seated.',
                'Spools: check for tangles, crossed wraps, free ends, and note old or brittle spools.',
            ][:count]
            session['steps'] = session.get('steps', [None] * count)
            session['problems'] = session.get('problems', [])
            buttons = ['Done', 'Skip', 'Problem found']
            for i, instruction in enumerate(instructions):
                if i < len(session['steps']) and session['steps'][i] is not None:
                    continue
                answer = await _confirm(adapter, source, metadata, f'maintenance-{i}',
                                        f'{instruction} Choose an outcome.', buttons, (globals().get('_config').question_timeout_s() if globals().get('_config') else 1800))
                action = 'problem' if answer == 'Problem found' else 'done' if answer == 'Done' else 'skip'
                session['steps'][i] = action
                if action == 'problem':
                    session['problems'].append(instruction)
                maintenance.save_session(session, state)
            if mode == 'full':
                cold_pull_runs = [r for r in maintenance.load().get('runs', []) if r.get('cold_pull')]
                last_pull = cold_pull_runs[-1].get('cold_pull', {}) if cold_pull_runs else {}
                pla_hot, pla_cold, petg_hot, petg_cold, retry_cold = _cold_pull_temps()
                options = [f'Run PLA (trial) cold pull {pla_hot}/{pla_cold}',
                           f'Run PETG cold pull {petg_hot}/{petg_cold}', 'Skip cold pull']
                if last_pull.get('next_cold') == retry_cold:
                    options[0] = f'Run PLA (trial) cold pull {pla_hot}/{retry_cold}'
                answer = await _confirm(adapter, source, metadata, 'maintenance-coldpull',
                    'Cold pull requires you at the printer. Bed must be clear; it homes. Screen may look frozen for a few minutes.', options, (globals().get('_config').question_timeout_s() if globals().get('_config') else 1800))
                if answer and answer != 'Skip cold pull':
                    cold = retry_cold if answer.endswith(f'{pla_hot}/{retry_cold}') else None
                    record, _cold_message = await _coldpull_workflow(
                        adapter, source, metadata, source_tag='maintenance', initial_cold=cold,
                        selected_material=answer)
                    session['cold_pull'] = record.get('cold_pull') if record else {'result': 'cancelled'}
                    if session.get('cold_pull'):
                        session['cold_pull']['source'] = 'maintenance'
                    if record:
                        session['cold_pull_record'] = record
                        # The shared workflow already persisted the physical pull.
                        # Maintenance completion must not append a duplicate record.
                        session['cold_pull_record_persisted'] = True
            session['completed'] = True
            session['hours_total'] = await asyncio.to_thread(_maintenance_hours_now)
            final_record = session.pop('cold_pull_record', None) or session
            if not session.pop('cold_pull_record_persisted', False):
                maintenance.append_run(final_record)
            state.pop('pending', None)
            maintenance.save(state)
            message = f'{mode.title()} maintenance checklist saved. '
            if session.get('cold_pull', {}).get('result') == 'failed':
                message += f" Cold pull failed: {session['cold_pull'].get('detail', 'check printer')}"
            elif session.get('cold_pull', {}).get('result') not in (None, 'cancelled', 'awaiting_tip'):
                message += f" Cold pull recorded: {session['cold_pull']['result']}."
            elif session.get('cold_pull', {}).get('result') == 'awaiting_tip':
                message += ' Cold pull completed; tip result is awaiting operator input.'
            else:
                message += ' No printer action was used.'
        await adapter.send(chat_id=source.chat_id, metadata=metadata, content=message)
    except Exception as exc:
        logger.exception('maintenance workflow failed')
        await adapter.send(chat_id=source.chat_id, metadata=metadata, content=f'Maintenance stopped: {exc}. No further printer action was sent.')
    finally:
        _seen.pop(key, None)
    return {'action': 'skip', 'reason': 'maintenance handled'}


async def runout_intake_dispatch(event, gateway, **kwargs):
    """Answer a pending filament-runout pause. Inventory writes only.

    A generic pause leaves no pending record, so an ordinary message is ignored.
    Nothing here resumes, heats, or moves the printer.
    """
    source = _discord_operator(event, gateway)
    text = str(getattr(event, 'text', '')).strip()
    if source is None or not text or text.startswith('/'):
        return None
    from print_buddy.hermes import moonraker_event_listener as listener
    context = listener.load_runout_pause()
    if context is None:
        return None
    choice = listener.parse_runout_reply(text)
    if text.strip().casefold() in ('same', 'same spool', 'no change'):
        choice = {'status': 'same'}
    adapter = gateway._delivery_adapter_for(source)
    metadata = {'thread_id': source.thread_id} if source.thread_id else None
    if choice['status'] in ('need_choice', 'need_spool_number', 'need_new_fields', 'unparsed'):
        await adapter.send(chat_id=source.chat_id, metadata=metadata, content=(
            'Spool question is still open and usage is still paused. Reply with a spool number, '
            '`same`, or: brand, material, color, grams. Nothing was changed.'))
        return {'action': 'skip', 'reason': 'spool reply needs a choice'}
    from print_buddy.hermes import spool_sync
    try:
        message = await asyncio.to_thread(_answer_spool_question, listener, spool_sync, context, choice)
    except Exception as exc:
        logger.exception('spool question answer failed')
        await adapter.send(chat_id=source.chat_id, metadata=metadata,
                           content=f'Nothing changed: {exc}. Usage is still paused; answer again.')
        return {'action': 'skip', 'reason': 'spool answer failed closed'}
    await adapter.send(chat_id=source.chat_id, metadata=metadata, content=message)
    return {'action': 'skip', 'reason': 'spool question answered'}


def _answer_spool_question(listener, spool_sync, context, choice):
    """Apply one answer. Spoolman first, then hardware map + screen, then usage resumes. No motion."""
    current = listener.load_runout_pause()
    if current is None:
        raise spool_sync.SpoolError('that spool question was already answered; nothing changed')
    context = current
    slot = context.get('slot')
    prior = context.get('prior_spool_id')
    if choice['status'] == 'same':
        keep = prior or context.get('suspended_spool_id')
        if not keep:
            raise spool_sync.SpoolError('I do not know which spool was there before; reply with its number')
        spool_sync.resume_usage(int(keep))
        listener.clear_runout_pause()
        return f'Got it, same spool #{keep}. Usage tracking is back on #{keep}.'
    if slot not in (1, 2, 3, 4):
        raise spool_sync.SpoolError('I could not tell which slot changed; use `/spool change <slot> <spool#>`')
    created = ''
    if choice['status'] == 'new':
        state = spool_sync.load_state()
        previous_slot = (state.get('ifs_slots') or {}).get(str(slot)) or {}
        existing_hex = spool_sync.screen_hex(previous_slot.get('hex'))
        made = spool_sync.create_spool({'brand': choice.get('brand'), 'material': choice['material'],
                                        'color': choice['color'], 'grams': choice['grams'], 'hex': existing_hex})
        spool_id = int(made['spool_id'])
        created = f'Created spool #{spool_id}. '
    else:
        spool_id = int(choice['spool_id'])
    runout = context.get('cause') == 'runout'
    if runout and prior and int(prior) == spool_id:
        raise spool_sync.SpoolError(f'spool #{spool_id} is the one that just ran out; pick another')
    if runout and prior:
        spool_sync.spoolman('PATCH', f'/api/v1/spool/{int(prior)}',
                            {'archived': True, 'remaining_weight': 0, 'location': 'depleted'})
    result = spool_sync.bind_slot(slot, spool_id, prior_disposition='keep' if runout else 'shelf',
                                  note=f"Bound from Discord after mid-print {context.get('cause')}")
    spool_sync.resume_usage(spool_id)
    listener.clear_runout_pause()
    gone = f' Spool #{prior} marked used up.' if runout and prior else ''
    return f'{created}{spool_sync.bind_text(result)}{gone} Usage tracking is back on, charged to #{spool_id}.'


async def spool_dispatch(event, gateway, **kwargs):
    """/spool list | change <slot> <spool#> | new brand, material, color, grams[, hex] | sync. Never moves the printer."""
    source, adapter = _control_preamble(event, gateway, '/spool', with_args=True)
    if source is None:
        return None
    if adapter is None:
        return {'action': 'skip', 'reason': 'spool UI delivery unavailable'}
    from print_buddy.hermes import spool_sync
    metadata = {'thread_id': source.thread_id} if source.thread_id else None
    args = str(getattr(event, 'text', '')).strip()[len('/spool'):].strip()
    verb, _, rest = args.partition(' ')
    verb = verb.casefold()
    try:
        if verb in ('', 'list', 'ls'):
            message = await asyncio.to_thread(spool_sync.list_text)
        elif verb in ('change', 'set', 'load'):
            parts = rest.replace('#', ' ').split()
            if len(parts) != 2 or not all(p.isdigit() for p in parts):
                raise spool_sync.SpoolError('format: /spool change <slot 1-4> <spool#>')
            result = await asyncio.to_thread(spool_sync.bind_slot, int(parts[0]), int(parts[1]))
            message = spool_sync.bind_text(result)
        elif verb in ('new', 'add'):
            made = await asyncio.to_thread(spool_sync.create_spool, spool_sync.parse_new(rest))
            message = (f"Created spool #{made['spool_id']} on the shelf"
                       f"{' (existing filament type reused)' if made['reused_filament'] else ''}. "
                       f"Load it with `/spool change <slot> {made['spool_id']}`.")
        elif verb == 'sync':
            message = 'Printer screen from Spoolman:\n' + '\n'.join(await asyncio.to_thread(spool_sync.sync_all_screens))
        else:
            message = ('`/spool list` · `/spool change <slot> <spool#>` · '
                       '`/spool new brand, material, color, grams[, hex]` · `/spool sync`')
    except Exception as exc:
        logger.exception('/spool failed')
        message = f'Nothing changed: {exc}'
    await adapter.send(chat_id=source.chat_id, metadata=metadata, content=message)
    return {'action': 'skip', 'reason': 'spool command handled'}


async def lookup_dispatch(event, gateway, **kwargs):
    """/print-lookup <query>: a read-only search of the root registries (issue #169).

    Answers from knowledge/, firmware/ and printers/ data only. There is no printer
    client on this path: a lookup cannot heat, move, extrude or change anything.
    """
    source, adapter = _control_preamble(event, gateway, '/print-lookup', with_args=True)
    if source is None:
        return None
    if adapter is None:
        return {'action': 'skip', 'reason': 'lookup UI delivery unavailable'}
    from print_buddy import knowledge
    metadata = {'thread_id': source.thread_id} if source.thread_id else None
    query = str(getattr(event, 'text', '')).strip()[len('/print-lookup'):].strip()
    try:
        message = await asyncio.to_thread(knowledge.lookup_text, query)
    except Exception:
        logger.exception('/print-lookup failed')
        message = 'Lookup failed; the registries could not be read. Nothing was sent to the printer.'
    await adapter.send(chat_id=source.chat_id, metadata=metadata, content=message)
    return {'action': 'skip', 'reason': 'print lookup handled'}


TROUBLESHOOT_STOP_LABEL = 'Stop here'


async def troubleshoot_dispatch(event, gateway, **kwargs):
    """/print-troubleshoot [flow]: a guided physical troubleshooting walkthrough (issue #169).

    Step-by-step advice with the flow's safety warnings first. Questions are the
    bot's own buttons; answers only move the walkthrough forward. Nothing is sent
    to the printer at any step: no heat, no motion, no extrusion, no macro. Where
    a fix is an action (a cold pull), the flow points at the existing guarded
    command, which asks for its own confirmation separately.
    """
    source, adapter = _control_preamble(event, gateway, '/print-troubleshoot', with_args=True)
    if source is None:
        return None
    if adapter is None:
        return {'action': 'skip', 'reason': 'troubleshoot UI delivery unavailable'}
    from print_buddy import knowledge
    metadata = {'thread_id': source.thread_id} if source.thread_id else None
    arg = str(getattr(event, 'text', '')).strip()[len('/print-troubleshoot'):].strip().casefold()
    registry = knowledge.default_registry()
    flow = registry.flow(arg) if arg else None
    if flow is None and arg:
        for match in registry.lookup(arg, limit=5):
            if match.kind == 'flow':
                flow = match.item
                break
            if match.kind == 'symptom' and match.item.flow:
                flow = registry.flow(match.item.flow)
                break
    if flow is None:
        await adapter.send(chat_id=source.chat_id, metadata=metadata,
                           content='Which problem?\n' + knowledge.flows_text(registry)
                           + '\nYou can also describe the symptom, e.g. `/print-troubleshoot extruder clicking`.')
        return {'action': 'skip', 'reason': 'troubleshoot usage shown'}
    key = ('troubleshoot', source.chat_id, source.thread_id)
    if key in _seen:
        await adapter.send(chat_id=source.chat_id, metadata=metadata,
                           content='A troubleshooting walkthrough is already open here. Finish or stop that one first.')
        return {'action': 'skip', 'reason': 'troubleshoot already active'}
    _seen[key] = time.monotonic()
    completed = False
    try:
        session = knowledge.FlowSession(flow)
        timeout = (globals().get('_config').safety_confirm_timeout_s() if globals().get('_config') else 600)
        while True:
            step = session.step()
            if step.done:
                completed = True
                break
            if step.safety:
                await adapter.send(chat_id=source.chat_id, metadata=metadata,
                                   content=f"**{flow.title}** — safety first:\n"
                                           + '\n'.join(f'- {line}' for line in step.safety))
            if step.kind == 'advice':
                await adapter.send(chat_id=source.chat_id, metadata=metadata, content=step.text)
                if session.advance().done:
                    completed = True
                    break
                continue
            answer = await _confirm(adapter, source, metadata, 'print-troubleshoot', step.text,
                                    [*step.choices, TROUBLESHOOT_STOP_LABEL], timeout)
            if answer is None or answer == TROUBLESHOOT_STOP_LABEL:
                break
            session.answer(answer)
    except Exception:
        logger.exception('/print-troubleshoot failed')
        await adapter.send(chat_id=source.chat_id, metadata=metadata,
                           content='Troubleshooting stopped on an error. Nothing was sent to the printer.')
        return {'action': 'skip', 'reason': 'troubleshoot failed'}
    finally:
        _seen.pop(key, None)
    await adapter.send(chat_id=source.chat_id, metadata=metadata,
                       content=(f'{flow.title}: walkthrough complete. ' if completed
                                else 'Troubleshooting stopped. ') + knowledge.ADVISORY_NOTE)
    return {'action': 'skip', 'reason': 'troubleshoot handled'}


async def print_stop_dispatch(event, gateway, **kwargs):
    """/print-stop: one confirm button, then at most one Moonraker cancel for the confirmed file."""
    source = getattr(event, 'source', None)
    text = str(getattr(event, 'text', '')).strip()
    if text != '/print-stop':
        return None
    if (source is None or not _is_bot_platform(source)
            or getattr(source, 'profile', None) != '3dprinting'
            or getattr(event, 'internal', False) or getattr(source, 'is_bot', False)
            or getattr(source, 'profile_route_rejected', False)):
        return None
    if not getattr(source, 'user_id', None) or not gateway._is_user_authorized_for_source(source):
        return None
    adapter = gateway._delivery_adapter_for(source)
    if adapter is None:
        return {'action': 'skip', 'reason': 'print stop UI delivery unavailable'}
    import uuid
    from print_buddy.hermes import print_stop
    from print_buddy.hermes import discord_print_summary as summary
    metadata = {'thread_id': source.thread_id} if source.thread_id else None
    key = ('print-stop',)
    if key in _seen:
        await adapter.send(chat_id=source.chat_id, metadata=metadata,
                           content='A stop prompt is already open. Nothing sent.')
        return {'action': 'skip', 'reason': 'stop prompt already active'}
    _seen[key] = time.monotonic()
    try:
        st = await asyncio.to_thread(print_stop.status)
        if st['state'] not in print_stop.ACTIVE:
            message = f"Nothing to stop: printer is {st['state']}. Nothing sent."
        else:
            from print_buddy.core.prompts import gateway as _prompts
            cm = _prompts()
            name = summary.format_event('started', st['filename']).splitlines()[0].split(': ', 1)[-1]
            where = print_stop.where_text(st)
            question = f"Stop {name} ({st['state']}{', ' + where if where else ''})? This cancels the print."
            choices = [print_stop.STOP_LABEL, print_stop.KEEP_LABEL]
            cid = uuid.uuid4().hex
            session_key = f'print-stop:{source.chat_id}:{source.thread_id}'
            cm.register(cid, session_key, question, choices)
            try:
                sent = await adapter.send_clarify(chat_id=str(source.chat_id), question=question,
                                                  choices=choices, clarify_id=cid,
                                                  session_key=session_key, metadata=metadata)
                if not getattr(sent, 'success', False):
                    raise RuntimeError('button delivery failed')
                answer = await asyncio.to_thread(cm.wait_for_response, cid, (globals().get('_config').safety_confirm_timeout_s() if globals().get('_config') else 600))
            finally:
                cm.resolve_gateway_clarify(cid, cm.CANCELLED)
            if answer != print_stop.STOP_LABEL:
                message = 'Kept printing. Nothing sent.'
            else:
                _sent, message = await asyncio.to_thread(print_stop.cancel, st['filename'])
    except Exception as exc:
        logger.exception('print-stop failed')
        message = f'Stop failed: {exc}. Check the printer screen.'
    finally:
        _seen.pop(key, None)
    await adapter.send(chat_id=source.chat_id, content=message, metadata=metadata)
    return {'action': 'skip', 'reason': 'deterministic print stop handled'}


def _control_preamble(event, gateway, command, with_args=False):
    """Shared gate for the /print-resume, /print-again and /print-reboot commands."""
    source = getattr(event, 'source', None)
    text = str(getattr(event, 'text', '')).strip()
    if not (text == command or with_args and text.startswith(command + ' ')):
        return None, None
    if (source is None or not _is_bot_platform(source)
            or getattr(source, 'profile', None) != '3dprinting'
            or getattr(event, 'internal', False) or getattr(source, 'is_bot', False)
            or getattr(source, 'profile_route_rejected', False)):
        return None, None
    if not getattr(source, 'user_id', None) or not gateway._is_user_authorized_for_source(source):
        return None, None
    return source, gateway._delivery_adapter_for(source)


async def _confirm(adapter, source, metadata, prefix, question, choices, timeout):
    import uuid
    from print_buddy.core.prompts import gateway as _prompts
    cm = _prompts()
    cid = uuid.uuid4().hex
    session_key = f'{prefix}:{source.chat_id}:{source.thread_id}'
    cm.register(cid, session_key, question, choices)
    try:
        sent = await adapter.send_clarify(chat_id=str(source.chat_id), question=question, choices=choices,
                                          clarify_id=cid, session_key=session_key, metadata=metadata)
        if not getattr(sent, 'success', False):
            raise RuntimeError('button delivery failed')
        return await asyncio.to_thread(cm.wait_for_response, cid, timeout)
    finally:
        cm.resolve_gateway_clarify(cid, cm.CANCELLED)


async def print_resume_dispatch(event, gateway, **kwargs):
    """/print-resume: one confirm button, then at most one Moonraker resume. Never restarts anything."""
    source, adapter = _control_preamble(event, gateway, '/print-resume')
    if source is None:
        return None
    if adapter is None:
        return {'action': 'skip', 'reason': 'print resume UI delivery unavailable'}
    from print_buddy.hermes import print_control as pc
    from print_buddy.hermes import discord_print_summary as summary
    metadata = {'thread_id': source.thread_id} if source.thread_id else None
    key = ('print-resume',)
    if key in _seen:
        await adapter.send(chat_id=source.chat_id, metadata=metadata, content='A resume prompt is already open. Nothing sent.')
        return {'action': 'skip', 'reason': 'resume prompt already active'}
    _seen[key] = time.monotonic()
    try:
        st = await asyncio.to_thread(pc.status)
        if not pc.resumable(st):
            message = pc.not_resumable_text(st)
        else:
            name = summary.format_event('started', st['filename']).splitlines()[0].split(': ', 1)[-1]
            where = pc.where_text(st)
            answer = await _confirm(adapter, source, metadata, 'print-resume',
                                    f"Resume {name}{' (' + where + ')' if where else ''}? Make sure nothing is in the way.",
                                    [pc.RESUME_LABEL, pc.KEEP_LABEL], (globals().get('_config').safety_confirm_timeout_s() if globals().get('_config') else 600))
            if answer != pc.RESUME_LABEL:
                message = 'Left it paused. Nothing sent.'
            else:
                _sent, message = await asyncio.to_thread(pc.resume, st['filename'])
    except Exception as exc:
        logger.exception('print-resume failed')
        message = f'Resume failed: {exc}. Check the printer screen.'
    finally:
        _seen.pop(key, None)
    await adapter.send(chat_id=source.chat_id, content=message, metadata=metadata)
    return {'action': 'skip', 'reason': 'deterministic print resume handled'}


async def print_again_dispatch(event, gateway, **kwargs):
    """/print-again [list | N | name]: an archived printed job from the start, behind one bed-clear button."""
    source, adapter = _control_preamble(event, gateway, '/print-again', with_args=True)
    if source is None:
        return None
    if adapter is None:
        return {'action': 'skip', 'reason': 'print again UI delivery unavailable'}
    from print_buddy.hermes import print_control as pc
    from print_buddy.hermes import print_next
    metadata = {'thread_id': source.thread_id} if source.thread_id else None
    query = str(getattr(event, 'text', '')).strip()[len('/print-again'):].strip()
    if query.casefold() in ('list', 'ls', 'history'):
        rows = await asyncio.to_thread(pc.printed_jobs)
        await adapter.send(chat_id=source.chat_id, metadata=metadata, content=pc.printed_list_text(rows))
        return {'action': 'skip', 'reason': 'print again list handled'}
    if query.casefold() in ('stop', 'cancel', 'clear'):
        had = await asyncio.to_thread(pc.cancel_copies)
        await adapter.send(chat_id=source.chat_id, metadata=metadata,
                           content='Remaining copies dropped. The current print is untouched.' if had
                           else 'No copies were queued.')
        return {'action': 'skip', 'reason': 'print again queue cleared'}
    key = ('print-again',)
    if key in _seen:
        await adapter.send(chat_id=source.chat_id, metadata=metadata, content='A print again prompt is already open. Nothing started.')
        return {'action': 'skip', 'reason': 'print again prompt already active'}
    _seen[key] = time.monotonic()
    try:
        is_next = query.casefold() in ('next', 'continue')
        if is_next:
            target = await asyncio.to_thread(pc.next_copy_target)
            question = (f"Print copy {target['copy_number']} of {target['copies']} of {target['title']}?\n"
                        "Confirm the bed is clear.")
        else:
            query, copies = pc.split_count(query)
            target = await asyncio.to_thread(pc.resolve_print, query)
            target['copies'] = copies
            question = pc.again_question(target)
        answer = await _confirm(adapter, source, metadata, 'print-again', question,
                                [pc.AGAIN_LABEL, pc.AGAIN_CANCEL_LABEL], (globals().get('_config').question_timeout_s() if globals().get('_config') else 1800))
        if answer != pc.AGAIN_LABEL:
            message = 'Cancelled or expired. Nothing uploaded or started.'
        else:
            from print_buddy.hermes import discord_print_summary as print_summary
            gcode_seconds, _filament_g = print_summary.gcode_metadata(target['gcode'])
            number = print_summary._print_number({'_job_path': str(Path(target['job_json']).parent)})
            estimate = (f"estimated print time ~{print_summary.human(gcode_seconds)}, plus warmup "
                        f"~{print_summary.human(gcode_seconds + print_summary.WARMUP_S)} total"
                        if gcode_seconds is not None else 'estimated print time unknown')
            number_line = f"Print #{number}; " if number else 'Print number unknown; '
            await adapter.send(chat_id=source.chat_id, metadata=metadata,
                               content=f"Preparing to start {target['title']}. {number_line}{estimate}.")
            ok, detail = await print_next.start(target)
            if ok:
                await asyncio.to_thread(pc.mark_copy_started if is_next else pc.queue_copies,
                                        *(() if is_next else (target,)))
            # The Moonraker listener owns the sole canonical start announcement,
            # including the model roast. A successful print-now request may still
            # be preparing, so do not race it with a second, incomplete receipt.
            message = (None if ok
                       else f"Did not start {target['title']}. Not retrying.\n{detail}")
            if not ok and _start_refused_needs_restart(detail):
                message += '\n' + await _offer_force_restart(adapter, source, metadata, pc, detail)
    except print_next.NextPlateError as exc:
        message = f'Nothing to print again: {exc}. Nothing uploaded or started.'
    except Exception as exc:
        logger.exception('print-again failed')
        message = f'Print again failed: {exc}. Check the printer screen before retrying.'
    finally:
        _seen.pop(key, None)
    if message:
        await adapter.send(chat_id=source.chat_id, content=message, metadata=metadata)
    return {'action': 'skip', 'reason': 'deterministic print again handled'}


def _print_restart_request(event):
    """Recognize direct natural-language requests, not mere mentions of restarting."""
    text = str(getattr(event, 'text', '') or '').strip().casefold()
    if re.search(r'\b(?:do not|don.t|never|avoid|without)\b.{0,30}\b(?:restart|reboot|power[- ]?cycle)\b', text):
        return False
    if re.search(r'\b(?:should|could|would|can|may|need to|want to)\b.{0,25}\b(?:restart|reboot|power[- ]?cycle)\b', text):
        return True
    return bool(re.fullmatch(r'(?:please\s+)?(?:restart|reboot)(?:\s+(?:the\s+)?(?:printer|machine|firmware))?[.! ]*', text)
                or re.search(r'\b(?:restart|reboot)\s+(?:the\s+)?(?:printer|machine|firmware)\b', text))


def _start_refused_needs_restart(detail):
    text = str(detail or '').casefold()
    if not text.startswith('refused:'):
        return False
    return any(word in text for word in ('paused', 'cancelled', 'canceled', 'shutdown', 'klippy',
                                         'stale', 'retained job', 'not idle', 'state is error'))


async def _offer_force_restart(adapter, source, metadata, pc, reason):
    """Offer the existing confirmation flow after a safe-to-restart start refusal."""
    answer = await _confirm(adapter, source, metadata, 'print-reboot',
        f"{reason} Force restart clears a stuck screen or heaters and wipes any Resume the printer screen is offering. Retry the print manually afterward.",
        [pc.REBOOT_LABEL, pc.CANCEL_REBOOT_LABEL], (globals().get('_config').safety_confirm_timeout_s() if globals().get('_config') else 600))
    if answer != pc.REBOOT_LABEL:
        return 'Not restarted. Nothing sent; retry the print only after checking the printer.'
    await adapter.send(chat_id=source.chat_id, metadata=metadata,
                       content='Restarting the printer now. The print will not start automatically; retry it after restart.')
    _ok, message = await asyncio.to_thread(pc.force_reboot)
    return message + (' Retry the print manually.' if _ok else '')


async def print_reboot_dispatch(event, gateway, **kwargs):
    """Natural restart requests use the safe ZMod REBOOT button flow."""
    source, adapter = _control_preamble(event, gateway, '/print-reboot')
    if source is None:
        if not _is_print_discord_event(event):
            return None
        text = str(getattr(event, 'text', '') or '').strip().casefold()
        if not _print_restart_request(event):
            return None
        source = event.source
        if not getattr(source, 'user_id', None) or not gateway._is_user_authorized_for_source(source):
            return None
        adapter = gateway._delivery_adapter_for(source)
        if adapter is None:
            return {'action': 'skip', 'reason': 'print reboot UI delivery unavailable'}
    if adapter is None:
        return {'action': 'skip', 'reason': 'print reboot UI delivery unavailable'}
    from print_buddy.hermes import print_control as pc
    metadata = {'thread_id': source.thread_id} if source.thread_id else None
    key = ('print-reboot',)
    if key in _seen:
        await adapter.send(chat_id=source.chat_id, metadata=metadata, content='A restart is already open or running. Nothing sent.')
        return {'action': 'skip', 'reason': 'reboot already active'}
    _seen[key] = time.monotonic()
    try:
        st = await asyncio.to_thread(pc.status)
        if st.get('klippy') == 'shutdown' and st.get('state') != 'printing':
            # Emergency stop: REBOOT is a G-code macro Klipper rejects while shut down.
            # One FIRMWARE_RESTART clears it; no motion, no heat. Still needs the confirm button.
            from print_buddy.hermes import print_alert_buttons as pab
            reason = await asyncio.to_thread(pab.shutdown_message)
            answer = await _confirm(adapter, source, metadata, 'print-reboot',
                                    "The printer is in an emergency stop. " + pab.explain(reason)
                                    + "\nClear it with one firmware restart? No motion or heat. This ends the "
                                    "stopped job for good. Only do this after the bed and nozzle are clear.",
                                    [pc.REBOOT_LABEL, pc.CANCEL_REBOOT_LABEL], (globals().get('_config').safety_confirm_timeout_s() if globals().get('_config') else 600))
            if answer != pc.REBOOT_LABEL:
                message = 'Not restarted. Nothing sent.'
            else:
                await adapter.send(chat_id=source.chat_id, metadata=metadata,
                                   content='Clearing the emergency stop now. Up to a minute.')
                _ok, message = await asyncio.to_thread(pab.clear_shutdown)
                after = await asyncio.to_thread(pc.status)
                message += f" Job state now {after.get('state')!r}."
        elif st.get('state') not in {'idle', 'standby', 'complete', 'cancelled', 'error'} or st.get('klippy') != 'ready':
            message = (f"Refused: printer job state is {st.get('state')!r}, controller state is "
                       f"{st.get('klippy')!r}. Check the printer screen before any restart. Nothing sent.")
        else:
            answer = await _confirm(adapter, source, metadata, 'print-reboot',
                                    "Force restart the AD5X? This clears a stuck screen or heaters, and it wipes "
                                    "any Resume the printer screen is offering. Takes about two minutes.",
                                    [pc.REBOOT_LABEL, pc.CANCEL_REBOOT_LABEL], (globals().get('_config').safety_confirm_timeout_s() if globals().get('_config') else 600))
            if answer != pc.REBOOT_LABEL:
                message = 'Not restarted. Nothing sent.'
            else:
                await adapter.send(chat_id=source.chat_id, metadata=metadata,
                                   content='Restarting the printer now. About two minutes.')
                _ok, message = await asyncio.to_thread(pc.force_reboot)
    except Exception as exc:
        logger.exception('print-reboot failed')
        message = f'Restart failed: {exc}. Check the printer screen.'
    finally:
        _seen.pop(key, None)
    await adapter.send(chat_id=source.chat_id, content=message, metadata=metadata)
    return {'action': 'skip', 'reason': 'deterministic print reboot handled'}


async def typed_quantity_dispatch(event, gateway, **kwargs):
    """A typed reply ("4", "3 copies") while a print quantity prompt is open answers that prompt."""
    if not _is_print_discord_event(event):
        return None
    source = event.source
    if not getattr(source, 'user_id', None) or not gateway._is_user_authorized_for_source(source):
        return None
    from print_buddy.core.link_intake import typed_reply
    error = typed_reply(source, getattr(event, 'text', ''))
    if error is None:
        return None
    if error:
        adapter = gateway._delivery_adapter_for(source)
        if adapter is not None:
            await adapter.send(chat_id=source.chat_id, content=error,
                               metadata={'thread_id': source.thread_id} if source.thread_id else None)
    return {'action': 'skip', 'reason': 'typed print quantity handled'}


def _recent_model_link_from_db():
    import sqlite3, re
    db_path = _hermes_home() / 'state.db'
    if not db_path.exists():
        return None
    try:
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute("SELECT content FROM messages WHERE role in ('assistant', 'user') ORDER BY id DESC LIMIT 25")
        rows = cur.fetchall()
        URL_RE = re.compile(r'https?://(?:www\.)?(?:makerworld\.com/en/models/\d+[^\s\)\>\]]*|printables\.com/model/\d+[^\s\)\>\]]*)')
        for (content,) in rows:
            if not content:
                continue
            m = URL_RE.findall(content)
            if m:
                return m[0]
    except Exception:
        pass
    return None


def _recent_model_link(source):
    """The last model link in this channel: Print Buddy's own state first, then Hermes' state.db."""
    from print_buddy.bot import recent_links
    found = recent_links.recall(getattr(source, 'chat_id', None), getattr(source, 'thread_id', None))
    if found:
        return found
    from print_buddy.core import prompts
    if prompts.active():
        return None  # standalone: there is no Hermes session history to read
    return _recent_model_link_from_db()


def _is_affirmative_followup(text: str) -> bool:
    if not text:
        return False
    import re
    t = text.strip().lower()
    patterns = [
        r"\b(let's|lets)\s+(do\s+it|print\s+(it|this|that))\b",
        r"\bprint\s+(it|this|that|now)\b",
        r"\bgo\s+ahead\b",
        r"\bdo\s+it\b",
        r"\bstart\s+(it|this|printing)\b",
        r"\byes,?\s+(please\s+)?(print|do\s+it)\b",
        r"\b(sounds|looks)\s+good,?\s*(print|do\s+it)?\b",
        r"\byep,?\s*(print|do\s+it)?\b",
    ]
    return any(re.search(p, t) for p in patterns)


async def model_link_dispatch(event, gateway, **kwargs):
    source = getattr(event, 'source', None)
    if (source is None or not _is_bot_platform(source)
            or getattr(source, 'profile', None) != '3dprinting'
            or getattr(event, 'internal', False) or getattr(source, 'is_bot', False)
            or getattr(source, 'profile_route_rejected', False)):
        return None
    if not getattr(source, 'user_id', None) or not gateway._is_user_authorized_for_source(source):
        return None
    from print_buddy.core.link_intake import model_link
    from print_buddy.core.prepared_intake import prepare_link as collect_link, model_file, reprint_request, last_job
    text = getattr(event, 'text', '')
    reuse = None
    if not (model_link(text) or model_file(event)):
        if _is_affirmative_followup(text):
            recent_url = _recent_model_link(source)
            if recent_url:
                text = f"{recent_url} {text}"
                setattr(event, 'text', text)
        if not (model_link(text) or model_file(event)):
            # "print the previous one in black": offer the last archived model instead of asking for a link.
            reuse = last_job() if reprint_request(text) else None
            if reuse is None:
                return None
    linked = model_link(text)
    if linked:
        from print_buddy.bot import recent_links
        recent_links.remember(source.chat_id, linked, getattr(source, 'thread_id', None))
    event.metadata['fresh_model_intake_handled'] = True
    event.metadata['live_session_key'] = gateway._session_key_for_source(source)
    adapter = gateway._delivery_adapter_for(source)
    if adapter is None:
        return {'action': 'skip', 'reason': 'new model color UI delivery unavailable'}
    # Receipt BEFORE the slow download/inventory: receive_ack never sees handled events.
    mid = getattr(event, 'message_id', None)
    if reuse is None and mid:
        ack = None  # the preview and color picker are the acknowledgement
        if ack:
            await _deliver_receipt(source, adapter, mid, ack)
    from print_buddy.core import printer_state
    from print_buddy.core.link_intake import PromptCancelled
    root = _state_dir() / 'model-link-intakes'
    try:
        return await collect_link(event, adapter, printer_state.load(), root, reuse=reuse)
    except PromptCancelled:
        await adapter.send(chat_id=source.chat_id,
            content='Cancelled. Nothing sliced, nothing uploaded, nothing started. '
                    'The colors and job are saved if you want to resume.',
            metadata={'thread_id': source.thread_id} if source.thread_id else None)
        return {'action': 'skip', 'reason': 'operator cancelled intake'}
    except Exception:
        logger.exception('New model intake failed; original event must not reach stale agent context')
        try:
            await adapter.send(chat_id=source.chat_id,
                content='New model color intake failed. No previous job was resumed and nothing started.',
                metadata={'thread_id': source.thread_id} if source.thread_id else None)
        except Exception:
            logger.exception('New model intake failure delivery also failed')
        return {'action': 'skip', 'reason': 'new model intake unavailable; no start authorized'}




def alert_button_handlers(bot, adapter):
    """Handle listener buttons, including IFS inventory choices, for authorized Discord users."""
    from print_buddy.hermes import print_alert_buttons as pab

    def allowed(interaction):
        user = getattr(interaction, 'user', None)
        uid = str(getattr(user, 'id', ''))
        checker = getattr(adapter, '_is_allowed_user', None)
        if getattr(user, 'bot', False):
            return False
        if callable(checker):
            try:
                return checker(uid, user, guild=getattr(interaction, 'guild', None),
                               channel_ids={str(getattr(interaction, 'channel_id', ''))})
            except Exception:
                return False
        return True

    async def reject(interaction):
        await interaction.response.send_message('Not allowed. Nothing changed.', ephemeral=True)

    from print_buddy.core import prepared_intake as _pi
    STOP_PREFIX = _pi.STOP_CUSTOM_ID

    async def send_stop(source, text, clarify_id):
        """Status step: one Stop button, no 'Other (type answer)'. Returns the sent message."""
        import discord
        target = getattr(source, 'thread_id', None) or getattr(source, 'chat_id', None)
        channel = bot.get_channel(int(target)) if target else None
        if channel is None and target:
            channel = await bot.fetch_channel(int(target))
        if channel is None:
            return None
        view = discord.ui.View(timeout=3600)
        view.add_item(discord.ui.Button(label=_pi.STOP_LABEL, style=discord.ButtonStyle.danger,
                                        custom_id=f'{STOP_PREFIX}{clarify_id}'))
        return await channel.send(text, view=view)

    async def edit_refusal(source, message, text, context, raw):
        """Retire Stop and replace it with plain guidance plus a single Retry button."""
        import discord
        from print_buddy.hermes import print_alert_buttons as pab
        receipt = (context or {}).get('receipt') or {}
        receipt_id = str(receipt.get('intake_id') or '')
        if not re.fullmatch(r'[a-f0-9]{32}', receipt_id):
            await message.edit(content=text, view=None)
            return False
        view = discord.ui.View(timeout=(globals().get('_config').question_timeout_s() if globals().get('_config') else 1800))
        view.add_item(discord.ui.Button(label='Retry', style=discord.ButtonStyle.primary,
                                        custom_id=pab.retry_custom_id(receipt_id)))
        await message.edit(content=text, view=view)
        return True

    _pi.REFUSAL_EDITOR = edit_refusal

    async def handle_stop(interaction, clarify_id):
        from print_buddy.core.prompts import gateway as _prompts
        cm = _prompts()
        resolved = cm.resolve_gateway_clarify(clarify_id, _pi.STOP_LABEL)
        note = 'Stopping.' if resolved else 'That step already finished. Nothing to stop.'
        try:
            await interaction.response.edit_message(view=None)
            await interaction.followup.send(note)
        except Exception:
            logger.debug('could not acknowledge Stop', exc_info=True)

    _pi.STOP_SENDER = send_stop
    async def insertion_select(interaction, aid, alert):
        import discord
        from print_buddy.hermes import spool_sync
        slot = alert.get('slot')
        if slot not in (1, 2, 3, 4):
            await interaction.response.send_message('That insertion prompt is invalid. Nothing changed.', ephemeral=True)
            return
        state = await asyncio.to_thread(spool_sync.load_state)
        mapped = (state.get('ifs_slots') or {}).get(str(slot)) or {}
        loaded_ids = {int(record.get('spoolman_id')) for key, record in (state.get('ifs_slots') or {}).items()
                      if str(key) != str(slot) and isinstance(record, dict) and record.get('spoolman_id') is not None}
        try:
            spools = await asyncio.to_thread(spool_sync.spoolman, 'GET', '/api/v1/spool?allow_archived=false', None)
        except Exception as exc:
            await interaction.response.send_message(f'Could not load shelf spools: {exc}. Nothing changed.', ephemeral=True)
            return
        options = pab.insertion_select_options(spools, mapped, loaded_ids)
        if not options:
            await interaction.response.send_message('No live shelf spools are available. Nothing changed.', ephemeral=True)
            return
        select_aid = await asyncio.to_thread(pab.record, 'insertion-select', '', slot=slot)
        select = discord.ui.Select(placeholder='Choose a shelf spool', min_values=1, max_values=1,
                                   options=[discord.SelectOption(**option) for option in options],
                                   custom_id=f'pbinsertselect:{select_aid}')
        view = discord.ui.View(timeout=(globals().get('_config').question_timeout_s() if globals().get('_config') else 1800))
        view.add_item(select)
        await interaction.response.edit_message(content='Choose the shelf spool to map to this IFS slot. Nothing changes until you select one.', view=view)

    async def insertion_modal(interaction, aid, alert):
        import discord
        slot = alert.get('slot')
        if slot not in (1, 2, 3, 4):
            await interaction.response.send_message('That insertion prompt is invalid. Nothing changed.', ephemeral=True)
            return
        modal_aid = await asyncio.to_thread(pab.record, 'insertion-modal', '', slot=slot)

        class NewSpoolModal(discord.ui.Modal, title='New IFS spool'):
            def __init__(self):
                super().__init__(custom_id=f'pbinsertmodal:{modal_aid}')
                self.fields = {}
                for field in pab.new_insertion_fields():
                    item = discord.ui.TextInput(label=field['label'], custom_id=field['key'],
                                                required=field['required'], max_length=field['max_length'])
                    self.fields[field['key']] = item
                    self.add_item(item)

            async def on_submit(self, modal_interaction):
                pending = await asyncio.to_thread(pab.take, modal_aid)
                if not pending:
                    await modal_interaction.response.send_message('That form is old or was already submitted. Nothing changed.', ephemeral=True)
                    return
                try:
                    from print_buddy.hermes import spool_sync
                    values = {key: str(item.value or '') for key, item in self.fields.items()}
                    result = await asyncio.to_thread(pab.create_and_bind_insertion, pending['slot'], values, spool_sync)
                except Exception as exc:
                    logger.exception('new insertion spool failed')
                    await modal_interaction.response.send_message(f'Nothing changed: {exc}', ephemeral=True)
                    return
                await modal_interaction.response.send_message(result)

        await interaction.response.send_modal(NewSpoolModal())

    async def spool_question_select(interaction, alert):
        import discord
        from print_buddy.hermes import spool_sync
        context = alert.get('context') or {}
        slot = context.get('slot')
        if slot not in (1, 2, 3, 4):
            await interaction.response.send_message('That spool question has no usable slot. Nothing changed.', ephemeral=True)
            return
        state = await asyncio.to_thread(spool_sync.load_state)
        mapped = (state.get('ifs_slots') or {}).get(str(slot)) or {}
        loaded_ids = {int(record.get('spoolman_id')) for key, record in (state.get('ifs_slots') or {}).items()
                      if str(key) != str(slot) and isinstance(record, dict) and record.get('spoolman_id') is not None}
        try:
            spools = await asyncio.to_thread(spool_sync.spoolman, 'GET', '/api/v1/spool?allow_archived=false', None)
        except Exception as exc:
            await interaction.response.send_message(f'Could not load shelf spools: {exc}. Nothing changed.', ephemeral=True)
            return
        options = pab.insertion_select_options(spools, mapped, loaded_ids)
        if not options:
            await interaction.response.send_message('No live shelf spools are available. Nothing changed.', ephemeral=True)
            return
        select_aid = await asyncio.to_thread(pab.record, 'spool-question-select', '', context=context)
        select = discord.ui.Select(placeholder='Choose the replacement shelf spool', min_values=1, max_values=1,
                                   options=[discord.SelectOption(**option) for option in options],
                                   custom_id=f'pbspoolselect:{select_aid}')
        view = discord.ui.View(timeout=(globals().get('_config').question_timeout_s() if globals().get('_config') else 1800))
        view.add_item(select)
        await interaction.response.edit_message(content='Choose the shelf spool to charge for the rest of this print. Nothing changes until you select one.', view=view)

    async def spool_question_modal(interaction, alert):
        import discord
        context = alert.get('context') or {}
        if context.get('slot') not in (1, 2, 3, 4):
            await interaction.response.send_message('That spool question has no usable slot. Nothing changed.', ephemeral=True)
            return
        modal_aid = await asyncio.to_thread(pab.record, 'spool-question-modal', '', context=context)

        class NewSpoolModal(discord.ui.Modal, title='New replacement spool'):
            def __init__(self):
                super().__init__(custom_id=f'pbspoolmodal:{modal_aid}')
                self.fields = {}
                for field in pab.new_insertion_fields():
                    item = discord.ui.TextInput(label=field['label'], custom_id=field['key'],
                                                required=field['required'], max_length=field['max_length'])
                    self.fields[field['key']] = item
                    self.add_item(item)

            async def on_submit(self, modal_interaction):
                pending = await asyncio.to_thread(pab.take, modal_aid)
                if not pending:
                    await modal_interaction.response.send_message('That form is old or was already submitted. Nothing changed.', ephemeral=True)
                    return
                try:
                    from print_buddy.hermes import moonraker_event_listener as listener
                    from print_buddy.hermes import spool_sync
                    values = {key: str(item.value or '') for key, item in self.fields.items()}
                    choice = {'status': 'new', 'brand': values['brand'] or None, 'material': values['material'],
                              'color': values['color'], 'grams': values['grams'], 'lot': values['lot'] or None}
                    result = await asyncio.to_thread(_answer_spool_question, listener, spool_sync,
                                                     pending['context'], choice)
                except Exception as exc:
                    logger.exception('new spool-question spool failed')
                    await modal_interaction.response.send_message(f'Nothing changed: {exc}', ephemeral=True)
                    return
                await modal_interaction.response.send_message(result)

        await interaction.response.send_modal(NewSpoolModal())

    async def handle_spool_question(interaction, action, aid):
        alert = await asyncio.to_thread(pab.take, aid)
        if not alert or alert.get('kind') != 'spool-question':
            await interaction.response.send_message('That spool question is old or was already answered. Nothing changed.', ephemeral=True)
            return
        try:
            await interaction.message.edit(view=None)
        except Exception:
            logger.debug('could not strip spool-question buttons', exc_info=True)
        if action == 'existing':
            await spool_question_select(interaction, alert)
            return
        if action == 'new':
            await spool_question_modal(interaction, alert)
            return
        try:
            from print_buddy.hermes import moonraker_event_listener as listener
            from print_buddy.hermes import spool_sync
            result = await asyncio.to_thread(_answer_spool_question, listener, spool_sync,
                                             alert.get('context') or {}, {'status': 'same'})
        except Exception as exc:
            logger.exception('same spool-question answer failed')
            await interaction.response.send_message(f'Nothing changed: {exc}', ephemeral=True)
            return
        await interaction.response.send_message(result)

    async def handle_spool_question_select(interaction, aid):
        pending = await asyncio.to_thread(pab.take, aid)
        if not pending or pending.get('kind') != 'spool-question-select':
            await interaction.response.send_message('That shelf choice is old or was already used. Nothing changed.', ephemeral=True)
            return
        try:
            spool_id = int((getattr(interaction, 'data', {}) or {}).get('values', [None])[0])
            from print_buddy.hermes import moonraker_event_listener as listener
            from print_buddy.hermes import spool_sync
            result = await asyncio.to_thread(_answer_spool_question, listener, spool_sync,
                                             pending['context'], {'status': 'existing', 'spool_id': spool_id})
        except Exception as exc:
            logger.exception('existing spool-question spool failed')
            await interaction.response.send_message(f'Nothing changed: {exc}', ephemeral=True)
            return
        await interaction.response.edit_message(content=result, view=None)

    async def handle_insertion(interaction, action, aid):
        alert = await asyncio.to_thread(pab.take, aid)
        if not alert or alert.get('kind') != 'insertion':
            await interaction.response.send_message('That insertion prompt is old or was already answered. Nothing changed.', ephemeral=True)
            return
        try:
            await interaction.message.edit(view=None)
        except Exception:
            logger.debug('could not strip insertion buttons', exc_info=True)
        slot = alert.get('slot')
        if action == 'same':
            await interaction.response.send_message(f'Slot {slot}: same spool confirmed. Nothing changed.')
        elif action == 'existing':
            await insertion_select(interaction, aid, alert)
        else:
            await insertion_modal(interaction, aid, alert)

    async def handle_insertion_select(interaction, aid):
        pending = await asyncio.to_thread(pab.take, aid)
        if not pending or pending.get('kind') != 'insertion-select':
            await interaction.response.send_message('That shelf choice is old or was already used. Nothing changed.', ephemeral=True)
            return
        try:
            spool_id = int((getattr(interaction, 'data', {}) or {}).get('values', [None])[0])
            from print_buddy.hermes import spool_sync
            result = await asyncio.to_thread(pab.bind_insertion_existing, pending['slot'], spool_id, spool_sync)
        except Exception as exc:
            logger.exception('existing insertion spool failed')
            await interaction.response.send_message(f'Nothing changed: {exc}', ephemeral=True)
            return
        await interaction.response.edit_message(content=result, view=None)

    async def handle_plate_pick(interaction, aid):
        """Choosing a plate only prepares one Confirm button; nothing starts here."""
        import discord
        from print_buddy.hermes import print_next
        alert = await asyncio.to_thread(pab.peek, aid)
        if not alert or alert.get('kind') != 'plate-picker':
            await interaction.response.send_message('That plate picker is old or was already used. Nothing started.', ephemeral=True)
            return
        try:
            part_id = str((getattr(interaction, 'data', {}) or {}).get('values', [None])[0])
            target = print_next.resolve_next(print_next.load_plan(alert['plan_id']), part_id=part_id)
        except Exception as exc:
            await interaction.response.send_message(f'Cannot start that plate: {exc}. Nothing started.', ephemeral=True)
            return
        if not await asyncio.to_thread(pab.take, aid):  # a parallel pick got there first
            await interaction.response.send_message('That plate picker is old or was already used. Nothing started.', ephemeral=True)
            return
        confirm_aid = await asyncio.to_thread(pab.record, 'plate-confirm', '', plan_id=alert['plan_id'], part_id=part_id)
        view = discord.ui.View(timeout=(globals().get('_config').question_timeout_s() if globals().get('_config') else 1800))
        view.add_item(discord.ui.Button(label=pab.CONFIRM_LABEL, style=discord.ButtonStyle.success,
                                        custom_id=f'{pab.PLATE_PREFIX}:go:{confirm_aid}'))
        await interaction.response.edit_message(content=print_next.question_text(target), view=view)

    async def handle_plate_go(interaction, aid):
        """The one Confirm tap. The alert is single use and print_next.start re-checks the printer and claim."""
        from print_buddy.hermes import print_next
        alert = await asyncio.to_thread(pab.take, aid)
        if not alert or alert.get('kind') != 'plate-confirm':
            await interaction.response.send_message('That confirmation is old or was already used. Nothing started.', ephemeral=True)
            return
        try:
            target = print_next.resolve_next(print_next.load_plan(alert['plan_id']), part_id=alert['part_id'])
        except Exception as exc:
            await interaction.response.edit_message(content=f'Cannot start that plate: {exc}. Nothing started.', view=None)
            return
        await interaction.response.edit_message(content=f"Preparing to start {target['label']}.", view=None)
        await run_plate_start(interaction, target, alert)

    async def run_plate_start(interaction, target, alert, accept_spool_change=False):
        """Start the plate once. A spool swapped since slicing gets one extra single-use button, not a retry."""
        import discord
        from print_buddy.hermes import print_next
        view = None
        try:
            ok, detail = await print_next.start({**target, 'accept_spool_change': True} if accept_spool_change else target)
            message = None if ok else (f"Did not start {target['label']}. Not retrying. "
                                       f"`/print-next {alert['plan_id']} {alert['part_id']}` offers a fresh button.\n{detail}")
            if not ok and not accept_spool_change and SPOOL_CHANGED in str(detail):
                spool_aid = await asyncio.to_thread(pab.record, 'plate-spool', '',
                                                    plan_id=alert['plan_id'], part_id=alert['part_id'])
                view = discord.ui.View(timeout=(globals().get('_config').question_timeout_s() if globals().get('_config') else 1800))
                view.add_item(discord.ui.Button(label=USE_LOADED_SPOOL, style=discord.ButtonStyle.success,
                                                custom_id=f'{pab.PLATE_PREFIX}:spool:{spool_aid}'))
                message = f"Did not start {target['label']}.\n{_spool_change_text(target, detail)}"
        except Exception as exc:
            logger.exception('plate picker start failed')
            message = f'Next plate failed: {exc}. Check the printer screen before retrying.'
        if message:  # None: start accepted, printer preparing; the listener posts "Print started".
            await (interaction.followup.send(message, view=view) if view else interaction.followup.send(message))

    async def handle_plate_spool(interaction, aid):
        """Single-use 'use the loaded spool' tap. start() still re-checks the printer, claim and live slot."""
        from print_buddy.hermes import print_next
        alert = await asyncio.to_thread(pab.take, aid)
        if not alert or alert.get('kind') != 'plate-spool':
            await interaction.response.send_message('That spool confirmation is old or was already used. Nothing started.', ephemeral=True)
            return
        try:
            target = print_next.resolve_next(print_next.load_plan(alert['plan_id']), part_id=alert['part_id'])
        except Exception as exc:
            await interaction.response.edit_message(content=f'Cannot start that plate: {exc}. Nothing started.', view=None)
            return
        await interaction.response.edit_message(content=f"Preparing to start {target['label']} with the loaded spool.", view=None)
        await run_plate_start(interaction, target, alert, accept_spool_change=True)

    async def handle_intake_retry(interaction, receipt_id):
        """Retry this exact refused plate through print_next.start and all of its guards."""
        import discord
        from print_buddy.hermes import print_alert_buttons as pab, print_next
        from print_buddy.core import prepared_intake as intake
        receipt_path = _state_dir() / 'model-link-intakes' / f'{receipt_id}.json'

        def retry_view():
            view = discord.ui.View(timeout=(globals().get('_config').question_timeout_s() if globals().get('_config') else 1800))
            view.add_item(discord.ui.Button(label='Retry', style=discord.ButtonStyle.primary,
                                            custom_id=pab.retry_custom_id(receipt_id)))
            return view

        try:
            receipt = json.loads(receipt_path.read_text())
            target = receipt.get('retry_target')
            if (receipt.get('intake_id') != receipt_id or receipt.get('status') != 'START_REFUSED'
                    or receipt.get('uploaded') is True or receipt.get('started') is True
                    or not intake._safe_preupload_refusal(receipt.get('start_refusal_detail'))
                    or not isinstance(target, dict) or not target.get('gcode')
                    or not target.get('gcode_sha256')):
                raise ValueError('this is not a confirmed pre-upload refusal')
            if str(receipt.get('start_prompt_message_id') or '') != str(getattr(interaction.message, 'id', '') or ''):
                raise ValueError('this Retry button no longer matches the saved prompt')
        except Exception as exc:
            await interaction.response.edit_message(content=f'Retry unavailable: {exc}. Nothing started.', view=None)
            return

        await interaction.response.edit_message(content='Checking the printer again before retrying this prepared print.', view=None)
        try:
            ok, detail = await print_next.start(target)
        except Exception as exc:
            logger.exception('pre-upload retry failed')
            detail = f'REFUSED: printer readiness could not be confirmed: {exc}. Nothing uploaded or started.'
            ok = False
        receipt.update(status='STARTED' if ok else 'START_REFUSED', started=bool(ok),
                       uploaded=bool(ok), start_refusal_detail='' if ok else str(detail))
        receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
        if ok:
            logger.info('retry start accepted plan=%s part=%s', target.get('plan_id'), target.get('part_id'))
            await interaction.message.edit(content='Print accepted by the printer.', view=None)
            return
        raw = intake._raw_preupload_refusal(detail)
        logger.warning('print retry refused detail=%s', intake.logs.redact(raw or detail))
        if intake._safe_preupload_refusal(detail):
            await interaction.message.edit(content=intake._refusal_text(detail), view=retry_view())
        else:
            receipt.update(status='START_UNCERTAIN', uploaded=None, started=None,
                           start_refusal_detail=str(detail))
            receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
            await interaction.message.edit(content=(
                'The printer did not confirm the start result. Check its screen before taking action. '
                'Do not retry until the printer state is known.'), view=None)

    async def on_interaction(interaction):
        data = getattr(interaction, 'data', None) or {}
        custom_id = data.get('custom_id', '')
        if str(custom_id).startswith(_pi.RETRY_CUSTOM_ID):
            if not allowed(interaction):
                await reject(interaction)
                return
            receipt_id = pab.parse_retry_custom_id(custom_id)
            if not receipt_id:
                await interaction.response.send_message('That Retry button is invalid. Nothing started.', ephemeral=True)
                return
            await handle_intake_retry(interaction, receipt_id)
            return
        if str(custom_id).startswith(STOP_PREFIX):
            if not allowed(interaction):
                await reject(interaction)
                return
            await handle_stop(interaction, str(custom_id)[len(STOP_PREFIX):])
            return
        plate = pab.parse_plate_custom_id(custom_id)
        if plate:
            if not allowed(interaction):
                await reject(interaction)
                return
            await {'pick': handle_plate_pick, 'go': handle_plate_go,
                   'spool': handle_plate_spool}[plate[0]](interaction, plate[1])
            return
        parsed = pab.parse_custom_id(custom_id)
        insertion = pab.parse_insertion_custom_id(custom_id)
        spool_question = pab.parse_spool_question_custom_id(custom_id)
        is_select = str(custom_id).startswith('pbinsertselect:')
        is_spool_select = str(custom_id).startswith('pbspoolselect:')
        if not parsed and not insertion and not spool_question and not is_select and not is_spool_select:
            return
        if not allowed(interaction):
            await reject(interaction)
            return
        if insertion:
            await handle_insertion(interaction, *insertion)
            return
        if spool_question:
            await handle_spool_question(interaction, *spool_question)
            return
        if is_select:
            _, aid = str(custom_id).split(':', 1)
            await handle_insertion_select(interaction, aid)
            return
        if is_spool_select:
            _, aid = str(custom_id).split(':', 1)
            await handle_spool_question_select(interaction, aid)
            return
        action, aid = parsed
        alert = await asyncio.to_thread(pab.take, aid)
        label = {'resume': 'Resume', 'pause': 'Pause', 'acknowledge': "It's fine"}.get(action, 'Cancel')
        await interaction.response.send_message(
            f"{getattr(interaction.user, 'display_name', 'Someone')} tapped {label}. Checking the printer first.")
        try:
            await interaction.message.edit(view=None)
        except Exception:
            logger.debug('could not strip alert buttons', exc_info=True)
        try:
            result = await asyncio.to_thread(pab.handle_click, action, alert)
        except Exception as exc:
            logger.exception('alert button %s failed', action)
            result = f'{label} failed: {exc}. Check the printer screen. Not retrying.'
        await interaction.followup.send(result)

    bot.add_listener(on_interaction, 'on_interaction')


def register(ctx):
    # Print Buddy's button prompts already say what to do; drop Hermes's generic
    # "Pick one below, or click Other..." line for this gateway (a locale overlay, not a core patch).
    with contextlib.suppress(Exception):
        ctx.register_locale('en', {'platform': {'discord': {'prompt': {'clarify_hint_buttons': ''}}}})
    ctx.register_hook('pre_gateway_dispatch', guided_dispatch)
    ctx.register_hook('pre_gateway_dispatch', maintenance_dispatch)
    ctx.register_hook('pre_gateway_dispatch', coldpull_dispatch)
    ctx.register_hook('pre_gateway_dispatch', lookup_dispatch)
    ctx.register_hook('pre_gateway_dispatch', troubleshoot_dispatch)
    ctx.register_hook('pre_gateway_dispatch', typed_quantity_dispatch)
    ctx.register_hook('pre_gateway_dispatch', upload_wait_dispatch)
    ctx.register_hook('pre_gateway_dispatch', model_link_dispatch)
    ctx.register_hook('pre_gateway_dispatch', color_selection_dispatch)
    ctx.register_hook('pre_gateway_dispatch', print_next_dispatch)
    ctx.register_hook('pre_gateway_dispatch', runout_intake_dispatch)
    ctx.register_hook('pre_gateway_dispatch', print_stop_dispatch)
    ctx.register_hook('pre_gateway_dispatch', print_resume_dispatch)
    ctx.register_hook('pre_gateway_dispatch', print_again_dispatch)
    ctx.register_hook('pre_gateway_dispatch', spool_dispatch)
    ctx.register_hook('pre_gateway_dispatch', print_reboot_dispatch)
    ctx.register_hook('pre_gateway_dispatch', receive_ack)
    ctx.register_hook('post_tool_call', clarify_ack)
    ctx.register_hook('pre_tool_call', cap_discord_clarifications)
    ctx.register_hook('pre_tool_call', guard_terminal_loop)
    if hasattr(ctx, 'register_platform_handler'):
        ctx.register_platform_handler('discord', alert_button_handlers)
