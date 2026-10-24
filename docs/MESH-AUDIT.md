# Read-only mesh topology audit

[Project overview](../README.md) · [Documentation home](README.md) · [Contributing](../CONTRIBUTING.md)

Print Buddy includes a conservative, read-only triangle-topology audit in `print_buddy.core.model_job`. It evaluates indexed triangles for STL and 3MF source geometry without repairing, modifying, or slicing the model.

## Command-line usage

Run the audit directly without a printer, slicer, or network connection:

```bash
python3 -m print_buddy.core.model_job audit path/to/model.stl
python3 -m print_buddy.core.model_job audit path/to/project.3mf
```

The command prints a formatted JSON topology report to standard output and leaves the input file unchanged.

## Output metrics

The topology audit produces a versioned v1 report. Existing flat topology keys and the legacy `sha256` field remain intact for compatibility. The artifact also records source identity, bounding dimensions, units, and the printer profile when available.

| Field | Description |
| --- | --- |
| `schema_version` | Integer report schema version (currently `1`). |
| `source_sha256` | SHA-256 hex digest of the audited source file. The legacy `sha256` alias remains available. |
| `dimensions_mm` | `[x, y, z]` printable-geometry bounding-box dimensions in millimeters, rounded to two decimal places like `audit_source` geometry metrics. |
| `units` | Explicit dimensions unit policy, always `"mm"` in schema v1. |
| `printer_model` | Configured printer profile ID used for prepared-job fit probing; standalone audit reports `null` when no profile is configured. |
| `sha256` | Backward-compatible SHA-256 digest of the source file. |
| `triangle_count` | Total number of faces analyzed from the model. |
| `connected_shells` | Number of topologically connected face components. |
| `boundary_edges` | Number of mesh edges referenced by only one face (open holes or edges). |
| `non_manifold_edges` | Number of mesh edges referenced by more than two faces (T-junctions or fan geometry). |
| `singular_vertices` | Count of vertices with non-disk or non-manifold incident edge topology. |
| `winding_inconsistencies` | Number of shared edges with identical face traversal direction (inconsistent surface normals). |
| `degenerate_triangles` | Count of triangles with repeated or collinear vertices producing zero surface area. |
| `watertight` | Boolean indicating whether `boundary_edges == 0` and `non_manifold_edges == 0`. |
| `manifold` | Boolean indicating strict manifoldness (no boundaries, non-manifold edges, singular vertices, degenerate triangles, or winding inconsistencies). |

## Topology heuristics and limitations

- Vertices are welded by rounding each coordinate to six decimal places (a
  `1e-6` grid in source coordinate units), not by a Euclidean distance test.
  Nearby points across a rounding boundary need not weld.
- Repeated welded vertices and squared 3D cross-product magnitudes (squared
  double-area) `<= 1e-18` source-units to the fourth power are degenerate.
  The area test uses original coordinates. These faces count toward
  `triangle_count` but not edge, vertex-fan, or shell adjacency.
- Closed vertices must have one connected incident face fan. Two otherwise
  closed shells touching at one vertex are watertight by the edge-only
  definition but are not manifold.
- Shell adjacency uses a linear star (at most `k-1` links for `k` edge uses),
  including edges with more than 64 faces. It never expands all face pairs;
  dense fans remain connected and are still reported as non-manifold.
- Self-intersections are not detected. These heuristics are not a geometric
  proof or approval to print. An all-degenerate mesh can satisfy the
  edge-only `watertight` definition while failing `manifold` for degeneracy.

## Integration with job preparation

When preparing a print job, `audit(path, printer_model=...)`:

1. The mesh audit report is stored under the `mesh_audit` key in the source audit dictionary and persisted in `JOB.json`.
2. If geometry extraction fails (for example, an unreadable file or corrupt archive), the error message is recorded under `mesh_audit_error` without terminating intake processing prematurely.

## Offline placement candidates

The versioned `mesh_audit` report also includes `placement_candidates`. This is deterministic geometric discovery from the mesh vertices: it evaluates upright and axis-up placements at cardinal bed yaw angles and convex-hull edge yaw angles. Each row reports the rigid rotation, XYZ Euler angles, axis-aligned footprint, per-axis bed margins, minimum margin, fit result, and diagonal footprint gain. Rows are ranked deterministically, retaining the authored upright placement first when it fits.

This report is an estimate against the configured full bed and height, not a slicing acceptance result. It does not apply feature clearance, brim, adhesion, support, or object-spacing rules. Existing slicer-backed `rotation_alternatives()` remains the final orientation acceptance check before slicing; a candidate marked as fitting is not approval to print.

## Scope and boundaries

The topology audit is strictly read-only and conservative:

- It **does not repair** mesh defects, close open boundaries, or flip inverted normals. Automatic repair could alter mounting dimensions, bolt clearance, or wall thickness.
- It **does not detect** self-intersecting shells, unit scale errors, or slicer toolpath artifacts.
- It is not a guarantee of bed adhesion, slice success, or physical printability. Slicer-backed fit and clearance checks remain necessary prior to printing.
