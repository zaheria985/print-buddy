"""The Discord intake for the real bambulab.a1 model: no print button, Bambu package through the A1 guard.

Offline: the fake Discord adapter from test_prepared_intake answers buttons; the slice writes synthetic A1
G-code (tests/printers/test_bambulab_a1.a1_gcode). Nothing contacts a printer.
"""
import json
import zipfile
from pathlib import Path

import pytest

from print_buddy import printers
from print_buddy.core import delivery, model_job
from printers import a1_fixture
from printers.test_bambulab_a1 import A1 as A1_KEY, a1_gcode, ad5x_gcode, adapter_for
from .helpers import external
from .test_delivery import _delivery_menu, config_for, use_config
from .test_plate_thumbnail import box_stl
from .test_prepared_intake import env, run_flow  # noqa: F401  (pytest fixture)

print_next = external("print_next")
A1 = "bambulab.a1"


@pytest.fixture(autouse=True)
def a1_profiles(monkeypatch, tmp_path):
    """A1 templates generated from the invented fixture tree (trusted for the test only), never the profile's."""
    a1_fixture.trust(monkeypatch.setattr)
    a1_fixture.install(adapter_for(A1_KEY))


def a1_config(tmp_path):
    cfg = config_for(A1)
    cfg["paths"]["hermes_home"] = tmp_path / "hermes"
    cfg["orca"]["templates"] = tmp_path / "hermes" / "state" / "orca-profiles" / "bambulab_a1"
    a1_fixture.install(printers.get(A1, cfg))
    return cfg


def _a1_slice(monkeypatch, text):
    real_slice = model_job.slice_job

    def slice_as_a1(job_path, **kw):
        out = real_slice(job_path, **kw)
        for gcode in out:
            Path(gcode).write_text(text)
            box_stl(Path(gcode).parent / "geometry.stl", origin=(118, 123, 0))  # what a real slice leaves beside it
        data = json.loads(Path(job_path).read_text())
        model_job.write_job(Path(job_path).parent, {**data, "printer_model": A1})
        return out
    monkeypatch.setattr(model_job, "slice_job", slice_as_a1)


def test_a1_offers_no_print_and_packages_through_its_guard(env, monkeypatch, tmp_path):  # noqa: F811
    use_config(monkeypatch, a1_config(tmp_path))
    _a1_slice(monkeypatch, a1_gcode())
    # one color, slot 1, "Slice only", package, send, (menu ends)
    adapter, result, receipt = run_flow(env, [1, 0, 0, 1, 0])
    assert not any(print_next.START_LABEL in q["choices"] or any("print now" in c for c in q["choices"])
                   for q in adapter.questions)
    menus = _delivery_menu(adapter)
    assert menus[0]["choices"] == ["Send the files here", "Package for Bambu (no upload)", delivery.DONE_LABEL]
    ready = next(m for m in adapter.sent if m.startswith("Bambu package ready: plate_1.gcode.3mf."))
    assert "Nothing was uploaded to the Bambu Lab A1" in ready and "not been tested on a printer" in ready
    package = next(m for m in adapter.sent if "MEDIA:" in m).split("MEDIA:")[1].strip()
    with zipfile.ZipFile(package) as archive:
        assert archive.read("Metadata/plate_1.gcode").decode() == a1_gcode()
        # #275: the previews the A1 screen and the Bambu apps read, rendered from the sliced geometry.
        assert {"Metadata/plate_1.png", "Metadata/plate_1_small.png"} <= set(archive.namelist())
    assert env.starts == [] and receipt["status"] == "SLICED_NOT_STARTED" and "bed_clear_approval" not in receipt
    assert [d["method"] for d in receipt["deliveries"]] == ["bambu_handoff", "discord"]


def test_a1_refuses_to_package_foreign_start_code(env, monkeypatch, tmp_path):  # noqa: F811
    use_config(monkeypatch, a1_config(tmp_path))
    _a1_slice(monkeypatch, ad5x_gcode())
    adapter, result, receipt = run_flow(env, [1, 0, 0, 1, 1])
    assert not any("MEDIA:" in m and m.strip().endswith(".gcode.3mf") for m in adapter.sent)
    assert any("Klipper" in m or "non-Bambu commands" in m for m in adapter.sent)
    assert env.starts == [] and "bed_clear_approval" not in receipt
