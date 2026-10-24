"""Flashforge AD5X on ZMod Klipper: 4 slot IFS, native screen, stock firmware /detail.

Quirks: START_PRINT/END_PRINT come from the Orca template (the macro keys below are unused), the printer
screen owns the Z offset and mesh, and the physical sheet is 235 mm while Orca and the steppers allow 220.
ZMod-only code lives in printers/flashforge/zmod. Templates: templates/orca/flashforge_ad5x.
"""
from ..base import PrinterModel
from . import VENDOR
from .adapter import ZmodAdapter

MODEL = PrinterModel(
    key="flashforge.ad5x",
    label="Flashforge AD5X (ZMod)",
    adapter=ZmodAdapter,
    templates="flashforge_ad5x",
    supported_nozzle_sizes=(0.25, 0.4, 0.6, 0.8),
    supported_nozzle_types=("stainless_steel", "hardened_steel"),
    vendor=VENDOR,
    # Printer-specific defaults; firmware, macro names, margins, prime tower and cold pull come from VENDOR.
    settings={
        "printer": {
            "name": "Flashforge AD5X",
            # Usable print area, matching printable_area in the template machine.json.
            "bed_mm": [220.0, 220.0],
            "max_print_height_mm": 220.0,
            # Orca templates are tuned for this nozzle. Slicing refuses any other size.
            "nozzle_diameter_mm": 0.4,
            # Filament slots in the IFS.
            "ifs_slots": 4,
        },
    },
)
