"""Bambu Lab vendor profile: offline only. Print Buddy slices, validates and packages; a person moves the file.

No live connection: no MQTT, LAN FTPS or cloud client, so no telemetry, upload or print control. The
handoff format is a .gcode.3mf (Metadata/plate_N.gcode plus its MD5) in Bambu Studio's export layout; no
printer, Bambu Studio, Bambu Handy or SD-card run of it has been tested yet. Bambu Studio is the default
slicer for every Bambu Lab printer; [printer] slicer: orca opts back into Orca.
Every G-code line must be a plain G/M/T code: Klipper or ZMod start code is refused (base.start_code_problems).

Printer modules (bambulab/a1.py, later p1s.py) define MODEL = PrinterModel(..., vendor=VENDOR) with the
printer's own bed, height, nozzles and adapter; their sourced facts and profile identity are in facts.py,
their G-code checks in guard.py. Their Orca templates are generated from the operator's own OrcaSlicer
(profiles.py); none are shipped. See docs/PRINTER-PROFILES.md and docs/BAMBU-LAB.md.
"""
from ..base import SHARED_SETTINGS, VendorProfile, merge_settings

VENDOR = VendorProfile(
    key="bambulab",
    label="Bambu Lab",
    connection="offline",
    start_code="bambu",
    slicers=("bambu_studio", "orca"),  # Bambu Studio first: the default for every Bambu Lab printer
    file_formats=(".gcode.3mf", ".gcode"),
    delivery=("discord", "bambu_handoff"),
    default_delivery=("discord", "bambu_handoff"),
    settings=merge_settings(SHARED_SETTINGS, {
        "printer": {
            "firmware": "Bambu Lab (offline handoff only)",
            # Unused: the vendor start/end G-code comes from the Orca template. Kept valid for settings checks.
            "start_macro": "PRINT_START",
            "end_macro": "PRINT_END",
        },
    }),
)
