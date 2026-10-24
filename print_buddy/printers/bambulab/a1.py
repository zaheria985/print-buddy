"""Bambu Lab A1 (inherits bambulab VENDOR). Phase 1: offline slicing, fit checks and G-code validation only.

Quirks: the vendor start/end G-code parks the head off the bed on the wiper (X-48.2) and purge lane, so only
the Orca toolpath is held to the 256 mm bed; the vendor blocks are matched line by line against the pinned
templates instead. The AMS Lite is not driven: single filament only. Bambu Studio output is not verified for
the A1 and is refused. Sourced values: facts.py; docs/BAMBU-LAB.md. Templates: generated on the operator's
machine from their own OrcaSlicer into <state>/orca-profiles/bambulab_a1 (profiles.py); none are shipped.
"""
from ..base import PrinterModel
from . import VENDOR, facts
from .adapter import BambuOfflineAdapter


class A1Adapter(BambuOfflineAdapter):
    facts = facts.A1


MODEL = PrinterModel(
    key="bambulab.a1",
    label="Bambu Lab A1",
    adapter=A1Adapter,
    templates="bambulab_a1",
    generated_templates=True,
    vendor=VENDOR,
    # Proposed in config/protected-settings-baseline.bambulab.a1.proposed.json; not approved by this change.
    baseline="protected-settings-baseline.bambulab.a1.json",
    # Only the 0.4 mm vendor profile is templated; 0.2, 0.6 and 0.8 mm hotends exist but are refused here.
    supported_nozzle_sizes=(0.4,),
    # Stock stainless steel, or the hardened steel hotend Bambu requires for CF/GF and glow filaments.
    supported_nozzle_types=("stainless_steel", "hardened_steel"),
    settings={
        "printer": {
            "name": "Bambu Lab A1",
            "bed_mm": list(facts.A1["bed_mm"].value),
            "max_print_height_mm": facts.A1["max_print_height_mm"].value,
            "nozzle_diameter_mm": 0.4,
            "nozzle_type": "stainless_steel",
            "ifs_slots": 1,
        },
    },
)
