"""Slice identity must cover the complete slice recipe, not a label for it.

Every input that changes the G-code (transform, scale, copies, support/brim policy,
split assignments, printer model, material/nozzle, resolved profiles, slicer identity)
must change the binding, so a slice made for one recipe is never reused for another.
"""
import json
from pathlib import Path

import pytest

from print_buddy.core import model_job as mj
from print_buddy.core import orca_adapter, printer_state
from print_buddy.core.background_slice import slice_binding

from .test_artifact_binding import setup_job

UPRIGHT = [[1, 0, 0], [0, 1, 0], [0, 0, 1]]
ON_SIDE = [[1, 0, 0], [0, 0, -1], [0, 1, 0]]


def _job(**extra):
    return {
        'color_decisions': {'mode': 'single_color', 'source_sha256': 'source-a',
                            'selection': {'slot': 1, 'color': 'white', 'material': 'PLA'}},
        'scale': 1.0,
        'orientation': {'label': 'best fit', 'rotation': UPRIGHT, 'axis': 'z',
                        'dims_full_mm': [40, 10, 1], 'supports': False, 'brim': False},
        'copies': 1,
        'plate_mode': 'separate',
        'fit_brim_policy': {'width_mm': 0},
        'full_size_split_plan': {'plates': [['a', 'b'], ['c']]},
        **extra,
    }


def _with_orientation(**change):
    base = _job()
    return {**base, 'orientation': {**base['orientation'], **change}}


JOB_CHANGES = {
    'rotation matrix, same label': _with_orientation(rotation=ON_SIDE),
    'rotated envelope, same label': _with_orientation(dims_full_mm=[40, 1, 10]),
    'supports, same label': _with_orientation(supports=True),
    'orientation brim, same label': _with_orientation(brim=True),
    'fit brim policy': _job(fit_brim_policy={'width_mm': 5}),
    'split assignments': _job(full_size_split_plan={'plates': [['a'], ['b', 'c']]}),
    'printer model': _job(printer_model='sovol.sv06'),
    'material choice': _job(color_decisions={'mode': 'single_color', 'source_sha256': 'source-a',
                                             'selection': {'slot': 1, 'color': 'white', 'material': 'PETG'}}),
    'scale': _job(scale=0.8),
    'copies': _job(copies=3),
}


@pytest.mark.parametrize('name', sorted(JOB_CHANGES))
def test_every_job_recipe_input_changes_the_binding(name):
    assert slice_binding(JOB_CHANGES[name], 'source-a') != slice_binding(_job(), 'source-a'), name


def test_binding_is_a_versioned_recipe_and_ignores_dict_order():
    base = _job()
    binding = slice_binding(base, 'source-a')
    recipe = json.loads(binding)
    assert recipe['recipe_version'] == 1
    assert recipe['source_sha256'] == 'source-a'
    assert slice_binding(dict(reversed(list(base.items()))), 'source-a') == binding


def _hardware(**change):
    st = json.loads(Path(printer_state.STATE).read_text())
    for path, value in change.items():
        target = st
        *parents, leaf = path.split('.')
        for key in parents:
            target = target[key]
        target[leaf] = value
    return st


@pytest.mark.parametrize('path,value', [
    ('nozzle.diameter_mm', 0.6),
    ('nozzle.type', 'hardened'),
    ('ifs_slots.1.material', 'PETG'),
    ('ifs_slots.1.hex', '000000'),
])
def test_printer_material_and_nozzle_state_change_the_binding(monkeypatch, path, value):
    monkeypatch.setattr(printer_state, 'load', _hardware)
    before = slice_binding(_job(), 'source-a')
    monkeypatch.setattr(printer_state, 'load', lambda: _hardware(**{path: value}))
    assert slice_binding(_job(), 'source-a') != before


def test_resolved_profile_bytes_change_the_binding(tmp_path, monkeypatch):
    templates = tmp_path / 'templates'
    templates.mkdir()
    for name in ('machine', 'process', 'filament'):
        (templates / f'{name}.json').write_text((Path(orca_adapter.TEMPLATES) / f'{name}.json').read_text())
    monkeypatch.setattr(orca_adapter, 'TEMPLATES', templates)
    before = slice_binding(_job(), 'source-a')
    process = json.loads((templates / 'process.json').read_text())
    process['print_buddy_test_change'] = '1'
    (templates / 'process.json').write_text(json.dumps(process))
    assert slice_binding(_job(), 'source-a') != before


def test_slicer_identity_changes_the_binding(monkeypatch):
    before = slice_binding(_job(), 'source-a')
    monkeypatch.setattr(orca_adapter, 'ORCA_COMMAND', ['/opt/other-orca/orca-slicer'])
    assert slice_binding(_job(), 'source-a') != before


def test_slice_records_versioned_recipe_hash_in_the_artifact(tmp_path, monkeypatch):
    jp, _st, _gcode = setup_job(tmp_path, monkeypatch)
    job = json.loads(jp.read_text())
    record = job['slice_recipe']
    assert record['version'] == 1
    assert job['artifact_binding']['recipe_version'] == 1
    assert job['artifact_binding']['recipe_sha256'] == record['sha256']
    from print_buddy.core import slice_recipe
    expected = slice_binding(job, mj.file_sha256(job['downloaded_file']))
    assert record['sha256'] == slice_recipe.binding_sha256(expected)
    mj.require_current_artifact(job, job['gcode'])


def test_worker_refuses_a_request_bound_to_another_recipe(tmp_path, monkeypatch):
    jp, _st, _gcode = setup_job(tmp_path, monkeypatch)
    before = jp.read_bytes()
    calls = []
    monkeypatch.setattr(orca_adapter, 'slice_model', lambda *a, **k: calls.append(a))
    with pytest.raises(RuntimeError, match='recipe'):
        mj.slice_job(str(jp), recipe_sha256='0' * 64)
    assert calls == [] and jp.read_bytes() == before


def test_recipe_change_during_slicing_never_marks_the_artifact_ready(tmp_path, monkeypatch):
    jp, _st, _gcode = setup_job(tmp_path, monkeypatch)
    real = orca_adapter.slice_model

    def slice_then_rotate(*args, **kwargs):
        result = real(*args, **kwargs)
        job = json.loads(jp.read_text())
        job['orientation'] = {'label': 'best fit', 'rotation': ON_SIDE}
        jp.write_text(json.dumps(job))
        return result
    monkeypatch.setattr(orca_adapter, 'slice_model', slice_then_rotate)
    previous = json.loads(jp.read_text())['artifact_binding']
    with pytest.raises(RuntimeError, match='changed during slicing'):
        mj.slice_job(str(jp))
    assert json.loads(jp.read_text())['artifact_binding'] == previous


def test_artifact_sliced_before_recipe_identity_is_refused_not_guessed(tmp_path, monkeypatch):
    jp, _st, gcode = setup_job(tmp_path, monkeypatch)
    job = json.loads(jp.read_text())
    legacy = {key: job['artifact_binding'][key] for key in ('source_sha256', 'decisions_sha256', 'gcode_sha256')}
    job['artifact_binding'] = legacy
    job.pop('slice_recipe', None)
    with pytest.raises(RuntimeError, match='slice recipe'):
        mj.require_current_artifact(job, gcode)


def test_tampered_recipe_record_is_refused(tmp_path, monkeypatch):
    jp, _st, gcode = setup_job(tmp_path, monkeypatch)
    job = json.loads(jp.read_text())
    job['slice_recipe']['recipe']['copies'] = 4
    with pytest.raises(RuntimeError, match='slice recipe'):
        mj.require_current_artifact(job, gcode)


def test_worker_accepts_the_recipe_intake_bound_before_the_first_slice(tmp_path, monkeypatch):
    # Intake binds the job as saved before slicing; slice_job then normalizes the job
    # (printer model, resolved slot selection) and must still see the same recipe.
    from .test_color_intake import source, state
    from print_buddy.core import slice_recipe
    st = state(); st.update(bed_mm=[220, 220], max_print_height_mm=220, moonraker_url='offline://fake')
    for slot in st['ifs_slots'].values():
        slot['physical_color_verified'] = True
    jp = tmp_path / 'JOB.json'
    jp.write_text(json.dumps({'downloaded_file': str(source(tmp_path)),
                              'audit': {'plates': 1, 'size_x': 40, 'size_y': 10, 'size_z': 1}}))
    mj.intake_color_choices(jp, st, 'single_color', [1])
    monkeypatch.setattr(mj.printer_state, 'load', lambda: st)
    job = json.loads(jp.read_text())
    job['copies'] = 2
    jp.write_text(json.dumps(job))
    assert 'printer_model' not in job

    def slice_fake(src, folder, st, brim, color_decisions, filament_slot=None, copies=1):
        folder.mkdir(parents=True)
        out = folder / 'plate_1.gcode'
        out.write_text('G1 X1\n')
        return out, {'offline_test': True}, []
    monkeypatch.setattr(orca_adapter, 'slice_model', slice_fake)
    monkeypatch.setattr(mj, 'verify_gcode', lambda _: None)
    from print_buddy.printers.flashforge.zmod import multicolor_contract
    monkeypatch.setattr(multicolor_contract, 'finalize', lambda *a, **k: None)
    requested = slice_recipe.binding_sha256(slice_binding(job, mj.file_sha256(job['downloaded_file'])))
    mj.slice_job(str(jp), copies=2, recipe_sha256=requested)
    sliced = json.loads(jp.read_text())
    assert sliced['printer_model'] and sliced['artifact_binding']['recipe_sha256'] == requested
