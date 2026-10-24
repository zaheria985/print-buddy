"""Bambu Lab printers, offline only: Orca slicing and G-code validation, no transport to the printer.

VENDOR (bambulab/__init__.py) is the authority for connection ("offline"), start code, slicers and delivery.
This adapter only answers the PrinterAdapter contract for an offline printer: every live call raises
LiveUnsupported, and gcode_problems runs the printer's guard. Each printer subclasses it with its facts
(see a1.py). Deliberately imports nothing from the ZMod/AD5X code and has no Moonraker, MQTT, FTPS or cloud client.
"""
import json

from ..base import MultiColorUnsupported, PrinterAdapter
from . import guard, profiles


class LiveUnsupported(RuntimeError):
    """Print Buddy has no live connection to this printer: no telemetry, upload or print control."""


class BambuOfflineAdapter(PrinterAdapter):
    multi_slot = False
    facts: dict = {}  # the printer's sourced facts (facts.py), set by each printer's subclass
    # Slicers this printer's guard accepts. Bambu Studio is the Bambu Lab default (owner decision); every line
    # of its output still passes the same guard, which refuses anything that is not plain G/M/T code.
    verified_slicers = ("bambu_studio", "orca")

    def fact(self, name):
        return self.facts[name].value

    @property
    def slot_count(self):
        return 1

    @property
    def motion_envelope_mm(self):
        return self.fact("motion_envelope_mm")

    def _unsupported(self, what):
        return LiveUnsupported(f"{self.label}: {what} is not supported; Print Buddy only slices and validates "
                               "G-code for this printer. Nothing was sent.")

    # slicer: the vendor start/end G-code is used as is; Print Buddy adds and changes nothing. The only accepted
    # start and end are the ones in the verified generated profiles (profiles.py), so an [orca] templates
    # override with other vendor code is refused before Orca runs.
    def _vendor_gcode(self, key):
        return json.loads((self.pinned_templates() / "machine.json").read_text())[key]

    def machine_start_gcode(self, template_start, mesh="auto"):
        guard.require_bambu_code(template_start, f"{self.label} start G-code")
        if template_start != self._vendor_gcode("machine_start_gcode"):
            raise guard.ForeignStartCode(f"{self.label} start G-code is not the {self.fact('printer_model')} vendor "
                                         "sequence from the verified profiles; check [orca] templates")
        return template_start

    def machine_end_gcode(self, template_end):
        guard.require_bambu_code(template_end, f"{self.label} end G-code")
        if template_end != self._vendor_gcode("machine_end_gcode"):
            raise guard.ForeignStartCode(f"{self.label} end G-code is not the {self.fact('printer_model')} vendor "
                                         "sequence from the verified profiles; check [orca] templates")
        return template_end

    def start_plan(self, bed_temp, nozzle_temp, *, mesh="auto"):
        # Orca fills the vendor placeholders; these are the template lines the printer runs first.
        return self._vendor_gcode("machine_start_gcode").splitlines()

    def pinned_templates_folder(self):
        """Where this model's profiles are generated: <state>/orca-profiles/<templates>, never the repository."""
        from ... import settings
        return settings.generated_templates_dir(self.model, self.config)

    def pinned_templates(self):
        """The verified generated templates (profiles.require); an [orca] templates override is never the reference.

        Raises profiles.ProfileError when they are missing, edited or not a checked OrcaSlicer version.
        """
        return profiles.require(self)

    def gcode_problems(self, text, state=None):
        return guard.gcode_problems(text, self, state=state)

    def moonraker_start_commands(self, remote_filename):
        raise self._unsupported("starting a print")

    def normalize_tool_mapping(self, mapping, state):
        tools = {str(k) for k in (mapping or {"0": 1})}
        if tools != {"0"}:
            raise MultiColorUnsupported(
                f"{self.label}: {self.fact('ams')[0]} multi-color is not supported; this job uses tools "
                f"{sorted(tools)}. Slice it single-color.")
        return {"0": 1}

    def readiness_problems(self, state, status, selection=None, **kw):
        """Offline record checks only. There is no live status to read."""
        problems = []
        state = state or {}
        bed, height = self.fact("bed_mm"), self.fact("max_print_height_mm")
        record_bed = state.get("bed_mm", bed)
        if any(float(v) > limit for v, limit in zip(record_bed, bed)):
            problems.append(f"recorded bed {record_bed[0]:g}x{record_bed[1]:g} mm exceeds the {self.label} "
                            f"{bed[0]:g}x{bed[1]:g} mm printable area")
        if float(state.get("max_print_height_mm", height)) > height:
            problems.append(f"recorded height {float(state['max_print_height_mm']):g} mm exceeds the {self.label} "
                            f"{height:g} mm limit")
        nozzle = state.get("nozzle") or {}
        if nozzle.get("diameter_mm") not in self.supported_nozzle_sizes:
            problems.append(f"recorded nozzle {nozzle.get('diameter_mm')!r} mm has no {self.label} template "
                            f"(supported: {', '.join(f'{s:g}' for s in self.supported_nozzle_sizes)})")
        from ...core.nozzle import NozzleError, normalize_nozzle_type
        try:
            kind = normalize_nozzle_type(nozzle.get("type") or "")
        except NozzleError:
            kind = nozzle.get("type")
        if kind not in self.supported_nozzle_types:
            problems.append(f"recorded nozzle type {nozzle.get('type')!r} is not an {self.label} option "
                            f"({', '.join(self.supported_nozzle_types)})")
        return problems

    def moonraker_objects(self):
        raise self._unsupported("live telemetry")

    def normalize_status(self, raw):
        return {"state": "unknown", "filename": "", "progress": None, "hotend_c": None, "bed_c": None}
