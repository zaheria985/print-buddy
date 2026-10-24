#!/usr/bin/env python3
"""Generate a Bambu Lab printer's Orca templates from the operator's own OrcaSlicer installation.

    python3 scripts/bambu-templates.py --printer a1 --resources <OrcaSlicer resources folder> [--check]

Print Buddy ships no OrcaSlicer or Bambu Lab profiles. This reads the BBL vendor profiles from an installed
OrcaSlicer (the folder that holds profiles/BBL.json and profiles/BBL/; a Flatpak keeps it under
`flatpak info --show-location com.orcaslicer.OrcaSlicer`/files/share/OrcaSlicer/resources), resolves the
printer's profiles, refuses unless they hash to an identity recorded in print_buddy/printers/bambulab/facts.py,
and writes them with Print Buddy's overrides into <state>/orca-profiles/<printer> for the configured profile.
Nothing is downloaded and nothing is written into the repository.

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
    try:
        upstream, version = profiles.flatten(a.resources, adapter.fact("orca_profiles"))
        hashes = {kind: profiles.sha256(upstream[kind]) for kind in profiles.KINDS}
        if a.check:
            known = profiles.identity(adapter, hashes, version)
            if known is None:
                raise profiles.ProfileError(f"{a.resources}: vendor version {version}, sha256 {hashes} is not a "
                                            f"checked {model.label} identity")
            found = profiles.problems(adapter)
            print(f"{a.resources}: {known['label']} matches" + (f"; generated folder: {'; '.join(found)}"
                                                                 if found else "; generated folder is current"))
            return 1 if found else 0
        known = profiles.install(adapter, a.resources)
    except profiles.ProfileError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1
    print(f"wrote {model.label} Orca templates from {known['label']} to {adapter.pinned_templates_folder()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
