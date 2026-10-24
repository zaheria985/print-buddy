"""Slice with the real Orca CLI and the home templates, then run the guards on what it produced.

Skipped unless PRINT_BUDDY_REAL_SLICER=1. CI runs these in the real-slicer job with the same Flathub
Orca the home box uses. Locally: PRINT_BUDDY_REAL_SLICER=1 python3 -m unittest tests.slicer -v
"""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from print_buddy import settings
from print_buddy.core import slice_guard as sg
import pytest

REAL = os.environ.get("PRINT_BUDDY_REAL_SLICER") == "1"
# Flatpak Orca has a private /tmp and cannot read host temp files. Work inside the checkout,
# which sits under $HOME on the home box and the CI runner, where the sandbox can see it.
WORK = settings.ROOT / ".slicer-work"


def _orca_command():
    """Return the configured Orca command with the adapter's runtime overrides applied."""
    from print_buddy.core import orca_adapter
    return orca_adapter._orca_command()


def write_cube(path, size):
    """ASCII STL cube, size mm per side, corner at the origin."""
    s = float(size)
    v = [(x, y, z) for x in (0, s) for y in (0, s) for z in (0, s)]
    faces = [(0, 2, 3), (0, 3, 1), (4, 5, 7), (4, 7, 6), (0, 1, 5), (0, 5, 4),
             (2, 6, 7), (2, 7, 3), (0, 4, 6), (0, 6, 2), (1, 3, 7), (1, 7, 5)]
    lines = ["solid cube"]
    for a, b, c in faces:
        lines += ["facet normal 0 0 0", " outer loop"]
        lines += [f"  vertex {v[i][0]} {v[i][1]} {v[i][2]}" for i in (a, b, c)]
        lines += [" endloop", "endfacet"]
    path.write_text("\n".join(lines + ["endsolid cube"]) + "\n")
    return path


@unittest.skipUnless(REAL, "set PRINT_BUDDY_REAL_SLICER=1 to slice with the real Orca CLI")
class OrcaRealSliceTests(unittest.TestCase):
    def setUp(self):
        WORK.mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=WORK)
        self.dir = Path(self.tmp.name)
        self.settings = settings.current()
        templates = self.settings["orca"]["templates"]
        self.machine, self.process, self.filament = (templates / f"{n}.json" for n in ("machine", "process", "filament"))

    def tearDown(self):
        self.tmp.cleanup()

    def slice(self, stl, *extra):
        """Same flags the adapter uses for a plain single color STL."""
        out = self.dir / f"out_{stl.stem}"
        out.mkdir()
        command = [*_orca_command(),
                   "--load-settings", f"{self.machine};{self.process}", "--load-filaments", str(self.filament),
                   "--arrange", "1", "--orient", "0", "--ensure-on-bed", *extra,
                   "--allow-newer-file", "--slice", "0", "--outputdir", str(out), str(stl)]
        result = subprocess.run(command, capture_output=True, text=True, timeout=600)
        log = result.stdout + result.stderr
        print(f"\n$ {' '.join(command)}\nexit {result.returncode}\n{log[-3000:]}")
        self.assertNotIn("No such file", log, "Orca could not read the input; the sandbox cannot see this path")
        return result, out

    def guard(self, out):
        gcode = out / "plate_1.gcode"
        result_json = out / "result.json"
        r = sg.post_slice(gcode, tool_slots={0: 1}, result_json=result_json if result_json.is_file() else None)
        print(f"post_slice: {r.as_dict()}")
        return r

    def test_cube_slices_and_passes_the_guard(self):
        result, out = self.slice(write_cube(self.dir / "cube.stl", 20))
        self.assertEqual(result.returncode, 0)
        r = self.guard(out)
        self.assertNotEqual(r.status, "blocked", r.blocked)
        times = [l for l in (out / "plate_1.gcode").read_text(errors="replace").splitlines() if l.startswith(";") and "time" in l.lower()]
        self.assertEqual(r.warnings, [], f"guard missed Orca's print time header; time lines: {times[:10]}")

    def test_fixture_stl_slices_and_passes_the_guard(self):
        fixture = Path(__file__).parent / "fixtures" / "cube.stl"
        result, out = self.slice(fixture)
        self.assertEqual(result.returncode, 0)
        self.assertNotEqual(self.guard(out).status, "blocked")

    def test_four_copies_stay_on_the_bed(self):
        result, out = self.slice(write_cube(self.dir / "copies.stl", 30), "--clone-objects", "4")
        self.assertEqual(result.returncode, 0)
        self.assertNotEqual(self.guard(out).status, "blocked")

    def test_model_bigger_than_the_bed_is_never_accepted(self):
        bed = self.settings["printer"]["bed_mm"][0]
        result, out = self.slice(write_cube(self.dir / "huge.stl", bed + 10))
        gcode = out / "plate_1.gcode"
        accepted = result.returncode == 0 and gcode.is_file() and self.guard(out).status != "blocked"
        self.assertFalse(accepted, "an oversized model produced G-code the guard accepted")

    def test_protected_settings_baseline_round_trips_on_real_output(self):
        result, out = self.slice(write_cube(self.dir / "baseline.stl", 20))
        self.assertEqual(result.returncode, 0)
        gcode = out / "plate_1.gcode"
        found = sg._effective_config(gcode)
        self.assertIn("machine_start_gcode", found, "Orca output no longer carries the protected settings block")
        baseline = self.dir / "baseline.json"
        sg.approve_baseline(gcode, path=baseline)
        self.assertTrue(sg.enforce_approved_baseline(gcode, path=baseline).ok)


@pytest.mark.skipif(not REAL, reason="set PRINT_BUDDY_REAL_SLICER=1 to slice with the real Orca CLI")
# The A1 (0.4 mm only) slices from profiles generated from the installed OrcaSlicer: test_orca_real_bambu_a1.py.
@pytest.mark.parametrize("printer_key,size", [("flashforge.ad5x", 0.25), ("flashforge.ad5x", 0.4), ("flashforge.ad5x", 0.6), ("flashforge.ad5x", 0.8), ("sovol.sv06", 0.2)])
def test_real_slicer_supported_nozzle_sizes(printer_key, size):
    from print_buddy import printers
    from print_buddy.core import orca_adapter, nozzle

    WORK.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=WORK) as tmp_dir:
        dir_path = Path(tmp_dir)
        stl = write_cube(dir_path / f"cube_{size}.stl", 15)
        out = dir_path / f"out_{size}"
        out.mkdir()
        st = settings.current()
        printer_adapter = printers.get(printer_key, st)
        state = {
            "moonraker_url": "http://127.0.0.1:7125",
            "spoolman_url": "http://127.0.0.1:7912",
            "nozzle": {"diameter_mm": size, "type": "hardened_steel"},
            "spool": {"spoolman_id": 1, "material": "PLA", "density": 1.24},
            "temps_c": {"nozzle": 220, "bed": 60},
            "calibration_baseline": {"native_mesh": "auto"},
            "ifs_slots": {
                "1": {
                    "slot": 1,
                    "spoolman_id": 1,
                    "material": "PLA",
                    "physical_color_verified": True,
                    "hex": "FFFFFF",
                }
            },
        }
        gcode, _prov, _cfgs = orca_adapter.slice_model(stl, out, state, adapter=printer_adapter)
        assert gcode.is_file()
        content = gcode.read_text(errors="replace")
        assert f"; nozzle_diameter = {size}" in content
        tuned = nozzle.get_tuned_nozzle_profile(size)
        assert tuned is not None
        assert f"; line_width = {tuned.line_width}" in content
