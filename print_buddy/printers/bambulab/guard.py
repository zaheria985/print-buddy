"""Offline checks that keep Klipper/ZMod start code and unsupported jobs away from Bambu hardware.

Pure text checks: no printer, slicer or network. Used when templates are assembled, after slicing, before a
Bambu package is made, and at print-now. The limits come from the printer's adapter facts (facts.py).

Trust model. A sliced file is untrusted. Its toolpath is held to the bed by a modal interpreter
(slice_guard.toolpath_motion_problems). Its vendor start and end blocks run off the bed, so instead of bounds
they are matched line by line, in order, against the printer's verified templates (vendor_block_problems):
every executable line must be the next template line (placeholders match one token; only lines inside Orca
{if} blocks may be skipped) or one of Orca's own non-motion preamble codes, and every unconditional template line
must be present, so a vendor block cannot be edited, reordered, extended or cut short. The reference templates
are generated on the operator's machine from their own OrcaSlicer (profiles.py) and trusted only while their
flattened vendor profiles hash to an identity recorded in facts.py; an [orca] templates override is never the
reference, and missing or unverified profiles refuse every file.
"""
from __future__ import annotations

import json
import math
import re

from . import profiles

# Klipper extended commands (START_PRINT, PRINT_END, BED_MESH_PROFILE, SET_PRINT_STATS_INFO, CHANGE_ZCOLOR,
# SAVE_VARIABLE, LOAD_ZOFFSET, ...) are UPPER_SNAKE words. Bambu G-code lines start with G, M or T codes.
KLIPPER_COMMAND = re.compile(r"^[ \t]*([A-Z][A-Z0-9]*_[A-Z0-9_]+)\b", re.M)
# ZMod/AD5X markers that are not commands: START_PRINT's MESH= argument, zmod_color data, the ZMod MD5 line.
ZMOD_MARKER = re.compile(r"\bMESH=|zmod|^; MD5:", re.M | re.I)


class ForeignStartCode(RuntimeError):
    """G-code meant for a Klipper/ZMod printer was about to reach a Bambu Lab printer."""


def foreign_commands(text):
    """Klipper/ZMod commands and markers in text, in order, without repeats."""
    found = [m.group(1) for m in KLIPPER_COMMAND.finditer(text or "")]
    found += [m.group(0).strip() for m in ZMOD_MARKER.finditer(text or "")]
    return list(dict.fromkeys(found))


def require_bambu_code(text, what):
    """Raise ForeignStartCode when text holds any Klipper/ZMod command; return text unchanged."""
    bad = foreign_commands(text)
    if bad:
        raise ForeignStartCode(f"{what} contains Klipper/ZMod code ({', '.join(bad[:5])}); "
                               "it is never sent to a Bambu Lab printer")
    return text


def _config(text, key):
    match = re.search(rf"^; {re.escape(key)} = (.*)$", text, re.M)
    return match.group(1).strip() if match else None


def _numbers(text, command):
    return [float(v) for v in re.findall(rf"^[ \t]*{command}\b[^;\n]*?\bS(-?\d+(?:\.\d+)?)", text, re.M)]


# Codes Orca writes around the vendor blocks itself: progress, machine limits, modes, units, fan, spaghetti detector.
# None of them moves an axis.
ORCA_PREAMBLE = {"M73", "M201", "M203", "M204", "M205", "G90", "G21", "M82", "M83", "M106", "M107", "M981"}
_CONTROL = re.compile(r"\{(?:if|elsif|else|endif)\b[^}]*\}")
_PLACEHOLDER = re.compile(r"(\{[^{}]*\}|\[[^\[\]]*\])")


def _code(line):
    return re.sub(r"\s+", " ", line.split(";", 1)[0].strip())


def template_patterns(*templates):
    """(code, regex, required) per executable template line, in order. A placeholder matches one token.

    Lines inside an Orca {if}/{else} block are optional (Orca keeps one branch); every other line is required.
    """
    out = []
    for text in templates:
        depth = 0
        for line in text.splitlines():
            pieces = re.split(r"(\{(?:if|elsif|else|endif)\b[^}]*\})", line)
            for piece in pieces:
                control = _CONTROL.fullmatch(piece)
                if control:
                    word = re.match(r"\{(\w+)", piece).group(1)
                    depth += 1 if word == "if" else -1 if word == "endif" else 0
                    continue
                code = _code(piece)
                if not code:
                    continue
                parts = [r"[^\s;{}\[\]]+" if _PLACEHOLDER.fullmatch(t) else re.escape(t) for t in _PLACEHOLDER.split(code)]
                out.append((code, re.compile("".join(parts).replace(r"\ ", " ")), depth == 0))
                if ";" in piece:
                    break  # the rest of the line is a comment
    return out


def vendor_block_problems(block, patterns, what):
    """Lines of block that break the pinned template: refused unless each executable line is, in order, the next
    template line (skipping only optional, conditional ones) or one of Orca's own non-motion preamble codes, and
    every required template line appears. A missing required line (a truncated or edited block) is refused.
    """
    problems, index = [], 0
    for number, line in enumerate(block.splitlines(), 1):
        code = _code(line)
        if not code:
            continue
        k = index
        while k < len(patterns):
            if patterns[k][1].fullmatch(code):
                break
            if patterns[k][2]:
                k = len(patterns)  # a required line cannot be skipped
                break
            k += 1
        if k < len(patterns):
            index = k + 1
        elif code.split()[0].upper() not in ORCA_PREAMBLE:
            expected = next((c for c, _, req in patterns[index:] if req), None)
            problems.append(f"{what} line {number} is not in the pinned vendor template: {code[:60]}"
                            + (f" (expected {expected[:40]!r})" if expected else ""))
            if len(problems) == 3:
                return problems
    missing = [c for c, _, req in patterns[index:] if req]
    if missing:
        problems.append(f"{what} ends before the pinned vendor template does (truncated or edited?): "
                        f"{len(missing)} required line(s) missing, first {missing[0][:60]!r}")
    return problems


def _templates(adapter):
    root = adapter.pinned_templates()
    machine = json.loads((root / "machine.json").read_text())
    filament = json.loads((root / "filament.json").read_text())
    first = lambda v: v[0] if isinstance(v, list) else v  # noqa: E731
    return ((machine["machine_start_gcode"], first(filament.get("filament_start_gcode", ""))),
            (first(filament.get("filament_end_gcode", "")), machine["machine_end_gcode"]))


def gcode_problems(text, adapter, *, state=None):
    """Reasons this sliced G-code must not be handed to the adapter's printer; empty when it passes.

    state, when given, is the printer hardware record: its nozzle, spool material and temperatures must match.
    """
    from ...core import slice_guard
    from ...core.nozzle import NozzleError, effective_nozzle_temperature, is_abrasive_material, normalize_nozzle_type
    from ...core.printer_state import material_family, same_material

    fact = adapter.fact
    nozzle_sizes, nozzle_types = adapter.supported_nozzle_sizes, adapter.supported_nozzle_types
    text = text or ""
    problems = []
    bad = foreign_commands(text)
    if bad:
        problems.append(f"G-code contains Klipper/ZMod code ({', '.join(bad[:5])}); never sent to a Bambu Lab printer")
    name = fact("printer_model")
    model = _config(text, "printer_model")
    if model != name:
        problems.append(f"G-code was sliced for printer_model {model!r}, not {name!r}")
    if not re.search(r"^; generated by OrcaSlicer ", text, re.M):
        problems.append(f"only OrcaSlicer output is verified for the {name}; Bambu Studio and other slicers are refused")
    split = slice_guard.split_toolpath(text)
    if split is None:
        problems.append("cannot locate the OrcaSlicer toolpath (no layer change or end G-code marker); "
                        "bounds cannot be checked")
    else:
        prelude, body, epilogue = split
        try:
            start, end = _templates(adapter)
        except profiles.ProfileError as exc:
            problems.append(f"vendor start and end G-code cannot be checked: {exc}")
        else:
            problems += vendor_block_problems(prelude, template_patterns(*start), "start G-code")
            problems += vendor_block_problems(epilogue, template_patterns(*end), "end G-code")
        tools = sorted({int(n) for n in re.findall(r"^[ \t]*T(\d+)\b", body, re.M)} - {0})
        if tools:
            problems.append(f"G-code changes to tools {tools}; {fact('ams')[0]} multi-color is not supported")
        plate = _config(text, "curr_bed_type")
        if plate != fact("default_plate"):
            problems.append(f"plate type must be {fact('default_plate')!r} (the only plate the {name} profile "
                            f"is checked for), got {plate!r}")
        problems += slice_guard.toolpath_motion_problems(text, fact("bed_mm"), fact("motion_envelope_mm"),
                                                         fact("max_print_height_mm"), fact("travel_z_headroom_mm"))
    hotend_max, bed_max = fact("max_hotend_c"), fact("max_bed_c")
    for command, limit in (("M104", hotend_max), ("M109", hotend_max), ("M140", bed_max), ("M190", bed_max)):
        over = [v for v in _numbers(text, command) if v > limit]
        if over:
            problems.append(f"{command} S{max(over):g} exceeds the {name} limit of {limit} C")
    nozzle = _config(text, "nozzle_diameter")
    try:
        nozzle_mm = float(nozzle)
    except (TypeError, ValueError):
        problems.append(f"invalid embedded nozzle_diameter {nozzle!r}")
        nozzle_mm = None
    if nozzle_mm is not None and not any(math.isclose(nozzle_mm, s, abs_tol=1e-3) for s in nozzle_sizes):
        problems.append(f"nozzle {nozzle_mm:g} mm has no {name} template here (supported: "
                        f"{', '.join(f'{s:g}' for s in nozzle_sizes)})")
    try:
        nozzle_type = normalize_nozzle_type(_config(text, "nozzle_type") or "")
    except NozzleError:
        problems.append(f"invalid embedded nozzle_type {_config(text, 'nozzle_type')!r}")
        nozzle_type = None
    if nozzle_type is not None and nozzle_type not in nozzle_types:
        problems.append(f"nozzle type {nozzle_type} is not a {name} hotend option ({', '.join(nozzle_types)})")
    material = _config(text, "filament_type")
    if not material or ";" in material:
        problems.append(f"G-code must name exactly one filament_type, got {material!r}")
    else:
        family = material_family(material)
        if family.split("-", 1)[0] in fact("not_recommended_materials"):
            problems.append(f"{material} is not recommended by Bambu Lab for the {name}; refused")
        if is_abrasive_material(material) and nozzle_type != "hardened_steel":
            problems.append(f"{material} is abrasive; the {name} needs a hardened steel nozzle for it")
    if state:
        if nozzle_mm is not None and abs(nozzle_mm - float(state["nozzle"]["diameter_mm"])) > 1e-3:
            problems.append(f"G-code nozzle {nozzle_mm:g} != recorded {state['nozzle']['diameter_mm']}")
        if material and not same_material(material, state["spool"]["material"]):
            problems.append(f"G-code filament_type {material!r} != loaded {state['spool']['material']!r}")
        hotend = effective_nozzle_temperature(nozzle_type or "stainless_steel", float(state["temps_c"]["nozzle"]))
        bed_c = float(state["temps_c"]["bed"])
        if not any(math.isclose(v, bed_c, abs_tol=1e-3) for v in _numbers(text, "M140") + _numbers(text, "M190")):
            problems.append(f"no M140/M190 sets the recorded bed temperature {bed_c:g} C")
        if not any(math.isclose(v, hotend, abs_tol=1e-3) for v in _numbers(text, "M104") + _numbers(text, "M109")):
            problems.append(f"no M104/M109 sets the effective nozzle temperature {hotend:g} C")
    return problems
