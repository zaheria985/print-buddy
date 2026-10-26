"""The chat-platform interface the standalone bot core runs against (#164).

Handlers and ``BotCore`` talk to a platform only through this surface, so a new
chat service is one new implementation, not a fork of the intake, slicing or
print-control code:

* ``send(chat_id, content, reply_to=None, metadata=None)`` — post text. A
  ``MEDIA:/path`` marker in ``content`` becomes a file attachment, the
  convention every caller already uses.
* ``send_clarify(chat_id, question, choices, clarify_id, session_key,
  metadata=None)`` — ask a question with choices. Platforms with buttons
  render them; platforms without fall back to a numbered list that the
  typed-answer path resolves.
* ``answer_typed(session_key, text)`` — a typed reply to the open question
  of that session.
* ``close_prompt(clarify_id, note)`` — edit/close a prompt once answered.
* ``is_allowed_id(user_id)`` — may this user act at all?

Prompt bookkeeping is shared: choices resolve through the platform-neutral
registry in ``print_buddy.core.prompts`` under ``pbq:<clarify_id>:<index>``
ids, so no platform code touches intake state directly.

Per-platform limits live on the adapter as class attributes: ``name``,
``message_limit``, ``upload_max_bytes``, ``supports_buttons`` and
``max_buttons``. Callers that chunk text or size an upload read them from
the platform instead of hard-coding one service's numbers.

Discord is the first implementation (``print_buddy.bot.adapter``),
Telegram the second (``print_buddy.bot.telegram``) and Matrix the third
(``print_buddy.bot.matrix``: numbered-list prompts, no buttons, pure bot).

Adding a platform: implement the methods above in a new module under
``print_buddy/bot/`` (its adapter class is named ``StandaloneAdapter``,
like the Discord one), give it a client that turns service events into
``runtime.make_source`` / ``runtime.make_event`` values and button presses
(or typed answers) into registry resolutions, register its name in
``KNOWN_PLATFORMS`` and its module in ``platform_module``, add its
settings section, and drive it with a fake in ``tests/bot/`` — no handler
or core change should be needed.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from ..core import prompts

logger = logging.getLogger(__name__)

CUSTOM_PREFIX = "pbq:"

# Every platform name a config may name. Implementations register below as
# their stage of #164 lands; a known-but-unimplemented name is a clear
# settings/startup error, never a silent Discord fallback.
KNOWN_PLATFORMS = ("discord", "telegram", "matrix")


@dataclass
class SendResult:
    success: bool
    message_id: str | None = None
    error: str | None = None


def custom_id(clarify_id, index):
    return f"{CUSTOM_PREFIX}{clarify_id}:{index}"


def parse_custom_id(value):
    """(clarify_id, index) for a prompt button, else None."""
    value = str(value or "")
    if not value.startswith(CUSTOM_PREFIX):
        return None
    cid, _, index = value[len(CUSTOM_PREFIX):].rpartition(":")
    if not cid or not index.isdigit():
        return None
    return cid, int(index)


def platform_module(name):
    """The module implementing platform ``name``, or None when its stage has not landed."""
    if name == "discord":
        from . import adapter
        return adapter
    if name == "telegram":
        from . import telegram
        return telegram
    if name == "matrix":
        from . import matrix
        return matrix
    return None


def library_status(name):
    """(label, available, version-or-detail) for the platform's client library.

    Reported by the offline ``check`` command so the check itself never has
    to import a platform library directly. Stdlib-only platforms report
    themselves available with no library row needed.
    """
    if name == "discord":
        return platform_module(name).library_status()
    module = platform_module(name)
    if module is None:
        return (str(name), False, f"platform {name!r} is not implemented yet (#164)")
    return (str(name), True, "standard library client")


@runtime_checkable
class ChatPlatform(Protocol):
    """The operations the bot core needs from a chat service. See the module docstring."""

    name: str
    message_limit: int
    upload_max_bytes: int | None
    supports_buttons: bool
    max_buttons: int

    async def send(self, chat_id, content, reply_to=None, metadata=None) -> SendResult: ...
    async def send_clarify(self, chat_id, question, choices, clarify_id, session_key,
                           metadata=None) -> SendResult: ...
    async def answer_typed(self, session_key, text) -> bool: ...
    async def close_prompt(self, clarify_id, note) -> None: ...
    def is_allowed_id(self, user_id) -> bool: ...


class BasePlatformAdapter:
    """Shared prompt and allow-list behaviour for platform adapters.

    A concrete adapter only delivers: ``send``, ``send_clarify`` (recording
    the prompt row with ``_record_prompt``) and ``close_prompt`` (editing
    the service-side message so an answered question stops offering its
    choices). Who may act, which prompt is open for a session, typed
    answers and choice resolution behave the same on every platform.
    """

    name = "platform"
    message_limit = 2000
    upload_max_bytes: int | None = None
    supports_buttons = False
    max_buttons = 0
    # Pure-bot platforms set this False: BotCore then never calls the LLM
    # fallback for them, even when [llm] is configured (#164 stage 2, #73).
    uses_llm = True

    def __init__(self, allowed, *, registry=prompts):
        self.allowed = {str(u) for u in allowed}
        self.registry = registry
        self._prompts: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------ who may act

    def is_allowed_id(self, user_id):
        return str(user_id or "") in self.allowed

    def _is_allowed_user(self, uid, user=None, guild=None, channel_ids=None):
        """The check the listener alert buttons call (same name as the Hermes adapter's)."""
        if getattr(user, "bot", False):
            return False
        return self.is_allowed_id(uid)

    # ------------------------------------------------------------------ prompts

    def _record_prompt(self, clarify_id, row):
        self._prompts[str(clarify_id)] = row
        self._forget_closed()

    def _forget_closed(self):
        for cid in [c for c in self._prompts if not self.registry.is_open(c)]:
            self._prompts.pop(cid, None)

    def open_prompt(self, session_key):
        """The newest open button question asked under ``session_key``, else None."""
        # The registry key is the live chat session (who asked, where); send_clarify's key names the flow.
        hits = [cid for cid in self._prompts if self.registry.session_key(cid) == str(session_key)]
        return hits[-1] if hits else None

    async def answer_typed(self, session_key, text):
        """A typed reply to the open question of this session (Hermes routes these the same way)."""
        cid = self.open_prompt(session_key)
        if cid is None or not self.registry.resolve_gateway_clarify(cid, str(text)):
            return False
        await self.close_prompt(cid, f"Answered by message: {str(text)[:100]}")
        return True

    async def answer_choice(self, clarify_id, index, *, user_id=None, user=None):
        """Resolve prompt ``clarify_id`` with choice ``index``. Returns the label, else None.

        The platform-neutral half of a button press: allow check, label
        lookup and registry resolution. A concrete platform wraps this with
        its own acknowledgement (an interaction reply, a callback answer).
        """
        if user_id is not None and not self._is_allowed_user(str(user_id), user):
            return None
        row = self._prompts.get(str(clarify_id))
        labels = row["choices"] if row else (self.registry.choices(clarify_id) or [])
        if not 0 <= index < len(labels):
            return None
        if not self.registry.resolve_gateway_clarify(clarify_id, labels[index]):
            return None
        return labels[index]

    async def close_prompt(self, clarify_id, note):  # pragma: no cover - overridden per platform
        """Edit or close the service-side prompt message. Default: nothing to edit."""
        self._prompts.pop(str(clarify_id), None)
