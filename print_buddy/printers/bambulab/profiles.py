"""Bambu Lab Orca profiles, generated on the operator's machine from their own OrcaSlicer installation.

Print Buddy ships no OrcaSlicer or Bambu Lab profile content (those are AGPL-3.0). It ships only the profile
names, Print Buddy's own overrides (facts.ORCA_OVERRIDES) and the SHA-256 identity of the vendor profiles it has
been checked against (the printer's `orca_identity` fact). `install` reads the vendor index and profiles from an
installed OrcaSlicer `resources` folder, resolves their `inherits`/`include` chains, refuses unless the result has
an expected identity, and writes two copies into the profile's state folder (never the repository):

    <state>/orca-profiles/<model templates>/upstream/{machine,process,filament}.json   flattened vendor profiles
    <state>/orca-profiles/<model templates>/{machine,process,filament}.json            the same plus overrides
    <state>/orca-profiles/<model templates>/[upstream/]filaments/<MATERIAL>.json        each other checked material

`problems` re-checks that folder before it is used: the upstream copies must still hash to an expected identity,
the Orca templates must be exactly those copies plus the overrides, and the profile must describe the printer
in facts.py. Anything missing or different fails closed; nothing is fetched or regenerated implicitly.

Material-aware filaments (#264): a model's `orca_filaments` fact maps a base polymer (PLA, PETG, ...) to the one
vendor filament profile checked for it, and each identity records those profiles' hashes under "filaments".
`filament_template` picks the profile for the hardware record's material and verifies it the same way; a material
with no checked profile is refused, never sliced with another material's profile. filament.json stays the
model's default profile (PLA), so a folder generated before #264 keeps slicing PLA until it is regenerated. A model
without `orca_filaments` has only that default.

    python3 scripts/bambu-templates.py --printer a1 --resources <OrcaSlicer resources folder>
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import tempfile
from pathlib import Path

from . import facts

KINDS = ("machine", "process", "filament")
INDEX_LISTS = {"machine": "machine_list", "process": "process_list", "filament": "filament_list"}
UPSTREAM = "upstream"
FILAMENTS = "filaments"
VENDOR = "BBL"


class ProfileError(RuntimeError):
    """The printer's Orca profiles are missing, unreadable or not the ones Print Buddy was checked against."""


def install_hint(adapter):
    return (f"generate them from your own OrcaSlicer installation: python3 scripts/bambu-templates.py "
            f"--printer {adapter.model.printer_key} --resources <OrcaSlicer resources folder> "
            "(see docs/BAMBU-LAB.md)")


def find_vendor(resources):
    """The vendor profile folder (holding BBL.json and BBL/) under an OrcaSlicer resources folder, or raise."""
    root = Path(resources).expanduser()
    for profiles in (root / "profiles", root):
        if (profiles / f"{VENDOR}.json").is_file() and (profiles / VENDOR).is_dir():
            return profiles
        if (profiles / f"{VENDOR}.opc").is_file():
            raise ProfileError(f"{profiles} holds packed vendor profiles ({VENDOR}.opc, OrcaSlicer 2.5+); "
                               "only an OrcaSlicer whose resources hold BBL.json and BBL/ JSON profiles is supported")
    raise ProfileError(f"no OrcaSlicer {VENDOR} vendor profiles under {root} "
                       f"(expected profiles/{VENDOR}.json and profiles/{VENDOR}/)")


def _read(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        raise ProfileError(f"cannot read {path}: {exc}") from None


def flatten(resources, names):
    """{kind: profile} for the named vendor profiles, `inherits` and `include` resolved, as Orca loads them."""
    resolve, version = _resolver(resources)
    return {kind: resolve(names[kind]) for kind in KINDS}, version


def flatten_filaments(resources, names):
    """{name: profile} for the named vendor filament profiles, resolved like flatten."""
    resolve, _ = _resolver(resources)
    return {name: resolve(name) for name in names}


def _resolver(resources):
    profiles = find_vendor(resources)
    index = _read(profiles / f"{VENDOR}.json")
    paths = {entry["name"]: entry["sub_path"] for key in INDEX_LISTS.values() for entry in index.get(key, [])}

    def resolve(name, seen=()):
        if name not in paths:
            raise ProfileError(f"{VENDOR} vendor profile {name!r} is not in {profiles / (VENDOR + '.json')}")
        if name in seen:
            raise ProfileError(f"{VENDOR} vendor profile {name!r} inherits itself")
        data = _read(profiles / VENDOR / paths[name])
        out = resolve(data["inherits"], (*seen, name)) if data.get("inherits") else {}
        for include in data.get("include", []):
            out.update({k: v for k, v in _read(profiles / VENDOR / paths[include]).items()
                        if k not in ("type", "name", "instantiation", "from")})
        out.update(data)
        out.pop("inherits", None)
        out.pop("include", None)
        return out

    return resolve, index.get("version")


def _dump(data):
    head = [k for k in ("type", "name", "from", "instantiation") if k in data]
    return json.dumps({**{k: data[k] for k in head}, **{k: data[k] for k in sorted(data) if k not in head}},
                      indent=2, ensure_ascii=False) + "\n"


def sha256(data):
    return hashlib.sha256(_dump(data).encode()).hexdigest()


def template_text(kind, upstream):
    """The Orca template for one kind: the flattened vendor profile plus Print Buddy's overrides and settings ID."""
    data = dict(upstream)
    data.update(facts.ORCA_OVERRIDES[kind])
    data[facts.SETTINGS_IDS[kind]] = [data["name"]] if kind == "filament" else data["name"]
    return _dump(data)


def identity(adapter, hashes, version=None):
    """The expected identity these flattened profiles match (and, when given, their vendor version), or None."""
    for known in adapter.fact("orca_identity"):
        if known["sha256"] == hashes and (version is None or known["vendor_version"] == version):
            return known
    return None


def _first(value):
    return value[0] if isinstance(value, list) and value else value


def material_key(material):
    """The base polymer a vendor filament profile is chosen by: PLA+, Matte PLA and PLA-CF -> PLA, PETG -> PETG.

    Reinforced fills keep their base polymer's profile, as their temperatures do (printer_state); the guard still
    requires a hardened steel nozzle for them.
    """
    from ...core.printer_state import material_family
    family = material_family(material)
    return {"FLEXIBLE": "TPU", "NYLON": "PA", "POLYAMIDE": "PA"}.get(family.split("-", 1)[0], family.split("-", 1)[0])


def filament_set(adapter):
    """{material: vendor filament profile name} checked for this printer; empty when it has only the default."""
    return dict(adapter.fact("orca_filaments")) if "orca_filaments" in adapter.facts else {}


def filament_hashes(adapter, known):
    """{profile name: sha256} of every filament profile an identity covers, the default included."""
    return {**known.get("filaments", {}), adapter.fact("orca_profiles")["filament"]: known["sha256"]["filament"]}


def filament_problems(adapter, material, data):
    """Why a flattened filament profile must not be the one for material on this printer; empty when it may."""
    out, name = [], data.get("name")
    kind = _first(data.get("filament_type"))
    if material_key(kind) != material:
        out.append(f"filament profile {name!r} is {kind!r}, not {material}")
    machine = adapter.fact("orca_profiles")["machine"]
    if data.get("compatible_printers") and machine not in data["compatible_printers"]:
        out.append(f"filament profile {name!r} does not list {machine!r} in compatible_printers")
    return out


def describe_problems(adapter, upstream):
    """Profile values that disagree with the printer's facts: a profile for another printer is refused."""
    machine, out = upstream["machine"], []
    if machine.get("printer_model") != adapter.fact("printer_model"):
        out.append(f"machine profile is for {machine.get('printer_model')!r}, not {adapter.fact('printer_model')!r}")
    (w, d), height = adapter.fact("bed_mm"), adapter.fact("max_print_height_mm")
    if machine.get("printable_area") != ["0x0", f"{w:g}x0", f"{w:g}x{d:g}", f"0x{d:g}"]:
        out.append(f"machine printable_area {machine.get('printable_area')} is not 0..{w:g} x 0..{d:g} mm")
    # The Orca machine profile may be shorter than the vendor volume (P1S: 250 of 256 mm); default to the volume.
    orca_height = adapter.facts["orca_profile_height_mm"].value if "orca_profile_height_mm" in adapter.facts else height
    try:
        height_ok = math.isclose(float(machine.get("printable_height")), float(orca_height), abs_tol=1e-6)
    except (TypeError, ValueError):
        height_ok = False
    if not height_ok:
        out.append(f"machine printable_height {machine.get('printable_height')} is not {orca_height:g} mm")
    exclusion = adapter.facts["bed_exclude_area"].value if "bed_exclude_area" in adapter.facts else ()
    expected_exclusion = [f"{x:g}x{y:g}" for x, y in exclusion]
    if machine.get("bed_exclude_area") not in (None, [], [""]) and machine.get("bed_exclude_area") != expected_exclusion:
        out.append(f"machine bed_exclude_area {machine.get('bed_exclude_area')} is not "
                   f"{expected_exclusion or 'empty'}")
    nozzle = machine.get("nozzle_diameter")
    if [str(v) for v in (nozzle if isinstance(nozzle, list) else [nozzle])] != [f"{adapter.fact('stock_nozzle')[0]:g}"]:
        out.append(f"machine nozzle_diameter {nozzle} is not {adapter.fact('stock_nozzle')[0]:g} mm")
    return out


def check_resources(adapter, resources):
    """(upstream, {material: filament}, version, identity) from an installed OrcaSlicer, or ProfileError.

    Every profile must hash to one expected identity, describe the printer and, for filaments, its material.
    """
    upstream, version = flatten(resources, adapter.fact("orca_profiles"))
    hashes = {kind: sha256(upstream[kind]) for kind in KINDS}
    known = identity(adapter, hashes, version)
    if known is None:
        raise ProfileError(f"the {VENDOR} profiles in {resources} (vendor version {version}) are not a version "
                           f"Print Buddy has checked for the {adapter.label}: sha256 {hashes}. Nothing was written")
    problems = describe_problems(adapter, upstream)
    names = filament_set(adapter)
    flat = flatten_filaments(resources, sorted(set(names.values()))) if names else {}
    expected = filament_hashes(adapter, known)
    for material, name in names.items():
        if sha256(flat[name]) != expected.get(name):
            problems.append(f"{material} filament profile {name!r} (sha256 {sha256(flat[name])}) is not the one "
                            f"checked for {known.get('label')}")
        problems += filament_problems(adapter, material, flat[name])
    if problems:
        raise ProfileError("; ".join(problems) + ". Nothing was written")
    default = adapter.fact("orca_profiles")["filament"]
    return upstream, {material: flat[name] for material, name in names.items() if name != default}, version, known


def install(adapter, resources, folder=None):
    """Generate the printer's Orca templates from an installed OrcaSlicer; return the identity it matched.

    Nothing is written unless the flattened vendor profiles match an expected identity and the printer's facts.
    """
    folder = Path(folder or adapter.pinned_templates_folder())
    upstream, filaments, version, known = check_resources(adapter, resources)
    folder.parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=f".{folder.name}-", dir=folder.parent))
    try:
        (work / UPSTREAM).mkdir()
        for kind in KINDS:
            (work / UPSTREAM / f"{kind}.json").write_text(_dump(upstream[kind]))
            (work / f"{kind}.json").write_text(template_text(kind, upstream[kind]))
        if filaments:
            (work / UPSTREAM / FILAMENTS).mkdir()
            (work / FILAMENTS).mkdir()
        for material, data in filaments.items():
            (work / UPSTREAM / FILAMENTS / f"{material}.json").write_text(_dump(data))
            (work / FILAMENTS / f"{material}.json").write_text(template_text("filament", data))
        (work / "SOURCE.json").write_text(json.dumps(
            {"resources": str(Path(resources).expanduser().resolve()), "vendor_version": version,
             "identity": known.get("label"), "sha256": {kind: sha256(upstream[kind]) for kind in KINDS},
             "filaments": {material: {"name": name, "sha256": filament_hashes(adapter, known)[name]}
                           for material, name in filament_set(adapter).items()}}, indent=2) + "\n")
        old = folder.with_name(f".{folder.name}-old")
        if folder.exists():
            shutil.rmtree(old, ignore_errors=True)
            os.replace(folder, old)
        os.replace(work, folder)
        shutil.rmtree(old, ignore_errors=True)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return known


def problems(adapter, folder=None):
    """Why the generated templates in folder must not be used; empty when they are the checked profiles."""
    folder = Path(folder or adapter.pinned_templates_folder())
    if not all((folder / UPSTREAM / f"{kind}.json").is_file() for kind in KINDS):
        return [f"{adapter.label} Orca profiles are not installed in {folder}; {install_hint(adapter)}"]
    try:
        upstream = {kind: json.loads((folder / UPSTREAM / f"{kind}.json").read_text()) for kind in KINDS}
        templates = {kind: (folder / f"{kind}.json").read_text() for kind in KINDS}
    except (OSError, ValueError) as exc:
        return [f"{adapter.label} Orca profiles in {folder} are unreadable ({exc}); {install_hint(adapter)}"]
    hashes = {kind: sha256(upstream[kind]) for kind in KINDS}
    if identity(adapter, hashes) is None:
        return [f"{adapter.label} Orca profiles in {folder} are not a version Print Buddy has checked "
                f"(sha256 {hashes}); {install_hint(adapter)}"]
    out = [f"{folder / (kind + '.json')} is not the generated template (edited?); {install_hint(adapter)}"
           for kind in KINDS if templates[kind] != template_text(kind, upstream[kind])]
    return out + describe_problems(adapter, upstream)


def require(adapter, folder=None):
    """The verified template folder, or ProfileError."""
    found = problems(adapter, folder)
    if found:
        raise ProfileError("; ".join(found))
    return Path(folder or adapter.pinned_templates_folder())


def _no_profile(adapter, material, checked):
    return ProfileError(f"{adapter.label}: no checked vendor filament profile for {material!r} "
                        f"(checked: {', '.join(checked)}); load a checked material or add one (docs/BAMBU-LAB.md). "
                        "Nothing was sliced")


def filament_template(adapter, material, folder=None):
    """The verified generated filament template for the hardware record's material, or ProfileError.

    The profile is the one the printer's `orca_filaments` fact names for the material's base polymer, and its files
    must still be the checked vendor profile plus overrides. A material with no checked profile is refused.
    """
    folder = require(adapter, folder)
    key, names = material_key(material), filament_set(adapter)
    default = folder / "filament.json"
    if not names:
        kind = material_key(_first(json.loads(default.read_text()).get("filament_type")))
        if kind != key:
            raise _no_profile(adapter, material, [kind])
        return default
    if key not in names:
        raise _no_profile(adapter, material, names)
    name = names[key]
    if name == adapter.fact("orca_profiles")["filament"]:
        return default  # verified with the machine and process by require()
    upstream, path = folder / UPSTREAM / FILAMENTS / f"{key}.json", folder / FILAMENTS / f"{key}.json"
    if not (upstream.is_file() and path.is_file()):
        raise ProfileError(f"{adapter.label} {key} filament profile ({name}) is not installed in {folder}; "
                           f"{install_hint(adapter)}")
    try:
        data, text = json.loads(upstream.read_text()), path.read_text()
    except (OSError, ValueError) as exc:
        raise ProfileError(f"{adapter.label} {key} filament profile in {folder} is unreadable ({exc}); "
                           f"{install_hint(adapter)}") from None
    known = identity(adapter, {kind: sha256(json.loads((folder / UPSTREAM / f"{kind}.json").read_text()))
                               for kind in KINDS})
    if data.get("name") != name or sha256(data) != filament_hashes(adapter, known).get(name):
        raise ProfileError(f"{upstream} is not the checked {name!r} profile; {install_hint(adapter)}")
    if text != template_text("filament", data):
        raise ProfileError(f"{path} is not the generated template (edited?); {install_hint(adapter)}")
    found = filament_problems(adapter, key, data)
    if found:
        raise ProfileError("; ".join(found))
    return path


def filament_for_settings_id(adapter, settings_id, folder=None):
    """The verified filament template whose profile name a sliced file's filament_settings_id names, or ProfileError."""
    names = filament_set(adapter) or {None: adapter.fact("orca_profiles")["filament"]}
    material = next((m for m, name in names.items() if name == settings_id), False)
    if material is False:
        raise ProfileError(f"filament_settings_id {settings_id!r} is not a checked {adapter.label} vendor filament "
                           f"profile ({', '.join(names.values())})")
    if material is None:
        return require(adapter, folder) / "filament.json"
    return filament_template(adapter, material, folder)
