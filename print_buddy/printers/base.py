"""Vendor profile, printer model and adapter contract.

Layout: vendor first, then printer. One package per vendor under print_buddy/printers/, one module per
printer model inside it. The [printer] model key is "<vendor>.<printer>", the package and module names.

    flashforge/__init__.py   VENDOR: Moonraker, ZMod START_PRINT start code, OrcaSlicer, .gcode
    flashforge/ad5x.py       MODEL for "flashforge.ad5x"; adapter.py (ZMod adapter); zmod/ (AD5X-only code)
    klipper/__init__.py      VENDOR: Moonraker, PRINT_START/PRINT_END macros, OrcaSlicer, .gcode
    klipper/generic.py       MODEL for "klipper.generic"; adapter.py (the stock Klipper adapter)
    sovol/__init__.py        VENDOR: the Klipper vendor profile under the Sovol name
    sovol/sv06.py            MODEL for "sovol.sv06" on the stock Klipper adapter
    octoprint/__init__.py    VENDOR: OctoPrint REST, plain Marlin start code, OrcaSlicer, .gcode
    octoprint/marlin.py      MODEL for "octoprint.marlin"
    bambulab/__init__.py     VENDOR: no live connection, Bambu start code, OrcaSlicer or Bambu Studio,
                             .gcode.3mf handoff. Printer modules (A1, later P1S) drop in beside it.

A vendor package defines VENDOR = VendorProfile(...): what every printer of that vendor shares, namely
the connection type, the start G-code family, the slicers it may use, the file formats it hands over,
the delivery methods offered after a slice, and shared setting defaults (firmware, macros, layout
margins, prime tower, cold pull). A printer module defines MODEL = PrinterModel(..., vendor=VENDOR):
only what differs per printer (bed, height, printable-area exclusions, nozzle and material limits,
slots, AMS), its adapter class and its Orca template folder under templates/orca. The printer's
settings are merged over its vendor's settings, key by key, printer wins.

printers/__init__ finds every module with a MODEL, so adding a printer is one file plus templates, and
adding a vendor is one package with an __init__ defining VENDOR. Vendor code that is not a model lives
in helper modules without a MODEL or in a subpackage. See docs/PRINTER-PROFILES.md.

Settings merge, later wins key by key: vendor defaults, printer defaults, YAML home config, selected YAML/TOML local config, environment.
config/home.yaml picks the model with printer.model: flashforge.ad5x.

Where printer specifics live, and which responsibilities sit behind PrinterAdapter:

  responsibility                 flashforge.ad5x (ZMod)                  klipper.generic / sovol.sv06
  -----------------------------  --------------------------------------  ----------------------------
  build volume, margins          model settings                          same keys, 1 slot
  slot count / multi-slot        [printer] ifs_slots (IFS, 4)            1, single extruder
  Orca template folder           templates/orca/flashforge_ad5x          templates/orca/<model folder>
  Orca start G-code              START_PRINT ... MESH=<mesh> + M190/     PRINT_START BED= HOTEND=
                                 M104 preamble (adapter)                 (macro names from settings)
  start plan for a job           START_PRINT BED_TEMP EXTRUDER_TEMP      PRINT_START BED HOTEND
  tool -> slot mapping           zmod.multicolor_contract                T0 only; multicolor refused
  readiness check                model_job.check_filament_readiness      print_stats / heater sanity
  screen / slot sync             zmod.screen (CHANGE_ZCOLOR)             none (returns None)
  native status API              zmod.stock (/detail)                    none (returns None)
  telemetry normalization        Moonraker + zmod_color                  plain Moonraker objects

Core reaches ZMod code only through adapter.native (the printers.flashforge.zmod package): finalize,
embedded_mapping, normalize_mapping, describe_mapping, validate_materials, the stock /detail readers,
native_startup and native_idle. A printer without them raises MultiColorUnsupported from `native`.

Runtime decisions that follow the adapter (core/model_job, printer_state, orca_adapter, the listener):
  - multi_slot False (generic/sovol): no IFS selection or spool-slot validation, no multicolor finalization,
    no native_startup/native_idle, no mapped-native launch; slicing refuses multicolor and multi-plate sources
    with MultiColorUnsupported (a REFUSED line).
  - has_native_screen False: no display_off/LOAD_ZOFFSET/Adventurer5M offset authority, no ZMod MD5, no stock
    /detail polling, no screen-cancel firmware restart, no mesh-profile readiness (the printer owns its mesh).
  - readiness (readiness_problems), the start request (moonraker_start_commands) and the multicolor refusal
    (normalize_tool_mapping) are adapter calls on every model; flashforge.ad5x delegates to the old functions.
  - JOB.json records `printer_model` when sliced; print-now refuses a job whose model differs from
    [printer] model. A job without the field is a flashforge.ad5x job.

Still AD5X-coupled code in shared modules, reached only when multi_slot / has_native_screen. It is mixed
with printer-neutral logic (Spoolman, Discord, plate handling), so it stays until a second multi-slot model
exists to show the seam:
  - core/model_job: IFS selection and filament readiness, mapped-native launch (print_mapped_native), the
    zmod_color / display_off telemetry read, thin delegators to zmod.stock
  - core/prepared_intake, core/plate_merge, core/color_geometry, core/source_colors: ifs_slots bounds, the
    four-slot mapping and prime tower placement for IFS multicolor plates
  - core/orca_adapter: the START_PRINT/MESH start G-code goes through the adapter; IFS_SLOTS guard bounds
    and the tool-mapped prime tower retry remain
  - core/spool_lifecycle, core/clear_job: IFS slot reconciliation, native idle cancel handling
  - hermes/spool_sync (Spoolman binding), moonraker_event_listener (IFS presence poll, runout pause),
    print_control (native /detail cancel), print_next and the Discord IFS prompts: "AD5X" wording
"""
from __future__ import annotations

import copy
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class MultiColorUnsupported(RuntimeError):
    """The selected printer model cannot run this multi-color job."""


@dataclass(frozen=True)
class BuildVolume:
    bed_mm: tuple[float, float]
    max_height_mm: float
    single_copy_edge_mm: float
    multi_copy_edge_mm: float


# How Print Buddy reaches a printer. "offline" means it never does: it slices and validates, a person
# moves the file.
CONNECTIONS = ("moonraker", "octoprint", "offline")
# Start G-code families. Each printer runs only its own; see start_code_problems.
START_CODES = ("zmod", "klipper_macro", "marlin", "bambu")
# Slicers a vendor may pick as its primary. OrcaSlicer is the default everywhere.
SLICERS = ("orca", "bambu_studio")
# What can happen to a validated slice, besides nothing:
#   printer        start it on the printer through the vendor connection, behind the per-job bed-clear button
#   discord        attach the sliced files to the Discord thread
#   bambu_handoff  package a .gcode.3mf for Bambu Studio, Bambu Handy or the SD card; nothing is uploaded
DELIVERY_METHODS = ("printer", "discord", "bambu_handoff")


# Margins, prime tower and cold pull every current vendor shares. A vendor or printer overrides any key.
SHARED_SETTINGS = {
    "layout": {
        # Distance from the bed edge for a lone copy and for multi copy plates.
        "single_copy_edge_mm": 1.0,
        "multi_copy_edge_mm": 3.0,
        # Gaps when several source plates are merged onto one bed.
        "merge_edge_margin_mm": 8.0,
        "merge_spacing_mm": 10.0,
    },
    "prime_tower": {
        "size_mm": 45.0,
        # Clearance between the tower and any part.
        "part_clearance_mm": 8.0,
        # Closest the tower may sit to the bed edge.
        "edge_mm": 10.0,
        # Extra room reserved for the tower when merging plates.
        "merge_slack_mm": 5.0,
    },
    # Profile name = [hot C, cold C]
    "cold_pull": {"pla_trial": [220, 90], "petg": [245, 100]},
}


def merge_settings(base: dict, extra: dict) -> dict:
    """Deep copy of base with extra merged over it, key by key."""
    out = copy.deepcopy(base)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = merge_settings(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


@dataclass(frozen=True)
class VendorProfile:
    """What every printer of one vendor shares. Printer models inherit it and override settings."""

    key: str  # the vendor package name, the part of "vendor.printer" before the dot
    label: str
    connection: str  # one of CONNECTIONS
    start_code: str  # one of START_CODES
    # Allowed [printer] slicer values; the first is the default.
    slicers: tuple[str, ...] = ("orca",)
    # Tried once, isolated, after an OrcaSlicer execution failure. None disables the fallback.
    fallback_slicer: str | None = "bambu_studio"
    # File formats this vendor's printers take, primary first. Delivery hands over the primary format.
    file_formats: tuple[str, ...] = (".gcode",)
    # Delivery methods offered after a slice (DELIVERY_METHODS), and the ones on by default.
    # [delivery] methods in config picks a subset of `delivery`.
    delivery: tuple[str, ...] = ("printer", "discord")
    default_delivery: tuple[str, ...] = ("printer",)
    # Shared defaults for [printer], [layout], [prime_tower] and [cold_pull]; each printer model overrides.
    settings: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.connection not in CONNECTIONS:
            raise ValueError(f"vendor {self.key}: connection must be one of {CONNECTIONS}, got {self.connection!r}")
        if self.start_code not in START_CODES:
            raise ValueError(f"vendor {self.key}: start_code must be one of {START_CODES}, got {self.start_code!r}")
        if not self.slicers or any(s not in SLICERS for s in self.slicers):
            raise ValueError(f"vendor {self.key}: slicers must be from {SLICERS}, got {self.slicers!r}")
        unknown = [m for m in (*self.delivery, *self.default_delivery) if m not in DELIVERY_METHODS]
        if unknown:
            raise ValueError(f"vendor {self.key}: unknown delivery methods {unknown}")
        if not set(self.default_delivery) <= set(self.delivery):
            raise ValueError(f"vendor {self.key}: default_delivery must be a subset of delivery")
        if "printer" in self.delivery and self.connection == "offline":
            raise ValueError(f"vendor {self.key}: an offline vendor cannot offer printer delivery")
        if "bambu_handoff" in self.delivery and self.start_code != "bambu":
            raise ValueError(f"vendor {self.key}: bambu_handoff needs Bambu start code")

    def derive(self, **changes) -> "VendorProfile":
        """A vendor that shares this one's profile, e.g. a brand whose printers run stock Klipper."""
        from dataclasses import replace
        return replace(self, **changes)


@dataclass(frozen=True)
class PrinterModel:
    """One printer of one vendor: its own defaults, its adapter and its Orca template folder."""

    key: str  # "brand.model", the [printer] model value
    label: str
    adapter: type  # PrinterAdapter subclass
    templates: str  # folder under print_buddy/templates/orca
    # Printer-specific defaults for [printer], [layout], [prime_tower] and [cold_pull], merged over the
    # vendor's settings. Config files and the environment override both.
    settings: dict
    # Protected-settings baseline file under config/; None uses the shared protected-settings-baseline.json.
    baseline: str | None = None
    # Supported nozzle diameters in mm (e.g. 0.25, 0.4, 0.6, 0.8)
    supported_nozzle_sizes: tuple[float, ...] = (0.2, 0.25, 0.4, 0.6, 0.8)
    # Supported nozzle types (e.g. brass, hardened_steel, ruby, tungsten_carbide, diamond)
    supported_nozzle_types: tuple[str, ...] = (
        "brass",
        "hardened_steel",
        "stainless_steel",
        "ruby",
        "tungsten_carbide",
        "diamond",
    )
    # The vendor this printer inherits from. printers/__init__ fills it from the vendor package when a
    # model module leaves it out, and refuses a vendor that does not match the package.
    vendor: VendorProfile | None = None

    @property
    def vendor_key(self) -> str:
        return self.key.split(".", 1)[0]

    @property
    def printer_key(self) -> str:
        return self.key.split(".", 1)[1]

    def defaults(self) -> dict[str, Any]:
        """Settings this model contributes: vendor settings, then the printer's, then identity keys."""
        out = merge_settings(self.vendor.settings if self.vendor else {}, self.settings)
        printer = out.setdefault("printer", {})
        printer["model"] = self.key
        out.setdefault("orca", {})["templates"] = f"print_buddy/templates/orca/{self.templates}"
        if self.vendor is not None:
            printer["vendor"] = self.vendor.key
            printer["slicer"] = self.vendor.slicers[0]
            out["delivery"] = {"methods": list(self.vendor.default_delivery)}
        return out


class PrinterAdapter(ABC):
    """What call sites need from a printer model. Build with a settings dict (default: process settings)."""

    multi_slot: bool = False
    # ZMod-style native screen: display_off modes, saved/screen Z offset authority, MD5 gcode, stock /detail.
    has_native_screen: bool = False

    def __init__(self, config: dict[str, Any] | None = None, model: PrinterModel | None = None):
        if config is None:
            from .. import settings
            config = settings.current()
        if model is None:
            from . import model as _model
            model = _model(config["printer"]["model"])
        self.config = config
        self.model = model

    @property
    def key(self) -> str:
        return self.model.key

    @property
    def label(self) -> str:
        return self.model.label

    @property
    def baseline_path(self) -> Path | None:
        """This model's protected-settings baseline, or None for the shared file."""
        if self.model.baseline is None:
            return None
        from .. import settings
        return settings.ROOT / "config" / self.model.baseline

    @property
    def native(self):
        """ZMod/native helpers (printers.flashforge.zmod); stock Klipper has none."""
        raise MultiColorUnsupported(f"{self.label} has no ZMod/native support")

    @property
    def supported_nozzle_sizes(self) -> tuple[float, ...]:
        return self.model.supported_nozzle_sizes

    @property
    def supported_nozzle_types(self) -> tuple[str, ...]:
        return self.model.supported_nozzle_types

    # geometry and slots
    def build_volume(self) -> BuildVolume:
        p, lay = self.config["printer"], self.config["layout"]
        return BuildVolume((float(p["bed_mm"][0]), float(p["bed_mm"][1])), float(p["max_print_height_mm"]),
                           float(lay["single_copy_edge_mm"]), float(lay["multi_copy_edge_mm"]))

    @property
    @abstractmethod
    def slot_count(self) -> int: ...

    # slicer
    def templates_dir(self) -> Path:
        """Folder with machine.json, process.json and filament.json for Orca (an [orca] templates override applies)."""
        from .. import settings
        if self.config["printer"].get("model") == self.key:
            return Path(self.config["orca"]["templates"])
        return settings.ROOT / "print_buddy" / "templates" / "orca" / self.model.templates

    @abstractmethod
    def machine_start_gcode(self, template_start: str, mesh: str = "auto") -> str:
        """Orca machine_start_gcode for this printer; template_start is the template's own text."""

    @abstractmethod
    def machine_end_gcode(self, template_end: str) -> str: ...

    @abstractmethod
    def start_plan(self, bed_temp: float, nozzle_temp: float, *, mesh: str = "auto") -> list[str]:
        """Resolved start G-code lines for one job (what the printer executes first)."""

    @abstractmethod
    def moonraker_start_commands(self, remote_filename: str) -> list[dict[str, Any]]:
        """Moonraker requests, in order, that start a print: [{'method', 'path', 'body'}]."""

    # color / tools
    @abstractmethod
    def normalize_tool_mapping(self, mapping: dict, state: dict) -> dict[str, int]:
        """Validate a tool index -> physical slot map. Raises MultiColorUnsupported when unsupported."""

    # readiness and sync
    @abstractmethod
    def readiness_problems(self, state: dict, status: dict, selection: dict | None = None, **kw) -> list[str]:
        """Reasons the printer is not ready to start; empty when ready."""

    def screen_command(self, slot: int, material: str, hex_code: str) -> str | None:
        """G-code that tells a native screen what is loaded, or None when the printer has no screen sync."""
        return None

    def stock_api_url(self, host: str) -> str | None:
        """Vendor status endpoint, or None when the printer only speaks Moonraker."""
        return None

    # telemetry
    @abstractmethod
    def moonraker_objects(self) -> list[str]:
        """Moonraker objects to query for status."""

    @abstractmethod
    def normalize_status(self, raw: dict) -> dict[str, Any]:
        """Moonraker 'status' dict -> {state, progress, filename, hotend_c, bed_c, ...}."""


def basic_status(raw: dict) -> dict[str, Any]:
    """Plain Moonraker fields shared by every model."""
    raw = raw if isinstance(raw, dict) else {}
    stats = raw.get("print_stats") or {}
    return {
        "state": stats.get("state", "unknown"),
        "filename": stats.get("filename") or "",
        "progress": (raw.get("virtual_sdcard") or {}).get("progress"),
        "hotend_c": (raw.get("extruder") or {}).get("temperature"),
        "bed_c": (raw.get("heater_bed") or {}).get("temperature"),
    }


# A Bambu Lab printer runs plain G/M/T codes only. Klipper macros (PRINT_START, the ZMod
# START_PRINT ... MESH= line, CHANGE_ZCOLOR, LOAD_ZOFFSET) are another vendor's start code.
_GCODE_WORD = re.compile(r"^[GMT]\d+(?:\.\d+)?$", re.I)


def start_code_problems(text: str, vendor: VendorProfile) -> list[str]:
    """Reasons this G-code must not reach a printer of `vendor`; empty when none apply.

    Only Bambu start code is checked line by line: every command must be a G, M or T code, so no
    Klipper or ZMod macro can reach Bambu hardware. Other vendors keep their existing slice guards.
    """
    if vendor.start_code != "bambu":
        return []
    foreign = []
    for number, line in enumerate(text.splitlines(), 1):
        command = line.split(";", 1)[0].strip()
        if not command:
            continue
        word = command.split()[0]
        if not _GCODE_WORD.match(word):
            foreign.append(f"line {number}: {command[:60]}")
            if len(foreign) == 3:
                break
    if not foreign:
        return []
    return [f"G-code for {vendor.label} contains non-Bambu commands (Klipper or ZMod start code?): "
            + "; ".join(foreign)]
