#!/usr/bin/env python3
"""Approve protected settings from an offline Orca G-code artifact.

Run from the repository root:

    python3 -m print_buddy.core.approve_baseline approve-baseline <artifact>
    python3 -m print_buddy.core.approve_baseline approve-baseline <artifact> --baseline config/<model baseline>.json

Without --baseline it writes the shared config/protected-settings-baseline.json (the AD5X's). A model with its own
baseline file (PrinterModel.baseline) is approved only with --baseline naming that file; it never writes the
shared one.
"""
import sys
from pathlib import Path

from .slice_guard import BASELINE_PATH, approve_baseline

USAGE = ("Usage: python3 -m print_buddy.core.approve_baseline approve-baseline <artifact> "
         "[--baseline config/<baseline>.json]")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) == 2 and argv[0] == "approve-baseline":
        approve_baseline(Path(argv[1]))
    elif len(argv) == 4 and argv[0] == "approve-baseline" and argv[2] == "--baseline":
        path = Path(argv[3])
        if path.resolve() == BASELINE_PATH.resolve() or path.parent.resolve() != BASELINE_PATH.parent.resolve():
            raise SystemExit("--baseline must name a model baseline file in config/, not the shared one")
        approve_baseline(Path(argv[1]), path=path)
    else:
        raise SystemExit(USAGE)


if __name__ == "__main__":
    main()
