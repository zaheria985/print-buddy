"""Bambu Lab printers: Orca slicing and G-code validation, plus opt-in read-only LAN telemetry.

VENDOR (bambulab/__init__.py) is the authority for connection ("bambulab_lan"), start code, slicers and
delivery. The LAN path (client.py) exists only when the operator configures [bambu] lan_host and
lan_serial and supplies the LAN access code through an environment variable; without that configuration
the adapter behaves exactly as the original offline adapter did. Stage (b) added read-only telemetry;
stage (c) adds the FTPS upload transport (client.py) behind `upload()` — print start and control still
raise LiveUnsupported, and `printer` delivery is not offered until the print-start stage.
Each printer subclasses this adapter with its facts (see a1.py). Deliberately imports nothing from the
ZMod/AD5X code and has no Moonraker or cloud client.
"""
import json
import os
from pathlib import Path

from ..base import MultiColorUnsupported, PrinterAdapter
from . import guard, profiles
from .client import BambuLanClient, BambuLanError, signature_required


class LiveUnsupported(RuntimeError):
    """A Moonraker-shaped live call reached a Bambu Lab printer; Bambu uses its LAN path instead."""


# Bambu `gcode_state` values -> Print Buddy's normalized state vocabulary (the Klipper/OctoPrint shape).
_GCODE_STATES = {
    "IDLE": "standby", "INIT": "standby", "OFFLINE": "unknown", "UNKNOWN": "unknown",
    "RUNNING": "printing", "PREPARE": "printing", "SLICING": "printing",
    "PAUSE": "paused", "FINISH": "complete", "FAILED": "error",
}
_BUSY_STATES = {"RUNNING", "PREPARE", "SLICING", "PAUSE"}


class BambuAdapter(PrinterAdapter):
    multi_slot = False
    transport = "bambulab_lan"
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
        return LiveUnsupported(f"{self.label}: {what} is not supported over Moonraker; Bambu Lab printers "
                               "use the LAN path (status, upload and print start are separate calls). "
                               "Nothing was sent.")

    # -- LAN configuration -------------------------------------------------
    def _lan(self):
        return (self.config.get("bambu") or {})

    def lan_configured(self) -> bool:
        """True when [bambu] lan_host and lan_serial are both set (the access code is a secret, checked by client())."""
        lan = self._lan()
        return bool(str(lan.get("lan_host") or "").strip() and str(lan.get("lan_serial") or "").strip())

    def access_code(self) -> str:
        """The LAN access code from the environment variable [bambu] lan_access_code_env names. Never from config."""
        name = str(self._lan().get("lan_access_code_env") or "BAMBU_LAN_PASSWORD")
        return os.environ.get(name, "")

    def client(self, **overrides):
        """A LAN telemetry client for the configured printer. Raises BambuLanError when unconfigured."""
        if not self.lan_configured():
            raise BambuLanError(f"{self.label}: LAN telemetry is not configured; set [bambu] lan_host and "
                                "lan_serial and supply the LAN access code via the environment")
        return BambuLanClient(str(self._lan()["lan_host"]).strip(), str(self._lan()["lan_serial"]).strip(),
                              self.access_code(), **overrides)

    def upload(self, local_path, remote_name=None):
        """Upload a `.gcode.3mf` package to the printer's storage root (FTPS, size-verified).

        Stage (c): this is the transport the print-start stage builds on. By itself it
        starts nothing, and no delivery method calls it yet.
        """
        if not str(local_path).endswith(".gcode.3mf"):
            raise BambuLanError(f"{self.label}: only a packaged .gcode.3mf can be uploaded to a Bambu Lab "
                                "printer; slice and package the job first (the Bambu package delivery does this)")
        return self.client().upload(local_path, remote_name)

    def start_print(self, remote_name, plate="Metadata/plate_1.gcode"):
        """Send the print-start command for an uploaded package. Gates live in the print path."""
        return self.client().start_print(remote_name, plate)

    def control(self, action):
        """Pause, resume or stop the current print over LAN MQTT."""
        return self.client().control(action)

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

    def filament_template(self, templates, material):
        """The checked vendor filament profile for material (profiles.filament_template), or ProfileError.

        In the verified generated folder the profile is re-verified. An [orca] templates override may carry its own
        filaments/<MATERIAL>.json or a filament.json of that material; the guard still holds the output to the
        verified profiles. A material with no profile is refused either way.
        """
        templates = Path(templates)
        if templates.resolve() == self.pinned_templates_folder().resolve():
            return profiles.filament_template(self, material, templates)
        key = profiles.material_key(material)
        own = templates / profiles.FILAMENTS / f"{key}.json"
        if own.is_file():
            return own
        default = templates / "filament.json"
        kind = profiles._first(json.loads(default.read_text()).get("filament_type"))
        if profiles.material_key(kind) != key:
            raise profiles.ProfileError(f"{self.label}: the [orca] templates folder {templates} has no {key} filament "
                                        f"profile (its filament.json is {kind}). Nothing was sliced")
        return default

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
        """Hardware-record checks, plus live checks when `status` is a LAN snapshot (or its print payload).

        With no live status this is exactly the original offline behaviour. With a snapshot: a busy or
        errored printer is not ready, an active print error / HMS entry is reported, and the firmware
        signature bit (Developer Mode off) is reported because it blocks every future write.
        """
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
        payload = _print_payload(status)
        if payload is not None:
            gcode_state = str(payload.get("gcode_state") or "").upper()
            if gcode_state in _BUSY_STATES:
                problems.append(f"printer is busy ({gcode_state.lower()})")
            if payload.get("print_error"):
                problems.append(f"printer reports a print error (code {payload['print_error']})")
            hms = [h for h in (payload.get("hms") or []) if isinstance(h, dict) and h.get("attr", 0)]
            if hms:
                problems.append(f"printer reports {len(hms)} active HMS error(s)")
            if signature_required(payload):
                problems.append("printer firmware requires signed MQTT commands (Developer Mode is off): "
                                "telemetry works, but upload and print control would be blocked")
        return problems

    def moonraker_objects(self):
        raise self._unsupported("Moonraker telemetry (this printer reports over Bambu LAN MQTT)")

    def normalize_status(self, raw):
        """A LAN snapshot (client.snapshot()) or bare print payload -> the common status shape.

        Anything else (including a Moonraker-shaped dict) normalizes to unknown, as before.
        Temperatures come from the classic fields; newer firmware's packed `device.*.info.temp`
        words are not decoded in this stage.
        """
        payload = _print_payload(raw)
        if payload is None:
            return {"state": "unknown", "filename": "", "progress": None, "hotend_c": None, "bed_c": None}
        percent = payload.get("mc_percent")
        remaining = payload.get("mc_remaining_time")
        return {
            "state": _GCODE_STATES.get(str(payload.get("gcode_state") or "").upper(), "unknown"),
            "filename": payload.get("subtask_name") or payload.get("gcode_file") or "",
            "progress": percent / 100 if isinstance(percent, (int, float)) and not isinstance(percent, bool) else None,
            "hotend_c": _number(payload.get("nozzle_temper")),
            "bed_c": _number(payload.get("bed_temper")),
            "hotend_target_c": _number(payload.get("nozzle_target_temper")),
            "bed_target_c": _number(payload.get("bed_target_temper")),
            "remaining_min": remaining if isinstance(remaining, (int, float)) and not isinstance(remaining, bool) else None,
            "layer": payload.get("layer_num"),
            "total_layer": payload.get("total_layer_num"),
            "writes_blocked": signature_required(payload),
        }


def _print_payload(raw):
    """The Bambu `print` payload inside a snapshot dict, or `raw` itself when it is one. None otherwise."""
    if not isinstance(raw, dict):
        return None
    if isinstance(raw.get("print"), dict):
        return raw["print"]
    if "gcode_state" in raw or "mc_percent" in raw:
        return raw
    return None


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


# Backwards-compatible name: the A1/P1S modules and older tests import the offline-era class name.
BambuOfflineAdapter = BambuAdapter
