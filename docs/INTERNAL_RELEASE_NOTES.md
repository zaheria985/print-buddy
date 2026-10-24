# Print Buddy Internal Release Notes

# Unreleased

**What users can do now:**
- Set `logging.level`, `logging.format` (`text` or `json`) and an optional `logging.file` to control how much Print Buddy logs and where. Log lines carry a time with offset, severity and component, and the intake id while an intake runs.
- Find slicing trouble in the logs: each OrcaSlicer or Bambu Studio run records its exit code and elapsed time, and a switch to the Bambu fallback is a warning with the reason.

**Fixes and reliability:**
- Intake failures, fit check failures and slice failures now log the full traceback instead of only a short message in the receipt.
- Credentials are redacted from every log line and traceback.

**Technical details:**
- New `print_buddy/logs.py` (`configure`, `bind`, `stage`, `redact`). The standalone bot and the listener call `configure()`; the Hermes plugin path does not, so the gateway's own logging is untouched.
- The listener's `log()` helper still writes its legacy file and also feeds the shared logger, so existing tests and `listener.log` readers are unaffected.
- Issue: #150. Offline tests: `tests/test_logs.py`.

# Version 2.0.6

**Release Date:** 2026-10-20
**What users can do now:**
- Status steps such as "Checking whether it fits the bed" and slicing now show a single Stop button. The "Other (type answer)" button no longer appears on these steps.

**Fixes and reliability:**
- Fix intake freezing at the fit check and other Stop-guarded steps. The step's result is now returned as soon as the work finishes, instead of waiting up to an hour for someone to press Stop. Stop still cancels a step that is still running. The fit geometry itself was never at fault.
- Remove the Stop button from the message once its step finishes, so a stale Stop cannot be pressed.

# Version 2.0.5

**Release Date:** 2026-10-20
**What users can do now:**
- Control an intake from Discord with Cancel or Stop at supported stages; resume a saved intake after a prompt timeout; and continue an intake after confirming its saved decisions while a background slice is safely reused. Closes #61, #62, and #63.
- Configure supported nozzle diameter and material, with validation, abrasive-on-brass safety refusal, and refusal when no tuned nozzle profile exists. Closes #59.
- Recover from supported printer-side emergencies with guarded panic utilities; load IFS slots while idle; and remap a single-color print to another loaded slot. These controls are software capabilities; they do not imply physical validation here. #88.
- Set Discord prompt timeout values in configuration. #89.
- See clearer start readback that distinguishes preparing, printing, and confirmed extrusion. #87.

**Fixes and reliability:**
- Preserve verified plates when a derived plate fails a safety guard, and bind background slicing to the confirmed source and decisions. #77, #93.
- Harden Spoolman and IFS color/identity synchronization and prevent direct raw stock API requests. #78, #81.
- Make JOB.json writes atomic to prevent concurrent readers from seeing truncated data; repair related offline CI regressions. #79.
- Sanitize foreign 3MF settings and gate unsupported z-hop settings. #86.
- Correct fit margin accounting and feature-aware clearance, including explicit brim handling for tight fits. #85.
- Make Print Buddy scripts ignore the gateway's `HERMES_HOME`; make prompt timeouts configurable. #72, #89.

**Technical and packaging changes:**
- Add a Docker image and setup documentation, plus native Spoolman and configurable Orca runner support in CI. #75, #80, #82.
- Update printer naming in the README. #71.
- Pull request references: [#71](https://github.com/catduckgnaf/print-buddy/pull/71), [#72](https://github.com/catduckgnaf/print-buddy/pull/72), [#75](https://github.com/catduckgnaf/print-buddy/pull/75), [#77](https://github.com/catduckgnaf/print-buddy/pull/77), [#78](https://github.com/catduckgnaf/print-buddy/pull/78), [#79](https://github.com/catduckgnaf/print-buddy/pull/79), [#80](https://github.com/catduckgnaf/print-buddy/pull/80), [#81](https://github.com/catduckgnaf/print-buddy/pull/81), [#82](https://github.com/catduckgnaf/print-buddy/pull/82), [#85](https://github.com/catduckgnaf/print-buddy/pull/85), [#86](https://github.com/catduckgnaf/print-buddy/pull/86), [#87](https://github.com/catduckgnaf/print-buddy/pull/87), [#88](https://github.com/catduckgnaf/print-buddy/pull/88), [#89](https://github.com/catduckgnaf/print-buddy/pull/89), [#90](https://github.com/catduckgnaf/print-buddy/pull/90), [#91](https://github.com/catduckgnaf/print-buddy/pull/91), [#92](https://github.com/catduckgnaf/print-buddy/pull/92), [#93](https://github.com/catduckgnaf/print-buddy/pull/93).

---

# Version 2.0.4

**Release Date:** 2026-10-19
**Needs a migration on the box:** run `scripts/migrate-names.sh --dry-run`, then without it, then restart the listener; printer steps in `docs/RENAME-MIGRATION.md`.
**What shipped:**
- Rename: Print Bitch is Print Buddy everywhere (Klipper macro `PRINT_BUDDY`, plugin `print-buddy`, `print_buddy_*` state files, `PRINT_BUDDY_*` env vars). `[discord] bot_name` sets the bot's name. Closes #53.
- Printer models: one file per printer under `print_buddy/printers/<brand>/` (`flashforge.ad5x`, `sovol.sv06`, `klipper.generic`); `[printer] model` replaces `family`; printer defaults come from the model file; ZMod code moved to `printers/flashforge/zmod/`. Orca inputs for the AD5X and SV06 are unchanged (golden test). Closes #56.
- Stray state files: doctor warns, update and install remove empty or duplicate strays; other profiles are report-only. Closes #60.
- Discord flow corpus: six models run through the intake with fake clicks on every PR, and through real Orca in CI; a gateway `HERMES_HOME` regression test. Closes #58.
- State stays in the Print Buddy profile when the gateway sets `HERMES_HOME` (#57). Camera stall alert sensitivity 1 to 5 (#50).
- CI: Node 24 actions, runner pinned to Ubuntu 24.04.

---

# Version 2.0.3

**Release Date:** 2026-10-19
**What shipped:**
- `model_job._job_for_gcode`: `record_start` matches the started G-code against `JOB.json["gcode"]`, `derived_plates[].gcode` and `multipart_plan.parts[].gcode_path`. Before, only plate 1 resolved, so plates 2+ started with no `multipart_plan_id` and completions never advanced the plan (Valentine's Headphones Stand plates 2 and 3 were reconciled by hand). Closes #48.
- `model_job.copies_supported`: false for multi-plate audits, `full_size_split_plan`, `plate_mode == "separate"` and per-part colors. `prepared_intake` caps the quantity picker at 1 for those jobs and says so; `slice_job` clamps `copies` to 1 and records it. Closes #37.
- `print_mapped_native`: slot material check uses `printer_state.same_material` (PLA == PLA+).
- `scripts/home-update.sh`: interpreter order `$PYTHON`, Hermes agent venv, `python3`. `tests/test_home_update.py` strips `settings.LEGACY_ENV` from the subprocess environment.
- Tests: regression tests for #37 and #48 fail on 2.0.2 and pass here.

---

# Version 2.0.2

**Release Date:** 2026-10-19
**What shipped:** the full runtime moved into this repo with a symlink installer and a personal overlay; settings file and update script with rollback; printer families (flashforge_zmod, generic_klipper, sovol_klipper) routed through the adapter on the print-now path; plate picker and combined multi-plate timelapse; spool swap override; per plate estimates; CI with real Orca and Spoolman. Closes #19.

---

# Version 2.0.1

**Release Date:** 2026-10-18
**What shipped:**
- `model_job.filament_usage_by_selection` (private): extruder usage shared by several parts is split by part volume, and a volume guess is clamped to the scraped source plate total (`source_site_estimate`). Valentine's Headphones Stand: 5 x 247.8 g = 1,239 g before, 160 g after (MakerWorld: 160.03 g over 4 plates, 20,520 s).
- `prepared_intake.preview_sentence` (private): uses plate `prediction_s` and `weight_g` from the scraped instance instead of "unknown".
- `source_colors.py` (public): `_annotate_parts` and `_shape_word` read `3D/3dmodel.model` bounds and transforms to label generic primitives; volume guessed extruder stats carry `estimated: True`; the per part `[uses ~Ng]` suffix was removed.
- Moonraker listener (private): oversized timelapses are re-encoded under the Discord limit, with a text fallback.
- Tests: 797 passed in the deployed suite; 24 passed in this repository.
- Known open: the duplicate clarify title in Discord is an upstream Hermes adapter change, tracked separately.

---

# Version 2.0.0

**Release Date:** 2026-10-18
**What shipped:** The offline core under `print_buddy/core/`, example Orca templates, an MIT license, and one hardware-free test file. Discord, Moonraker, spool, and archive code stayed off the public tree. This number skips the 1.10 line so it is not read as version 10.

---

# Version 1.10.0

**Release Date:** 2026-10-17  
**Target Hardware:** FlashForge Adventurer 5X (AD5X) on ZMod Klipper + Moonraker with 4-slot IFS. OrcaSlicer 2.4.2 primary, Bambu Studio CLI fallback (both Flatpak).

---

## 1. Inch-Unit STL Detection and Auto-Scale

### Problem Statement
- An STL exported in inches (2.30 x 2.55 x 4.02 units) was read as millimeters, producing a 4 mm model. Orca returned `-100 Failed slicing the model`, which hid the cause. A direct-upload retry carried no `scale` in `JOB.json`, so it failed identically.

### Implementation Details
- **`model_job.py`**:
  - Explicit scale now uses `scale if scale is not None else job.get("scale")`, so a falsy check can no longer drop a user value; non-positive scales raise.
  - When no explicit scale is set, the source is `.stl`, and `max(sx, sy, sz) < 10.0`, the job is scaled by `25.4` if the result fits `bed_mm` and `max_print_height_mm`; otherwise it raises with the measured and would-be sizes.
  - `job["auto_scale"] = {factor, reason: "inch units", original_mm, final_mm}`; the key is removed when no auto-scale applies.
- **`orca_adapter.py`**: The `0 < scale <= 1` cap was removed (scale must only be `> 0`). Direct STL jobs with a scale or rotation transform vertices before slicing, so every slicer receives identical resolved geometry.
- **`discord_print_summary.py`**: Slice summary adds `Auto-scale: <orig> mm treated as inches, scaled 25.4x -> <final> mm`.
- **Field result:** the same ghost STL auto-scaled to 58.42 x 64.77 x 102.11 mm, sliced, and is printing.

## 2. Bambu Studio Fallback and Percent Line Widths

- Orca remains primary. Bambu Studio runs only after an Orca slicer failure or exhausted Orca conflict handling. Validation and placement refusals stay fail-closed and never invoke Bambu. Provenance records the producing slicer, and Discord is alerted on fallback.
- Bambu Studio CLI reads Orca percent widths (`110%`) as millimeters and exits `-18` / `238` with `too large line width`. The fallback-only process copy now resolves `inner_wall`, `outer_wall`, `top_surface`, `sparse_infill`, and `support` line widths against the machine `nozzle_diameter` (0.4 mm: 110% → 0.44, 93.75% → 0.375). The Orca template is unchanged A malformed width fails closed instead of passing a percent value to Bambu.

## 3. Acceleration Profile

| Feature | Before | 1.10.0 (mm/s²) |
|---|---:|---:|
| Default | 1,000 | 4,500 |
| Outer wall | 5,000 | 3,200 |
| Inner wall | 5,000 | 4,500 |
| Sparse / internal solid infill | 1,000 / 7,000 | 4,500 / 4,500 |
| Top surface | 2,000 | 3,000 |
| Travel | 10,000 | 4,500 |
| Initial layer / initial travel | 500 | 500 / 500 |

- Binding limit: X input shaper EI at 52.4 Hz recommends 4,747 mm/s². Moonraker reports `max_accel` 20,000, `minimum_cruise_ratio` 0.5, `square_corner_velocity` 9.0 (read only).
- Offline Orca estimate, Raskog large bin: 14h 26m → 10h 38m (26% less). Physical quality is validated by subsequent real prints only.

## 4. Multi-Copy Clone Placement

- For transformed STL input with `copies > 1`, clones are laid out in a grid with 10 mm gaps and written to `clones.stl`; the slicer receives one pre-placed mesh. A job raises when the grid does not fit the bed.

## 5. Reliability Fixes (Fast Track)

- **State:** `require_idle_settled` rereads Moonraker and native state together (up to 90 s, 5 s interval) before reporting preparing or idle.
- **Cancel:** listener posts a short acknowledgement first and collects the Klipper report asynchronously; bot-originated cancels no longer auto-restart firmware.
- **Notifications:** completion and timelapse are coalesced into one post; lifecycle notices are deduplicated by job identity (history id or filename plus start time) persisted in profile state, so reconnects do not replay them.
- **Color mapping:** explicit color decisions resolve to IFS slot and Spoolman spool identity, independent of the active spool.
- **Translation:** part name dictionary extended (for example `peine` → `comb`), bound to the 3MF part key and extruder.
- **Orca conflicts:** conflict logs map to overlap or intersection remediation; no redundant estimate slice.
- **Slice guard:** pre- and post-slice checks pin `machine_start_gcode` (SHA256, mesh name normalized), `machine_end_gcode`, `post_process`, `host_type`, and `printer_technology` to an approved baseline.
- **Service:** the Moonraker listener is `PartOf` the gateway, so a gateway restart always reloads listener code.

## 6. Test Suite Verification

- Deployed suite: **724 passed, 46 subtests passed** (2026-10-17 17:16 ET).
- New regressions: inch STL auto-scales and Orca receives scaled geometry; explicit scale overrides; oversize after 25.4x fails with size message; auto-scaled two-copy STL exports distinct geometry; Bambu fallback converts percent widths to absolute mm.
- Offline: auto-scaled ghost STL sliced with two copies; Bambu fallback produced G-code on the scaled model.

---

# Version 1.9.1

**Release Date:** 2026-10-15  
**Target Hardware:** FlashForge Adventurer 5X (AD5X / AD5M architecture) on ZMod Klipper + Moonraker with a 4-slot IFS (Internal Filament System) multi-material feeder.

---

## 1. Hardware Boundary Analysis & Margin Tuning

### Mechanical & Stepper Analysis
- **Physical Build Plate:** FlashForge spring steel build sheet measures **235 x 235 mm**.
- **Klipper Stepper Limits (`printer.cfg`):**
  - **X Stepper:** `position_min: -20.0`, `position_max: 225.0`, `position_endstop: 220.0` (Total physical stroke: 245 mm).
  - **Y Stepper:** `position_min: -20.0`, `position_max: 232.0`, `position_endstop: 220.0` (Total physical stroke: 252 mm).
  - **Z Stepper:** `position_min: -10.0`, `position_max: 230.0`.
- **Slicer Bed Configuration:** Nominal printable area remains **220 x 220 mm** (origin `[0, 0]` to `[220, 220]`), preserving standard slicer profile compatibility.

### Margin Contracts
- **`color_geometry.py`**:
  - Centering bounding box margin reduced from `5.0 mm` to `1.0 mm`.
- **`orca_adapter.py`**:
  - `SINGLE_COPY_EDGE_MM`: Reduced from `3.0 mm` to `1.0 mm`.
  - `MULTI_COPY_EDGE_MM`: Reduced from `8.0 mm` to `3.0 mm`.
- **Empirical Validation**:
  - Strandbeest Plate 1 (legs), spanning 215.9 x 217.3 mm, previously threw `RuntimeError: part group does not fit the target bed without cropping` because centered width 217.3 mm exceeded `220 - 2 * 2.5 = 215 mm`.
  - Under the 1.0 mm margin, Plate 1 fits at 100% scale and sliced cleanly to G-code with actual extrusion/travel extents:
    - X: `[2.23, 217.76]` (span: 215.53 mm)
    - Y: `[1.53, 218.47]` (span: 216.94 mm)
  - Both extents remain safely within the 220x220 mm origin coordinates and well clear of the physical stepper travel limits (`-20.0` to `225.0` / `232.0`).

---

## 2. Multi-Plate Capacity Evaluation & Bounding Box Decoupling

### Problem Statement
- In prior versions, `orca_adapter.py:orca_plate_capacity` blindly returned `1` for any multi-plate 3MF file to avoid false negative zero-capacity reports.
- Meanwhile, `verified_scale_choices` evaluated model dimensions by extracting `source_triangles` across the entire 3MF file. In multi-plate Bambu/Orca projects, plates are placed side-by-side in global coordinate space. For the 4-plate Strandbeest, this produced a global bounding box of **501 x 529 mm**, causing `verified_scale_choices` to conclude the model required scaling to `<= 0.41`.

### Implementation Details
- **`_multi_plate_capacity(source, state, scale, single_margin)`**:
  - Inspects `plate_manifest(source)` to extract individual plate object assignments.
  - Uses `color_geometry._world_bounds(archive, object_ids)` to compute the bounding box of each plate independently.
  - Compares each plate's scaled dimensions `(dx, dy, dz)` against usable bed boundaries (`eff_w = bed[0] - 2 * margin`, `eff_d = bed[1] - 2 * margin`, `max_h`).
  - Supports 90-degree bed rotation (`fits = (dx <= eff_w and dy <= eff_d) or (dy <= eff_w and dx <= eff_d)`).
  - Returns `0` if any plate exceeds usable dimensions; returns `1` only if all plates fit.
- **`verified_scale_choices(source, state)`**:
  - Multi-plate 3MF files bypass whole-canvas `source_triangles` extraction.
  - If capacity at 1.0 is `> 0`, returns `[1.0]`.
  - If capacity at 0.5 is `< 1`, returns `[]`.
  - Runs an integer binary search `low, high = 50, 99` over `orca_plate_capacity(..., scale=middle / 100.0)` to find the maximal scale where all plates fit.
  - Returns `_scale_ladder(best_scale)` (for Strandbeest, Plate 3 circular fan at 231 mm yields best scale `0.94`, returning `[0.94, 0.75, 0.5]`).

---

## 3. Multi-Plate Slicing Isolation & Per-Part Grouping Fix

### Problem Statement
1. In `model_job.py:1668`, `_slice_per_plate` contained:
   ```python
   if fit is not None:
       object_plate = {key: 1 for key in object_plate}
   ```
   If a scale factor was passed to a multi-plate model, all parts across all plates were reassigned to Plate 1 and sliced together onto a single plate.
2. In `model_job.py:per_part_fit_options`, parts were grouped strictly by filament slot:
   ```python
   groups.setdefault(slot, []).append(part['key'])
   ```
   For multi-plate models where multiple parts on different plates share slot 3, this attempted to fit all parts from all plates simultaneously onto one bed during fit testing, triggering `RuntimeError` and yielding empty fit options `{'scales': [], 'rotations': []}`.

### Implementation Details
- **`_slice_per_plate`**:
  - Guard updated to `if fit is not None and len(plates) <= 1:`, preserving plate boundaries when scaling multi-plate projects.
  - Added support for per-plate scale overrides via `job.get('plate_scales')`, enabling individual plates to scale independently (e.g. Plate 3 fan at 94%, Plates 1, 2, 4 at 100%).
- **`per_part_fit_options`**:
  - Updated grouping to use `(plate_id, slot)` tuples:
    ```python
    p_id = object_plate.get(str(part['object_id']), 1) if plates else 1
    slot = view['choices']['part:' + part['key']]['slot']
    groups.setdefault((p_id, slot) if plates else slot, []).append(part['key'])
    ```
  - Fit choices `[0.94, 0.75, 0.5]` now correctly evaluate to `honored: True` across all plates.

---

## 4. Conversational Continuity & Link Recovery

### Problem Statement
- When the bot recommended a model and the user replied affirmatively ("Let's do it", "Print it", "Go ahead"), the gateway link dispatcher did not find a URL in `event.text`, skipped the intake pipeline, fell through to LLM chat, and prompted the user to send the link again ("Send a new link to try again").

### Implementation Details
- **`plugins/print-feedback/__init__.py`**:
  - Added `_is_affirmative_followup(text)` matching affirmative intent patterns (`let's do it`, `lets print that`, `go ahead`, `do it`, `start printing`, `yes please print`, `sounds good`, `yep`).
  - Added `_recent_model_link_from_db()`: Queries the 25 most recent messages in `state.db` for MakerWorld or Printables URLs.
  - If affirmative intent is detected without a link, prepends the retrieved URL to `event.text`, automatically triggering the full fresh model intake pipeline.
- **Intake Cleanup**:
  - Removed `"Send a new link to try again."` from `link_intake.py`, `prepared_intake.py`, and `plugins/print-feedback/__init__.py`.

---

## 5. Test Suite Verification

- **Repository Unit Tests (`tests/`):**
  - `python3 -m unittest discover -s tests -v`: 13/13 passed.
- **Scripts & Integration Test Suite (`scripts/`):**
  - `test_orca_adapter.py`, `test_model_job.py`, `test_prepared_intake.py`: 171 passed, 41 subtests passed.
  - `test_plate_merge.py`, `test_source_colors.py`, `test_color_geometry.py`: 42 passed.
  - Total: 213 unit/integration tests passed with 0 regressions.
- **Full Slice Verification:**
  - Strandbeest 4-plate project with scale `0.94` sliced end-to-end via `slice_job`, producing 4 valid G-code plates (Plate 1: 2h 22m, Plate 2: 2h 11m, Plate 3: 1h 14m, Plate 4: 1h 37m).
