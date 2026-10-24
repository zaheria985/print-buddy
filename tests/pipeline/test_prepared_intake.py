"""A: one-click 'slice and print (bed is clear)' through the real prepare_link flow.

Buttons are answered via the real clarify gateway. Slicing is stubbed (offline, no Orca); the start goes
through the real print_next.start gates with a stub print-now executable and a temp claim dir.
"""
import asyncio
import json
import stat
import sys
from functools import partial
from pathlib import Path
from types import SimpleNamespace as S

import pytest

from print_buddy.core import model_job  # noqa: E402
from print_buddy.core import prepared_intake  # noqa: E402
from print_buddy.core.background_slice import slice_binding  # noqa: E402
from print_buddy.core.slice_recipe import binding_sha256  # noqa: E402
from .helpers import external  # noqa: E402
print_next = external("print_next")
from .test_color_intake import source, state  # noqa: E402


class Adapter:
    def __init__(self, picks):
        self.picks = iter(picks)
        self.questions = []
        self.sent = []

    async def send_clarify(self, **kw):
        self.questions.append(kw)
        if getattr(self, 'expire_once', False):
            self.expire_once = False
            return S(success=True)
        answer = kw['choices'][next(self.picks)]
        import tools.clarify_gateway as cm
        cm.resolve_gateway_clarify(kw['clarify_id'], answer)
        return S(success=True)

    # Exactly the production DiscordAdapter.send signature: a stray text= raises TypeError.
    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append(content)
        return S(success=True)


class _Kept:
    """Slice paths that are still on disk. A discarded early slice drops out of the list."""

    def __init__(self, raw):
        self.raw = raw

    def _live(self):
        return [p for p in self.raw if Path(p).exists()]

    def __len__(self):
        return len(self._live())

    def __getitem__(self, item):
        return self._live()[item]

    def __iter__(self):
        return iter(self._live())

    def __eq__(self, other):
        return self._live() == other

    def __bool__(self):
        return bool(self._live())

    def append(self, item):
        self.raw.append(item)

    def extend(self, items):
        self.raw.extend(items)


@pytest.fixture
def env(tmp_path, monkeypatch):
    src = source(tmp_path)
    folder_root = tmp_path / 'archive'
    monkeypatch.setattr(model_job, 'ARCHIVE', folder_root)
    monkeypatch.setattr(model_job, 'printables_inventory', lambda url: {
        'site': 'printables', 'slug': 'ghost', 'title': 'Ghost Stand',
        'sources': [{'name': 'ghost.3mf', 'instance_id': 1}]})
    monkeypatch.setattr(model_job, 'printables_download', lambda job, chosen, dest: _copy(src, dest))
    monkeypatch.setattr(model_job, 'audit', lambda path: {'plates': 1})
    # This fixture's synthetic 3MF intentionally lacks triangles; these intake tests
    # exercise approvals/lifecycle, so model a separately verified one-copy fit.
    from print_buddy.core import orca_adapter
    monkeypatch.setattr(orca_adapter, 'orca_plate_capacity', lambda path, st: 1)
    sliced = []
    kept = _Kept(sliced)
    recipes = []

    def fake_slice(job_path, recipe_sha256=None):
        recipes.append(recipe_sha256)
        gcode = Path(job_path).parent / 'sliced' / 'plate_1.gcode'
        gcode.parent.mkdir(parents=True, exist_ok=True)
        gcode.write_text('; estimated printing time (normal mode) = 1h 0m 0s\nG1 X1\n')
        data = json.loads(Path(job_path).read_text())
        data['derived_plates'] = [{'gcode': str(gcode), 'plate_id': 1, 'slot': 1}]
        model_job.write_job(Path(job_path).parent, data)
        try:
            return [gcode]
        finally:
            if gcode.exists():  # a discarded early slice removes it before this returns
                sliced.append(str(gcode))
    monkeypatch.setattr(model_job, 'slice_job', fake_slice)
    stub = tmp_path / 'print-now-stub'
    stub.write_text('#!/bin/sh\necho "started $2"\n')
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    starts = []
    real = print_next.start

    async def start(target):
        starts.append(target)
        return await real(target, model_job=stub, idle_check=lambda: None, timeout=1,
                          claims_dir=tmp_path / 'claims')
    monkeypatch.setattr(print_next, 'start', start)
    monkeypatch.setattr(prepared_intake.print_summary, 'find_job',
                        lambda filename, *args: (json.loads(Path(filename).parents[1].joinpath('JOB.json').read_text()),
                                          {'gcode': str(Path(filename)), 'plate': 1, 'parts': ['Ghost Stand'],
                                           'slot': 1, 'color': 'white', 'material': 'PLA',
                                           'filament_g': None, 'seconds': 3600}))
    return S(root=tmp_path / 'receipts', sliced=kept, starts=starts, tmp=tmp_path, recipes=recipes)


def _copy(src, dest):
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    out = dest / 'ghost.3mf'
    out.write_bytes(Path(src).read_bytes())
    return out


def run_flow(env, picks, mid='m1'):
    event = S(source=S(profile='3dprinting', chat_id='c1', thread_id=None, user_id='u'), message_id=mid,
              text='https://www.printables.com/model/1-ghost')
    adapter = Adapter(picks)
    result = asyncio.run(prepared_intake.prepare_link(event, adapter, state(), env.root, timeout=1))
    receipt = json.loads(next(env.root.glob('*.json')).read_text())
    return adapter, result, receipt


def final_choices(adapter):
    return adapter.questions[-1]['choices'] if adapter.questions[-1]['choices'][0].startswith('Slice') else None


def test_fit_choice_rows_are_four_or_fewer_and_never_offer_preview():
    rows = prepared_intake.fit_choice_rows({'scales': [0.98, 0.75, 0.5], 'rotations': [],
                                            'full_size_split': True})
    assert [row['label'] for row in rows] == ['Split plates at 100%', 'Shrink to 98%', 'Cancel']
    assert len(rows) <= 4
    packed = prepared_intake.fit_choice_rows({'split_plan': {'plates': [['a'], ['b']]},
                                              'scales': [0.98], 'rotations': []})
    assert packed[0]['label'] == '100% · split 2 plates' and packed[0]['plates'] == [['a'], ['b']]
    assert prepared_intake._fit_choice_reply('split', packed) == 0
    assert len(packed) <= 4
    assert prepared_intake._fit_choice_reply('100%', rows) == 0
    assert prepared_intake._fit_choice_reply('split', rows) == 0
    assert prepared_intake._fit_choice_reply('98%', rows) == 1
    assert prepared_intake._fit_choice_reply('98', rows) == 1
    assert prepared_intake._fit_choice_reply('cancel', rows) == 2
    with pytest.raises(ValueError, match='not available'):
        prepared_intake._fit_choice_reply('100%', rows[1:])


def test_one_click_slice_and_print_needs_no_typed_approval(env):
    # picks: one color for everything, slot 1, then choice 1 "Slice and print ... (bed is clear)"
    adapter, result, receipt = run_flow(env, [1, 0, 0])
    last = adapter.questions[-1]['choices']
    assert last[0].startswith('Slice and print') and 'bed is clear' in last[0]
    assert last[1] == 'Slice only' and last[2] == 'Stop with colors saved' and last[3] == 'Cancel print request'
    assert 'does not authorize printing' not in adapter.questions[-1]['question']
    assert len(env.sliced) == 1 and len(env.starts) == 1
    assert receipt['status'] == 'STARTED' and receipt['started'] is True
    approval = receipt['bed_clear_approval']
    assert approval['via'] == 'button' and approval['gcode'] == env.sliced[0]
    assert approval['gcode_sha256'] == env.starts[0]['gcode_sha256']
    assert approval['job_json'].endswith('JOB.json')
    assert not any('Approved' in q['question'] for q in adapter.questions)
    # The confirmed recipe is the one the worker was asked to slice, and it is the job's current recipe.
    job = json.loads(Path(approval['job_json']).read_text())
    current = binding_sha256(slice_binding(job, model_job.file_sha256(job['downloaded_file'])))
    assert env.recipes == [current]
    assert receipt['slice_approval']['recipe_version'] == 1
    assert receipt['slice_approval']['recipe_sha256'] == current


def test_slice_only_posts_start_button_bound_to_exact_plate(env):
    adapter, result, receipt = run_flow(env, [1, 0, 1, 0])  # slice only, then START button
    assert adapter.questions[-1]['choices'] == [print_next.START_LABEL, print_next.CANCEL_LABEL]
    assert len(env.starts) == 1 and receipt['status'] == 'STARTED'


def test_slice_only_then_cancel_never_starts(env):
    adapter, result, receipt = run_flow(env, [1, 0, 1, 1])
    assert env.starts == [] and receipt['status'] == 'SLICED_NOT_STARTED' and receipt['started'] is False


def test_stop_during_live_start_reaps_child_and_reports_uncertain_outcome(env, monkeypatch, tmp_path):
    # The pipeline conftest provides the in-memory clarify gateway stand-in.
    started = tmp_path / 'print_now_started'
    late = tmp_path / 'print_now_late'
    stub = env.tmp / 'print-now-stub'
    stub.write_text(
        f'#!{sys.executable}\n'
        'import signal, time\n'
        'from pathlib import Path\n'
        'signal.signal(signal.SIGTERM, signal.SIG_IGN)\n'
        f'Path({str(started)!r}).write_text("running")\n'
        'time.sleep(30)\n'
        f'Path({str(late)!r}).write_text("continued")\n'
    )
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(print_next, 'STOP_GRACE_S', 0.1)
    real_spawn = asyncio.create_subprocess_exec
    spawned = []

    async def capture_spawn(*args, **kwargs):
        proc = await real_spawn(*args, **kwargs)
        if args and args[0] == str(stub):
            spawned.append(proc)
        return proc
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', capture_spawn)

    class InteractiveAdapter(Adapter):
        def __init__(self, answers):
            self.answers = iter(answers)
            self.questions = []
            self.sent = []

        async def send_clarify(self, **kw):
            self.questions.append(kw)
            answer = kw['choices'][next(self.answers)]
            import tools.clarify_gateway as cm
            cm.resolve_gateway_clarify(kw['clarify_id'], answer)
            return S(success=True)

    class StopMessage:
        async def edit(self, **kwargs):
            return None

    press_tasks = []

    async def stop_sender(_source, _text, clarify_id):
        import tools.clarify_gateway as cm

        async def press_after_spawn():
            for _ in range(500):
                if started.exists():
                    cm.resolve_gateway_clarify(clarify_id, prepared_intake.STOP_LABEL)
                    return
                await asyncio.sleep(0.01)
            raise AssertionError('print-now subprocess did not start')

        press_tasks.append(asyncio.create_task(press_after_spawn()))
        return StopMessage()

    monkeypatch.setattr(prepared_intake, 'STOP_SENDER', stop_sender)
    event = S(source=S(profile='3dprinting', chat_id='c1', thread_id=None, user_id='u'),
              message_id='stop-live-start', text='https://www.printables.com/model/1-ghost')
    adapter = InteractiveAdapter([1, 0, 0])

    async def run():
        result = await prepared_intake.prepare_link(event, adapter, state(), env.root, timeout=3)
        await asyncio.gather(*press_tasks)
        await asyncio.sleep(0.1)
        return result

    result = asyncio.run(run())
    assert result['reason'] == 'start outcome uncertain after cancellation'
    assert started.exists() and not late.exists()
    assert len(spawned) == 1 and spawned[0].returncode is not None
    receipt = json.loads(next(env.root.glob('*.json')).read_text())
    assert receipt['status'] == 'START_UNCERTAIN'
    assert receipt['sliced'] is True and receipt['uploaded'] is None and receipt['started'] is None
    assert len(list((env.tmp / 'claims').glob('*.json'))) == 1
    claim = json.loads(next((env.tmp / 'claims').glob('*.json')).read_text())
    assert claim['outcome'] == 'uncertain_cancelled'
    assert any('UNCERTAIN:' in line and 'Not retrying' in line for line in adapter.sent)
    assert not any('Nothing sliced, uploaded or started.' in line for line in adapter.sent)


def test_stop_choice_discards_the_early_slice_and_never_starts(env):
    adapter, result, receipt = run_flow(env, [1, 0, 2])
    # Issue 63 starts the slice while the confirm question is up. Stop throws it away.
    assert env.starts == [] and receipt['status'] == 'COLORS_SAVED_STOPPED'
    assert not any(env.root.parent.glob('archive/*/sliced/*.gcode'))


def test_expired_question_offers_one_resume_and_keeps_the_saved_intake(env):
    # Expire the first question, then continue: mode, two part colors, stop.
    adapter = Adapter([0, 0, 0, 0, 2])
    adapter.expire_once = True
    event = S(source=S(profile='3dprinting', chat_id='c1', thread_id=None, user_id='u'), message_id='m1',
              text='https://www.printables.com/model/1-ghost')
    result = asyncio.run(prepared_intake.prepare_link(event, adapter, state(), env.root, timeout=1))
    questions = adapter.questions
    resume = next(q for q in questions if 'expired' in q['question'].lower())
    assert resume['choices'] == ['Resume', 'Cancel']
    assert any('color' in q['question'].lower() for q in questions)
    assert result['action'] == 'skip'
    assert env.sliced == [] and env.starts == []
    assert any('Nothing was sliced' in line for line in adapter.sent)


def test_resume_continues_the_saved_intake_instead_of_stopping_at_the_marker(env):
    # First color question expires. Resume must ask the next saved question
    # (the color choice), not stop after the action marker.
    # Picks: Resume, per-part mode, two part colors, then stop with colors saved.
    adapter = Adapter([0, 0, 0, 0, 2])
    adapter.expire_once = True
    event = S(source=S(profile='3dprinting', chat_id='c1', thread_id=None, user_id='u'), message_id='m1',
              text='https://www.printables.com/model/1-ghost')
    result = asyncio.run(prepared_intake.prepare_link(event, adapter, state(), env.root, timeout=1))
    questions = [q['question'] for q in adapter.questions]
    assert any('expired' in q.lower() for q in questions)
    assert any('color' in q.lower() for q in questions), questions
    assert result['action'] == 'skip'
    assert env.starts == [] and env.sliced == []


def test_changed_gcode_after_slice_is_refused_by_existing_gate(env, monkeypatch):
    real_slice = model_job.slice_job

    def tamper(job_path, **kwargs):
        out = real_slice(job_path, **kwargs)
        starts_target = Path(out[0])
        # change the bytes after the sha is taken by prepare_link? simulate via start wrapper below
        return out
    monkeypatch.setattr(model_job, 'slice_job', tamper)
    original = print_next.start

    async def tampered(target):
        Path(target['gcode']).write_text('; tampered\n')
        return await original(target)
    monkeypatch.setattr(print_next, 'start', tampered)
    adapter, result, receipt = run_flow(env, [1, 0, 0])
    assert receipt['status'] == 'START_REFUSED' and receipt['started'] is False
    assert any('REFUSED' in m for m in adapter.sent)


def test_failed_slice_retry_needs_fresh_approval_and_start_button(env, monkeypatch):
    real_slice = model_job.slice_job
    calls = []

    def fail_once(job_path, *args, **kwargs):
        calls.append(str(job_path))
        if len(calls) == 1:
            raise RuntimeError('synthetic slice failure')
        return real_slice(job_path, *args, **kwargs)

    monkeypatch.setattr(model_job, 'slice_job', fail_once)
    adapter, result, receipt = run_flow(env, [1, 0, 0, 0, 0], mid='retry-approved')

    retry = [q for q in adapter.questions if q['choices'][0] == 'Retry slice once (no print)']
    starts = [q for q in adapter.questions if q['choices'] == [print_next.START_LABEL, print_next.CANCEL_LABEL]]
    assert len(calls) == 2 and len(retry) == 1 and len(starts) == 1
    assert receipt['slice_attempt'] == receipt['slice_approval']['attempt'] == 2
    assert receipt['status'] == 'STARTED' and receipt['started'] is True
    assert len(env.starts) == 1
    assert receipt['bed_clear_approval']['gcode'] == env.starts[0]['gcode']


def test_cancelled_slice_retry_cannot_be_replayed(env, monkeypatch):
    calls = []

    def always_fail(job_path, *args, **kwargs):
        calls.append(str(job_path))
        raise RuntimeError('synthetic slice failure')

    monkeypatch.setattr(model_job, 'slice_job', always_fail)
    adapter, _result, receipt = run_flow(env, [1, 0, 0, 1], mid='retry-cancelled')
    assert len(calls) == 1 and env.starts == []
    assert receipt['status'] == 'CANCELLED' and receipt['sliced'] is False
    assert 'slice_approval' not in receipt

    event = S(source=S(profile='3dprinting', chat_id='c1', thread_id=None, user_id='u'),
              message_id='retry-cancelled', text='https://www.printables.com/model/1-ghost')
    result = asyncio.run(prepared_intake.prepare_link(event, Adapter([]), state(), env.root, timeout=1))
    assert result['reason'] == 'duplicate model-link event'
    assert len(calls) == 1 and env.starts == []


REAL_AWAITING = ('START REQUEST ACCEPTED {"file": "ghost_plate_1_abc.gcode", "printing_confirmed": false}\n'
                 'START SUBMITTED; AWAITING CONFIRMATION {"started": false, "accepted": true, "phase": "preparing", '
                 '"file": "ghost_plate_1_abc.gcode", "message": "submitted, awaiting confirmation"}')


def test_intake_recognizes_real_print_now_output():
    assert print_next.still_preparing(REAL_AWAITING)
    assert print_next.still_preparing('START ACCEPTED; PREPARATION STILL RUNNING {"started": false}')
    assert not print_next.still_preparing('START OK {"started": true}')
    assert print_next.started_file(REAL_AWAITING) == 'ghost_plate_1_abc.gcode'


def test_start_still_preparing_posts_one_warming_up_start_and_records_its_id(env, monkeypatch, tmp_path):
    monkeypatch.setattr(print_next, 'START_MESSAGES', tmp_path / 'start_messages.json')

    async def preparing(target):
        env.starts.append(target)
        return True, REAL_AWAITING
    monkeypatch.setattr(print_next, 'start', preparing)

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append(content)
        return S(success=True, message_id=f'm{len(self.sent)}')
    monkeypatch.setattr(Adapter, 'send', send)
    adapter, result, receipt = run_flow(env, [1, 0, 0])
    assert receipt['status'] == 'STARTED' and len(env.starts) == 1
    starts = [m for m in adapter.sent if m.startswith('Print started')]
    assert len(starts) == 1 and 'Warming up' in starts[0]
    assert print_next.take_start_message('ghost_plate_1_abc.gcode') == f'm{len(adapter.sent)}'


def test_preparation_metadata_mismatch_never_blocks_start(env, monkeypatch):
    """A Discord summary defect must not veto the exact already-approved print."""
    async def preparing(target):
        env.starts.append(target)
        return True, REAL_AWAITING
    monkeypatch.setattr(print_next, 'start', preparing)
    monkeypatch.setattr(prepared_intake.print_summary, 'format_preparation',
                        lambda *_args, **_kw: 'Print preparing: stale metadata unavailable')
    adapter, _result, receipt = run_flow(env, [1, 0, 0])
    assert receipt['status'] == 'STARTED' and len(env.starts) == 1
    assert any(msg.startswith('Preparing to start') and '#unknown' not in msg for msg in adapter.sent)


def test_duplicate_event_does_not_start_twice(env):
    run_flow(env, [1, 0, 0], mid='dup')
    adapter = Adapter([])
    event = S(source=S(profile='3dprinting', chat_id='c1', thread_id=None, user_id='u'), message_id='dup',
              text='https://www.printables.com/model/1-ghost')
    result = asyncio.run(prepared_intake.prepare_link(event, adapter, state(), env.root, timeout=1))
    assert result['reason'] == 'duplicate model-link event' and len(env.starts) == 1


def test_message_identity_is_a_sha256_not_the_raw_discord_snowflake(env, monkeypatch):
    """A Discord snowflake stays event metadata, never a --intake-id value."""
    mid = '1000000000000000001'
    expected = model_job.intake_id_from_message(
        S(profile='3dprinting', chat_id='c1', thread_id=None), mid)
    assert len(expected) == 64 and expected == expected.lower()
    assert expected != mid
    monkeypatch.setattr(model_job, 'printables_inventory', lambda url: {
        'site': 'printables', 'slug': 'identity', 'title': 'Identity',
        'sources': [{'name': 'identity.3mf', 'instance_id': 1}]})
    event = S(source=S(profile='3dprinting', chat_id='c1', thread_id=None, user_id='u'),
              message_id=mid, text='https://www.printables.com/model/1-identity')
    result = asyncio.run(prepared_intake.prepare_link(event, Adapter([1, 0, 1]), state(), env.root, timeout=1))
    assert result['action'] == 'skip'
    receipt = json.loads((env.root / f'{expected}.json').read_text())
    assert receipt['intake_id'] == expected
    assert receipt['intake_id'] != mid


# ---- Phase 1.7: concise AD5X preview before color selection (offline, display only) ----
AD5X_ROW = {'name': 'ghost.3mf', 'instance_id': 1, 'site_printer': 'Flashforge Adventurer 5X', 'plates': [
    {'name': 'p1', 'prediction_s': 3600, 'weight_g': 12.4}, {'name': 'p2', 'prediction_s': 1800, 'weight_g': 7.2}]}


def run_preview(env, monkeypatch, summary, row, picks=(2,), send_fails=False):
    monkeypatch.setattr(model_job, 'printables_inventory', lambda url: {
        'site': 'printables', 'slug': 'ghost', 'title': 'Ghost Stand', 'summary': summary, 'sources': [row]})
    log = []

    class Ordered(Adapter):
        async def send_clarify(self, **kw):
            log.append(('ask', kw['question']))
            return await super().send_clarify(**kw)

        async def send(self, chat_id, content, reply_to=None, metadata=None):
            log.append(('send', content))
            if send_fails and content.startswith('That looks like'):
                raise RuntimeError('discord down')
            return await super().send(chat_id, content, reply_to, metadata)
    event = S(source=S(profile='3dprinting', chat_id='c1', thread_id=None, user_id='u'), message_id='pv1',
              text='https://www.printables.com/model/1-ghost')
    adapter = Ordered([1, 0, *picks])
    asyncio.run(prepared_intake.prepare_link(event, adapter, state(), env.root, timeout=1))
    return log, adapter


def test_intake_spool_projection_uses_total_once_for_a_single_color_job():
    job = {'color_decisions': {'mode': 'single_color', 'selection':
           {'slot': 1, 'color': 'white', 'material': 'PLA', 'spoolman_id': 10}}}
    manifest = {'filament_stats': {'total_g': 18.5, 'extruders': {1: {'g': 11}, 2: {'g': 7.5}}}}
    assert model_job.intake_spool_projection(job, {10: 250}, manifest) == [{
        'slot': 1, 'color': 'white', 'material': 'PLA', 'spoolman_id': 10,
        'estimated_used_g': 18.5, 'remaining_g': 250.0, 'projected_remaining_g': 231.5}]


def test_intake_spool_projection_groups_multicolor_choices_by_spool():
    white = {'slot': 1, 'color': 'white', 'material': 'PLA', 'spoolman_id': 10}
    black = {'slot': 3, 'color': 'black', 'material': 'PLA', 'spoolman_id': 12}
    job = {'color_decisions': {'mode': 'per_part', 'choices': {
        'part:1': white, 'filament:1': white, 'filament:2': black}}}
    manifest = {'filament_stats': {'total_g': 13, 'extruders': {1: {'g': 5}, 2: {'g': 8}}}}
    assert model_job.intake_spool_projection(job, {10: 50, 12: 30}, manifest) == [
        {'slot': 1, 'color': 'white', 'material': 'PLA', 'spoolman_id': 10,
         'estimated_used_g': 5.0, 'remaining_g': 50.0, 'projected_remaining_g': 45.0},
        {'slot': 3, 'color': 'black', 'material': 'PLA', 'spoolman_id': 12,
         'estimated_used_g': 8.0, 'remaining_g': 30.0, 'projected_remaining_g': 22.0}]


def test_intake_spool_projection_scales_by_copies_and_flags_shortfall():
    job = {'color_decisions': {'mode': 'single_color', 'selection':
           {'slot': 1, 'color': 'white', 'material': 'PLA', 'spoolman_id': 10}}}
    manifest = {'filament_stats': {'total_g': 40}}
    rows = model_job.intake_spool_projection(job, {10: 100}, manifest, copies=3)
    assert rows[0]['estimated_used_g'] == 120.0 and rows[0]['projected_remaining_g'] == -20.0
    assert model_job.format_spool_projection(rows) == (
        'Slot 1 white PLA: needs about 120 g; spool has 100 g, short by about 20 g.')


def test_intake_spool_weights_prefers_spoolman_then_slot_record_never_guesses():
    st = {'ifs_slots': {'1': {'spoolman_id': 10, 'remaining_grams': 500},
                        '3': {'spoolman_id': 12}}}
    assert model_job.intake_spool_weights(st, [10, 12]) == {10: 500.0}
    assert model_job.intake_spool_weights(st, [10, 12], lambda sid: {'remaining_weight': 321.5}) == {
        10: 321.5, 12: 321.5}

    def down(sid):
        raise OSError('spoolman down')
    assert model_job.intake_spool_weights(st, [10, 12], down) == {10: 500.0}
    rows = model_job.intake_spool_projection(
        {'color_decisions': {'mode': 'single_color', 'selection': {'slot': 3, 'color': 'black',
                                                                    'material': 'PLA', 'spoolman_id': 12}}},
        {}, {'filament_stats': {'total_g': 9}})
    assert model_job.format_spool_projection(rows) == 'Slot 3 black PLA: needs about 9 g; spool weight unknown.'


def test_color_save_prompt_shows_spool_projection_and_estimate_before_print_choice(env, monkeypatch):
    from print_buddy.core import source_colors
    real = source_colors.inventory
    monkeypatch.setattr(source_colors, 'inventory',
                        lambda path: {**real(path), 'filament_stats': {'total_g': 25, 'total_m': 8, 'extruders': {}}})
    st = state()
    st['ifs_slots']['1']['remaining_grams'] = 200
    adapter = Adapter([1, 0, 2])  # one color, slot 1, stop with colors saved
    event = S(source=S(profile='3dprinting', chat_id='c1', thread_id=None, user_id='u'), message_id='spool1',
              text='https://www.printables.com/model/1-ghost')
    asyncio.run(prepared_intake.prepare_link(event, adapter, st, env.root, timeout=1))
    saved = [q['question'] for q in adapter.questions if q['choices'] and q['choices'][0].startswith('Slice and print')]
    assert len(saved) == 1
    assert saved[0].startswith('White PLA · ')
    assert 'Slot 1 white PLA: needs about 25 g; spool has 200 g, about 175 g left after.' in saved[0]
    assert 'AD5X' not in saved[0]
    assert env.starts == []


def test_preview_precedes_first_color_prompt_with_validated_estimate(env, monkeypatch):
    log, adapter = run_preview(env, monkeypatch, 'A sturdy stand for a ghost figure. Print upright. https://x.io/a.3mf', AD5X_ROW)
    assert 'estimated print time about 1h 30m' in log[0][1]
    assert '1h 35m total' in log[0][1]
    assert '20 g of filament before color selection.' in log[0][1]
    assert [k for k, _ in log].index('ask') > 0
    assert 'http' not in log[0][1] and '.3mf' not in log[0][1]
    assert sum(1 for k, t in log if k == 'send' and t.startswith('That looks like')) == 1
    assert env.sliced == [] and env.starts == []  # preview never slices, uploads or starts


@pytest.mark.parametrize('extra', [
    {}, {'site_printer': 'Bambu X1C'},
    {'plates': [{'prediction_s': 3600, 'weight_g': None}]},
    {'plates': [{'prediction_s': 'soon', 'weight_g': 5}]},
    {'plates': [{'prediction_s': 3600, 'weight_g': 5}, {'prediction_s': 60}]},
    {'plates': [{'prediction_s': True, 'weight_g': 5}]},
])
def test_preview_omits_estimate_when_incomplete_but_keeps_description(env, monkeypatch, extra):
    row = {**AD5X_ROW, **extra}
    if extra == {}:
        row = {'name': 'ghost.3mf', 'instance_id': 1}
    log, _ = run_preview(env, monkeypatch, 'A sturdy stand.', row)
    tail = ('Model page estimate (for Bambu X1C): about 1h 30m and 20 g of filament over 2 plates. '
            'The AD5X figure after slicing will differ somewhat.' if extra.get('site_printer')
            else 'The model page gives no time or filament estimate; I will report both after slicing.')
    assert log[0] == ('send', 'That looks like Ghost Stand, A sturdy stand. I will use the selected profile: ghost. ' + tail)


def test_preview_names_a_selected_profile_without_a_description(env, monkeypatch):
    row = {'name': 'ghost.3mf', 'title': 'Fine 0.16 mm profile', 'instance_id': 1}
    log, _ = run_preview(env, monkeypatch, '', row)
    assert log[0] == ('send', 'That looks like Ghost Stand. I will use the selected profile: Fine 0.16 mm profile. The model page gives no time or filament estimate; I will report both after slicing.')


def test_preview_hides_an_opaque_download_filename(env, monkeypatch):
    row = {'name': 'model_7f3c1a8e.3mf', 'instance_id': 1}
    log, _ = run_preview(env, monkeypatch, '', row)
    assert log[0][0] == 'ask'


def test_no_preview_when_nothing_to_say_and_color_flow_unchanged(env, monkeypatch):
    log, adapter = run_preview(env, monkeypatch, '', {'name': 'ghost.3mf', 'instance_id': 1})
    assert log[0][0] == 'ask' and not any(t.startswith('That looks like') for _, t in log)


def test_preview_delivery_failure_never_blocks_color_selection(env, monkeypatch):
    log, adapter = run_preview(env, monkeypatch, 'A sturdy stand.', AD5X_ROW, send_fails=True)
    assert [k for k, _ in log].count('ask') == 3 and env.starts == []


def test_preview_does_no_printer_or_network_io(env, monkeypatch):
    import socket
    import subprocess
    monkeypatch.setattr(socket, 'socket', lambda *a, **k: pytest.fail('network I/O'))
    monkeypatch.setattr(subprocess, 'run', lambda *a, **k: pytest.fail('subprocess'))
    monkeypatch.setattr(subprocess, 'Popen', lambda *a, **k: pytest.fail('subprocess'))
    assert prepared_intake.preview_sentence({'title': 'T', 'summary': 'Nice.'}, AD5X_ROW).startswith('That looks like T, Nice.')


@pytest.mark.parametrize('text', [
    'https://makerworld.com/en/models/3382409-miniature-baroque-frame#profileId-3848581 how long would this take?',
    'How many fit? https://makerworld.com/en/models/3382409-miniature-baroque-frame#profileId-3848581',
])
def test_model_question_link_is_still_recognized(text):
    from print_buddy.core.link_intake import model_link
    assert model_link(text) == 'https://makerworld.com/en/models/3382409-miniature-baroque-frame#profileId-3848581'


def test_linked_maker_profile_is_selected_automatically(env, monkeypatch):
    url = 'https://makerworld.com/en/models/3382409-miniature-baroque-frame#profileId-3848581'
    rows = [
        {'instance_id': 3848581, 'title': '0.2 mm layer'},
        {'instance_id': 3873478, 'title': 'Optimized parameters'},
    ]
    monkeypatch.setattr(model_job, 'maker_inventory', lambda _url: {
        'site': 'makerworld', 'slug': 'frame', 'title': 'Frame', 'sources': rows})
    selected = []

    def download(job, chosen, dest):
        selected.append(chosen['instance_id'])
        return _copy(env.tmp / 'source.3mf', dest)

    monkeypatch.setattr(model_job, 'maker_download', download)
    event = S(source=S(profile='3dprinting', chat_id='c1', thread_id=None, user_id='u'),
              message_id='profiles', text=url)
    # Contract (2026-10-12): the URL profileId is used directly; no profile button.
    # one color, slot 1, then stop. The early slice starts during the question and is discarded.
    adapter = Adapter([1, 0, 2])
    asyncio.run(prepared_intake.prepare_link(event, adapter, state(), env.root, timeout=1))
    assert not any('print profiles' in q['question'] for q in adapter.questions)
    assert selected == [3848581]
    assert env.starts == [] and not any(env.root.parent.glob('archive/*/sliced/*.gcode'))


def test_quantity_buttons_choose_midpoint_and_answer_duration(env, monkeypatch):
    from print_buddy.core import orca_adapter
    assert orca_adapter.quantity_buttons(0) == [1]
    assert orca_adapter.quantity_buttons(4) == [1, 2, 4]
    monkeypatch.setattr(orca_adapter, 'orca_plate_capacity', lambda path, st: 3)
    calls = []

    def fake_slice(job_path, copies=None, recipe_sha256=None):
        calls.append(copies)
        gcode = Path(job_path).parent / 'sliced' / 'plate_1.gcode'
        gcode.parent.mkdir(parents=True, exist_ok=True)
        gcode.write_text('; estimated printing time (normal mode) = 2h 11m 12s\nG1 X1\n')
        data = json.loads(Path(job_path).read_text())
        # The real single plate path: Orca clones the copies and records how many.
        data['gcode'] = str(gcode)
        data['sliced_copies'] = {'gcode': str(gcode), 'copies': copies or 1}
        model_job.write_job(Path(job_path).parent, data)
        return [gcode]

    monkeypatch.setattr(model_job, 'slice_job', fake_slice)
    monkeypatch.setattr(prepared_intake, '_preview_estimate', lambda _row: None)
    event = S(source=S(profile='3dprinting', chat_id='c1', thread_id=None, user_id='u'),
              message_id='duration', text='https://www.printables.com/model/1-ghost how long would this take?')
    # one color, slot 1, choose 2 copies, slice only, then decline start
    adapter = Adapter([1, 0, 1, 1, 1])
    asyncio.run(prepared_intake.prepare_link(event, adapter, state(), env.root, timeout=1))
    quantity = next(q for q in adapter.questions if 'copies' in q['question'].lower())
    assert quantity['choices'] == ['One copy', 'Two copies', 'Max (3 copies)', 'Cancel']
    assert not any(q['question'].startswith('Confirm: slice exactly 2 copies') for q in adapter.questions)
    final = next(q for q in adapter.questions if q['choices'] and q['choices'][0].startswith('Slice and print'))
    assert '2 copies' in final['question'] and final['choices'][0].startswith('Slice and print')
    assert calls == [2]
    assert any('2 copies' in text and '~2h 11m printing' in text and '+~5 min warmup' in text
               for text in adapter.sent)
    assert env.starts == []


def test_typed_quantity_uses_value_not_button_position(env, monkeypatch):
    from print_buddy.core import orca_adapter
    monkeypatch.setattr(orca_adapter, 'orca_plate_capacity', lambda path, st: 6)
    calls = []

    def fake_slice(job_path, copies=None, recipe_sha256=None):
        calls.append(copies)
        return []

    monkeypatch.setattr(model_job, 'slice_job', fake_slice)
    monkeypatch.setattr(prepared_intake, '_preview_estimate', lambda _row: None)
    event = S(source=S(profile='3dprinting', chat_id='c1', thread_id=None, user_id='u'),
              message_id='typed-quantity', text='https://www.printables.com/model/1-ghost')

    class TypedQuantity(Adapter):
        async def send_clarify(self, **kw):
            self.questions.append(kw)
            import tools.clarify_gateway as cm
            from print_buddy.core import link_intake
            if 'How many copies' in kw['question']:
                assert 'will not fit' in link_intake.typed_reply(event.source, '9')
                assert link_intake.typed_reply(event.source, '4') == ''
            else:
                cm.resolve_gateway_clarify(kw['clarify_id'], kw['choices'][next(self.picks)])
            return S(success=True)

    # one color, slot 1, (typed 4 copies), slice only, then decline start
    adapter = TypedQuantity([1, 0, 1, 1])
    asyncio.run(prepared_intake.prepare_link(event, adapter, state(), env.root, timeout=1))
    assert calls == [4]
    quantity = next(q for q in adapter.questions if 'How many copies' in q['question'])
    assert quantity['choices'] == ['One copy', 'Two copies', 'Max (6 copies)', 'Cancel']


def test_capacity_zero_full_size_split_saves_separate_mode_before_slice(env, monkeypatch):
    from print_buddy.core import orca_adapter
    from .test_plate_merge import _source
    path = env.tmp / 'two-large-parts.3mf'
    _source(path, {'1': (150, 150, 10), '2': (150, 150, 10)}, [['1', '2']])
    monkeypatch.setattr(model_job, 'printables_download', lambda job, chosen, dest: _copy(path, dest))
    monkeypatch.setattr(orca_adapter, 'orca_plate_capacity',
                        lambda path, st, scale=1.0, copy_limit=16, **kw: 0 if scale == 1.0 else 1)
    monkeypatch.setattr(orca_adapter, 'fit_options',
                        lambda path, st: {'scales': [0.98, 0.75, 0.5], 'rotations': []})

    def fake_split_slice(job_path, copies=None, recipe_sha256=None):
        job_path = Path(job_path)
        data = json.loads(job_path.read_text())
        gcodes, rows = [], []
        for index, keys in enumerate(data['full_size_split_plan']['plates'], 1):
            gcode = job_path.parent / 'sliced' / f'plate_{index}.gcode'
            gcode.parent.mkdir(parents=True, exist_ok=True)
            gcode.write_text(f'; plate {index} at 100%\\nG1 X1\\n')
            gcodes.append(gcode)
            rows.append({'gcode': str(gcode), 'plate_id': index, 'slot': 1, 'part_keys': keys})
        data['derived_plates'] = rows
        model_job.write_job(Path(job_path).parent, data)
        env.sliced.extend(map(str, gcodes))
        return gcodes

    monkeypatch.setattr(model_job, 'slice_job', fake_split_slice)
    event = S(source=S(profile='3dprinting', chat_id='c1', thread_id=None, user_id='u'),
              message_id='too-large', text='https://www.printables.com/model/1-ghost')
    adapter = Adapter([1, 0, 0, 1, 2])  # ..., slice only, then Cancel in the plate picker
    result = asyncio.run(prepared_intake.prepare_link(event, adapter, state(), env.root, timeout=1))
    scale_prompt = next(q for q in adapter.questions if 'does not fit the AD5X bed' in q['question'])
    assert scale_prompt['choices'][0] == '100% · split 2 plates'
    assert 'Part 1 on plate 1' in scale_prompt['question'] and 'Part 2 on plate 2' in scale_prompt['question']
    job_data = json.loads(next(env.root.parent.glob('archive/*/JOB.json')).read_text())
    assert job_data['scale'] == 1.0
    assert job_data['plate_mode'] == 'separate'
    assert job_data['full_size_split_plan']['plates'] == [['1:1'], ['2:1']]
    assert result['reason'] == 'operator did not start the sliced plate'
    assert len(env.sliced) == 2 and env.starts == []
    plan_id = job_data['multipart_plan']['plan_id']
    plan = print_next.load_plan(plan_id)
    assert [part['plate'] for part in plan['parts']] == [1, 2]
    assert [part['status'] for part in plan['parts']] == ['planned', 'planned']
    assert [part['part_keys'] for part in job_data['derived_plates']] == [['1:1'], ['2:1']]


def test_capacity_zero_offers_rotation_alternative(env, monkeypatch):
    from print_buddy.core import orca_adapter
    monkeypatch.setattr(orca_adapter, 'orca_plate_capacity',
                        lambda path, st, scale=1.0, copy_limit=16, **kw: 0 if scale == 1.0 and not kw.get('rotation') else 1)
    rot = {'axis': '+Y', 'name': 'Rotate 90° on X', 'scale': 1.0, 'dims_mm': [126.5, 209.8, 219.8],
           'dims_full_mm': [128.4, 213.0, 223.1], 'rotation': [1, 0, 0, 0, 0, -1, 0, 1, 0],
           'supports': True, 'brim': True, 'label': 'Rotate 90° on X, full size (needs supports + brim)'}
    monkeypatch.setattr(orca_adapter, 'fit_options',
                        lambda path, st: {'scales': [0.89, 0.75, 0.5], 'rotations': [rot]})
    event = S(source=S(profile='3dprinting', chat_id='c1', thread_id=None, user_id='u'),
              message_id='too-large-rot', text='https://www.printables.com/model/1-ghost')
    # One color, slot 1, pick choice 0 (the rotated alternative), slice only, decline start.
    adapter = Adapter([1, 0, 0, 1, 1])
    result = asyncio.run(prepared_intake.prepare_link(event, adapter, state(), env.root, timeout=1))
    scale_prompt = next(q for q in adapter.questions if 'does not fit the AD5X bed' in q['question'])
    assert scale_prompt['choices'][0] == '100% + supports'
    assert 'needs supports' in scale_prompt['question']
    job_data = json.loads(next(env.root.parent.glob('archive/*/JOB.json')).read_text())
    assert job_data.get('scale', 1.0) == 1.0
    assert job_data['orientation']['axis'] == '+Y'
    assert job_data['orientation']['brim'] is True
    assert result['reason'] == 'operator did not start the sliced plate'


def test_capacity_zero_cancel_stops_without_slice(env, monkeypatch):
    from print_buddy.core import orca_adapter
    monkeypatch.setattr(orca_adapter, 'orca_plate_capacity', lambda path, st, **kw: 0)
    monkeypatch.setattr(orca_adapter, 'fit_options',
                        lambda path, st: {'scales': [0.98, 0.75, 0.5], 'rotations': []})
    event = S(source=S(profile='3dprinting', chat_id='c1', thread_id=None, user_id='u'),
              message_id='too-large-cancel', text='https://www.printables.com/model/1-ghost')
    adapter = Adapter([1, 0, 1])  # shrink, slot, then Cancel
    result = asyncio.run(prepared_intake.prepare_link(event, adapter, state(), env.root, timeout=1))
    assert result['reason'] == 'cancelled oversized model'
    assert any('Print cancelled' in text for text in adapter.sent)
    assert env.sliced == [] and env.starts == []


def _per_part_oversized(env, monkeypatch, honored, answers):
    from print_buddy.core import orca_adapter
    monkeypatch.setattr(orca_adapter, 'orca_plate_capacity', lambda path, st, **kw: 0 if not kw else 1)
    monkeypatch.setattr(orca_adapter, 'fit_options',
                        lambda path, st: {'scales': [0.882, 0.5], 'rotations': [
                            {'label': 'Rotate 90° on Y, 94.4%', 'scale': 0.944, 'axis': '+Y'}]})
    seen = []

    def per_part(path, decisions, options, bed):
        seen.append((decisions['mode'], bed, options))
        return {**options, 'scales': [s for s in options['scales'] if s in honored], 'rotations': []}
    monkeypatch.setattr(model_job, 'per_part_fit_options', per_part)
    event = S(source=S(profile='3dprinting', chat_id='c1', thread_id=None, user_id='u'),
              message_id='per-part-too-large', text='https://www.printables.com/model/1-ghost')
    adapter = Adapter(answers)
    result = asyncio.run(prepared_intake.prepare_link(event, adapter, state(), env.root, timeout=1))
    return adapter, result, seen


def test_per_part_offers_only_fits_every_slot_plate_honors(env, monkeypatch):
    # Per-part mode, both parts on the first slot, then pick the first fit row.
    adapter, result, seen = _per_part_oversized(env, monkeypatch, {0.5}, [0, 0, 0, 0])
    assert seen and seen[0][0] == 'per_part'
    fit_question = next(q for q in adapter.questions if 'does not fit the AD5X bed' in q['question'])
    assert not any('88' in c or '94' in c for c in fit_question['choices'])
    assert any('50%' in c for c in fit_question['choices'])
    receipt = json.loads(next(env.root.glob('*.json')).read_text())
    assert receipt['fit_options'] == {'scales': [0.5], 'rotations': []}
    assert receipt['scale'] == 0.5
    assert env.starts == []


def test_per_part_with_no_honored_fit_explains_single_color_resend(env, monkeypatch):
    adapter, result, _ = _per_part_oversized(env, monkeypatch, set(), [0, 0, 0, 0])
    assert adapter.questions[-1]['choices'] == ['Do not print (Cancel)']
    assert 'none of the resize options work with these per-part colors' in adapter.questions[-1]['question']
    assert 'Cancel, then resend the link and choose single color to resize it.' in adapter.questions[-1]['question']
    receipt = json.loads(next(env.root.glob('*.json')).read_text())
    assert receipt['fit_options'] == {'scales': [], 'rotations': []}
    assert result['reason'] == 'cancelled oversized model'
    assert env.sliced == [] and env.starts == []


def _many_profiles(env, monkeypatch, answers):
    url = 'https://makerworld.com/en/models/386963-build-plate-cleaner#profileId-287445'
    rows = [{'instance_id': 287440 + i, 'title': f'Profile {i}'} for i in range(1, 8)]
    monkeypatch.setattr(model_job, 'maker_inventory', lambda _url: {
        'site': 'makerworld', 'slug': 'cleaner', 'title': 'Cleaner', 'sources': rows})
    selected = []

    def download(job, chosen, dest):
        selected.append(chosen['instance_id'])
        return _copy(env.tmp / 'source.3mf', dest)
    monkeypatch.setattr(model_job, 'maker_download', download)

    class Typed(Adapter):
        async def send_clarify(self, **kw):
            self.questions.append(kw)
            first = not hasattr(self, 'done')
            self.done = True
            import tools.clarify_gateway as cm
            cm.resolve_gateway_clarify(kw['clarify_id'], answers if first else kw['choices'][next(self.picks)])
            return S(success=True)
    adapter = Typed([1, 0, 2])
    event = S(source=S(profile='3dprinting', chat_id='c1', thread_id=None, user_id='u'), message_id='many', text=url)
    result = asyncio.run(prepared_intake.prepare_link(event, adapter, state(), env.root, timeout=1))
    return adapter, result, selected


def test_url_profile_id_wins_even_with_many_profiles(env, monkeypatch):
    # Contract (2026-10-12): a profileId fragment selects that exact profile automatically.
    adapter, _, selected = _many_profiles(env, monkeypatch, '6')
    assert not any('print profiles' in q['question'] for q in adapter.questions)
    assert selected == [287445]


def test_many_profiles_without_fragment_still_require_a_button(env, monkeypatch):
    url = 'https://makerworld.com/en/models/386963-build-plate-cleaner'
    rows = [{'instance_id': 287440 + i, 'title': f'Profile {i}'} for i in range(1, 8)]
    monkeypatch.setattr(model_job, 'maker_inventory', lambda _url: {
        'site': 'makerworld', 'slug': 'cleaner', 'title': 'Cleaner', 'sources': rows})
    selected = []
    monkeypatch.setattr(model_job, 'maker_download',
                        lambda job, chosen, dest: selected.append(chosen['instance_id']) or _copy(env.tmp / 'source.3mf', dest))
    adapter = Adapter([7])  # last button is Cancel
    event = S(source=S(profile='3dprinting', chat_id='c1', thread_id=None, user_id='u'), message_id='many2', text=url)
    asyncio.run(prepared_intake.prepare_link(event, adapter, state(), env.root, timeout=1))
    assert 'print profiles' in adapter.questions[0]['question']
    assert selected == [] and env.sliced == [] and env.starts == []


# --- multipart intake freeze: source inspection must be off-loop, and the part count must be announced ---

def _slow_inventory_flow(env, monkeypatch, audit_manifest=None):
    """Run a multipart (Shell/Eyes) intake with a slow inventory; return what ran where."""
    import threading
    from print_buddy.core import source_colors
    real_inventory = source_colors.inventory
    seen = {'threads': [], 'ticks': 0}

    def slow_inventory(path):
        seen['threads'].append(threading.current_thread())
        import time
        time.sleep(0.3)
        return real_inventory(path)
    monkeypatch.setattr(source_colors, 'inventory', slow_inventory)
    if audit_manifest is not None:
        monkeypatch.setattr(model_job, 'audit', lambda path: {'plates': 1, 'color_manifest': audit_manifest(path)})

    order = []

    class Ordered(Adapter):
        async def send_clarify(self, **kw):
            order.append(('ask', kw['question']))
            return await super().send_clarify(**kw)

        async def send(self, chat_id, content, reply_to=None, metadata=None):
            order.append(('say', content))
            return await super().send(chat_id, content, reply_to, metadata)

    async def main():
        async def ticker():
            while True:
                seen['ticks'] += 1
                await asyncio.sleep(0.01)
        task = asyncio.create_task(ticker())
        event = S(source=S(profile='3dprinting', chat_id='c1', thread_id=None, user_id='u'),
                  message_id=__import__('uuid').uuid4().hex, text='https://www.printables.com/model/1-ghost')
        adapter = Ordered([0, 0, 1, 2])  # per-part mode, Shell->slot1, Eyes->slot2, stop
        try:
            await prepared_intake.prepare_link(event, adapter, state(), env.root, timeout=1)
        finally:
            task.cancel()
        return adapter
    adapter = asyncio.run(main())
    return adapter, order, seen, real_inventory


def test_multipart_inspection_runs_off_event_loop_and_loop_stays_responsive(env, monkeypatch):
    import threading
    adapter, order, seen, _ = _slow_inventory_flow(env, monkeypatch)
    assert seen['threads'], 'inventory was never invoked'
    assert all(t is not threading.main_thread() for t in seen['threads'])
    assert seen['ticks'] >= 10  # a 0.3 s blocking call on the loop would leave ~1 tick
    # The flow stops at the confirm question, so the early slice is discarded and nothing starts.
    assert env.starts == [] and not any(env.root.parent.glob('archive/*/sliced/*.gcode'))


def test_multipart_first_prompt_states_part_count_and_asks_color_choice(env, monkeypatch):
    adapter, order, _, _ = _slow_inventory_flow(env, monkeypatch)
    assert order[0][0] == 'ask'  # no separate message precedes the prompt
    first = adapter.questions[0]
    assert first['question'].startswith('Inspected 2 parts: Shell, Eyes.')
    assert 'Choose colors for these parts or one color for everything?' in first['question']
    assert len(first['choices']) == 3
    assert first['choices'][-1] == 'Cancel'
    # Per-part answers continue to the per-part prompts instead of stalling.
    assert [q['question'] for q in adapter.questions[1:3]] == [
        'Choose the color/material for Shell, author made it White',
        'Choose the color/material for Eyes, author made it Black']


def test_audit_manifest_reused_only_when_bound_to_current_source_bytes(env, monkeypatch):
    from print_buddy.core import source_colors
    real = source_colors.inventory  # captured before the helper patches it
    # Matching hash: the audit's manifest is reused, so no second inspection happens.
    _, _, seen, _ = _slow_inventory_flow(env, monkeypatch, audit_manifest=real)
    assert seen['threads'] == []
    # Stale hash: the audit manifest is ignored and the file is re-inspected (off-loop).
    for f in env.root.glob('*.json'):
        f.unlink()
    _, _, seen, _ = _slow_inventory_flow(env, monkeypatch, audit_manifest=lambda p: {**real(p), 'source_sha256': 'stale'})
    assert len(seen['threads']) == 1


def test_dropped_3mf_runs_same_intake_without_any_site(env, monkeypatch):
    def boom(*a, **k):
        raise AssertionError('no site may be contacted for a dropped file')
    for name in ('maker_inventory', 'maker_download', 'printables_inventory', 'printables_download'):
        monkeypatch.setattr(model_job, name, boom)
    src = next(Path(model_job.ARCHIVE).parent.rglob('*.3mf'), None) or env.tmp / 'x'
    drop = env.tmp / 'doc_0123456789ab_Can Holder.3mf'
    drop.write_bytes(Path(src).read_bytes() if Path(src).is_file() else b'')
    event = S(source=S(profile='3dprinting', chat_id='c1', thread_id=None, user_id='u'), message_id='m9',
              text='', media_urls=[str(drop)])
    assert prepared_intake.model_file(event) == drop
    assert prepared_intake.model_file(S(media_urls=[str(drop), str(drop)])) is None
    adapter = Adapter([1, 0, 0])
    asyncio.run(prepared_intake.prepare_link(event, adapter, state(), env.root, timeout=1))
    receipt = json.loads(next(env.root.glob('*.json')).read_text())
    job = json.loads(Path(receipt['job_json']).read_text())
    assert receipt['url'] == 'upload:Can Holder.3mf' and job['site'] == 'upload'
    assert Path(job['downloaded_file']).name == 'Can Holder.3mf'
    assert receipt['status'] == 'STARTED' and len(env.starts) == 1


def test_upload_title_reads_3mf_designer_title(tmp_path):
    import zipfile
    f = tmp_path / 'CanHolder3MF.3mf'
    with zipfile.ZipFile(f, 'w') as z:
        z.writestr('3D/3dmodel.model', '<model><metadata name="Title">Slim 12oz Can Holder, Bubbl&#8217;r</metadata></model>')
    assert prepared_intake.upload_title(f) == 'Slim 12oz Can Holder, Bubbl\u2019r'
    stl = tmp_path / 'x.stl'
    stl.write_bytes(b'solid x')
    assert prepared_intake.upload_title(stl) is None


@pytest.mark.parametrize('text,hit', [
    ('print the previous print in black', True), ('reprint that', True), ('print it again in white', True),
    ('print the last one', True), ('print another', True), ('how is the print going', False),
    ('reprint https://www.printables.com/model/1-ghost', False)])
def test_reprint_request_detection(text, hit):
    assert prepared_intake.reprint_request(text) is hit


def test_reprint_offers_last_model_and_never_downloads(env, monkeypatch):
    run_flow(env, [1, 0, 0], mid='first')
    def boom(*a, **k):
        raise AssertionError('reprint must not contact any site')
    for name in ('maker_inventory', 'maker_download', 'printables_inventory', 'printables_download'):
        monkeypatch.setattr(model_job, name, boom)
    prev = prepared_intake.last_job()
    assert prev and prev['title'] == 'Ghost Stand'
    event = S(source=S(profile='3dprinting', chat_id='c1', thread_id=None, user_id='u'), message_id='again',
              text='print the previous print in black')
    adapter = Adapter([0, 1, 0, 0])  # yes reprint, slot 2, slice and print
    asyncio.run(prepared_intake.prepare_link(event, adapter, state(), env.root, timeout=1, reuse=prev))
    assert adapter.questions[0]['question'].startswith('Did you mean Ghost Stand')
    receipt = json.loads((env.root / (prepared_intake.hashlib.sha256(json.dumps(
        ['3dprinting', 'c1', '', 'again']).encode()).hexdigest() + '.json')).read_text())
    job = json.loads(Path(receipt['job_json']).read_text())
    assert receipt['url'].startswith('reprint:') and job['reused_from'] == prev['intake_id']
    assert Path(job['downloaded_file']).read_bytes() == Path(prev['downloaded_file']).read_bytes()
    assert len(env.starts) == 2


def test_reprint_after_restart_explains_interrupted_slice_and_asks_again(env):
    run_flow(env, [1, 0, 0], mid='first')
    prev = prepared_intake.last_job()
    folder = Path(prev['downloaded_file']).parents[1]
    # Left by a Print Buddy process that stopped mid-slice.
    (folder / 'WORKER.json').write_text(json.dumps({'state': 'running', 'step': 'slicing', 'started_at': 1.0,
                                                     'worker_pid': 1, 'owner': 'previous-run'}))
    event = S(source=S(profile='3dprinting', chat_id='c1', thread_id=None, user_id='u'), message_id='again',
              text='print the previous print in black')
    adapter = Adapter([0, 1, 0, 0])  # yes reprint, slot 2, slice and print
    asyncio.run(prepared_intake.prepare_link(event, adapter, state(), env.root, timeout=1, reuse=prev))
    first = adapter.questions[0]['question']
    assert 'interrupted during slicing' in first and 'asked again' in first
    assert not (folder / 'WORKER.json').exists()  # explained once
    # The new intake asked for its own color and bed-clear choices before starting.
    assert len(adapter.questions) >= 3 and len(env.starts) == 2


def test_declined_reprint_does_nothing(env):
    run_flow(env, [1, 0, 0], mid='first')
    prev = prepared_intake.last_job()
    event = S(source=S(profile='3dprinting', chat_id='c1', thread_id=None, user_id='u'), message_id='no',
              text='reprint that')
    adapter = Adapter([1])
    result = asyncio.run(prepared_intake.prepare_link(event, adapter, state(), env.root, timeout=1, reuse=prev))
    assert result['reason'] == 'reprint declined' and len(env.sliced) == 1 and len(env.starts) == 1
    assert 'Nothing was sliced or started' in adapter.sent[-1]



# Issue 16: plate choice and one bed-clear confirmation per plate -------------------------

def _split_env(env, monkeypatch, tag):
    """Two-plate full-size split with distinct per-plate durations."""
    from print_buddy.core import orca_adapter
    from .test_plate_merge import _source
    path = env.tmp / f'{tag}.3mf'
    _source(path, {'1': (150, 150, 10), '2': (150, 150, 10)}, [['1', '2']])
    monkeypatch.setattr(model_job, 'printables_download', lambda job, chosen, dest: _copy(path, dest))
    monkeypatch.setattr(orca_adapter, 'orca_plate_capacity',
                        lambda path, st, scale=1.0, copy_limit=16, **kw: 0 if scale == 1.0 else 1)
    monkeypatch.setattr(orca_adapter, 'fit_options',
                        lambda path, st: {'scales': [0.98, 0.75, 0.5], 'rotations': []})

    def fake_split_slice(job_path, copies=None, recipe_sha256=None):
        job_path = Path(job_path)
        data = json.loads(job_path.read_text())
        gcodes, rows = [], []
        for index, keys in enumerate(data['full_size_split_plan']['plates'], 1):
            gcode = job_path.parent / 'sliced' / f'plate_{index}.gcode'
            gcode.parent.mkdir(parents=True, exist_ok=True)
            gcode.write_text(f'; estimated printing time (normal mode) = {index}h 0m 0s\nG1 X1\n')
            gcodes.append(gcode)
            rows.append({'gcode': str(gcode), 'plate_id': index, 'slot': 1, 'part_keys': keys})
        data['derived_plates'] = rows
        model_job.write_job(Path(job_path).parent, data)
        env.sliced.extend(map(str, gcodes))
        return gcodes
    monkeypatch.setattr(model_job, 'slice_job', fake_split_slice)
    monkeypatch.setattr(prepared_intake.print_summary, 'find_job',
                        lambda filename, *a: (json.loads(Path(filename).parents[1].joinpath('JOB.json').read_text()),
                                              {'gcode': str(filename), 'plate': int(Path(filename).stem[-1]),
                                               'parts': [f'Part {Path(filename).stem[-1]}'], 'slot': 1,
                                               'color': 'white', 'material': 'PLA',
                                               'filament_g': None, 'seconds': 3600 * int(Path(filename).stem[-1])}))
    return S(source=S(profile='3dprinting', chat_id='c1', thread_id=None, user_id='u'),
             message_id=tag, text='https://www.printables.com/model/1-ghost')


def _plate_prompts(adapter):
    return [q for q in adapter.questions if q['question'] == 'Which plate prints first?']


def test_split_print_now_picker_choice_honored_and_bed_asked_once(env, monkeypatch):
    event = _split_env(env, monkeypatch, 'pick-second')
    adapter = Adapter([1, 0, 0, 0, 1])  # color, slot, 100% split, Slice and print now, picker -> plate 2
    asyncio.run(prepared_intake.prepare_link(event, adapter, state(), env.root, timeout=1))
    picker = _plate_prompts(adapter)
    assert len(picker) == 1
    assert picker[0]['choices'][0] == 'Plate 1: Part 1 · white PLA · ~1h 0m'
    assert picker[0]['choices'][1] == 'Plate 2: Part 2 · white PLA · ~2h 0m'
    assert picker[0]['choices'][2] == 'Cancel'
    assert len(env.starts) == 1 and env.starts[0]['part_id'] == 'plate2'
    assert Path(env.starts[0]['gcode']).name == 'plate_2.gcode'
    # The only bed-clear wording is the single "Slice and print now (bed is clear)" choice.
    assert not any(print_next.START_LABEL in q['choices'] for q in adapter.questions)
    assert sum('bed is clear' in c for q in adapter.questions for c in q['choices']) == 1
    assert not any('/print-next' in m for m in adapter.sent)


def test_split_print_now_first_plate_default_order(env, monkeypatch):
    event = _split_env(env, monkeypatch, 'pick-first')
    adapter = Adapter([1, 0, 0, 0, 0])
    asyncio.run(prepared_intake.prepare_link(event, adapter, state(), env.root, timeout=1))
    assert [t['part_id'] for t in env.starts] == ['plate1']


def test_split_slice_only_starts_nothing_until_pick_and_one_confirm(env, monkeypatch):
    event = _split_env(env, monkeypatch, 'slice-only-split')
    adapter = Adapter([1, 0, 0, 1, 1, 1])  # slice only, plate 2, then Cancel at the single confirm
    result = asyncio.run(prepared_intake.prepare_link(event, adapter, state(), env.root, timeout=1))
    assert env.starts == [] and result['reason'] == 'operator did not start the sliced plate'
    confirms = [q for q in adapter.questions if q['choices'] == [print_next.START_LABEL, print_next.CANCEL_LABEL]]
    assert len(confirms) == 1 and 'plate 2 of 2' in confirms[0]['question']
    receipt = json.loads(next(env.root.glob('*.json')).read_text())
    assert receipt['status'] == 'SLICED_NOT_STARTED' and receipt['first_plate'] == 'plate2'


def test_split_slice_only_picker_cancel_starts_nothing(env, monkeypatch):
    event = _split_env(env, monkeypatch, 'slice-only-cancel')
    adapter = Adapter([1, 0, 0, 1, 2])
    asyncio.run(prepared_intake.prepare_link(event, adapter, state(), env.root, timeout=1))
    assert env.starts == []
    assert not any(q['choices'] == [print_next.START_LABEL, print_next.CANCEL_LABEL] for q in adapter.questions)


def test_single_plate_has_no_picker(env):
    adapter, _result, _receipt = run_flow(env, [1, 0, 0])
    assert _plate_prompts(adapter) == [] and len(env.starts) == 1


@pytest.mark.parametrize('job, expected', [
    ({}, True),
    ({'audit': {'plates': 1}, 'color_decisions': {'mode': 'single_color'}}, True),
    ({'audit': {'plates': 2}}, False),
    ({'color_decisions': {'mode': 'per_part'}}, False),
    ({'plate_mode': 'separate'}, False),
    ({'full_size_split_plan': {'plates': [['a'], ['b']]}}, False),
])
def test_copies_supported_only_for_single_plate_jobs(job, expected):
    assert model_job.copies_supported(job) is expected


def test_per_part_job_is_never_offered_copies(env, monkeypatch):
    """#37: only the single plate path clones copies, so per-part intake must not ask for a count."""
    from print_buddy.core import orca_adapter
    monkeypatch.setattr(orca_adapter, 'orca_plate_capacity', lambda path, st, **kw: 3)
    # Per-part mode, both parts on the first slot, slice only, then decline start.
    adapter, result, receipt = run_flow(env, [0, 0, 0, 1, 1], mid='per-part-copies')
    assert not any('How many copies' in q['question'] for q in adapter.questions)
    assert any(text.startswith('Printing one copy.') for text in adapter.sent)
    assert receipt['copies'] == 1


def test_stop_during_a_long_step_cancels_it_and_leaves_nothing_running():
    """#61: Stop on a progress message cancels the running step and raises Stopped."""
    import threading
    from tools import clarify_gateway as cm

    class Live:
        def __init__(self):
            self.cid = None

        async def send_clarify(self, **kw):
            self.cid = kw['clarify_id']
            return S(success=True)

        async def send(self, **kw):
            self.status = kw['content']
            return S(success=True)

    started = threading.Event()
    finished = threading.Event()

    async def slow():
        started.set()
        try:
            await asyncio.sleep(30)
        finally:
            finished.set()

    adapter = Live()

    async def run():
        task = asyncio.ensure_future(prepared_intake._with_stop(
            adapter, S(chat_id='stop-mid-step'), 'stop-mid-step', None,
            'Uploading to the printer. Press Stop to cancel before it starts.', slow(), allow_stop=True))
        for _ in range(200):
            if started.is_set() and adapter.cid:
                break
            await asyncio.sleep(0.01)
        assert adapter.cid, 'the Stop button was never registered'
        assert cm.resolve_gateway_clarify(adapter.cid, prepared_intake.STOP_LABEL)
        with pytest.raises(prepared_intake.Stopped):
            await task

    asyncio.run(run())
    assert started.is_set()
    assert finished.wait(2), 'the cancelled step was still running'


def test_status_only_work_returns_without_registering_a_stop_prompt():
    """Status-only fit work must not create an interactive Stop prompt."""
    import time as _time

    class Live:
        def __init__(self):
            self.cid = None
            self.status = None

        async def send_clarify(self, **kw):
            self.cid = kw['clarify_id']
            return S(success=True)

        async def send(self, **kw):
            self.status = kw['content']
            return S(success=True)

    async def quick():
        await asyncio.sleep(0.01)
        return {'fits': True, 'copies': 1}

    adapter = Live()

    async def run():
        return await asyncio.wait_for(prepared_intake._with_stop(
            adapter, S(chat_id='no-press'), 'no-press', None,
            'Checking whether it fits the bed. Press Stop to cancel.', quick()), 5)

    started = _time.monotonic()
    assert asyncio.run(run()) == {'fits': True, 'copies': 1}
    assert adapter.cid is None, 'status-only work must not register a Stop prompt'
    assert adapter.status == 'Checking whether it fits the bed.'
    assert _time.monotonic() - started < 5


def test_work_error_surfaces_without_a_stop_press():
    """A failing step must raise its own error promptly, not hang behind the Stop button."""
    class Live:
        async def send_clarify(self, **kw):
            return S(success=True)

    async def boom():
        raise ValueError('model is larger than the bed')

    async def run():
        return await asyncio.wait_for(prepared_intake._with_stop(
            Live(), S(chat_id='no-press-err'), 'no-press-err', None, 'Checking. Press Stop to cancel.', boom()), 5)

    with pytest.raises(ValueError, match='larger than the bed'):
        asyncio.run(run())
