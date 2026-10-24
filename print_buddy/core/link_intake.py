"""Fresh Discord URL intake. Local labels only; never contact a printer or source site."""
import asyncio
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urlsplit
import uuid

from .. import settings


def model_link(text):
    # A question beside a link is still fresh intake, not historical recovery.
    text = str(text or '').strip()
    if re.match(r'^(?:/)?(?:retry|resume)\b', text, re.I):
        return None
    urls = re.findall(r'https?://[^\s<>\)\]]+', text)
    if len(urls) != 1:
        return None
    text = urls[0].rstrip('.,;!?')
    host = (urlsplit(text).hostname or '').lower()
    path = urlsplit(text).path
    if host in {'makerworld.com', 'www.makerworld.com'} and path.startswith('/en/models/'):
        return text
    if host in {'printables.com', 'www.printables.com'} and re.match(r'/(?:[a-z]{2}/)?model/', path):
        return text
    return None


def has_duration_question(text: str) -> bool:
    """Detect whether a message asks about print duration or time."""
    if not text:
        return False
    return bool(re.search(r'\b(how long|duration|how much time|print time|when.*finish|take)\b', str(text), re.IGNORECASE))


# Prompts that accept a typed reply, by who was asked where and for which intake:
# (chat_id, thread_id, user_id, session_key) -> (clarify_id, parser). Newest last.
_typed_prompts = {}


def _scope(source):
    return (str(getattr(source, 'chat_id', '')), str(getattr(source, 'thread_id', '') or ''),
            str(getattr(source, 'user_id', '') or ''))


def typed_reply(source, text):
    """Route typed text to the sender's newest open typed prompt in this chat. Returns None when they
    have none open (another operator's prompt never takes it), '' when the reply resolved it, or a
    corrective message (the prompt stays open)."""
    from .prompts import gateway as _prompts
    cm = _prompts()
    scope = _scope(source)
    keys = [key for key in _typed_prompts if key[:3] == scope]
    if not scope[2] or not keys:
        return None
    cid, parser = _typed_prompts[keys[-1]]
    if str(text or '').strip().casefold() in CANCEL_WORDS:
        return '' if cm.resolve_gateway_clarify(cid, 'cancel') else None
    try:
        value = parser(text)
    except ValueError as exc:
        return str(exc)
    # A prompt a button already answered is closed: let the text fall through instead of swallowing it.
    return '' if cm.resolve_gateway_clarify(cid, str(value)) else None


CANCEL_WORDS = {'cancel', 'stop', 'nevermind', 'never mind', 'abort'}


def _typed_choice(text, choices):
    """Map clear plain-language intent to a visible button, or None to keep asking."""
    value = str(text or '').strip().casefold()
    if not value:
        return None
    if value in CANCEL_WORDS:
        terms = ('cancel', "don't print", 'stop')
    elif re.fullmatch(r'\d+', value):
        index = int(value) - 1
        return index if 0 <= index < len(choices) else None
    elif 'split' in value:
        terms = ('split',)
    elif value in {'100%', '100', 'full size', 'full'} or 'full size' in value:
        terms = ('100%', 'full size')
    elif 'preview' in value:
        terms = ('preview',)
    elif any(word in value for word in ('shrink', 'smaller', 'small')):
        terms = ('shrink', 'smaller', '%')
    elif value in {'yes', 'start', 'print', 'go'}:
        terms = ('start', 'slice and print', 'yes')
    else:
        return None
    for index, choice in enumerate(choices):
        if any(term in str(choice).casefold() for term in terms):
            return index
    return None


class PromptCancelled(RuntimeError):
    """The operator cancelled a prompt. Nothing after this point should run."""


class PromptExpired(RuntimeError):
    """A button question reached its timeout with no choice and no decision saved."""


CANCEL_LABEL = "Cancel"


async def ask(adapter, source, session_key, question, choices, timeout=None, allow_other=False, parser=None,
              live_key=None):
    """Ask a button question. With ``parser``, a typed reply in the same chat is also accepted;
    ``live_key`` registers under the live chat session so a busy gateway routes the text here
    instead of queueing it as a new agent turn."""
    from .prompts import gateway as _prompts
    cm = _prompts()
    registration_key = live_key or session_key
    # Every question offers Cancel. Callers that already end on a cancel choice keep theirs.
    cancel_added = not choices or not any(term in str(choices[-1]).casefold()
                                          for term in ("cancel", "don't print", "do not print"))
    shown = list(choices) + ([CANCEL_LABEL] if cancel_added else [])
    typed_key = _scope(source) + (str(session_key),)
    cid = None
    try:
        for attempt in range(2):
            # Register BEFORE sending: a fast click can resolve the prompt inside send_clarify,
            # and re-registering afterwards would discard that answer.
            cid = uuid.uuid4().hex
            cm.register(cid, registration_key, question, shown)
            if parser is not None:
                _typed_prompts.pop(typed_key, None)  # re-added last: the newest open question takes typed text
                _typed_prompts[typed_key] = (cid, parser)
            result = await adapter.send_clarify(chat_id=str(source.chat_id), question=question,
                choices=shown, clarify_id=cid, session_key=session_key,
                metadata={'thread_id': source.thread_id} if source.thread_id else None)
            if not result.success:
                raise RuntimeError('color prompt delivery failed')
            answer = await asyncio.to_thread(cm.wait_for_response, cid, timeout)
            if cancel_added and answer == CANCEL_LABEL:
                raise PromptCancelled('cancelled')
            if answer in choices:
                return choices.index(answer)
            if isinstance(answer, str) and parser is not None:
                try:
                    parsed = parser(answer)
                    if parsed is not None:
                        return parsed
                except ValueError:
                    # The live gateway can replay the original request text into an
                    # active prompt. Ignore non-answers and keep the prompt available.
                    pass
            if answer is None:
                raise PromptExpired('this question expired before a button was pressed; no decision saved')
            if answer == cm.CANCELLED:
                break
            if isinstance(answer, str):
                picked = _typed_choice(answer, choices)
                if picked is not None:
                    return picked
                if parser is not None:
                    try:
                        return parser(answer)
                    except ValueError:
                        # A live session may replay the original request text (for example,
                        # a URL plus a duration question) into the first typed prompt. Keep
                        # the prompt open instead of treating that context as a bad reply.
                        pass
            if attempt == 0:
                question += '\nPick a button or type split / 100% / shrink / cancel.'
        raise RuntimeError('clarification requires a valid button choice; no decision saved')
    finally:
        if parser is not None and _typed_prompts.get(typed_key, (None,))[0] == cid:
            _typed_prompts.pop(typed_key, None)
        cm.resolve_gateway_clarify(cid, cm.CANCELLED)
        cm.wait_for_response(cid, 0.001)


async def collect_link(event, adapter, state, root, timeout=300):
    from .model_job import resolve_filament_selection, build_slot_labels
    source = event.source
    url = model_link(event.text)
    message_id = getattr(event, 'message_id', None)
    if not url or not message_id:
        raise ValueError('plain model link requires a stable Discord message ID')
    from .model_job import intake_id_from_message
    intake_id = intake_id_from_message(source, message_id)
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    path = root / (intake_id + '.json')
    receipt = {'intake_id': intake_id, 'url': url, 'status': 'AWAITING_COLOR',
               'sliced': False, 'uploaded': False, 'started': False}
    # Cross-process reservation; completed/failed events are not automatically replayed.
    try:
        with path.open('x') as f:
            json.dump(receipt, f)
    except FileExistsError:
        return {'action': 'skip', 'reason': 'duplicate model-link event'}
    try:
        selections = [resolve_filament_selection(state, dict(label, slot=int(slot)))
                      for slot, label in sorted(state.get('ifs_slots', {}).items())]
        labels = build_slot_labels(selections)
        if not labels or len(labels) > settings.current()['printer']['ifs_slots'] or len(set(labels)) != len(labels):
            raise ValueError('requires one to four distinct configured slots')
        session_key = 'model-link:' + intake_id
        mode = await ask(adapter, source, session_key,
            'New model request: print in one color, or choose colors per part after inspecting the source?',
            ['Print in one color', 'Choose colors per part'], timeout)
        receipt['mode'] = 'single_color' if mode == 0 else 'per_part'
        if mode == 0:
            slot = await ask(adapter, source, session_key,
                'Choose the color for this NEW request, even if this model was printed before. These are configured slot labels.',
                labels, timeout)
            receipt['filament_selection'] = selections[slot]
        receipt['status'] = 'COLOR_CONFIRMED' if mode == 0 else 'PART_COLORS_REQUIRED'
        path.write_text(json.dumps(receipt, indent=2) + '\n')
        packet = json.dumps(receipt, sort_keys=True)
        return {'action': 'rewrite', 'text': (
            f'{url}\n\nCURRENT MODEL INTAKE: {packet}\n'
            'This is a NEW request, not a retry, even for the same URL. History is reference only. '
            'Retrieve fresh source inventory with python3 -m print_buddy.core.model_job fetch and --intake-id ' + intake_id + '. '
            'Do not reuse any historical JOB.json, variant, color decisions, artifact or approval. '
            'Use only this receipt for the initial single-color selection; per-part mode still requires '
            'fresh named-part choices after source inspection. Ask fresh variant/completeness and slicing/bed-clear gates. '
            'No slicing, upload or launch is authorized by this URL or these color choices. '
            'A historical sensor refusal is not current telemetry and does not block intake.')}
    except Exception as exc:
        receipt['status'] = 'FAILED'
        path.write_text(json.dumps(receipt, indent=2) + '\n')
        await adapter.send(chat_id=source.chat_id,
            content=f'New model color selection stopped: {exc}. No slicing, upload or print started.',
            metadata={'thread_id': source.thread_id} if source.thread_id else None)
        return {'action': 'skip', 'reason': 'new model color confirmation failed'}
