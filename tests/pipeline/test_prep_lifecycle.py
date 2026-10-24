"""Lifecycle regression tests for Discord print progress and state transitions.

Verifies:
1. Receipt is valid JSON (no literal '\\n') at the START_REQUESTED transition and after completion.
2. Immediate preparation message sent after bed-clear approval, before start.
3. Message sequence and ordering: slice summary -> preparation message -> started confirmation.
4. Refused/failed start: slice summary -> preparation message -> precise failure message (no printing claim).
5. Repeated event deduplication: no duplicate start calls or duplicate messages.
6. Mapped multicolor flow consistency: same lifecycle, preparation message, and truthfulness.
7. Slice-only then button start flow lifecycle and ordering.
"""
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace as S

import pytest


from print_buddy.core import model_job  # noqa: E402
from print_buddy.core import prepared_intake  # noqa: E402
from .helpers import external  # noqa: E402
print_next = external("print_next")
from .test_color_intake import state  # noqa: E402
from .test_prepared_intake import Adapter, env, run_flow  # noqa: E402


PREP_TEXT = 'Preparing to start'


def test_single_color_successful_flow_messages_and_order(env):
    """After bed-clear approved, prep message is sent before start, followed by started confirmation."""
    adapter, result, receipt = run_flow(env, [1, 0, 0])  # single color, slot 1, slice and print now

    assert len(adapter.log) == 2, f'Expected prep and one start message: {adapter.log}'

    assert not any(m.startswith('Sliced:') for m in adapter.log)
    assert any(PREP_TEXT in m for m in adapter.log), f'Missing preparation message in: {adapter.log}'
    assert any(m.startswith('Print started:') for m in adapter.log), f'Missing started confirmation in: {adapter.log}'

    # No separate slice narration in the start flow.
    prep_idx = next(i for i, m in enumerate(adapter.log) if PREP_TEXT in m)
    start_idx = next(i for i, m in enumerate(adapter.log) if m.startswith('Print started:'))

    assert prep_idx < start_idx, f'Wrong order: prep={prep_idx}, start={start_idx}'

    # Verify receipt state
    assert receipt['status'] == 'STARTED'
    assert receipt['started'] is True
    assert len(env.starts) == 1


def test_receipt_parseable_at_start_requested_transition(env, monkeypatch):
    """Regression: receipt on disk must be valid JSON at the START_REQUESTED transition (no literal \\n)."""
    real_start = print_next.start
    transition_verified = []

    async def checked_start(target):
        # Inspect receipt right as print_next.start is entered
        receipt_files = list(env.root.glob('*.json'))
        assert len(receipt_files) == 1, f'Expected 1 receipt file, found {len(receipt_files)}'
        rp = receipt_files[0]
        raw = rp.read_text()
        assert not raw.endswith('\\\\n'), 'Receipt ends with literal \\\\n instead of newline'
        assert not raw.endswith('\\n'), 'Receipt ends with literal \\n characters instead of newline character'
        assert raw.endswith('\n'), 'Receipt does not end with newline character'
        data = json.loads(raw)  # MUST parse without JSONDecodeError
        assert data.get('status') == 'START_REQUESTED'
        assert data.get('bed_clear_approval') is not None
        assert data['bed_clear_approval']['via'] == 'button'
        transition_verified.append(True)
        return await real_start(target)

    monkeypatch.setattr(print_next, 'start', checked_start)

    adapter, result, receipt = run_flow(env, [1, 0, 0])
    assert len(transition_verified) == 1, 'Transition check did not run'
    assert receipt['status'] == 'STARTED'

    # Final receipt on disk must also be valid JSON
    final_file = next(env.root.glob('*.json'))
    final_data = json.loads(final_file.read_text())
    assert final_data['status'] == 'STARTED'


def test_refused_start_sends_prep_then_precise_failure_no_printing_claim(env, monkeypatch):
    """When start is refused, send prep message followed by precise failure; do not claim printing."""
    refusal_reason = 'REFUSED: fresh printer check failed: printer is not idle (busy, active.gcode, Printing). Nothing uploaded or started.'

    async def refused_start(target):
        return False, refusal_reason

    monkeypatch.setattr(print_next, 'start', refused_start)

    adapter, result, receipt = run_flow(env, [1, 0, 0])

    assert len(adapter.log) == 2, f'Expected prep and one refusal message: {adapter.log}'

    prep_idx = next(i for i, m in enumerate(adapter.log) if PREP_TEXT in m)

    assert not any(m.startswith('Sliced:') for m in adapter.log)
    assert not any('print started' in m.lower() for m in adapter.log)
    assert adapter.log[1].startswith('REFUSED:')

    # Receipt reflects refusal
    assert receipt['status'] == 'START_REFUSED'
    assert receipt['started'] is False

    # Receipt is valid JSON
    receipt_file = next(env.root.glob('*.json'))
    data = json.loads(receipt_file.read_text())
    assert data['status'] == 'START_REFUSED'


def test_duplicate_event_deduplication_and_no_duplicate_messages(env):
    """A duplicate model-link event does not trigger a second start or duplicate Discord messages."""
    adapter1, result1, receipt1 = run_flow(env, [1, 0, 0], mid='dedup-1')
    assert len(env.starts) == 1
    initial_sent_count = len(adapter1.log)
    assert initial_sent_count == 2

    # Send identical event with a new adapter
    adapter2 = Adapter([])
    event2 = S(
        source=S(profile='3dprinting', chat_id='c1', thread_id=None, user_id='u'),
        message_id='dedup-1',
        text='https://www.printables.com/model/1-ghost',
    )
    result2 = asyncio.run(prepared_intake.prepare_link(event2, adapter2, state(), env.root, timeout=1))

    assert result2['action'] == 'skip'
    assert result2['reason'] == 'duplicate model-link event'
    # No new start was initiated
    assert len(env.starts) == 1
    # No messages sent on the duplicate attempt
    assert len(adapter2.sent) == 0


def test_multicolor_mapped_flow_lifecycle_and_messages(env, monkeypatch):
    """Mapped multicolor flow follows the exact same lifecycle: slice -> prep -> started."""
    transition_verified = []
    real_start = print_next.start

    async def checked_start(target):
        rp = next(env.root.glob('*.json'))
        raw = rp.read_text()
        assert not raw.endswith('\\\\n') and not raw.endswith('\\n')
        assert raw.endswith('\n')
        data = json.loads(raw)
        assert data.get('status') == 'START_REQUESTED'
        transition_verified.append(True)
        return await real_start(target)

    monkeypatch.setattr(print_next, 'start', checked_start)

    # picks:
    # 0: Choose colors for these parts (per_part multicolor)
    # 0: Shell -> Slot 1 (white PLA)
    # 1: Eyes -> Slot 3 (black PLA)
    # 0: Slice and print now (bed is clear)
    adapter, result, receipt = run_flow(env, [0, 0, 1, 0])

    assert len(transition_verified) == 1
    assert receipt['color_decisions']['mode'] == 'per_part'
    assert receipt['status'] == 'STARTED'
    assert receipt['started'] is True
    assert len(env.starts) == 1

    # Messages check
    has_prep = any(PREP_TEXT in m for m in adapter.log)
    has_started = any(m.startswith('Print started:') for m in adapter.log)

    assert has_prep, f'Missing preparation message in: {adapter.log}'
    assert has_started, f'Missing started confirmation in: {adapter.log}'

    assert not any('Sliced:' in m for m in adapter.log)
    prep_idx = next(i for i, m in enumerate(adapter.log) if PREP_TEXT in m)
    start_idx = next(i for i, m in enumerate(adapter.log) if m.startswith('Print started:'))
    assert prep_idx < start_idx


def test_multicolor_refused_start_sends_prep_then_precise_failure(env, monkeypatch):
    """Multicolor flow with start refusal sends prep then precise failure without claiming start."""
    refusal_reason = 'REFUSED: the next plate G-code changed after the prompt. Nothing uploaded or started.'

    async def refused_start(target):
        return False, refusal_reason

    monkeypatch.setattr(print_next, 'start', refused_start)

    # picks: per_part (0), Shell->slot 1 (0), Eyes->slot 3 (1), Slice and print now (0)
    adapter, result, receipt = run_flow(env, [0, 0, 1, 0])

    prep_idx = next(i for i, m in enumerate(adapter.log) if PREP_TEXT in m)
    fail_idx = next(i for i, m in enumerate(adapter.log) if 'REFUSED:' in m)
    assert prep_idx < fail_idx
    assert not any('Print started:' in m for m in adapter.log)
    assert receipt['status'] == 'START_REFUSED'
    assert receipt['started'] is False


def test_slice_only_then_start_button_lifecycle(env, monkeypatch):
    """Slice only, then explicit Start button confirmation sends prep before start."""
    # picks:
    # 1: One color for everything
    # 0: Slot 1
    # 1: Slice only
    # 0: Start this exact plate? -> Start next plate · bed is clear
    adapter, result, receipt = run_flow(env, [1, 0, 1, 0])

    assert receipt['status'] == 'STARTED'
    assert receipt['started'] is True
    assert len(env.starts) == 1

    # The noisy 'Sliced:' post was retired; the Start button question is the only pre-start prompt.
    assert not any(m.startswith('Sliced:') for m in adapter.log)
    # 'Preparing to start' rides on the single Stop/Uploading prompt, not a separate post.
    assert sum(PREP_TEXT in m for m in adapter.log) == 1
    assert not any(m.startswith(PREP_TEXT) and 'Uploading' not in m for m in adapter.log)
    assert any(m.startswith('Print started:') for m in adapter.log)
