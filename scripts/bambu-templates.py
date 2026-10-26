#!/usr/bin/env python3
"""Generate a Bambu Lab printer's Orca templates from the operator's own OrcaSlicer installation.

    python3 scripts/bambu-templates.py --printer a1 --resources <OrcaSlicer resources folder> [--check]

Print Buddy ships no OrcaSlicer or Bambu Lab profiles. This reads the BBL vendor profiles from an installed
OrcaSlicer (the folder that holds profiles/BBL.json and profiles/BBL/; a Flatpak keeps it under
`flatpak info --show-location com.orcaslicer.OrcaSlicer`/files/share/OrcaSlicer/resources), resolves the
printer's profiles, refuses unless they hash to an identity recorded in print_buddy/printers/bambulab/facts.py,
and writes them with Print Buddy's overrides into <state>/orca-profiles/<printer> for the configured profile.
That includes one checked filament profile per material the printer lists (`orca_filaments`; A1 and P1S: PLA, PETG,
TPU, PVA); slicing picks the one for the loaded material. Nothing is downloaded and nothing is written into the
repository.

--check writes nothing: it reports whether the installed OrcaSlicer has a checked identity and whether the
generated folder (if any) is current. Exit 1 on any problem.
"""
import argparse
import importlib
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--printer", default="a1", help="printer profile module under print_buddy/printers/bambulab")
    ap.add_argument("--resources", required=True, type=Path, help="the installed OrcaSlicer resources folder")
    ap.add_argument("--check", action="store_true", help="verify only; write nothing")
    a = ap.parse_args()
    from print_buddy import printers, settings
    from print_buddy.printers.bambulab import profiles
    model = importlib.import_module(f"print_buddy.printers.bambulab.{a.printer}").MODEL
    adapter = printers.get(model.key, settings.load(environ={**os.environ, "PRINT_BUDDY__PRINTER__MODEL": model.key}))
    materials = profiles.filament_set(adapter) or {"default": adapter.fact("orca_profiles")["filament"]}
    checked = ", ".join(f"{m} ({name})" for m, name in materials.items())
    try:
        if a.check:
            _, _, _, known = profiles.check_resources(adapter, a.resources)
            found = profiles.problems(adapter)
            for material in profiles.filament_set(adapter):
                try:
                    profiles.filament_template(adapter, material)
                except profiles.ProfileError as exc:
                    found.append(str(exc))
            print(f"{a.resources}: {known['label']} matches for {checked}"
                  + (f"; generated folder: {'; '.join(dict.fromkeys(found))}" if found
                     else "; generated folder is current"))
            return 1 if found else 0
        known = profiles.install(adapter, a.resources)
    except profiles.ProfileError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1
    print(f"wrote {model.label} Orca templates from {known['label']} to {adapter.pinned_templates_folder()}; "
          f"filament profiles: {checked}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
