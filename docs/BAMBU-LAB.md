# Bambu Lab printers (phase 1: A1, offline only)

[Documentation home](README.md) · [Printer profiles](PRINTER-PROFILES.md)

Select the A1 with `printer.model: bambulab.a1`. Print Buddy slices for the A1, checks fit and G-code, and can package the result. It never connects to the printer, and nothing in this phase has been tested on an A1. The P1S, also part of issue #127, is not covered here.

Print Buddy ships no A1 slicer profiles. Before the first A1 slice, generate them from your own OrcaSlicer (see [Set up the A1 profiles](#set-up-the-a1-profiles)).

## Set up the A1 profiles

The A1's OrcaSlicer machine, process and filament profiles are OrcaSlicer's and Bambu Lab's work, under the AGPL-3.0, so they are not in this repository. Print Buddy builds its A1 templates from the OrcaSlicer installed on the same machine:

```bash
python3 scripts/bambu-templates.py --printer a1 --resources <OrcaSlicer resources folder>
```

- **The resources folder** is the one holding `profiles/BBL.json` and `profiles/BBL/`. For the Flathub build it is under `$(flatpak info --show-location com.orcaslicer.OrcaSlicer)/files/share/`. For an AppImage it is `squashfs-root/resources`.
- **What it does.** It resolves the A1 profiles' `inherits` and `include` chains and refuses unless they hash to an OrcaSlicer version Print Buddy has been checked against. Today that is only OrcaSlicer 2.4.2, with BBL vendor profiles `02.01.00.19`. It then writes the profiles and Print Buddy's overrides into `<hermes_home>/state/orca-profiles/bambulab_a1/`, never into the checkout. Nothing is downloaded.
- **`--check`** writes nothing. It reports whether the installed OrcaSlicer matches and whether the generated folder is current.
- **Missing, edited or unrecognised profiles refuse everything.** Slicing, `verify-gcode`, packaging and `print-now` all fail closed with the command to run. That includes another OrcaSlicer version.
- **OrcaSlicer 2.5 is refused.** It packs its vendor profiles into one `BBL.opc` file, which Print Buddy does not read. Supporting another version means checking it and adding its identity to `facts.py`.

## How the A1 fits the vendor and printer profiles

The A1 is one printer module under the existing Bambu Lab vendor profile. See [Printer profiles](PRINTER-PROFILES.md) for the general layout.

| File | Role |
|---|---|
| `print_buddy/printers/bambulab/__init__.py` | `VENDOR`: the only authority for connection (`offline`), start code (`bambu`), slicers, file formats and delivery (`discord`, `bambu_handoff`; never `printer`) |
| `print_buddy/printers/bambulab/a1.py` | `MODEL` for `bambulab.a1` with `vendor=VENDOR`: bed, height, nozzle, slots, adapter, templates, baseline file |
| `print_buddy/printers/bambulab/adapter.py` | `BambuOfflineAdapter`: every live call raises `LiveUnsupported`. `connection` is read from `VENDOR`. `verified_slicers` allows Bambu Studio (default) and OrcaSlicer |
| `print_buddy/printers/bambulab/guard.py` | The A1 G-code checks, run after slicing, before packaging and at `print-now` |
| `print_buddy/printers/bambulab/facts.py` | Supplemental metadata only: each hardware value the guard enforces, with its source and whether it was verified, plus the profile names, Print Buddy's overrides and the SHA-256 identity of the checked OrcaSlicer profiles. It decides nothing about connection, slicer or delivery |
| `print_buddy/printers/bambulab/profiles.py` | Generates the A1 templates from an installed OrcaSlicer and re-verifies them before every use |
| `<hermes_home>/state/orca-profiles/bambulab_a1/` | The generated templates, on the operator's machine only (`MODEL.generated_templates`) |

A P1S drops in as `bambulab/p1s.py`: its facts (profile names and a checked identity), an `A1Adapter`-style subclass and a `MODEL` with `generated_templates=True`.

## What works and what does not

| Capability | Status |
|---|---|
| Slicing with OrcaSlicer | Supported with OrcaSlicer 2.4.2's own profiles, generated locally; real-slicer CI on Flathub 2.4.2. The pinned 2.5.0 alpha packs its profiles, so profile generation refuses it, and CI records that refusal |
| Fit checks against 256 × 256 × 256 mm | Supported; the same deterministic checks as other models. Geometry is centred on the printer's own bed (see below) |
| G-code validation | Supported: after slicing, before packaging, at `print-now` and `verify-gcode` |
| Delivery | `discord` and `bambu_handoff` from `VENDOR`. A `.gcode.3mf` package is built in Bambu Studio's export layout. Opening it in Bambu Studio or Bambu Handy, printing it from SD, or printing a plain `.gcode` on an A1 has **not** been tested |
| Print | Not offered. `print-now` verifies the file, then refuses. The bed-clear approval is not used up |
| Bambu Studio as slicer | Default for the A1, like every Bambu Lab printer; `[printer] slicer: orca` opts out. Its output passes the same A1 guard. No real Bambu Studio A1 slice has been run yet |
| Live telemetry, upload, print control | Not supported. No MQTT, LAN FTPS or cloud client, no credentials |
| AMS Lite multi-color | Not supported: tool changes are refused, as are multi-color, multi-part and multi-plate sources |
| P1S | Not started |

Supported firmware and authentication modes for live use: none. `services.moonraker_url` and `services.octoprint_url` must stay empty for an offline vendor; settings refuse to load otherwise. The Moonraker listener, camera watchdog, timelapse download and cold pull do not apply.

## What the guard checks, and what it trusts

A sliced file is untrusted. `guard.gcode_problems` refuses it unless all of the following hold:

1. **Foreign code.** No Klipper or ZMod code anywhere: `START_PRINT`, `MESH=`, `PRINT_START`, `END_PRINT`, any Klipper `UPPER_SNAKE` command, or the ZMod `; MD5:` line. `base.start_code_problems` independently requires every line to be a G, M or T code.
2. **Identity.** `printer_model = Bambu Lab A1`, and the file was generated by OrcaSlicer.
3. **Split.** The file divides into prelude, toolpath and epilogue (`slice_guard.split_toolpath`):
   - Orca 2.5+ marks the boundaries with `MACHINE_START_GCODE_END` and `MACHINE_END_GCODE_START`.
   - Orca 2.4 has no such tags. There the toolpath runs from the first `; CHANGE_LAYER` (or `;LAYER_CHANGE`) to the last `Custom` role tag (`; FEATURE: Custom` or `;TYPE:Custom`).
   - These markers are comments, so they only split the file. They are never a reason to trust a block.
4. **Vendor blocks.** The prelude and epilogue are matched line by line against the **verified generated** vendor templates (`guard.vendor_block_problems`):
   - Each executable line must be the next template line. A placeholder matches exactly one token.
   - Only lines inside an Orca `{if}` block may be skipped. Every other template line must be present, so heater-off lines cannot go missing.
   - The only other lines allowed are Orca's own non-motion preamble codes: `M73`, `M201`–`M205`, `G90`, `G21`, `M82`/`M83`, `M106`/`M107`, `M981`.
   - So a vendor block cannot be edited, reordered, extended or truncated.
   - An `[orca] templates` override is never the reference. A template start or end that differs from the verified profile is refused before Orca runs.
   - Missing or unverified profiles refuse the file.
5. **Toolpath motion.** A modal interpreter (`slice_guard.toolpath_motion_problems`) runs through the whole file. In the toolpath:
   - **State it tracks.** G90/G91 positioning, M82/M83 extruder mode (G90/G91 make the extruder mode unknown until the next M82/M83), G92, and the XYZ position. The position is never carried into the toolpath from the vendor start.
   - **Where moves may go.** A relative move needs a known starting position. Every move stays inside the motion envelope, with Z from 0 to height + 1 mm.
   - **Extrusion.** Every possibly extruding move (an E move in an unknown mode counts) must be at a known position on the 256 mm bed at 0 < Z ≤ 256.
   - **Refused outright.** Arcs, homing, probing other than Bambu's in-print head-wrap probes (G39, G39.3), and XYZ coordinate resets.
   - **Outside the toolpath.** In the prelude and epilogue, XY moves are held to the envelope.
6. **Spoofed markers fail closed.** A spoofed, duplicated or missing marker either moves vendor lines into the toolpath, where they fail the motion checks, or moves toolpath lines into a template check, where they fail. It cannot hide code.
7. **Tools, limits and materials.**
   - No tool change in the toolpath (AMS Lite).
   - M104/M109 at most 300 °C; M140/M190 at most 100 °C.
   - Exactly one filament type. Materials Bambu lists as not recommended are refused; abrasive filament needs `nozzle.type: hardened_steel`.
   - 0.4 mm nozzle, stainless or hardened steel.
8. **Plate type.** The file's settings block must name `curr_bed_type = Textured PEI Plate`.
9. **Hardware record.** When given, the nozzle, material and temperatures must match it.

The generated templates are trusted only while `profiles.problems` finds nothing:

- The flattened vendor profiles kept in `upstream/` still hash to an identity recorded in `facts.py`.
- Each template is exactly that profile plus Print Buddy's overrides.
- The profile describes the A1 in `facts.py`: model name, 256 × 256 mm area, 256 mm height, no exclusion area, 0.4 mm nozzle.
- It contains no Klipper or ZMod code.

That makes them a faithful copy of the installed OrcaSlicer's A1 profile. It does not prove they are correct for an A1; that needs a printer.

The offline tests never see a real profile. They generate templates from an invented resource tree (`tests/printers/fixtures/orca_resources`), trusted for the test only. Its G-code is written for the tests, with placeholders, an `{if}` branch, off-bed moves and heater-off lines, and it is not Bambu or OrcaSlicer code.

## Plate type

The A1 vendor profile's default plate is the Textured PEI Plate, and its start G-code has a branch that runs only for that plate, a small nozzle offset. Orca's CLI does not take the plate from the vendor profile; it resolved Cool Plate. Print Buddy's process override therefore sets `curr_bed_type: Textured PEI Plate` explicitly, and the guard refuses any other plate. Real-slicer CI reads that branch from the generated profile and checks that its lines ran. Other plates are not supported in this phase.

## Protected-settings baseline

The A1 has its own baseline file, `config/protected-settings-baseline.bambulab.a1.json`. It does not exist yet, so the baseline check only warns.

`config/protected-settings-baseline.bambulab.a1.proposed.json` is a proposal. It records what OrcaSlicer 2.4.2 writes for the A1's protected keys, and the real-slicer CI test checks that the supported slicer's output matches it. Nothing reads it as an approval.

Approving it is a separate, deliberate maintainer step:

1. Slice an A1 job with the supported slicer.
2. Review the protected values.
3. Run:

   ```bash
   python3 -m print_buddy.core.approve_baseline approve-baseline <plate_1.gcode> \
       --baseline config/protected-settings-baseline.bambulab.a1.json
   ```

The command prints the differences and writes only that file. `--baseline` refuses the shared `config/protected-settings-baseline.json`, which stays the AD5X's.

## Geometry

Exporters that place a model for an operator-chosen scale or rotation used to centre it at a fixed 110 mm, the middle of a 220 mm bed. That puts large A1 parts off the bed. `orca_adapter._place_triangles`, `export_geometry` and `place_stl` now centre on the target printer's own bed (half its `bed_mm`):

- **220 mm beds:** unchanged.
- **A 240 mm part on the A1:** lands at 8 to 248 mm.
- **Existing printers with a local `bed_mm` override that is not 220 mm:** these now centre on their own bed rather than at 110 mm.

Known limitation: the multi-color and negative-volume exporters in `core/color_geometry.py` still centre at 110 mm. Those paths are unreachable for the single-filament A1.

## Values and sources

| Value | Source | Verified |
|---|---|---|
| Printable area 0 to 256 × 0 to 256 mm, height 256 mm | OrcaSlicer A1 profile; checked again in every generated profile | Yes. Bambu also publishes 256 × 256 × 256 mm |
| No bed exclusion area | OrcaSlicer A1 profile; checked again in every generated profile | Yes |
| 0.4 mm stainless steel stock nozzle, Textured PEI default plate | OrcaSlicer A1 profile | Yes |
| Profile identity: SHA-256 of the flattened A1 machine, process and filament profiles of OrcaSlicer 2.4.2 (BBL `02.01.00.19`) | Computed from an installed OrcaSlicer 2.4.2 | Yes. Hashes only |
| Hotend at most 300 °C, bed at most 100 °C | Bambu Lab A1 spec pages | **No**: read from search-result extracts; bambulab.com was unreachable from the build environment |
| 0.2, 0.6 and 0.8 mm hotends; hardened steel needed for CF/GF and glow filaments | Bambu Lab A1 spec pages | **No**, same reason. Only 0.4 mm is templated and accepted |
| PLA, PETG, TPU, PVA ideal; ABS, ASA, PC, PA, PET and fiber-filled versions not recommended | Bambu Lab A1 spec pages | **No**, same reason. Not-recommended materials are refused outright, a conservative choice |
| AMS Lite: 4 spools | Bambu Lab A1 Combo pages | **No**. Not used |
| `.gcode.3mf` as Bambu's job format; LAN/cloud connection types | Bambu Lab pages | **No** |
| Motion envelope X −48.2 to 267, Y −0.5 to 262.5 mm; 1 mm travel headroom above 256 | Derived from the vendor G-code moves | **No**: not a Bambu figure and not a hardware certification |

All values are in `print_buddy/printers/bambulab/facts.py` with the same source labels.

## Provenance and licence

**Maintainer approval needed.** The A1 profiles are OrcaSlicer's, which took them from Bambu Lab's Bambu Studio, and both are AGPL-3.0. This repository is MIT, so it no longer contains them. Generating them from the operator's own OrcaSlicer at install time is the approach this change takes. Whether that basis is acceptable is for the maintainer, or legal review, to approve. This document is not that approval.

What this repository holds, and what it does not:

| Item | In the repository | Kind |
|---|---|---|
| OrcaSlicer/Bambu A1 machine, process and filament profiles, and their G-code | **No.** Generated per installation into `<hermes_home>/state/orca-profiles/bambulab_a1/` | Third-party, AGPL-3.0, read from the operator's OrcaSlicer |
| Profile names, the `ORCA_OVERRIDES` keys and values, settings-ID keys | Yes, `facts.py` | Original: identifiers and Print Buddy's own settings |
| SHA-256 identity of the checked profiles and hardware numbers | Yes, `facts.py` | Facts: hashes and measurements, no profile text |
| `config/protected-settings-baseline.bambulab.a1.proposed.json` | Yes | SHA-256 of the start and end G-code OrcaSlicer 2.4.2 writes, plus `post_process` (empty) and `printer_technology` (`FFF`); no G-code |
| `profiles.py`, `scripts/bambu-templates.py`, adapter, guard, tests | Yes | Original |
| `tests/printers/fixtures/orca_resources` | Yes | Original, invented for the tests (see its README); no OrcaSlicer or Bambu content |

Upstream reference, cited and not republished: [OrcaSlicer](https://github.com/SoftFever/OrcaSlicer) tag `v2.4.2`, commit `8500fcdccaa10b5099ac20d252af3a7c560046f1`, `resources/profiles/BBL/`. The A1 profiles there are:

- `machine/Bambu Lab A1 0.4 nozzle.json` and its parents;
- `process/0.20mm Standard @BBL A1.json`;
- `filament/Generic PLA @BBL A1.json`.

An earlier revision of this pull request committed flattened copies of these files with a notice. They were removed in the same pull request, and they remain in its branch history.

## Evidence

| Kind | What exists | Not covered |
|---|---|---|
| Geometry | Offline tests: centring on 220 and 256 mm beds, fit bounds on the A1 volume, hardware-record limits | Physical bed calibration |
| Real slicer | CI real-slicer job on the exact head, Flathub OrcaSlicer 2.4.2. It generates the A1 profiles from that installation and runs the production `model_job.slice_job` → verification → `bambu_handoff` → `print_now` path in a process configured as `bambulab.a1`. It checks that the guard passes on real output, the plate branch ran, the toolpath bounds, the baseline proposal, an oversized model and AD5X output. The 2.5.0-alpha job checks that profile generation refuses that version | A1 slicing with any other Orca version; Bambu Studio |
| Packaging consumer | Offline: the `.gcode.3mf` holds exactly the validated G-code and is refused for wrong-model or foreign files; the Discord flow offers it and no print | Opening the package in Bambu Studio or Bambu Handy; reading it from SD on an A1 |
| Hardware | None | Everything on a real A1: start sequence, envelope, plate offset, temperatures, materials, a finished print |
