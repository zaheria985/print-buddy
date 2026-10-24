# Fake OrcaSlicer resources for the Bambu Lab A1 tests

Written for Print Buddy's tests: MIT, like the rest of this repository. It is **not** OrcaSlicer or Bambu Lab
material. No profile value, setting or G-code line here was copied from either project.

The tree has the same layout an installed OrcaSlicer uses: `profiles/BBL.json` indexes the profiles, which use
`inherits` and `include`. Only the profile names (the identifiers Orca loads them by) and the printer's published
dimensions match the real A1. The start, end and filament G-code are invented sequences. They exercise the
guard: placeholders, an `{if}` branch, moves off the bed and unconditional heater-off lines. Never print with it.

The tests generate templates from this tree with `print_buddy.printers.bambulab.profiles.install` and trust its
hashes for the test only. Real slicing uses the operator's own OrcaSlicer (see docs/BAMBU-LAB.md).
