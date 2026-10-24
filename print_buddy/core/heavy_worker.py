"""Run one CPU-heavy intake step in a child process and print its JSON result.

Large 3MF inspection is pure-Python XML work that holds the GIL for seconds at a time, so
asyncio.to_thread still freezes the gateway event loop and Discord button callbacks time out.
A child process has its own GIL and its own memory, so the gateway stays responsive.
"""
import importlib
import json
import sys
from pathlib import Path

ALLOWED = {
    ('model_job', 'audit'), ('model_job', 'slice_job'), ('model_job', 'maker_download'),
    ('model_job', 'printables_download'), ('source_colors', 'inventory'),
    ('orca_adapter', 'orca_plate_capacity'), ('orca_adapter', 'fit_options'),
    ('model_job', 'per_part_fit_options'), ('plate_merge', 'full_size_split_possible'),
    ('geometry_layout', 'inspect_geometry'), ('geometry_layout', 'arrange_and_export'),
    ('geometry_layout', 'probe_single_plate_rearrange'),
    ('cad_split', 'check_source_against_split_plan'), ('cad_split', 'prepare_cad_split'),
}


def _plain(value):
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    return value


def main():
    # Works as `python3 -m print_buddy.core.heavy_worker` or as a plain script path.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    request = json.loads(sys.stdin.read())
    key = (request['module'], request['func'])
    if key not in ALLOWED:
        raise SystemExit(f'not an offloadable step: {key}')
    func = getattr(importlib.import_module(f'print_buddy.core.{key[0]}'), key[1])
    args = list(request.get('args', []))
    if key != ('model_job', 'slice_job') and args and isinstance(args[0], str):
        args[0] = Path(args[0])  # these take a Path source file; JSON carried it as a string
    result = func(*args, **request.get('kwargs', {}))
    sys.stdout.write('\n' + json.dumps({'ok': True, 'path': isinstance(result, Path), 'result': _plain(result)}))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:  # report to the parent; it re-raises with the same message
        sys.stdout.write('\n' + json.dumps({'ok': False, 'type': type(exc).__name__, 'error': str(exc)}))
