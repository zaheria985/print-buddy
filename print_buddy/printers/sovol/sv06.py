"""Sovol SV06 on stock Klipper: single extruder, PRINT_START/PRINT_END macros, no vendor API.

Quirks: multi-color and multi-plate sources are refused. The bed and height below are the values the SV06
has always sliced with here (they match templates/orca/sovol_sv06/machine.json). Templates: templates/orca/sovol_sv06.
"""
from ..base import PrinterModel
from . import VENDOR
from ..klipper.adapter import KlipperAdapter

MODEL = PrinterModel(
    key="sovol.sv06",
    label="Sovol SV06 (Klipper)",
    adapter=KlipperAdapter,
    templates="sovol_sv06",
    baseline="protected-settings-baseline.sovol.sv06.json",
    supported_nozzle_sizes=(0.2, 0.4, 0.6, 0.8),
    supported_nozzle_types=("brass", "hardened_steel", "ruby", "tungsten_carbide", "diamond"),
    vendor=VENDOR,
    # Printer-specific defaults; firmware, macros, margins, prime tower and cold pull come from VENDOR.
    settings={
        "printer": {
            "name": "Sovol SV06",
            "bed_mm": [220.0, 220.0],
            "max_print_height_mm": 220.0,
            "nozzle_diameter_mm": 0.4,
            "ifs_slots": 1,
        },
    },
)
