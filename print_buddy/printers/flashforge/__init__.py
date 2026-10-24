"""Flashforge vendor profile: ZMod Klipper behind Moonraker, START_PRINT ... MESH= start code, OrcaSlicer, .gcode."""
from ..base import SHARED_SETTINGS, VendorProfile, merge_settings

VENDOR = VendorProfile(
    key="flashforge",
    label="Flashforge",
    connection="moonraker",
    start_code="zmod",
    settings=merge_settings(SHARED_SETTINGS, {
        "printer": {
            "firmware": "ZMod Klipper",
            # ZMod start/end live in each printer's Orca template; these names only document them.
            "start_macro": "START_PRINT",
            "end_macro": "END_PRINT",
        },
    }),
)
