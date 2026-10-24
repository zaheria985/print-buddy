"""Offline lifecycle tests for the decisions-bound early slicer."""
import asyncio
import json
from pathlib import Path

from print_buddy.core import model_job
from print_buddy.core.background_slice import BackgroundSlice, slice_binding


def _job(decisions=None, **extra):
    return {
        'color_decisions': decisions or {'mode': 'single_color', 'selection': {'slot': 1, 'color': 'white'}},
        'scale': 1.0,
        'orientation': {'label': 'upright'},
        'copies': 1,
        'plate_mode': 'single',
        **extra,
    }


def test_binding_includes_source_sha_and_every_gcode_decision():
    base = _job()
    digest = slice_binding(base, 'source-a')
    assert json.loads(digest)['source_sha256'] == 'source-a'
    assert slice_binding(dict(reversed(list(base.items()))), 'source-a') == digest
    assert slice_binding(base, 'source-b') != digest
    for change in (
        {'scale': 0.8},
        {'orientation': {'label': 'rotated'}},
        {'copies': 2},
        {'plate_mode': 'multi'},
        {'full_size_split_plan': {'plates': [1, 2]}},
        {'color_decisions': {'mode': 'single_color', 'selection': {'slot': 2, 'color': 'black'}}},
    ):
        assert slice_binding({**base, **change}, 'source-a') != digest


def test_changed_binding_discards_old_slice_before_starting_new_one(tmp_path):
    async def run():
        folder = tmp_path / 'intake'
        folder.mkdir()
        job_path = folder / 'JOB.json'
        job_path.write_text('{}')
        runner = BackgroundSlice()
        entered = asyncio.Event()
        release = asyncio.Event()
        finished = []
        invocation = 0

        async def slice_func(path, work, _runner):
            nonlocal invocation
            invocation += 1
            current = invocation
            entered.set()
            if current == 1:
                await release.wait()
            work.mkdir(parents=True, exist_ok=True)
            output = work / f'plate-{current}.gcode'
            output.write_text(path)
            finished.append(output)
            return [output]

        await runner.start(job_path, 'old decisions', slice_func)
        await entered.wait()
        old_work = runner.work
        async def unblock():
            await asyncio.sleep(0.02)
            release.set()
        asyncio.create_task(unblock())
        await runner.start(job_path, 'new decisions', slice_func)
        assert runner.binding == 'new decisions'
        assert old_work is not None and list(old_work.glob('*.gcode')) == []
        assert runner.running()
        await runner.discard()
        assert not runner.running()
        assert not (folder / 'sliced').exists()
        assert finished == []  # cancellation stops the old writer before it writes; nothing is left

    asyncio.run(run())


def test_confirm_takes_completed_slice_without_starting_another(tmp_path):
    async def run():
        folder = tmp_path / 'intake'
        folder.mkdir()
        job_path = folder / 'JOB.json'
        job_path.write_text('{}')
        runner = BackgroundSlice()
        calls = []

        async def slice_func(path, work, _runner):
            calls.append(path)
            work.mkdir(parents=True, exist_ok=True)
            output = work / 'plate.gcode'
            output.write_text('G1 X1\n')
            return [output]

        await runner.start(job_path, 'same decisions', slice_func)
        await runner.started.wait()
        while runner.running():
            await asyncio.sleep(0)
        result = await runner.take('same decisions')
        assert result == [folder / 'sliced' / 'plate.gcode']
        assert calls == [str(job_path)]
        assert (folder / 'sliced' / 'plate.gcode').exists()

    asyncio.run(run())


def test_changed_decision_digest_and_source_hash_never_match_prior_slice(tmp_path):
    first = _job()
    source = tmp_path / 'source.3mf'
    source.write_bytes(b'original source')
    original = slice_binding(first, model_job.file_sha256(source))
    changed_colors = {**first, 'color_decisions': {'mode': 'single_color', 'selection': {'slot': 2}}}
    assert slice_binding(changed_colors, model_job.file_sha256(source)) != original
    source.write_bytes(b'changed source')
    assert slice_binding(first, model_job.file_sha256(source)) != original
