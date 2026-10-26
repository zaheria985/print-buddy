"""Invented P1S-shaped Orca resources for tests only; no Bambu/Orca profile content."""
import json
import tempfile
from pathlib import Path

from print_buddy.printers.bambulab import facts, profiles
from print_buddy.printers.bambulab.p1s import P1SAdapter

HERMES = Path(tempfile.mkdtemp(prefix="p1s-test-profile-"))
NAMES = {"machine": "Bambu Lab P1S 0.4 nozzle", "process": "0.20mm Standard @BBL X1C",
         "filament": "Bambu PLA Basic @BBL X1C"}
FILAMENTS = {m: n for m, n in facts.P1S["orca_filaments"].value.items() if n != NAMES["filament"]}


def resources(root):
    root = Path(root)
    bbl = root / "profiles" / "BBL"
    for folder in (bbl / "machine", bbl / "process", bbl / "filament"):
        folder.mkdir(parents=True, exist_ok=True)
    index = {"name": "Print Buddy invented P1S test data", "version": "00.00.00.00"}
    for kind, filename in (("machine", "machine"), ("process", "process"), ("filament", "filament")):
        index[f"{kind}_list"] = [{"name": NAMES[kind], "sub_path": f"{kind}/{filename}.json"}]
    (root / "profiles").mkdir(parents=True, exist_ok=True)
    (root / "profiles" / "BBL.json").write_text(json.dumps(index))
    machine = {
        "type": "machine", "name": NAMES["machine"], "from": "system", "instantiation": "true",
        "printer_model": "Bambu Lab P1S", "printer_variant": "0.4", "nozzle_diameter": ["0.4"],
        "nozzle_type": ["stainless_steel", "stainless_steel"],
        "printable_area": ["0x0", "256x0", "256x256", "0x256"], "printable_height": "250",
        "bed_exclude_area": ["0x0", "18x0", "18x28", "0x28"],
        "machine_start_gcode": "G90\nM140 S[bed_temperature_initial_layer_single]\nM104 S[nozzle_temperature_initial_layer]\nG1 X20 Y20 F6000\nG28\nM109 S[nozzle_temperature_initial_layer]\n",
        "machine_end_gcode": "G91\nG1 Z2 F600\nG90\nG1 X20 Y20 F6000\nM104 S0\nM140 S0\n",
        "time_lapse_gcode": "M73 P50\n", "change_filament_gcode": "M400\n",
        "default_print_profile": NAMES["process"], "default_filament_profile": [NAMES["filament"]],
    }
    process = {"type": "process", "name": NAMES["process"], "from": "system", "instantiation": "true",
               "layer_height": "0.2", "compatible_printers": [NAMES["machine"]]}
    filament = {"type": "filament", "name": NAMES["filament"], "from": "system", "instantiation": "true",
                "filament_type": ["PLA"], "nozzle_temperature": ["220", "220"],
                "nozzle_temperature_initial_layer": ["220", "220"],
                "filament_start_gcode": ["; fixture filament start\\n"],
                "filament_end_gcode": ["; fixture filament end\\n"],
                "compatible_printers": [NAMES["machine"]]}
    for filename, data in (("machine", machine), ("process", process), ("filament", filament)):
        (bbl / filename / f"{filename}.json").write_text(json.dumps(data))
    # One invented profile for each other checked material, under the names facts.P1S selects.
    for material, name in FILAMENTS.items():
        index["filament_list"].append({"name": name, "sub_path": f"filament/{material}.json"})
        (bbl / "filament" / f"{material}.json").write_text(json.dumps({
            **filament, "name": name, "filament_type": [material],
            "filament_start_gcode": [f"; fixture {material} filament start\\n"]}))
    (root / "profiles" / "BBL.json").write_text(json.dumps(index))
    return root


def identity(root=None):
    root = resources(root or Path(tempfile.mkdtemp(prefix="p1s-resources-")))
    upstream, version = profiles.flatten(root, NAMES)
    return {"label": "Print Buddy invented P1S fixture (not OrcaSlicer)", "vendor_version": version,
            "sha256": {kind: profiles.sha256(upstream[kind]) for kind in profiles.KINDS},
            "filaments": {name: profiles.sha256(data)
                          for name, data in profiles.flatten_filaments(root, FILAMENTS.values()).items()}}


def fixture_facts():
    root = resources(Path(tempfile.mkdtemp(prefix="p1s-resources-")))
    return {**facts.P1S, "orca_identity": facts.Fact((identity(root),), "TEST", False)}


def trust(setattr=None):
    value = fixture_facts()
    if setattr is None:
        P1SAdapter.facts = value
    else:
        setattr(P1SAdapter, "facts", value)


def install(adapter, root=None):
    root = resources(root or Path(tempfile.mkdtemp(prefix="p1s-resources-")))
    profiles.install(adapter, root)
    return adapter.pinned_templates_folder()

