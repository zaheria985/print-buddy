# Printer profiles and post-slice delivery

Printer support is organised **vendor first, then printer**. A vendor profile holds what all of a vendor's printers share. Each printer profile holds only what is different about that printer, and inherits everything else from its vendor. The options offered after a slice (print, send the files to Discord, package for Bambu) come from these profiles. A Bambu printer offers the Bambu handoff; a Klipper printer never does.

## Layout

```
print_buddy/printers/
  base.py                VendorProfile, PrinterModel, PrinterAdapter, start_code_problems
  flashforge/
    __init__.py          VENDOR  Flashforge: Moonraker, ZMod START_PRINT start code, OrcaSlicer, .gcode
    ad5x.py              MODEL   flashforge.ad5x: 220 mm bed, 4-slot IFS, 0.25 to 0.8 mm nozzles
  klipper/
    __init__.py          VENDOR  Klipper: Moonraker, PRINT_START/PRINT_END macros, OrcaSlicer, .gcode
    generic.py           MODEL   klipper.generic
  sovol/
    __init__.py          VENDOR  Sovol: the Klipper profile under the Sovol name
    sv06.py              MODEL   sovol.sv06
  octoprint/
    __init__.py          VENDOR  OctoPrint: REST to Marlin, slicer-owned start code, OrcaSlicer, .gcode
    marlin.py            MODEL   octoprint.marlin
  bambulab/
    __init__.py          VENDOR  Bambu Lab: no live connection, Bambu start code, Orca or Bambu Studio,
                                 .gcode.3mf handoff
    a1.py                MODEL   bambulab.a1: 256 mm bed, 0.4 mm nozzle, single filament, offline only
                                 (adapter.py, guard.py, facts.py, profiles.py; see BAMBU-LAB.md)
    (p1s.py)             the P1S lands here (issue #127)
templates/orca/<folder>/ machine.json, process.json, filament.json per printer; a model with
                         generated_templates (the A1) ships none: <state>/orca-profiles/<folder>
```

The `[printer] model` key is `<vendor>.<printer>`, which is the package name and the module name. Existing keys did not change.

### What lives where

| Vendor profile (`VENDOR = VendorProfile(...)`) | Printer profile (`MODEL = PrinterModel(...)`) |
|---|---|
| `connection`: `moonraker`, `octoprint` or `offline` | `bed_mm`, `max_print_height_mm` (printable area) |
| `start_code`: `zmod`, `klipper_macro`, `marlin` or `bambu` | printable-area exclusions and motion envelope (in its Orca `machine.json` and adapter) |
| `slicers`: allowed `[printer] slicer` values, first is default | `supported_nozzle_sizes`, `supported_nozzle_types`, `nozzle_diameter_mm` |
| `fallback_slicer`: one isolated attempt after an Orca failure | `ifs_slots` / AMS / AMS Lite, multi-color support |
| `file_formats`: what the printer takes, primary first | `adapter` class, Orca `templates` folder, protected-settings `baseline` |
| `delivery` and `default_delivery`: post-slice options | `label`, `printer.name` |
| `settings`: shared `firmware`, macro names, layout margins, prime tower, cold pull | any vendor setting it needs to override |

Settings merge in this order, later wins key by key:

1. vendor `settings`
2. printer `settings`
3. `config/home.yaml`
4. `config/local.yaml` (or `PRINT_BUDDY_CONFIG`)
5. environment variables

The loaded settings also carry `printer.vendor`, `printer.slicer` and `delivery.methods`.

`tests/printers/golden/model_defaults.json` and `model_settings.json` were captured before the vendor layer existed. `tests/printers/test_vendor_profiles.py` checks that every existing printer still produces exactly those settings. The only differences are the three keys above.

## Config

Existing configs keep working unchanged:

```yaml
printer:
  model: flashforge.ad5x       # the full vendor.printer key, as before
```

These forms are also accepted, and are stored as the full key:

```yaml
printer:
  vendor: sovol                # "bambu" is accepted for "bambulab"
  model: sv06                  # the printer part; on its own it works when only one vendor has that name
  slicer: orca                 # orca everywhere; bambu_studio only where the vendor allows it (Bambu Lab)
delivery:
  methods: [printer, discord]  # a subset of the vendor's delivery options; default is the vendor default
```

Combinations the vendor does not offer stop startup and name the setting. Examples are a vendor that disagrees with a full model key, `slicer: bambu_studio` on a Klipper printer, and `bambu_handoff` on a Flashforge printer. Every existing vendor defaults to `methods: [printer]`, so its Discord flow is exactly what it was. Add `discord` to get a "Send the files here" button after slicing.

## Adding a printer

A new Sovol printer is one module plus its Orca templates. Everything not listed comes from the Sovol vendor profile:

```python
# print_buddy/printers/sovol/sv08.py
"""Sovol SV08 on stock Klipper. Templates: templates/orca/sovol_sv08."""
from ..base import PrinterModel
from ..klipper.adapter import KlipperAdapter
from . import VENDOR

MODEL = PrinterModel(
    key="sovol.sv08",
    label="Sovol SV08 (Klipper)",
    adapter=KlipperAdapter,
    templates="sovol_sv08",
    vendor=VENDOR,
    supported_nozzle_sizes=(0.4,),
    settings={"printer": {"name": "Sovol SV08", "bed_mm": [350.0, 350.0], "max_print_height_mm": 345.0,
                          "nozzle_diameter_mm": 0.4, "ifs_slots": 1}},
)
```

Then add `print_buddy/templates/orca/sovol_sv08/{machine,process,filament}.json` and select it with `printer: {model: sovol.sv08}`. Discovery registers the new module automatically. A module that leaves out `vendor=` inherits its package's `VENDOR`. A module that names another vendor is refused.

A new vendor is a package with an `__init__.py` defining `VENDOR = VendorProfile(key="<package name>", ...)`. `VendorProfile` checks its own consistency. An offline vendor cannot offer `printer`, and only Bambu start code can offer `bambu_handoff`.

## After a slice

The Discord flow is unchanged up to the slice: preview, colors, copies, then "Slice and print now (bed is clear)", "Slice only", "Stop with colors saved" and "Cancel print request". The slice summary still shows per-plate artifacts, estimates and validation. A vendor without `printer` delivery (Bambu Lab) does not get the "Slice and print now" button.

After **Slice only**:

- **Only `printer` enabled** (the default for every existing printer): the same question as before, with the Start button and Cancel.
- **More options enabled**: one menu with the options the profile enables:
  - **Start next plate · bed is clear**: the same per-job bed-clear button, and still the only way to print. It runs every existing print-now gate.
  - **Send the files here** (`discord`): attaches each plate's file to the thread.
  - **Package for Bambu (no upload)** (`bambu_handoff`): builds a `.gcode.3mf` per plate.
  - **Done, don't print**

  After a delivery the menu returns without that option ("Anything else for this slice?"). Each method runs to completion at most once per slice.

Slicer choice, file delivery, printer handoff and print authorization are separate decisions. A delivery never creates, uses up or implies a bed-clear approval.

**Binding.** When the slice returns, the receipt records each G-code's SHA256. A delivery sends only files listed in this job's `JOB.json` whose bytes still match. A changed, missing or unknown file gets "Not delivered: ..." and nothing is sent.

**Discord size limits.** `[discord] upload_max_bytes` (default 9.5 MB, under Discord's 10 MB limit for servers without boosts) is the cap per file:

- A file under the cap is attached as is.
- A larger `.gcode` is zipped (G-code usually shrinks 4 to 6 times) and the zip is sent if it fits.
- If it still does not fit, or it is already compressed (`.3mf`), it is not sent. The reply gives its size and its path on the Print Buddy host.

Every file is one message with one `MEDIA:` attachment, labelled "Plate n of m". A summary line follows: "Sent n of m file(s) here. Too big for Discord: ... Failed: ...".

**Failures and retries.** A failed upload (the adapter reports `success=False`, or the send raises) is reported with the error, and the option stays in the menu. A retry sends only the files not yet delivered. Too-big files cannot be fixed by a retry, so they count as reported and the option closes. Every attempt is recorded under `deliveries` in the intake receipt. Expiry and cancellation behave as before.

## Bambu Lab

| Capability | Status |
|---|---|
| Vendor profile, settings, delivery options | Implemented. Printer profiles (A1, P1S) come from issue #127 and drop into `bambulab/` |
| Start-code guard | Implemented. After slicing, and again before packaging, every G-code line for a Bambu printer must be a plain G, M or T code. Klipper or ZMod macros (`PRINT_START`, `START_PRINT ... MESH=`, `CHANGE_ZCOLOR`, `LOAD_ZOFFSET`, `BED_MESH_*`) block the slice or the package |
| `.gcode.3mf` packaging | Implemented, offline and deterministic. Contents: `Metadata/plate_<n>.gcode`, its upper-case MD5 in `Metadata/plate_<n>.gcode.md5`, `Metadata/slice_info.config` with the plate index and nozzle, and an empty `3D/3dmodel.model`, laid out the way Bambu Studio exports a sliced plate. **Not yet verified on Bambu hardware or in Bambu Studio.** Thumbnails and `printer_model_id` are not written |
| Discord delivery | Implemented. Bambu Lab's primary format is `.gcode.3mf`, so "Send the files here" sends the packages |
| Bambu Studio as the primary slicer | Implemented, opt-in with `printer.slicer: bambu_studio`, allowed only by the Bambu Lab profile. Runs the configured `[bambu] command` (Flathub Bambu Studio with `--unshare=network`) with the same CLI flags as the existing fallback: `--datadir`, `--load-settings`, `--load-filaments`, `--arrange`/`--orient`, `--slice 0`, `--outputdir`. Orca is not run and there is no fallback. Every Print Buddy guard still applies to its output. The real-slicer CI job covers OrcaSlicer only, so this path is tested with a mocked CLI |
| LAN, FTPS, MQTT or cloud upload | **Unsupported.** No client exists. "Package for Bambu" says nothing was uploaded |
| Starting, pausing or stopping a print, telemetry | **Unsupported.** No Start button is offered for a Bambu printer. Open the package in Bambu Studio or Bambu Handy, or copy it to the SD card |
| AMS / AMS Lite multi-color | Unsupported, per the printer profile (issue #127) |

Bambu Studio is not treated as a submission path. Picking it as the slicer changes only which program writes the G-code. Live upload or print control would need its own evaluation of firmware, LAN versus cloud authentication and credential handling before any of it is built.
