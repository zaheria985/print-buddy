# 100%-scale geometry inspection, plate arrangement, and export

`print_buddy.core.geometry_layout` inspects STL and 3MF geometry in millimeters, packs multi-object or single-object assemblies onto a nominal build volume at 100% physical scale without shrinking, and exports verified geometry artifacts plus a machine-readable `validation.json` report.

This workflow is offline preparation only. It never invokes a slicer, uploads files to Moonraker, or starts a print.

## Status ladder

Every inspection or arrangement report includes a five-stage `status_ladder` so nominal bounding-box fit is never confused with slicer acceptance or physical print verification:

1. `geometry_inspected`: STL/3MF triangles, unit conversion, transforms, and mesh topology were parsed and measured.
2. `geometry_nominal_fit`: Every part's axis-aligned bounding box fits inside the usable nominal bed (`bed_mm` minus `2 * margin_mm` on X/Y, `0..bed_z` on Z) with `>= gap_mm` clearance between part bounding boxes.
3. `slicer_capacity_verified`: Verified by OrcaSlicer plate capacity / exclusion-zone checks (`not_run` during offline geometry layout).
4. `gcode_sliced_and_guarded`: Sliced by OrcaSlicer and passed Print Buddy offline G-code safety guards (`not_run` during offline geometry layout).
5. `physical_print_tested`: Confirmed on physical hardware (`not_run` during offline geometry layout).

## CLI usage

Inspect an STL or 3MF without writing arranged geometry files:

```bash
python3 -m print_buddy.core.model_job layout path/to/model.3mf --inspect-only
```

Arrange and export 100%-scale artifacts (`*_100pct_arranged_GEOMETRY_ONLY.3mf`, `*_100pct_arranged.stl`, per-part `*_100pct.stl`, `plate_preview.png`, and `validation.json`):

```bash
python3 -m print_buddy.core.model_job layout path/to/model.3mf -o out/model_layout
```

Optional flags:

- `--bed X Y Z`: nominal build volume in mm (default: `220 220 220`).
- `--margin MM`: XY bed margin in mm (default: `5.0`).
- `--gap MM`: minimum XY gap between part bounding boxes in mm (default: `5.0`).
- `--input-unit UNIT`: explicit unit (`mm`, `cm`, `m`, `inch`, `ft`, `micron`) for unitless STL files.
- `--split-stl-components`: split disconnected STL triangle shells into separate selectable parts before packing.
- `--allow-rotations DEG ...`: candidate Z rotations in degrees.

## Scale, unit, and 3MF metadata rules

- **No silent shrinking:** `additional_scale_factor` is always `1.0`. If a single rigid body cannot fit the usable bed at 100% scale across the allowed Z rotations, `arrange_and_export` raises `RuntimeError` instead of scaling the part down.
- **Authored 3MF transforms:** If a source 3MF build item or component already carries a non-unit scale or shear in its 3MF transform matrix (for example an authored `0.85` scale factor), `inspect_geometry` records a warning and preserves that authored geometry. It never claims to recover unknown original CAD dimensions by inverting internal 3MF transforms.
- **STL units:** STL files do not reliably encode units. When `--input-unit` is omitted, millimeters (`1.0`) are assumed and recorded in `warnings`.
- **3MF semantic guards:**
  - Archives containing negative-volume boolean cutters (`subtype="negative_volume"`) are refused by geometry-only arrangement so boolean holes are never silently dropped.
  - Multi-object geometry-only exports are named `*_GEOMETRY_ONLY.3mf` because they contain `<mesh>` and `<build>` items only. When a millimeter 3MF has unique printable build items, `arrange_and_export` also writes `*_100pct_arranged_preserved.3mf` with updated `<item transform="...">` entries so source metadata and color assignments remain intact for downstream OrcaSlicer slicing.
