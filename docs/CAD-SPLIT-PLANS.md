# Opt-in reviewed CAD split plans and dowel connectors

Print Buddy never auto-cuts a single connected solid mesh when arranging parts (`python3 -m print_buddy.core.model_job layout`). When an oversized single mesh cannot or should not be printed intact, `python3 -m print_buddy.core.model_job cad-split` provides an **opt-in**, schema-validated CAD split workflow with alignment/connector bores and printable chamfered dowels.

## Safety and structural boundaries

1. **Intact diagonal print first:** Before cutting any mesh, `cad-split` checks whether the intact mesh fits the nominal bed under a Z rotation (such as 45 degrees on a 220 x 220 x 220 mm AD5X bed) and exports the intact rotated STL alongside the split artifacts.
2. **Reviewed split plans only:** Every split requires an explicit `--plan` preset (`rack_adapter_10to19`, `macmini_mount_a1347`) or a reviewed JSON split-plan file. Unreviewed arbitrary planar cuts are rejected.
3. **Volume conservation and topology checks:** When `--execute` runs OpenSCAD CSG booleans, Print Buddy verifies that both resulting halves are watertight, 1-shell solids and that the removed volume equals the theoretical cylindrical bore volume within tolerance (`whole_volume - (left_volume + right_volume) == expected_bore_volume`).
4. **Honest status reporting:** `split_validation.json` records the 5-stage `status_ladder` (`geometry_inspected`, `geometry_nominal_fit`, `slicer_capacity_verified`, `gcode_sliced_and_guarded`, `physical_print_tested`) and never marks slicer or physical print stages as passed during offline CAD preparation.
5. **No structural load rating:** Socket bores and printed/metal dowels locate and bond parts; no structural load rating has been performed.

## Built-in reviewed presets

| Preset ID | Target geometry | Split plane | Connectors & clearances |
| --- | --- | --- | --- |
| `rack_adapter_10to19` | 10-to-19-inch rack adapter (`245.0 x 44.5 x 20.0 mm`, 1 shell) | `X = 0.0 mm` | 4 blind bores (`Ø5.40 mm x 16.5 mm` per side at `Y = ±12 mm, Z = 6, 14 mm`) + 4 printable chamfered dowels (`Ø5.00 mm x 30.5 mm`, `0.20 mm` radial clearance, `2.5 mm` length relief) |
| `macmini_mount_a1347` | Mac Mini A1347 mount (`254.0 x 44.625 x 183.4078 mm`, 2 overlapping shells) | `X = 0.0 mm` (shell-wise cut + union) | 2 front M3 pin bores (`Ø3.35 mm x 11.5 mm` per side at `Y = -18.7 mm, Z = 4.5, 11.5 mm`) + 1 rear filament guide bore (`Ø2.15 mm x 11.5 mm` per side at `Y = 20.7 mm, Z = 5.5 mm`) |

## CLI usage

Prepare the intact rotated STL, printable chamfered dowels, OpenSCAD CSG script, and `split_validation.json` without invoking external CAD binaries:

```bash
python3 -m print_buddy.core.model_job cad-split \
  path/to/10to19_adapter_with_ears.stl \
  --plan rack_adapter_10to19 \
  --outdir ./rack_split_out
```

Also execute OpenSCAD to render the boolean split halves and verify post-boolean volume conservation:

```bash
python3 -m print_buddy.core.model_job cad-split \
  path/to/10to19_adapter_with_ears.stl \
  --plan rack_adapter_10to19 \
  --outdir ./rack_split_out \
  --execute
```
