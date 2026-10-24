"""OctoPrint vendor profile: single-extruder Marlin printers over OctoPrint's REST API, slicer-owned start code."""
from ..base import SHARED_SETTINGS, VendorProfile, merge_settings

VENDOR = VendorProfile(
    key="octoprint",
    label="OctoPrint",
    connection="octoprint",
    start_code="marlin",
    settings=merge_settings(SHARED_SETTINGS, {
        # Marlin has no start macros; the names stay valid for the settings check and are not sent.
        "printer": {"firmware": "Marlin", "start_macro": "PRINT_START", "end_macro": "PRINT_END"},
    }),
)
