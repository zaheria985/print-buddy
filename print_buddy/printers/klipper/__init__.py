"""Klipper vendor profile: stock Klipper behind Moonraker, PRINT_START/PRINT_END macros, OrcaSlicer, .gcode."""
from ..base import SHARED_SETTINGS, VendorProfile, merge_settings

VENDOR = VendorProfile(
    key="klipper",
    label="Klipper",
    connection="moonraker",
    start_code="klipper_macro",
    settings=merge_settings(SHARED_SETTINGS, {
        "printer": {"firmware": "Klipper", "start_macro": "PRINT_START", "end_macro": "PRINT_END"},
    }),
)
