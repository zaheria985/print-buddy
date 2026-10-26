"""Offline Orca adapter. Geometry conversion never imports project machine settings."""
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import time
import xml.etree.ElementTree as ET
import zipfile
from .. import logs, printers, settings
from ..printers.base import start_code_problems
from . import mesh_fast
from .compact_geometry import IndexedTriangles
from . import slice_guard as sg
from .slice_guard import pre_slice, post_slice

logger = logging.getLogger(__name__)

_S = settings.current()
TEMPLATES = _S['orca']['templates']
BED = _S['printer']['bed_mm']
MAX_HEIGHT_MM = _S['printer']['max_print_height_mm']
IFS_SLOTS = _S['printer']['ifs_slots']
NOZZLE_MM = _S['printer']['nozzle_diameter_mm']
# PB_ORCA_CMD selects a native OrcaSlicer binary without changing the home Flatpak default.
# ORCA_FLATPAK_APP_ID selects a different installed Flatpak app while preserving that default.
def _orca_command():
    """Return the effective command, honoring runtime overrides for test tracks."""
    override = os.environ.get('PB_ORCA_CMD')
    if override:
        return [override]
    command = list(_S['orca']['command'])
    app_id = os.environ.get('ORCA_FLATPAK_APP_ID')
    if command and Path(command[0]).name == 'flatpak' and len(command) > 2 and command[1] == 'run':
        if app_id:
            if command[2].startswith('--'):
                return [command[0], 'run', command[2], app_id, *command[3:]]
            return [command[0], 'run', app_id, *command[3:]]
        if command[2] == 'com.orcaslicer.OrcaSlicer':
            commit = os.environ.get('ORCA_FLATPAK_COMMIT')
            if commit:
                return [command[0], 'run', f'--commit={commit}', *command[2:]]
    return command


ORCA_COMMAND = _orca_command()
BAMBU_COMMAND = list(_S['bambu']['command'])
_FLASH_STUDIO_CLI_CACHE: dict = {}


def _flash_studio_command(printer):
    """Return the configured Flash Studio executable, or None for normal Orca behavior."""
    configured = printer.config.get('slicer', {}).get('flash_studio_path', '')
    if not configured:
        return None
    vendor = printer.model.vendor
    if vendor is None or vendor.key != 'flashforge':
        raise RuntimeError('[slicer] flash_studio_path is only supported for Flashforge printers')
    path = Path(configured).expanduser()
    if not path.is_file() or not os.access(path, os.X_OK):
        raise RuntimeError(f'[slicer] flash_studio_path is configured but is not an executable file: {path}')
    return [str(path.resolve())]


def _check_flash_studio_cli(command):
    """Fail closed unless this exact executable advertises every flag used for this slice."""
    executable = Path(command[0])
    stat = executable.stat()
    cache_key = (str(executable.resolve()), stat.st_mtime_ns, stat.st_size)
    required = {arg.split('=', 1)[0] for arg in command[1:] if arg.startswith('--')}
    supported = _FLASH_STUDIO_CLI_CACHE.get(cache_key)
    if supported is None:
        try:
            help_result = subprocess.run([str(executable), '--help'], capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f'Flash Studio CLI help check failed for {executable}: {exc}') from exc
        help_text = help_result.stdout + '\n' + help_result.stderr
        if help_result.returncode:
            raise RuntimeError(f'Flash Studio CLI help check failed ({help_result.returncode}) for {executable}')
        supported = set(re.findall(r'--[A-Za-z][A-Za-z0-9-]*', help_text))
        _FLASH_STUDIO_CLI_CACHE[cache_key] = supported
    missing = sorted(required - supported)
    if missing:
        raise RuntimeError('Flash Studio CLI does not advertise required headless option(s): '
                           + ', '.join(missing))


# Margin semantics: this is bed-edge clearance only; object-to-object spacing
# is a separate packing gap. Apply edge clearance once by shrinking the usable bed,
# never by also dilating each object's footprint. Feature clearance is added below.
MULTI_COPY_EDGE_MM = _S['layout']['multi_copy_edge_mm']
SINGLE_COPY_EDGE_MM = _S['layout']['single_copy_edge_mm']
NS = {'c': 'http://schemas.microsoft.com/3dmanufacturing/core/2015/02'}
IDENTITY = [1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0]


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def export_geometry(source, target, scale=1.0, rotation=None, bed=None):
    """Flatten 3MF build/component transforms exactly; preserve every triangle/order.

    No mesh repair, merging or per-part rearrangement. An optional operator-selected
    rigid rotation, then uniform scale, is applied before rigid centering on the bed (bed, default the configured one).
    A source with negative/modifier volumes goes through color_geometry, which still centres at 110 mm.
    STL float32 rounding is recorded by hashes. Non-mm and nonprintable builds refuse.
    """
    scale = float(scale) if scale is not None else 1.0
    if not scale > 0:
        raise ValueError('scale must be greater than 0')
    _check_rotation(rotation)
    if has_cutters(source):
        # Flattening to STL would either print the cutter as a solid (Orca -101
        # conflict) or drop it and fill the hole. Keep it as a negative volume.
        if Path(target).suffix.lower() != '.3mf':
            raise RuntimeError('source has negative/modifier volumes; export to .3mf, not STL')
        from .color_geometry import export_volume_plate
        return export_volume_plate(source, target, scale=scale, rotation=rotation)
    return _place_triangles(_3mf_triangles(source), target, scale, rotation, bed)


def has_cutters(source):
    source = Path(source)
    if source.suffix.lower() != '.3mf':
        return False
    from .source_colors import cutter_ids
    with zipfile.ZipFile(source) as archive:
        return bool(cutter_ids(archive))


_TRIANGLE_CACHE: dict = {}


def _cache_key(source):
    st = Path(source).stat()
    return (str(Path(source).resolve()), st.st_mtime_ns, st.st_size)


def _3mf_triangles(source):
    """Flattened build triangles of a 3MF, parsed once per process per file version.

    Fit checks read the same dense mesh many times (capacity, scale ladder, rotations).
    Parsing is ~20 s for 683k triangles, so it is cached on (path, mtime, size). The list
    is shared: callers must not mutate it.
    """
    key = _cache_key(source)
    hit = _TRIANGLE_CACHE.get(key)
    if hit is None:
        _TRIANGLE_CACHE.clear()  # one model per worker; never hold two dense meshes
        hit = _TRIANGLE_CACHE[key] = _parse_3mf_triangles(source)
    return hit


def _parse_3mf_triangles(source):
    with zipfile.ZipFile(source) as archive:
        models = {}
        tables = {}
        def model(name):
            name = name.lstrip('/')
            if name not in models:
                data, fast = mesh_fast.archive_tables(archive, name)
                if fast is not None:
                    tables[name] = fast
                root = ET.fromstring(data)
                if root.get('unit', 'millimeter') != 'millimeter':
                    raise RuntimeError('only millimeter 3MF geometry supported')
                models[name] = (root, {o.get('id'): o for o in root.findall('c:resources/c:object', NS)})
            return models[name]
        def apply(point, t):
            # Same float operation order as sum(p[j] * t[j*3+i]) + t[9+i]; output is bit-identical.
            x, y, z = (float(value) for value in point)
            return (0 + x * t[0] + y * t[3] + z * t[6] + t[9],
                    0 + x * t[1] + y * t[4] + z * t[7] + t[10],
                    0 + x * t[2] + y * t[5] + z * t[8] + t[11])
        def transform(node):
            values = list(map(float, node.get('transform', ' '.join(map(str, IDENTITY))).split()))
            if len(values) != 12:
                raise RuntimeError('invalid 3MF transform')
            return values
        def transform_vertices(vertices, chain):
            # Vectorized twin of [apply(p, t) for t in chain for p in vertices]:
            # each coordinate keeps apply's exact left-to-right operation
            # order (0 + x*t[0] + y*t[3] + z*t[6] + t[9]) as separate
            # elementwise steps, so results are bit-identical per vertex.
            import numpy as np
            result = np.asarray(vertices, dtype=np.float64).reshape((-1, 3))
            for t in chain:  # transform each shared vertex once, not once per triangle reference
                x, y, z = result[:, 0], result[:, 1], result[:, 2]
                result = np.stack((
                    0.0 + x * t[0] + y * t[3] + z * t[6] + t[9],
                    0.0 + x * t[1] + y * t[4] + z * t[7] + t[10],
                    0.0 + x * t[2] + y * t[5] + z * t[8] + t[11],
                ), axis=1)
            return result

        def collect(name, oid, chain, active):
            key = (name, oid)
            if key in active:
                raise RuntimeError('cyclic 3MF component reference')
            obj = model(name)[1][oid]
            mesh = obj.find('c:mesh', NS)
            out = IndexedTriangles()
            if mesh is not None:
                if name in tables:
                    table = tables[name].get(oid)
                    if table is not None:
                        out.extend_indexed(transform_vertices(table.vertices, chain),
                                            table.triangles)
                else:
                    import numpy as np
                    vertices = [tuple(float(v.get(k)) for k in ('x', 'y', 'z')) for v in mesh.findall('c:vertices/c:vertex', NS)]
                    faces = [tuple(int(tri.get(i)) for i in ('v1', 'v2', 'v3')) for tri in mesh.findall('c:triangles/c:triangle', NS)]
                    if faces:
                        face_array = np.array(faces, dtype=np.int64)
                        count = len(vertices)
                        if int(face_array.max()) >= count or int(face_array.min()) < -count:
                            # The reference generator raises IndexError from
                            # vertices[a] for exactly this range.
                            raise IndexError('3MF triangle vertex index out of range')
                        face_array = np.where(face_array < 0, face_array + count, face_array)
                        out.extend_indexed(transform_vertices(vertices, chain), face_array)
            for component in obj.findall('c:components/c:component', NS):
                external = next((v for k, v in component.attrib.items() if k.endswith('}path')), name)
                out.extend(collect(external.lstrip('/'), component.get('objectid'),
                                   [transform(component)] + chain, active | {key}))
            return out
        from .source_colors import cutter_ids
        cutters = cutter_ids(archive)
        root, objects = model('3D/3dmodel.model')
        result = IndexedTriangles()
        for item in root.findall('c:build/c:item', NS):
            if item.get('printable', '1') != '1':
                raise RuntimeError('nonprintable build item requires explicit selection')
            oid = item.get('objectid')
            if cutters and any(k[0] == oid for k in cutters):
                # Footprint/fit only: printed volumes, never negative or modifier volumes.
                top = objects[oid]
                for component in top.findall('c:components/c:component', NS):
                    if (oid, component.get('objectid')) in cutters:
                        continue
                    external = next((v for k, v in component.attrib.items() if k.endswith('}path')), '3D/3dmodel.model')
                    result.extend(collect(external.lstrip('/'), component.get('objectid'),
                                          [transform(component), transform(item)], {('3D/3dmodel.model', oid)}))
                continue
            result.extend(collect('3D/3dmodel.model', oid, [transform(item)], set()))
    if not result:
        raise RuntimeError('3MF contains no printable triangles')
    return result


def _object_triangles(source, object_id):
    """World-space triangles of one build object, for per-part surface stats.

    The per-object companion to :func:`_parse_3mf_triangles`: same component
    walk and transform composition, restricted to the build items of a single
    object id, returned as a plain [(a, b, c), ...] list of world-space
    vertex triples. Returns [] when the object has no build item. Used by the
    split fit check (#221 R1), which needs one part's faces — not the whole
    model's — to measure bed contact and overhang for a reorientation.
    """
    object_id = str(object_id)

    def local(tag):
        return tag.split('}')[-1]

    def children(node, name):
        return [child for child in node if local(child.tag) == name]

    def child(node, name):
        found = children(node, name)
        return found[0] if found else None

    with zipfile.ZipFile(source) as archive:
        models = {}

        def model(name):
            # Elements are matched by local name, exactly as
            # color_geometry._world_item_points reads the same packages:
            # namespace-less and core-namespace files both parse.
            name = name.lstrip('/')
            if name not in models:
                root = ET.fromstring(archive.read(name))
                if root.get('unit', 'millimeter') != 'millimeter':
                    raise RuntimeError('only millimeter 3MF geometry supported')
                objects = {}
                for resources in children(root, 'resources'):
                    for obj in children(resources, 'object'):
                        objects[obj.get('id')] = obj
                models[name] = (root, objects)
            return models[name]

        def apply(point, t):
            x, y, z = point
            return (0 + x * t[0] + y * t[3] + z * t[6] + t[9],
                    0 + x * t[1] + y * t[4] + z * t[7] + t[10],
                    0 + x * t[2] + y * t[5] + z * t[8] + t[11])

        def transform(node):
            values = list(map(float, node.get('transform', ' '.join(map(str, IDENTITY))).split()))
            if len(values) != 12:
                raise RuntimeError('invalid 3MF transform')
            return values

        def collect(name, oid, chain, active):
            key = (name, oid)
            if key in active:
                raise RuntimeError('cyclic 3MF component reference')
            obj = model(name)[1][oid]
            out = []
            mesh = child(obj, 'mesh')
            if mesh is not None:
                vertex_nodes = []
                for vertices_node in children(mesh, 'vertices'):
                    vertex_nodes.extend(children(vertices_node, 'vertex'))
                vertices = [tuple(float(v.get(k)) for k in ('x', 'y', 'z')) for v in vertex_nodes]
                for t in chain:
                    vertices = [apply(p, t) for p in vertices]
                for triangles_node in children(mesh, 'triangles'):
                    for tri in children(triangles_node, 'triangle'):
                        a, b, c = (int(tri.get(i)) for i in ('v1', 'v2', 'v3'))
                        out.append((vertices[a], vertices[b], vertices[c]))
            components = []
            for wrapper in children(obj, 'components'):
                components.extend(children(wrapper, 'component'))
            for component in components:
                external = next((v for k, v in component.attrib.items()
                                 if k.split('}')[-1] == 'path'), name)
                out.extend(collect(external.lstrip('/'), component.get('objectid'),
                                   [transform(component)] + chain, active | {key}))
            return out

        root, _objects = model('3D/3dmodel.model')
        result = []
        for build in children(root, 'build'):
            for item in children(build, 'item'):
                if item.get('objectid') != object_id:
                    continue
                if item.get('printable', '1') != '1':
                    raise RuntimeError('nonprintable build item requires explicit selection')
                result.extend(collect('3D/3dmodel.model', object_id, [transform(item)], set()))
        return result


def _check_rotation(rotation):
    if rotation is None:
        return None
    values = [float(v) for v in rotation]
    if len(values) != 9:
        raise ValueError('rotation must be a 3x3 row-major matrix')
    # Only rigid rotations: orthonormal rows and determinant +1 (no mirroring or scaling).
    rows = [values[0:3], values[3:6], values[6:9]]
    for i in range(3):
        for j in range(3):
            dot = sum(rows[i][k] * rows[j][k] for k in range(3))
            if abs(dot - (1.0 if i == j else 0.0)) > 1e-6:
                raise ValueError('rotation must be orthonormal')
    det = (rows[0][0] * (rows[1][1] * rows[2][2] - rows[1][2] * rows[2][1])
           - rows[0][1] * (rows[1][0] * rows[2][2] - rows[1][2] * rows[2][0])
           + rows[0][2] * (rows[1][0] * rows[2][1] - rows[1][1] * rows[2][0]))
    if abs(det - 1.0) > 1e-6:
        raise ValueError('rotation must not mirror the model')
    return values


def _rotate(point, m):
    x, y, z = point
    return (m[0] * x + m[1] * y + m[2] * z, m[3] * x + m[4] * y + m[5] * z, m[6] * x + m[7] * y + m[8] * z)


def _place_indexed(result, target, scale, rotation, bed):
    """_place_triangles over indexed storage: transform the shared vertices
    once, compute bounds from gathered faces in chunks, and stream the binary
    STL out in bounded batches. Arithmetic mirrors the scalar path per point:
    rotate (m[0]*x + m[1]*y + m[2]*z, ...), then scale, then translate in
    float64 before the float32 STL conversion."""
    import numpy as np
    vertices = result.vertices
    if rotation is not None:
        m = rotation
        x, y, z = vertices[:, 0], vertices[:, 1], vertices[:, 2]
        vertices = np.stack((
            m[0] * x + m[1] * y + m[2] * z,
            m[3] * x + m[4] * y + m[5] * z,
            m[6] * x + m[7] * y + m[8] * z,
        ), axis=1)
    if scale != 1.0:
        vertices = vertices * scale
    faces = result.faces
    lo = [math.inf, math.inf, math.inf]
    hi = [-math.inf, -math.inf, -math.inf]
    for start in range(0, len(faces), 8192):
        pts = vertices[faces[start:start + 8192].ravel()]
        chunk_lo, chunk_hi = pts.min(axis=0), pts.max(axis=0)
        for axis in range(3):
            lo[axis] = min(lo[axis], float(chunk_lo[axis]))
            hi[axis] = max(hi[axis], float(chunk_hi[axis]))
    bounds = [(lo[axis], hi[axis]) for axis in range(3)]
    center = [float(v) / 2 for v in (bed or BED)]
    delta = [center[a] - sum(bounds[a]) / 2 for a in (0, 1)] + [-bounds[2][0]]
    placed_bounds = [(bounds[a][0] + delta[a], bounds[a][1] + delta[a]) for a in range(3)]
    record = np.dtype([('normal', '<f4', (3,)), ('points', '<f4', (9,)), ('attr', '<u2')])
    with Path(target).open('wb') as output:
        output.write(b'3MF transformed triangles, rigid centered translation'.ljust(80, b'\0'))
        output.write(struct.pack('<I', len(result)))
        for start in range(0, len(faces), 8192):
            chunk = faces[start:start + 8192]
            pts = vertices[chunk.ravel()].reshape((-1, 3, 3)) + np.asarray(delta)
            rows = np.zeros(len(chunk), dtype=record)
            rows['points'] = pts.reshape((-1, 9)).astype('<f4')
            output.write(rows.tobytes())
    res = {'triangles': len(result), 'build_bounds_mm': bounds, 'translation_mm': delta,
           'placed_bounds_mm': placed_bounds,
           'converted_sha256': digest(target), 'policy': 'all triangles; component/build transforms; rigid translation only; float32 STL'}
    if scale != 1.0:
        res['uniform_scale'] = str(scale)
        res['scale_policy'] = 'uniform scale applied to source vertices before rigid bed centering'
    if rotation is not None:
        res['rotation_matrix'] = rotation
        res['policy'] += '; operator-selected rigid rotation applied before scale'
    return res


def _place_triangles(result, target, scale=1.0, rotation=None, bed=None):
    """Rotate (rigid), uniformly scale, then center on the bed with the lowest vertex on Z0.

    bed is the target printer's (x, y) bed in mm; the configured printer's bed when omitted (220 mm: 110, 110).
    """
    scale = float(scale) if scale is not None else 1.0
    if not scale > 0:
        raise ValueError('scale must be greater than 0')
    rotation = _check_rotation(rotation)
    if isinstance(result, IndexedTriangles):
        return _place_indexed(result, target, scale, rotation, bed)
    if rotation is not None:
        result = [[_rotate(p, rotation) for p in tri] for tri in result]
    if scale != 1.0:
        result = [[(p[0] * scale, p[1] * scale, p[2] * scale) for p in tri] for tri in result]
    # One rigid translation only: center XY on the bed and put the lowest vertex on Z0.
    center = [float(v) / 2 for v in (bed or BED)]
    bounds = [(min(p[a] for t in result for p in t), max(p[a] for t in result for p in t)) for a in range(3)]
    delta = [center[a] - sum(bounds[a]) / 2 for a in (0, 1)] + [-bounds[2][0]]
    placed_bounds = [(bounds[a][0] + delta[a], bounds[a][1] + delta[a]) for a in range(3)]
    with Path(target).open('wb') as output:
        output.write(b'3MF transformed triangles, rigid centered translation'.ljust(80, b'\0'))
        output.write(struct.pack('<I', len(result)))
        for triangle in result:
            output.write(struct.pack('<12fH', 0, 0, 0, *(p[a] + delta[a] for p in triangle for a in range(3)), 0))
    res = {'triangles': len(result), 'build_bounds_mm': bounds, 'translation_mm': delta,
           'placed_bounds_mm': placed_bounds,
           'converted_sha256': digest(target), 'policy': 'all triangles; component/build transforms; rigid translation only; float32 STL'}
    if scale != 1.0:
        res['uniform_scale'] = str(scale)
        res['scale_policy'] = 'uniform scale applied to source vertices before rigid bed centering'
    if rotation is not None:
        res['rotation_matrix'] = rotation
        res['policy'] += '; operator-selected rigid rotation applied before scale'
    return res


def read_stl_triangles(path):
    """Read binary or ASCII STL triangles as [[(x, y, z)] * 3]."""
    path = Path(path)
    with path.open('rb') as stream:
        header = stream.read(84)
        if len(header) == 84:
            count = struct.unpack('<I', header[80:84])[0]
            if path.stat().st_size == 84 + count * 50:
                from .compact_geometry import CompactTriangles
                result = CompactTriangles()
                while chunk := stream.read(50 * 8192):
                    for row in struct.iter_unpack('<12fH', chunk):
                        result.append([row[3:6], row[6:9], row[9:12]])
                return result
    data = path.read_bytes()
    text = data.decode('utf-8', errors='ignore')
    points = [tuple(map(float, m.groups())) for m in re.finditer(
        r'vertex\s+(\S+)\s+(\S+)\s+(\S+)', text)]
    if not points or len(points) % 3:
        raise RuntimeError(f'{path} is not a readable STL')
    return [points[i:i + 3] for i in range(0, len(points), 3)]


def source_triangles(source):
    """All printable triangles of a 3MF (build transforms applied) or STL, in source coordinates.

    Cached per process; the returned list is shared and must not be mutated.
    """
    source = Path(source)
    if source.suffix.lower() == '.3mf':
        return _3mf_triangles(source)
    key = ('stl',) + _cache_key(source)
    hit = _TRIANGLE_CACHE.get(key)
    if hit is None:
        _TRIANGLE_CACHE.clear()
        hit = _TRIANGLE_CACHE[key] = read_stl_triangles(source)
    return hit


_FOOTPRINT_CACHE: dict = {}


def _footprint(source, rotation=None):
    """(XY convex hull, z_min, z_max) of the model after an optional rigid rotation.

    Orca's arrange only sees the XY hull, so the fit probe needs nothing else. Cached on
    file version + rotation so repeated scale probes skip the dense mesh entirely.
    """
    source = Path(source)
    key = (_cache_key(source), tuple(rotation) if rotation is not None else None)
    hit = _FOOTPRINT_CACHE.get(key)
    if hit is None:
        if len(_FOOTPRINT_CACHE) > 16:
            _FOOTPRINT_CACHE.clear()
        points = _unique_points(source_triangles(source))
        if not points:
            raise RuntimeError(f'Orca fit probe cannot determine capacity for {source}: projected geometry is empty')
        if rotation is None:
            xy = [(p[0], p[1]) for p in points]
            zs = [p[2] for p in points]
        else:
            m = rotation
            xy = [(m[0] * x + m[1] * y + m[2] * z, m[3] * x + m[4] * y + m[5] * z) for x, y, z in points]
            zs = [m[6] * x + m[7] * y + m[8] * z for x, y, z in points]
        hit = _FOOTPRINT_CACHE[key] = (_hull(xy), min(zs), max(zs))
    return hit


def place_stl(source, target, scale=1.0, rotation=None, bed=None):
    """Write a rotated/scaled/bed-centered STL from a 3MF or STL source."""
    source = Path(source)
    if source.suffix.lower() == '.3mf':
        return export_geometry(source, target, scale=scale, rotation=rotation, bed=bed)
    return _place_triangles(read_stl_triangles(source), target, scale, rotation, bed)


# Thin, print-in-place prints (flexy rings, articulated toys): per the AD5X Flexy Ring guide,
# Arachne walls, 2 walls, 15% line infill, 0.20 mm first layer at 20 mm/s, moderate wall speeds,
# and nothing that can fuse moving gaps (brim, supports, ironing, fuzzy skin).
FLEX_PROFILE = {
    'wall_generator': 'arachne', 'wall_loops': '2',
    'sparse_infill_density': '15%', 'sparse_infill_pattern': 'line',
    'initial_layer_print_height': '0.2', 'initial_layer_speed': '20', 'initial_layer_infill_speed': '20',
    'outer_wall_speed': '70', 'inner_wall_speed': '130', 'sparse_infill_speed': '160',
    'brim_type': 'no_brim', 'brim_width': '0', 'enable_support': '0', 'raft_layers': '0',
    'ironing_type': 'no ironing', 'fuzzy_skin': 'none',
}

PROCESS_KEYS = ('layer_height', 'initial_layer_print_height', 'wall_loops', 'sparse_infill_density',
                'sparse_infill_pattern', 'brim_type', 'brim_width', 'brim_object_gap', 'enable_support',
                'support_type', 'support_on_build_plate_only', 'top_shell_layers', 'bottom_shell_layers',
                'top_shell_thickness', 'bottom_shell_thickness', 'gap_fill_target', 'filter_out_gap_fill',
                'wall_generator')


RANGE_ERROR = re.compile(r'^(\w+): (\S+) not in range \[([-0-9.eE+]+),([-0-9.eE+]+)\]', re.M)


def _clamp(value, low, high):
    def one(v):
        try:
            x = float(v)
        except ValueError:
            return v
        if low <= x <= high:
            return v
        x = min(max(x, low), high)
        return str(int(x)) if x.is_integer() else repr(x)
    if isinstance(value, list):
        return [one(v) for v in value]
    return ','.join(one(v) for v in str(value).split(','))


def _template_settings(template_root):
    settings = {}
    for name in ('machine.json', 'process.json', 'filament.json'):
        path = Path(template_root) / name
        if path.is_file():
            settings.update(json.loads(path.read_text()))
    return settings


IMPORTED_AUTO_SENTINELS = {
    'raft_first_layer_expansion': '-1',
    'tree_support_wall_count': '-1',
}


def _rewrite_project_settings(path, settings, name):
    tmp = Path(path).with_suffix('.sanitizing')
    with zipfile.ZipFile(path) as archive, zipfile.ZipFile(tmp, 'w', zipfile.ZIP_DEFLATED) as out:
        for info in archive.infolist():
            out.writestr(info, json.dumps(settings, indent=4) if info.filename == name else archive.read(info))
    tmp.replace(path)


def _sanitize_imported_settings(path, templates, slot_count, filament_profiles=(), filament_template=None):
    name = 'Metadata/project_settings.config'
    with zipfile.ZipFile(path) as archive:
        if name not in archive.namelist():
            return {}
        settings = json.loads(archive.read(name))
    filament_template = Path(filament_template or Path(templates) / 'filament.json')
    own = {**_template_settings(templates), **json.loads(filament_template.read_text())}
    changed = {}
    for key, sentinel in IMPORTED_AUTO_SENTINELS.items():
        previous = settings.get(key)
        if isinstance(previous, list):
            replacement = ['0' if str(value) == sentinel else value for value in previous]
        elif str(previous) == sentinel:
            replacement = '0'
        else:
            continue
        if replacement != previous:
            settings[key] = replacement
            changed[key] = [previous, replacement]
    slot_count = max(1, min(int(slot_count), IFS_SLOTS))
    template_filament = json.loads(filament_template.read_text())
    per_slot_filament = tuple(key for key in template_filament if key.startswith('filament_') and not key.endswith('_settings_id'))
    for key in ('filament_z_hop_types', 'filament_wipe', 'filament_wipe_distance'):
        if key not in settings or key not in template_filament:
            continue
        template_values = template_filament[key]
        value = template_values[0] if isinstance(template_values, list) else template_values
        count = len(settings[key]) if isinstance(settings[key], list) else slot_count
        replacement = [value] * count if isinstance(settings[key], list) else value
        previous = settings[key]
        if previous != replacement:
            settings[key] = replacement
            changed[key] = [previous, replacement]
    for key in per_slot_filament:
        if key in settings and isinstance(template_filament[key], list) and template_filament[key]:
            template_values = template_filament[key]
            count = len(settings[key]) if isinstance(settings[key], list) else slot_count
            replacement = [template_values[0]] * count
            previous = settings[key]
            if previous != replacement:
                settings[key] = replacement
                changed[key] = [previous, replacement]
    for key in ('z_hop', 'z_hop_types', 'travel_slope', 'wipe', 'wipe_distance',
                'retract_lift_above', 'retract_lift_below', 'enable_arc_fitting', 'brim_type'):
        if key in settings and key in own and settings[key] != own[key]:
            previous = settings[key]
            settings[key] = own[key]
            changed[key] = [previous, own[key]]
    for key, value in own.items():
        if not key.endswith('_settings_id') or key not in settings:
            continue
        if key == 'filament_settings_id':
            template_id = value[0] if isinstance(value, list) else value
            count = len(settings[key]) if isinstance(settings[key], list) else slot_count
            existing = settings[key] if isinstance(settings[key], list) else [settings[key]]
            replacement = [
                item if sg.is_allowed_filament_id(item, {template_id}) else template_id
                for item in existing
            ]
            if len(replacement) < count:
                replacement.extend([template_id] * (count - len(replacement)))
            elif len(replacement) > count and count > 0:
                replacement = replacement[:count]
        else:
            replacement = value
        previous = settings[key]
        if previous != replacement:
            settings[key] = replacement
            changed[key] = [previous, replacement]
    area = own.get('printable_area', [])
    points = [tuple(map(float, point.split('x'))) for point in area]
    min_x, max_x = min(p[0] for p in points), max(p[0] for p in points)
    min_y, max_y = min(p[1] for p in points), max(p[1] for p in points)
    width = float(own.get('prime_tower_width', 0))
    for key, low, high in (('wipe_tower_x', min_x, max_x-width),
                           ('wipe_tower_y', min_y, max_y-width)):
        if key in settings:
            replacement = _clamp(settings[key], low, high)
            if replacement != settings[key]:
                changed[key] = [settings[key], replacement]
                settings[key] = replacement
    if changed:
        _rewrite_project_settings(path, settings, name)
    return changed


def sanitize_project_settings(path, log_text='', templates=None, slot_count=None, filament_template=None):
    """Normalize Orca sentinels and foreign motion/profile data on private 3MF copies.

    filament_template is the job's filament profile; the template folder's filament.json by default."""
    del log_text
    if Path(path).suffix.lower() != '.3mf':
        return {}
    templates = Path(templates or TEMPLATES)
    with zipfile.ZipFile(path) as archive:
        name = 'Metadata/project_settings.config'
        if name not in archive.namelist():
            return {}
        imported = json.loads(archive.read(name))
    if slot_count is None:
        slot_count = max((len(imported[key]) if isinstance(imported.get(key), list) else 1
                          for key in ('filament_settings_id', 'filament_type', 'filament_colour')
                          if imported.get(key) is not None), default=1)
    return _sanitize_imported_settings(path, templates, slot_count, filament_template=filament_template)


def clamp_rejected_settings(path, log_text):
    """Clamp only keys Orca names in a range error, to Orca's stated bounds.

    This is the post-failure recovery. It does not pre-normalize imported
    sentinels and it does not touch keys absent from the log.
    """
    bad = {m.group(1): (float(m.group(3)), float(m.group(4))) for m in RANGE_ERROR.finditer(log_text or '')}
    if not bad or Path(path).suffix.lower() != '.3mf':
        return {}
    name = 'Metadata/project_settings.config'
    with zipfile.ZipFile(path) as archive:
        if name not in archive.namelist():
            return {}
        settings = json.loads(archive.read(name))
    changed = {}
    for key, (low, high) in bad.items():
        if key in settings:
            new = _clamp(settings[key], low, high)
            if new != settings[key]:
                changed[key] = [settings[key], new]
                settings[key] = new
    if not changed:
        return {}
    _rewrite_project_settings(path, settings, name)
    return changed


def finalize_zmod(path, color, material, printer=None):
    """Single logical tool equivalent of official zmod-preprocess color/MD5 steps (the printer's ZMod code).

    Does not select a physical slot.
    """
    return (printer or printers.current()).native.finalize(path, (color, material))


def slicer_choice(printer):
    """Primary slicer for this printer: [printer] slicer for the configured model, else its vendor default.

    The vendor's first slicer: Bambu Studio for every Bambu Lab printer, OrcaSlicer for everyone else."""
    vendor = printer.model.vendor
    default = vendor.slicers[0] if vendor else 'orca'
    if printer.config.get('printer', {}).get('model') != printer.key:
        return default
    return printer.config['printer'].get('slicer') or default


def _keep_failure_logs(folder: Path) -> Path:
    """Copy slicer logs out of a slice folder that is deleted after a failure; return the kept dir."""
    folder = Path(folder)
    try:
        from ..hermes import _config
        keep = Path(_config.hermes_home()) / 'state' / 'slice-failures' / folder.name
        keep.mkdir(parents=True, exist_ok=True)
        for name in ('orca.log', 'flash-studio.log', 'bambu.log', 'result.json', '00000.log', 'process.json', 'machine.json'):
            if (folder / name).is_file():
                shutil.copy2(folder / name, keep / name)
        return keep
    except Exception:  # never mask the slicer error with a logging error
        return folder


def _run_slicer(command, name, folder):
    """subprocess.run for a slicer, logging the command name, exit code and elapsed time.

    Arguments are not logged (they hold local paths and profile contents); the slicer's own output is
    kept in <folder>/orca.log or bambu.log, and a failure logs where to find it.
    """
    started = time.monotonic()
    logger.info('%s run started folder=%s', name, folder)
    try:
        # Run inside the job folder: OrcaSlicer 2.5 writes 00000.log into its working
        # directory at startup and aborts when that directory is not writable (the
        # Docker image's /app is not). The folder is also where _keep_failure_logs
        # already collects 00000.log from.
        result = subprocess.run(command, capture_output=True, text=True, timeout=600, cwd=folder)
    except (subprocess.TimeoutExpired, OSError):
        logger.exception('%s did not complete after %.1fs', name, time.monotonic() - started)
        raise
    level = logging.WARNING if result.returncode else logging.INFO
    logger.log(level, '%s run finished rc=%s in %.1fs', name, result.returncode, time.monotonic() - started)
    if result.returncode:
        tail = (result.stdout + result.stderr).strip().splitlines()[-3:]
        logger.debug('%s output tail: %s', name, ' | '.join(tail))
    return result


def slice_model(source, folder, state, brim=None, first_layer=False, color_decisions=None, preserve_layout=False, filament_slot=None, item_shifts=None, fit_by_object=None, copies=None, scale=None, rotation=None, supports=None, object_ids=None, adapter=None, brim_policy=None):
    source, folder = Path(source), Path(folder)
    # An explicit adapter (a job prepared for another model) brings its own templates; the default keeps TEMPLATES.
    printer = adapter or printers.current()
    templates = printer.templates_dir() if adapter is not None else TEMPLATES
    if scale is not None:
        scale = float(scale)
        if not scale > 0:
            raise ValueError('scale must be greater than 0')
    rotation = _check_rotation(rotation)
    if rotation is not None and (preserve_layout or item_shifts or
                                 (color_decisions and color_decisions.get('mode') == 'per_part')):
        raise ValueError('rotation is only supported for whole-model single-color slicing')
    if fit_by_object is not None and not (color_decisions and color_decisions.get('mode') == 'per_part'):
        raise ValueError('fit_by_object is only supported for per-part color decisions')
    folder.mkdir(parents=True, exist_ok=True)
    machine, process, filament = [json.loads((templates / f'{name}.json').read_text()) for name in ('machine', 'process', 'filament')]
    template_nozzle_dia = float(machine.get('nozzle_diameter', [NOZZLE_MM])[0] if isinstance(machine.get('nozzle_diameter'), list) else machine.get('nozzle_diameter', NOZZLE_MM))
    nozzle_dia = float(state['nozzle']['diameter_mm'])
    from .nozzle import UntunedNozzleError, get_tuned_nozzle_profile
    tuned_profile = get_tuned_nozzle_profile(nozzle_dia)
    supported_sizes = getattr(printer.model, 'supported_nozzle_sizes', (NOZZLE_MM,))
    if nozzle_dia not in supported_sizes or tuned_profile is None:
        raise UntunedNozzleError(f'Orca templates are tuned for a {template_nozzle_dia:g} mm nozzle; no tuned profile exists for {nozzle_dia:g} mm on printer {printer.label!r}')
    author = {}
    geometry = {'policy': 'original STL unchanged', 'source_sha256': digest(source)}
    from .source_colors import inventory, validate_decisions
    from .model_job import resolve_filament_selection
    color_decisions = (validate_decisions(inventory(source), color_decisions, state, resolve_filament_selection)
                       if color_decisions else None)
    mapping = None
    mapped_profile = False
    if color_decisions and color_decisions.get('mode') == 'per_part':
        from .color_geometry import export_color_geometry
        input_path = folder / 'geometry.3mf'
        mapping, geometry = export_color_geometry(source, input_path, color_decisions, item_shifts=item_shifts,
                                                  object_ids=object_ids, scale=scale, fit_by_object=fit_by_object)
        with zipfile.ZipFile(source) as archive:
            if 'Metadata/project_settings.config' in archive.namelist():
                author = json.loads(archive.read('Metadata/project_settings.config'))
    elif source.suffix.lower() == '.3mf' and not preserve_layout:
        with zipfile.ZipFile(source) as archive:
            if 'Metadata/project_settings.config' in archive.namelist():
                author = json.loads(archive.read('Metadata/project_settings.config'))
        input_path = folder / ('geometry.3mf' if has_cutters(source) else 'geometry.stl')
        geometry.update(export_geometry(source, input_path, scale=scale or 1.0, rotation=rotation,
                                        bed=printer.build_volume().bed_mm))
        if scale is not None:
            geometry['scale_policy'] = 'uniform scale applied to transformed source vertices before bed centering'
    elif rotation is not None or (source.suffix.lower() == '.stl' and scale is not None):
        # Direct STL with an operator-selected orientation or scale: transform vertices
        # before slicing so every slicer receives identical resolved geometry.
        input_path = folder / 'geometry.stl'
        geometry.update(place_stl(source, input_path, scale=scale or 1.0, rotation=rotation,
                                  bed=printer.build_volume().bed_mm))
    else:
        if source.suffix.lower() == '.3mf':
            with zipfile.ZipFile(source) as archive:
                if 'Metadata/project_settings.config' in archive.namelist():
                    author = json.loads(archive.read('Metadata/project_settings.config'))
        input_path = source
        geometry = {'policy': 'derived plate; documented translation preserved; no arrange', 'source_sha256': digest(source)}
    if brim_policy is not None:
        geometry['fit_brim_policy'] = json.dumps(brim_policy, sort_keys=True)
    # Preserve author structural intent, not P1S motion/material calibration.
    process.update({k: author[k] for k in PROCESS_KEYS if k in author})
    # The active printer template, not imported project settings, defines the
    # brim mode; an explicit operator width may still control its extent.
    process['brim_type'] = json.loads((templates / 'process.json').read_text())['brim_type']
    process.update({'filter_out_gap_fill': '0', 'gap_fill_target': author.get('gap_fill_target', 'everywhere'),
                    'enable_support': author.get('enable_support', '0'), 'brim_width': str(brim) if brim is not None else author.get('brim_width', '0')})
    if brim_policy is not None:
        process['brim_type'] = brim_policy.get('type', 'no_brim')
        process['brim_width'] = str(brim_policy.get('width_mm', 0))
    if supports is not None:
        process.update({'enable_support': '1' if supports else '0'})
        if supports:
            process.setdefault('support_type', 'tree(auto)')
    if first_layer:
        process.update({'layer_height': '0.2', 'initial_layer_print_height': '0.2', 'wall_loops': '2',
                        'brim_width': '0', 'brim_type': 'no_brim', 'initial_layer_speed': '20',
                        'initial_layer_infill_speed': '20', 'enable_support': '0'})
    baseline = state.get('calibration_baseline') or {}
    mesh = baseline.get('native_mesh', 'auto')
    if not isinstance(mesh, str) or not re.fullmatch(r'[A-Za-z0-9_]+', mesh):
        raise RuntimeError('invalid calibration baseline mesh')
    machine['machine_start_gcode'] = printer.machine_start_gcode(machine['machine_start_gcode'], mesh)
    machine['machine_end_gcode'] = printer.machine_end_gcode(machine.get('machine_end_gcode', ''))
    machine['default_print_profile'] = process['name']
    raw_nozzle_type = ((state.get('nozzle') or {}).get('type')
                       or settings.current().get('printer', {}).get('nozzle_type')
                       or 'hardened_steel')
    from .nozzle import normalize_nozzle_type
    nozzle_type = normalize_nozzle_type(raw_nozzle_type)
    machine['nozzle_type'] = nozzle_type
    machine['nozzle_diameter'] = [str(nozzle_dia)]
    if tuned_profile is not None:
        machine['printer_variant'] = str(tuned_profile.diameter_mm)
        machine['min_layer_height'] = [str(tuned_profile.min_layer_height)]
        machine['max_layer_height'] = [str(tuned_profile.max_layer_height)]
        machine['retraction_length'] = [str(tuned_profile.retraction_length)]
        process['layer_height'] = str(tuned_profile.default_layer_height)
        process['initial_layer_print_height'] = str(tuned_profile.default_initial_layer_height)
        process['line_width'] = str(tuned_profile.line_width)
        process['initial_layer_line_width'] = str(tuned_profile.initial_layer_line_width)
        process['internal_solid_infill_line_width'] = str(tuned_profile.line_width)
    if author.get('wall_generator') == 'arachne' and not first_layer and not mapping:
        # The designer chose variable-width walls: treat it as a thin/print-in-place model
        # and use the flex profile so moving gaps are not fused (AD5X Flexy Ring guide).
        process.update(FLEX_PROFILE)
        if supports:
            process.update({'enable_support': '1'})
        if brim_policy is not None:
            process['brim_type'] = brim_policy.get('type', 'no_brim')
            process['brim_width'] = str(brim_policy.get('width_mm', 0))
        geometry['thin_feature_profile'] = 'arachne flex (designer asked for arachne walls)'
    from .nozzle import get_nozzle_type_adjustments
    adj = get_nozzle_type_adjustments(nozzle_type)
    machine['nozzle_temperature_range_high'] = [str(int(adj['max_temperature_c']))]
    slots = state.get('ifs_slots') or {}
    if filament_slot is not None:
        slot = slots.get(str(filament_slot)) or slots.get(filament_slot)
        if not isinstance(slot, dict):
            raise RuntimeError(f'derived plate slot {filament_slot} is not in the saved IFS map')
        if not slot.get('spoolman_id') or not slot.get('material') or not slot.get('physical_color_verified'):
            raise RuntimeError('IFS slot color/material mapping is unverified')
        if slot.get('spoolman_id') != state['spool'].get('spoolman_id'):
            # The selected slot, not whichever spool happened to be active last,
            # defines this job's material; launch syncs the active spool to it.
            # The slot record above is the physical gate (operator-verified
            # color/material). Spoolman is advisory only (#235): every lookup
            # outcome — unreachable, missing, archived or a different material
            # on file — warns and slicing proceeds on the slot's recorded
            # material/name and the recorded density/temperatures.
            state = {**state, 'spool': {**state['spool'], 'spoolman_id': slot['spoolman_id'],
                                        'name': slot.get('name'), 'material': slot['material']}}
            from .printer_state import same_material, spoolman_url
            try:
                import urllib.request
                spool_url = f"{spoolman_url(state)}/api/v1/spool/{slot['spoolman_id']}"
                with urllib.request.urlopen(spool_url, timeout=6) as response:
                    spool = json.load(response)
                if spool.get('archived'):
                    print(f"WARNING: Spoolman spool #{slot['spoolman_id']} is archived; "
                          f"slicing slot {filament_slot} on its recorded {slot['material']} anyway", flush=True)
                elif not same_material(spool.get('filament', {}).get('material'), slot['material']):
                    print(f"WARNING: Spoolman spool #{slot['spoolman_id']} material "
                          f"{spool.get('filament', {}).get('material')!r} differs from slot {filament_slot}'s "
                          f"recorded {slot['material']!r}; slicing on the slot record", flush=True)
            except Exception as exc:
                print(f"WARNING: Spoolman lookup failed for slot {filament_slot} spool "
                      f"#{slot['spoolman_id']}: {exc}; slicing on the slot record", flush=True)
        color = slot.get('hex')
    else:
        matching = [s for s in slots.values() if s.get('spoolman_id') == state['spool'].get('spoolman_id')]
        color = state['spool'].get('color_code')
        if slots:
            if len(matching) != 1:
                raise RuntimeError('selected spool must resolve to exactly one IFS slot')
            slot = matching[0]
            from .printer_state import same_material
            if not same_material(slot.get('material'), state['spool']['material']) or not slot.get('physical_color_verified'):
                raise RuntimeError('IFS slot color/material mapping is unverified')
            color = slot.get('hex')
    if color is not None:
        if not isinstance(color, str) or not re.fullmatch(r'#?[0-9A-Fa-f]{6}', color):
            raise RuntimeError('filament color must be an explicit six-digit RGB value')
        color = '#' + color.lstrip('#').upper()
    from .printer_state import _material_temperature_contract
    _material_temperature_contract(state)
    # The printer picks the filament profile for the job's material (Bambu Lab: its checked vendor profile for that
    # base polymer, refused when there is none); the hardware record then sets type, density and temperatures.
    filament_template = printer.filament_template(templates, state['spool']['material'])
    filament = json.loads(Path(filament_template).read_text())
    from .nozzle import effective_nozzle_temperature
    base_nozzle_temp = float(state['temps_c']['nozzle'])
    effective_nozzle_temp = effective_nozzle_temperature(nozzle_type, base_nozzle_temp)
    filament.update({'filament_type': [state['spool']['material']], 'filament_density': [str(state['spool']['density'])],
                     'filament_colour': [color or filament['filament_colour'][0]],
                     'nozzle_temperature': [str(int(effective_nozzle_temp))],
                     'nozzle_temperature_initial_layer': [str(int(effective_nozzle_temp))]})
    if 'filament_flow_ratio' in filament and filament['filament_flow_ratio']:
        try:
            base_flow = float(filament['filament_flow_ratio'][0])
            adj_flow = round(base_flow * adj['flow_multiplier'], 4)
            filament['filament_flow_ratio'] = [str(adj_flow)]
        except (ValueError, TypeError):
            pass
    for key in ('cool_plate_temp', 'eng_plate_temp', 'hot_plate_temp', 'textured_plate_temp'):
        filament[key] = filament[key + '_initial_layer'] = [str(state['temps_c']['bed'])]
    configs = []
    for name, data in zip(('machine', 'process', 'filament'), (machine, process, filament)):
        path = folder / f'{name}.json'
        path.write_text(json.dumps(data, indent=2) + '\n')
        configs.append(path)
    filament_paths = [configs[2]]
    if mapping:
        import copy
        machine.update({'single_extruder_multi_material': '1', 'change_filament_gcode': ''})
        tower_x, tower_y = geometry['prime_tower_xy_mm']
        tower_width = str(tuned_profile.prime_tower_width if tuned_profile else 45)
        process.update({'enable_prime_tower': '1', 'prime_tower_width': tower_width,
                        'wipe_tower_x': str(tower_x), 'wipe_tower_y': str(tower_y),
                        'purge_in_prime_tower': '0'})
        configs[0].write_text(json.dumps(machine, indent=2) + '\n')
        configs[1].write_text(json.dumps(process, indent=2) + '\n')
        filament_paths = []
        for tool, physical_slot in mapping.items():
            slot = state['ifs_slots'][str(physical_slot)]
            from .printer_state import same_material
            if not same_material(slot['material'], state['spool']['material']):
                raise RuntimeError('mixed material requires independently calibrated per-slot temperatures/density')
            data = copy.deepcopy(filament)
            data.update({'name': f"Hermes slot {physical_slot} {slot['material']}",
                         'filament_settings_id': [f"Hermes slot {physical_slot} {slot['material']}"],
                         'filament_colour': ['#' + slot['hex'].lstrip('#').upper()],
                         'filament_is_support': ['0']})
            path = folder / f'filament_tool_{tool}.json'
            path.write_text(json.dumps(data, indent=2) + '\n')
            filament_paths.append(path)
        configs.extend(filament_paths)
        mapped_profile = True
    elif filament_slot is not None:
        # A derived single-color plate is still T0. Record the already selected
        # physical slot for finalization and provenance. Do not enter the
        # multicolor profile path: that requires prime-tower geometry this
        # plate does not have, and the filament list stays one entry.
        mapping = {'0': int(filament_slot)}
    prepositioned_scale = (scale is not None or rotation is not None) and input_path != source and not preserve_layout
    if copies and int(copies) > 1 and not (preserve_layout or item_shifts or mapped_profile):
        # Clone placement must be explicit: slicer auto-arrange can overlap clones on
        # transformed STL input, producing a self-conflicting G-code path.
        points = _unique_points(source_triangles(input_path))
        if not points:
            raise RuntimeError(f'cannot arrange {copies} copies: transformed geometry is empty')
        bounds = [(min(p[a] for p in points), max(p[a] for p in points)) for a in range(3)]
        width, depth = bounds[0][1] - bounds[0][0], bounds[1][1] - bounds[1][0]
        bed_x, bed_y = state.get('bed_mm', BED)
        count = int(copies)
        cols = max(1, int((float(bed_x) + 10) // (width + 10)))
        rows = (count + cols - 1) // cols
        if rows * (depth + 10) > float(bed_y) + 10:
            raise RuntimeError(f'{count} copies of {width:g}x{depth:g} mm do not fit the printer bed')
        shifts = []
        for index in range(count):
            col, row = index % cols, index // cols
            shifts.append((col * (width + 10), row * (depth + 10)))
        clones = folder / 'clones.stl'
        triangles = source_triangles(input_path)
        duplicated = [[(point[0] + dx, point[1] + dy, point[2]) for point in triangle]
                      for dx, dy in shifts for triangle in triangles]
        geometry.update(_place_triangles(duplicated, clones, bed=printer.build_volume().bed_mm))
        input_path = clones
        copies = None
    output = folder / 'plate_1.gcode'
    prepositioned_scale = (scale is not None or rotation is not None) and input_path != source and not preserve_layout
    command = [*ORCA_COMMAND, '--load-settings', f'{configs[0]};{configs[1]}',
               '--load-filaments', ';'.join(map(str, filament_paths)),
               # Mapped (multicolor) geometry is already placed and the prime tower
               # computed around it; Orca's default auto-arrange would move parts
               # into the tower lane (-101 G-code conflict), so disable it explicitly.
               *(['--arrange', '0', '--orient', '0'] if (preserve_layout or item_shifts or prepositioned_scale or mapped_profile) else ['--arrange', '1', '--orient', '0', '--ensure-on-bed']),
               *(['--clone-objects', str(copies)] if (copies and int(copies) > 1) else []),
               '--allow-newer-file', '--slice', '0', '--outputdir', str(folder), str(input_path)]
    if scale is not None and not mapped_profile:
        if input_path.suffix.lower() == '.3mf':
            raise ValueError('uniform scale must be applied to exported geometry before slicing a 3MF')
        # Normal 3MF intake (and any rotated input) was already scaled during export.
        # Orca's CLI scale is retained only for direct STL input, where no pre-export occurs.
        if input_path == source:
            command[-1:-1] = ['--scale', str(scale)]
    sanitized = {}
    # Keep the untouched base templates for protected-setting comparison; gate the
    # final effective configs immediately before invoking Orca.
    base_profile = {**json.loads((templates / 'machine.json').read_text()),
                    **json.loads((templates / 'process.json').read_text()),
                    **json.loads(Path(filament_template).read_text())}
    effective_profile = {**machine, **process, **filament}
    placed_bounds = geometry.get('placed_bounds_mm')
    if placed_bounds and len(placed_bounds) >= 2:
        bounds_xy = (placed_bounds[0][0], placed_bounds[1][0], placed_bounds[0][1], placed_bounds[1][1])
    else:
        bounds_xy = None
    bed = tuple(state.get('bed_mm', BED))
    try:
        printable = machine.get('printable_area')
        if printable:
            points = [tuple(map(float, point.split('x'))) for point in printable]
            bed = (max(p[0] for p in points) - min(p[0] for p in points),
                   max(p[1] for p in points) - min(p[1] for p in points))
    except (TypeError, ValueError):
        pass
    # A single-color plate records T0 for provenance only. The pre-slice guard
    # compares map size with filament count, so it must not see that one-tool map.
    guard_mapping = mapping if mapped_profile else None
    selected_slot = filament_slot or 1
    tool_slots = {int(tool): int(slot) for tool, slot in guard_mapping.items()} if guard_mapping else {0: int(selected_slot)}
    # Protected-key differences here include adapter-sanctioned profile assembly
    # (for example the resolved start-G-code template), not untrusted overrides.
    # Only block on geometric/tooling findings that this adapter can establish.
    nozzle_type = ((state.get('nozzle') or {}).get('type')
                   or settings.current().get('printer', {}).get('nozzle_type')
                   or 'hardened_steel')
    used_materials = []
    if mapped_profile and mapping:
        for tool, physical_slot in mapping.items():
            slot = state.get('ifs_slots', {}).get(str(physical_slot))
            if slot and slot.get('material'):
                used_materials.append(slot['material'])
    if not used_materials:
        used_materials = filament.get('filament_type') or [state.get('spool', {}).get('material')]
    gate = pre_slice(bounds=bounds_xy, bed=bed, profile=effective_profile,
                     before_profile=None, filaments=len(filament_paths),
                     ifs_slots=IFS_SLOTS, tool_slots=tool_slots,
                     nozzle_type=nozzle_type, materials=used_materials,
                     nozzle_temp=effective_nozzle_temp, nozzle_temp_is_effective=True)
    if gate.status == 'blocked':
        raise RuntimeError('Pre-slice guard blocked: ' + '; '.join(gate.blocked))
    # Unknown placement is advisory, never a false block.
    if bounds_xy is None:
        gate.warn('Placed mesh bounds are unavailable; adapter did not block on geometry bounds.')
    if input_path == source and source.suffix.lower() == '.3mf':
        # Never mutate the source of record; Orca reads a private copy we may sanitize.
        private = folder / ('orca_input' + source.suffix)
        private.write_bytes(source.read_bytes())
        input_path = private
        command[-1] = str(private)
    if input_path.suffix.lower() == '.3mf':
        # Bambu's -1 auto sentinels are invalid in Orca 2.4. Normalize these
        # two known compatibility keys plus foreign motion provenance only on
        # the private working copy, never the original source.
        sanitized.update(sanitize_project_settings(
            input_path, templates=templates,
            slot_count=len(filament_paths) if mapped_profile else 1, filament_template=filament_template))
    # Never accept stale output if a failed slicer leaves the preceding artifact behind.
    output.unlink(missing_ok=True)
    primary = slicer_choice(printer)
    flash_studio_command = _flash_studio_command(printer)
    if flash_studio_command is not None:
        # This is an explicit, exclusive user choice, not an Orca fallback. Reuse
        # Print Buddy's resolved CLI arguments but replace the configured Orca command.
        primary = 'flash_studio'
        command = [*flash_studio_command, *command[len(ORCA_COMMAND):]]
    verified = getattr(printer, 'verified_slicers', None)
    if verified is not None and primary not in verified:
        raise RuntimeError(f'{printer.label}: {primary} output is not verified for this printer; '
                           f'set [printer] slicer to one of {", ".join(verified)}. Nothing was sliced.')
    if primary == 'bambu_studio':
        # Opt-in Bambu Studio as the primary slicer (vendor profile allows it, [printer] slicer picks it):
        # Orca never runs, so there is no Orca failure and no fallback.
        result = subprocess.CompletedProcess(command, 0, '', '')
    elif primary == 'flash_studio':
        _check_flash_studio_cli(command)
        result = _run_slicer(command, 'Flash Studio', folder)
        (folder / 'flash-studio.log').write_text(result.stdout + result.stderr)
        if result.returncode or not output.is_file() or output.stat().st_size == 0:
            raise RuntimeError(f'Flash Studio slicing failed ({result.returncode}) or produced no G-code; '
                               f'see {_keep_failure_logs(folder) / "flash-studio.log"}')
    else:
        result = _run_slicer(command, 'OrcaSlicer', folder)
        (folder / 'orca.log').write_text(result.stdout + result.stderr)
        if result.returncode:
            # Bounded recovery clamps only keys named in Orca's range error.
            # Already-normalized sentinels are not rewritten by this path.
            fixed = clamp_rejected_settings(input_path, result.stdout + result.stderr)
            if fixed:
                sanitized.update(fixed)
                result = _run_slicer(command, 'OrcaSlicer', folder)
                (folder / 'orca.log').write_text(result.stdout + result.stderr)
    if (primary == 'orca' and result.returncode and _orca_slicing_rejected(folder)
            and any(process.get(k) != v for k, v in FLEX_PROFILE.items())):
        # Classic walls fail thin print-in-place features (Orca -100, e.g. flexy rings);
        # Arachne variable-width walls slice them. One retry with the flex-print profile, logged.
        logger.warning('Orca failed (rc=%s); retrying once with the arachne flex profile', result.returncode)
        process.update(FLEX_PROFILE)
        geometry['thin_feature_profile'] = 'arachne flex (AD5X Flexy Ring guide)'
        configs[1].write_text(json.dumps(process, indent=2) + '\n')
        output.unlink(missing_ok=True)
        (folder / 'result.json').unlink(missing_ok=True)
        result = _run_slicer(command, 'OrcaSlicer', folder)
        (folder / 'orca.log').write_text(result.stdout + result.stderr)
    if (primary == 'orca' and mapped_profile and result.returncode and _orca_conflict(folder)):
        # Tower placement clears mesh bounds, but tree supports and brims can
        # still reach it. Retry the precomputed alternate tower origins only;
        # never relax Orca's conflict check.
        tried = [list(geometry['prime_tower_xy_mm'])]
        for tower_x, tower_y in geometry.get('prime_tower_candidates_mm', [])[1:]:
            process.update({'wipe_tower_x': str(tower_x), 'wipe_tower_y': str(tower_y)})
            configs[1].write_text(json.dumps(process, indent=2) + '\n')
            output.unlink(missing_ok=True)
            (folder / 'result.json').unlink(missing_ok=True)
            result = _run_slicer(command, 'OrcaSlicer', folder)
            (folder / 'orca.log').write_text(result.stdout + result.stderr)
            tried.append([tower_x, tower_y])
            if not (result.returncode and _orca_conflict(folder)):
                break
        geometry['prime_tower_xy_mm'] = tried[-1]
        geometry['prime_tower_attempts_mm'] = tried
    slicer = {'flash_studio': 'Flash Studio', 'bambu_studio': 'Bambu Studio'}.get(primary, 'OrcaSlicer')
    fallback = None
    vendor = printer.model.vendor
    bambu_allowed = (primary == 'orca'
                     and (vendor is None or vendor.fallback_slicer == 'bambu_studio')
                     and (verified is None or 'bambu_studio' in verified))
    orca_failed = primary == 'orca' and (result.returncode or not output.is_file()
                                         or output.stat().st_size == 0)
    if orca_failed and not bambu_allowed:
        details = _orca_failure_details(folder)
        raise RuntimeError(f'Orca slicing failed ({result.returncode}) or no G-code output{details}; '
                           f'see {_keep_failure_logs(folder) / "orca.log"}')
    if primary == 'flash_studio' and (result.returncode or not output.is_file() or output.stat().st_size == 0):
        raise RuntimeError(f'Flash Studio slicing failed ({result.returncode}) or produced no G-code; '
                           f'see {_keep_failure_logs(folder) / "flash-studio.log"}')
    if primary == 'bambu_studio' or orca_failed:
        # Only an actual Orca execution failure (or the opt-in Bambu Studio primary) reaches this
        # branch. All Print Buddy validation and placement refusals above remain fail-closed and
        # never invoke Bambu.
        details = _orca_failure_details(folder) if orca_failed else ''
        log_text = (result.stdout + '\n' + result.stderr).strip()
        if not orca_failed:
            orca_failure = 'Bambu Studio is the configured slicer'
        elif re.search(r'G-code conflicts detected', log_text, re.I):
            # A conflict is a slicer failure, not a Print Buddy refusal. Bambu gets
            # one isolated attempt after Orca's own retry/candidate logic is exhausted.
            orca_failure = ('Orca detected G-code conflicts: inspect the model for '
                            'overlapping/intersecting parts, separate or reposition them, then retry; '
                            'Bambu Studio is now the fallback attempt')
        else:
            orca_failure = (f'Orca slicing failed ({result.returncode}) or no G-code output{details}'
                            if details else
                            f'Orca slicing failed ({result.returncode}) or no G-code output')
        bambu_role = 'Bambu Studio fallback' if orca_failed else 'Bambu Studio'
        logger.warning('%s: %s', bambu_role, orca_failure)
        bambu_dir = folder / 'bambu'
        bambu_dir.mkdir(exist_ok=True)
        bambu_machine = bambu_dir / 'machine.json'
        bambu_process = bambu_dir / 'process.json'
        bambu_filaments = []
        # Bambu receives the already resolved production profiles. Do not use the
        # spike profile or its T[next_extruder] shortcut; this preserves the exact
        # production machine start/end and change-filament G-code values.
        bambu_machine.write_text(json.dumps(machine, indent=2) + '\n')
        # Bambu Studio reads Orca's percentage line widths as absolute millimeters.
        # Orca percentages are relative to the machine nozzle, not line_width.
        bambu_profile = dict(process)
        percent_width_keys = [
            key for key, raw_value in bambu_profile.items()
            if key.endswith('_line_width') and str(raw_value).strip().endswith('%')
        ]
        if percent_width_keys:
            try:
                nozzle_values = machine['nozzle_diameter']
                nozzle_diameter = float(nozzle_values[0] if isinstance(nozzle_values, list) else nozzle_values)
                for key in percent_width_keys:
                    value = str(bambu_profile[key]).strip()
                    bambu_profile[key] = f'{nozzle_diameter * float(value[:-1]) / 100:g}'
            except (KeyError, TypeError, ValueError, IndexError) as exc:
                raise RuntimeError(
                    f'cannot convert Orca percentage line widths for Bambu fallback: {exc}'
                ) from exc
        bambu_process.write_text(json.dumps(bambu_profile, indent=2) + '\n')
        for index, source_filament in enumerate(filament_paths):
            path = bambu_dir / f'filament_tool_{index}.json'
            path.write_bytes(source_filament.read_bytes())
            bambu_filaments.append(path)
        output.unlink(missing_ok=True)
        bambu_command = [
            *BAMBU_COMMAND, '--datadir', str(bambu_dir / 'datadir'),
            '--load-settings', f'{bambu_machine};{bambu_process}',
            '--load-filaments', ';'.join(map(str, bambu_filaments)),
            *(['--arrange', '0', '--orient', '0'] if (preserve_layout or item_shifts or prepositioned_scale or mapped_profile)
              else ['--arrange', '1', '--orient', '0', '--ensure-on-bed']),
            *(['--clone-objects', str(copies)] if (copies and int(copies) > 1) else []),
            '--allow-newer-file', '--slice', '0', '--outputdir', str(folder), str(input_path),
        ]
        try:
            bambu_result = _run_slicer(bambu_command, 'Bambu Studio', folder)
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise RuntimeError(f'{orca_failure}; {bambu_role} failed to start: {exc}') from exc
        (folder / 'bambu.log').write_text(bambu_result.stdout + bambu_result.stderr)
        if bambu_result.returncode or not output.is_file() or output.stat().st_size == 0:
            if primary == 'bambu_studio':
                raise RuntimeError(
                    f'Bambu Studio is the configured slicer; Bambu Studio failed ({bambu_result.returncode}) '
                    f'or produced no G-code; see {_keep_failure_logs(folder) / "bambu.log"}'
                )
            raise RuntimeError(
                f'{orca_failure}; {bambu_role} failed ({bambu_result.returncode}) or produced no G-code; '
                + f'see {_keep_failure_logs(folder) / "bambu.log"}'
            )
        if primary == 'bambu_studio':
            configs.extend([bambu_machine, bambu_process, *bambu_filaments])
            command = bambu_command
            slicer = 'Bambu Studio'
        else:
            configs.extend([bambu_machine, bambu_process, *bambu_filaments])
            command = bambu_command
            slicer = 'Bambu Studio'
            if orca_failed:
                fallback = {'from': 'OrcaSlicer', 'reason': orca_failure, 'orca_log': str(folder / 'orca.log'),
                            'bambu_log': str(folder / 'bambu.log')}
    text = output.read_text()
    generator = re.search(r'^; generated by ((?:OrcaSlicer|BambuStudio|Flash Studio).+)$', text, re.M)
    if generator is None or (primary == 'flash_studio' and not generator.group(1).startswith('Flash Studio')):
        if primary == 'flash_studio':
            raise RuntimeError('Flash Studio did not generate recognized G-code; see '
                               f'{_keep_failure_logs(folder) / "flash-studio.log"}')
        raise RuntimeError(f'output is not generated by {slicer}')
    if mapped_profile or mapping:
        # Explicit T0 (one physical slot) and per-part maps share the mapped
        # finalizer: one slot record, one zmod_color_data line, one checksum.
        # mapped_profile stays the prime-tower flag; this branch does not
        # build a tower or a second filament profile.
        printer.native.finalize(output, None, state, mapping)
    elif printer.has_native_screen:
        finalize_zmod(output, filament['filament_colour'][0], state['spool']['material'], printer)
    result_path = folder / 'result.json'
    post_gate = post_slice(output, result_json=None if slicer != 'OrcaSlicer' else (result_path if result_path.is_file() else None),
                           bed=bed, ifs_slots=IFS_SLOTS, tool_slots=tool_slots,
                           templates=templates, profile_paths=filament_paths,
                           envelope=printer.motion_envelope_mm)
    # Each vendor runs only its own start code: e.g. no Klipper/ZMod macro reaches a Bambu Lab printer.
    if vendor is not None:
        for problem in start_code_problems(output.read_text(errors='replace'), vendor):
            post_gate.block(problem)
    # Printer-specific refusals: the A1's vendor blocks, modal toolpath bounds, limits and hardware record.
    for problem in printer.gcode_problems(output.read_text(errors='replace'), state):
        post_gate.block(problem)
    # Each model approves its own protected settings; a model without a baseline file keeps the shared one.
    baseline_kw = {} if printer.baseline_path is None else {'path': printer.baseline_path}
    baseline_gate = (sg.enforce_approved_baseline(output, **baseline_kw)
                     if output.stat().st_size > 1000 else sg.Result())
    if baseline_gate.status == 'blocked':
        raise RuntimeError('Post-slice protected-settings guard blocked: ' + '; '.join(baseline_gate.blocked))
    if baseline_gate.warnings:
        post_gate.warn(*baseline_gate.warnings)
    if post_gate.status == 'blocked':
        raise RuntimeError('Post-slice guard blocked: ' + '; '.join(post_gate.blocked))
    warnings = gate.warnings + post_gate.warnings + baseline_gate.warnings
    return output, {'tool_mapping': mapping, 'source_sha256': digest(source), 'gcode_sha256': digest(output),
                    'config_sha256': {str(p): digest(p) for p in configs}, 'geometry': geometry,
                    'slice_guard': {'status': 'warnings' if warnings else 'ok', 'warnings': warnings,
                                    'effective_settings': post_gate.effective_settings},
                    "slicer_command": command, "generator": generator.group(1), "slicer": slicer,
                    'fallback': fallback,
                    'author_process': {k: author[k] for k in PROCESS_KEYS if k in author},
                    'sanitized_author_settings': sanitized,
                    'offline_geometry_and_slicer': True}, configs


def _orca_slicing_rejected(folder: Path) -> bool:
    """True when Orca's result.json reports -100 'Failed slicing the model' (thin features under Classic walls)."""
    try:
        payload = json.loads((folder / 'result.json').read_text())
    except (OSError, json.JSONDecodeError):
        return False
    return payload.get('return_code') == -100 and not _orca_conflict(folder)


def _orca_conflict(folder: Path) -> bool:
    """True when Orca's result.json reports its G-code path conflict (-101)."""
    try:
        payload = json.loads((folder / 'result.json').read_text())
    except (OSError, json.JSONDecodeError):
        return False
    return 'G-code conflicts detected' in str(payload.get('error_string', ''))


def _orca_failure_details(folder: Path) -> str:
    """Summarize result.json errors and named objects in Orca's debug log."""
    details = ''
    try:
        payload = json.loads((folder / 'result.json').read_text())
    except (OSError, json.JSONDecodeError):
        payload = {}
    if isinstance(payload, dict):
        error = payload.get('error_string')
        code = payload.get('return_code')
        if code is not None or error:
            details = f"; Orca return_code {code}: {error}"

    if _orca_conflict(folder):
        # Orca writes its debug log as 00000.log; orca.log is our CLI capture.
        log_text = ''
        for log in sorted(folder.glob('*.log')):
            try:
                log_text += log.read_text(errors='replace') + '\n'
            except OSError:
                pass
        names = []
        for line in log_text.splitlines():
            match = re.search(r'conflicts? found between\s+(.+?)\s+and\s+(.+?)(?:[.\r\t]|$)', line, re.I)
            if match:
                names.extend((match.group(1).strip(), match.group(2).strip()))
        if names:
            unique_names = list(dict.fromkeys(names))
            details += '; conflicting objects: ' + ' and '.join(unique_names)
    return details


def quantity_buttons(max_copies: int) -> list[int]:
    """Offer 1, 2, and the verified maximum when physically possible."""
    if max_copies <= 1:
        return [1]
    return sorted({1, min(2, max_copies), max_copies})


def _source_plate_count(source) -> int:
    """Count source plates named in a 3MF project; 0 when none are declared."""
    try:
        with zipfile.ZipFile(source) as archive:
            names = set(archive.namelist())
            ids = {int(m.group(1)) for n in names if (m := re.fullmatch(r'Metadata/plate_(\d+)\.(?:png|gcode)', n))}
            if 'Metadata/model_settings.config' in names:
                root = ET.fromstring(archive.read('Metadata/model_settings.config'))
                for plate in root.iter('plate'):
                    for meta in plate.iter('metadata'):
                        if meta.get('key') == 'plater_id' and meta.get('value') is not None:
                            ids.add(int(meta.get('value')))
            return len(ids)
    except (zipfile.BadZipFile, ET.ParseError, ValueError, OSError):
        return 0


def _multi_plate_capacity(source: Path, state: dict, scale: float = 1.0, single_margin: float = 1.0) -> int:
    """Check if all plates in a multi-plate 3MF fit the bed bounds at `scale`."""
    try:
        from . import model_job as mj
        from . import color_geometry as cg
        bed = state.get('bed_mm', BED)
        max_h = float(state.get('max_print_height_mm', MAX_HEIGHT_MM))
        eff_w = float(bed[0]) - 2 * single_margin
        eff_d = float(bed[1]) - 2 * single_margin
        manifest = mj.plate_manifest(source)
        with zipfile.ZipFile(source) as archive:
            for p in manifest.get('plates', []):
                ids = {str(o) for o in p.get('object_ids', [])}
                if not ids:
                    continue
                boxes = cg._world_bounds(archive, ids)
                if not boxes:
                    continue
                min_x = min(b[0][0] for b in boxes)
                max_x = max(b[0][1] for b in boxes)
                min_y = min(b[1][0] for b in boxes)
                max_y = max(b[1][1] for b in boxes)
                min_z = min(b[2][0] for b in boxes)
                max_z = max(b[2][1] for b in boxes)
                dx = (max_x - min_x) * scale
                dy = (max_y - min_y) * scale
                dz = (max_z - min_z) * scale
                if dz > max_h + 0.05:
                    return 0
                fits = (dx <= eff_w and dy <= eff_d) or (dy <= eff_w and dx <= eff_d)
                if not fits:
                    return 0
        return 1
    except Exception as exc:
        # An uninspectable plate is unknown, not "fits once": callers treat raises as probe failures.
        raise RuntimeError(f'multi-plate fit check cannot inspect {source}: {exc}') from exc


def orca_plate_capacity(source: Path, state: dict, spacing_mm: float = 10.0, edge_margin_mm: float | None = None,
                        scale: float = 1.0, copy_limit: int = 16, allow_rotations: bool = True,
                        rotation=None, supports: bool | None = None, brim_policy=None) -> int:
    """Return the Orca-confirmed copy count; probe failures raise and are never treated as no-fit.

    Copies keep ``MULTI_COPY_EDGE_MM`` from the bed edge; a lone copy only needs
    ``SINGLE_COPY_EDGE_MM`` (brim/skirt are off by default), so a model that fits once
    but not with multi-copy margins reports 1 instead of a false 0. Height is checked too.
    ``supports`` probes with the same support setting the slice will use (None: the 3MF
    author's setting, else off). Tree supports can double a part's footprint, so a probe
    without them over-reports copies.
    """
    import tempfile
    source = Path(source)
    scale = float(scale)
    if not 0 < scale <= 1:
        raise ValueError('scale must be greater than 0 and no more than 1')
    copy_limit = int(copy_limit)
    if copy_limit < 1:
        raise ValueError('copy_limit must be at least 1')
    bed = state.get('bed_mm', BED)
    bed_x, bed_y = float(bed[0]), float(bed[1])
    max_h = float(state.get('max_print_height_mm', MAX_HEIGHT_MM))
    multi_margin = MULTI_COPY_EDGE_MM if edge_margin_mm is None else float(edge_margin_mm)
    single_margin = SINGLE_COPY_EDGE_MM if edge_margin_mm is None else float(edge_margin_mm)
    if brim_policy is not None:
        state = {**state, 'brim_policy': brim_policy}
    feature_edge = required_feature_edge(state)
    multi_margin += feature_edge
    single_margin += feature_edge

    rotation = _check_rotation(rotation)
    if rotation is not None:
        allow_rotations = False  # the chosen yaw is exact; Orca must not turn it again
    if source.suffix.lower() == '.3mf' and rotation is None:
        if _source_plate_count(source) > 1:
            return _multi_plate_capacity(source, state, scale=scale, single_margin=single_margin)
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        # Orca's arrange only sees each object's XY convex hull, and the probe exports a
        # project without slicing. A prism of that hull arranges identically, but loads in
        # seconds instead of minutes for dense meshes (683k triangles took ~2 min per probe).
        # The hull is cached per file + rotation, so scale/rotation retries skip the mesh.
        hull, z_min, z_max = _footprint(source, rotation)
        if len(hull) < 3:
            raise RuntimeError(f'Orca fit probe cannot determine capacity for {source}: degenerate footprint')
        hdr = b'fit probe convex hull prism'.ljust(80, b'\0')
        min_x, max_x = min(p[0] for p in hull), max(p[0] for p in hull)
        min_y, max_y = min(p[1] for p in hull), max(p[1] for p in hull)
        w = max_x - min_x
        d = max_y - min_y
        if (z_max - z_min) * scale > max_h + 0.05:
            return 0  # Orca happily "arranges" a part taller than the gantry; refuse here.

        scaled_w, scaled_d = w * scale, d * scale
        expand_mm = max(0.0, (spacing_mm - 2.0) / 2.0)
        sx = (scaled_w + 2 * expand_mm) / w if w > 0 else 1.0
        sy = (scaled_d + 2 * expand_mm) / d if d > 0 else 1.0
        cx, cy = (min_x + max_x) / 2.0, (min_y + max_y) / 2.0

        z_lo, z_hi = 0.0, (z_max - z_min) * scale  # rest on the bed like _place_triangles
        if z_hi - z_lo < 0.01:
            z_hi = z_lo + 0.01

        def write_stl(path, fx, fy):
            pts = [(cx + (x - cx) * fx, cy + (y - cy) * fy) for x, y in hull]
            n = len(pts)
            tris = []
            for i in range(1, n - 1):  # hull is CCW: bottom faces down, top faces up
                tris.append(((*pts[0], z_lo), (*pts[i + 1], z_lo), (*pts[i], z_lo)))
                tris.append(((*pts[0], z_hi), (*pts[i], z_hi), (*pts[i + 1], z_hi)))
            for i in range(n):
                a, b = pts[i], pts[(i + 1) % n]
                tris.append(((*a, z_lo), (*b, z_lo), (*b, z_hi)))
                tris.append(((*a, z_lo), (*b, z_hi), (*a, z_hi)))
            with open(path, 'wb') as out:
                out.write(hdr)
                out.write(struct.pack('<I', len(tris)))
                for tri in tris:
                    out.write(struct.pack('<12fH', 0, 0, 0, *(c for p in tri for c in p), 0))

        dilated_stl = tmp / 'dilated.stl'
        write_stl(dilated_stl, sx, sy)
        single_stl = tmp / 'single.stl'
        write_stl(single_stl, scale, scale)
        m_base = json.loads(Path(TEMPLATES / 'machine.json').read_text())
        ppath = TEMPLATES / 'process.json'
        fpath = TEMPLATES / 'filament.json'
        if supports is None and source.suffix.lower() == '.3mf':
            try:
                with zipfile.ZipFile(source) as archive:
                    author = json.loads(archive.read('Metadata/project_settings.config'))
                supports = str(author.get('enable_support', '0')) == '1'
            except (KeyError, OSError, ValueError, zipfile.BadZipFile):
                supports = False
        if supports:
            p_tmpl = json.loads(ppath.read_text())
            p_tmpl['enable_support'] = '1'
            p_tmpl.setdefault('support_type', 'tree(auto)')
            ppath = tmp / 'process_supports.json'
            ppath.write_text(json.dumps(p_tmpl, indent=2))

        def machine_for(edge):
            m_tmpl = dict(m_base)
            m_tmpl['printable_area'] = [f"{edge}x{edge}", f"{bed_x - edge}x{edge}",
                                        f"{bed_x - edge}x{bed_y - edge}", f"{edge}x{bed_y - edge}"]
            path = tmp / f'machine_{edge}.json'
            path.write_text(json.dumps(m_tmpl, indent=2))
            return path

        def probe(stl, copies, edge, tag):
            probe_dir = tmp / f'probe_{tag}_{copies}'
            probe_dir.mkdir(exist_ok=True)
            cmd = [
                *ORCA_COMMAND,
                '--load-settings', f'{machine_for(edge)};{ppath}',
                '--load-filaments', str(fpath),
                '--allow-newer-file', '--clone-objects', str(copies),
                '--arrange', '1', *(['--allow-rotations'] if allow_rotations else []), '--orient', '0',
                '--ensure-on-bed',
                '--outputdir', str(probe_dir),
                '--export-3mf', 'probe.3mf',
                str(stl)
            ]
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=120, cwd=probe_dir)
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError(f'Orca fit probe timed out for {copies} copies') from exc
            p3mf = probe_dir / 'probe.3mf'
            if result.returncode != 0:
                raise RuntimeError(
                    f'Orca fit probe failed ({result.returncode}) for {copies} copies: '
                    f'{result.stderr.strip() or result.stdout.strip()}'
                )
            if not p3mf.is_file() or p3mf.stat().st_size == 0:
                raise RuntimeError(f'Orca fit probe produced no valid project for {copies} copies')
            try:
                with zipfile.ZipFile(p3mf) as z:
                    cfg = z.read('Metadata/model_settings.config').decode('utf-8', errors='ignore')
                    root = ET.fromstring(cfg)
            except (OSError, KeyError, zipfile.BadZipFile, ET.ParseError) as exc:
                raise RuntimeError(f'Orca fit probe output is malformed for {copies} copies') from exc
            plate_count = sum(1 for item in root.iter('plate')
                              if any(meta.get('key') == 'plater_id' for meta in item.iter('metadata')))
            if plate_count != 1:
                return False
            try:
                arranged = export_geometry(p3mf, probe_dir / 'arranged.stl')
                bounds = arranged['build_bounds_mm']
                x_bounds, y_bounds = bounds[0], bounds[1]
            except (KeyError, TypeError, ValueError, RuntimeError, OSError) as exc:
                raise RuntimeError(f'Orca fit probe cannot inspect arranged bounds for {copies} copies') from exc
            tolerance = 0.1
            return (
                float(x_bounds[0]) >= edge - tolerance and
                float(x_bounds[1]) <= bed_x - edge + tolerance and
                float(y_bounds[0]) >= edge - tolerance and
                float(y_bounds[1]) <= bed_y - edge + tolerance
            )

        expected_sweep = resolved_travel_sweep(state)
        policy = brim_policy or state.get('brim_policy')
        expected_brim = float(policy.get('width_mm', 0)) if policy and policy.get('type') == 'outer_only' else 0.0
        required = expected_brim + expected_sweep
        edge = SINGLE_COPY_EDGE_MM + required
        if copy_limit == 1:
            m_base['printable_area'] = [f"{edge}x{edge}", f"{bed_x - edge}x{edge}",
                                        f"{bed_x - edge}x{bed_y - edge}", f"{edge}x{bed_y - edge}"]
            return 1 if probe(single_stl, 1, edge, 'single') else 0
        # Multi-copy uses the independent 3 mm layout margin plus only feature extent.
        multi_edge = MULTI_COPY_EDGE_MM + required
        eff_edge = max(0.0, multi_edge - expand_mm)
        eff_w = bed_x - 2 * multi_edge
        eff_d = bed_y - 2 * multi_edge
        max_possible = max(1, min(copy_limit, int(
            (eff_w * eff_d) / max(1.0, (scaled_w + spacing_mm) * (scaled_d + spacing_mm)) * 1.5
        ) + 1))
        if not probe(dilated_stl, 1, eff_edge, 'multi'):
            return 1 if probe(single_stl, 1, edge, 'single') else 0
        best = 1
        for c in range(2, max_possible + 1):
            if probe(dilated_stl, c, eff_edge, 'multi'):
                best = c
            else:
                break
        return best


def verified_scale_choices(source: Path, state: dict, brim_policy=None) -> list[float]:
    """Return three verified uniform scales, led by the smallest reduction that fits.

    Fast path: the analytic footprint/height bound for the authored orientation gives a
    0.1% precise candidate that one or two Orca probes confirm (instead of ~8 sequential
    probes). Unreadable geometry falls back to the whole-percent Orca binary search.
    """
    source = Path(source)
    try:
        tris = source_triangles(source)
    except Exception:
        tris = None
    if source.suffix.lower() == '.3mf':
        count = _source_plate_count(source)
        if count > 1:
            if not tris:
                return []  # Painted multi-plate fine fit search is deferred; keep intake probe budget bounded.
            if count > 4:
                return []  # Avoid quadratic per-plate export/probe work for large projects.
    if brim_policy is not None:
        state = {**state, 'brim_policy': brim_policy}
    if tris:
        bound = _orientation_bound(_unique_points(tris), state, IDENTITY_ROT, yaw_search=False)[0]
        if bound >= 1.0 and orca_plate_capacity(source, state, scale=1.0, copy_limit=1, brim_policy=brim_policy) > 0:
            return [1.0]
        maximum = _confirm_scale(lambda s: orca_plate_capacity(source, state, scale=s, copy_limit=1, brim_policy=brim_policy) > 0,
                                 min(bound, 0.999))
        if maximum is not None:
            return _scale_ladder(maximum)
    if orca_plate_capacity(source, state, scale=1.0, copy_limit=1, brim_policy=brim_policy) > 0:
        return [1.0]
    if orca_plate_capacity(source, state, scale=0.5, copy_limit=1, brim_policy=brim_policy) < 1:
        return []

    low, high = 50, 99
    while low < high:
        middle = (low + high + 1) // 2
        if orca_plate_capacity(source, state, scale=middle / 100.0, copy_limit=1, brim_policy=brim_policy) > 0:
            low = middle
        else:
            high = middle - 1
    return _scale_ladder(low / 100.0)


def _scale_ladder(maximum):
    """Minimum shrink, a medium size, and the smallest structurally safe size (50%)."""
    if maximum < 0.52:
        return []
    medium = 0.75 if maximum > 0.75 else round((maximum + 0.5) / 2, 2)
    if medium >= maximum:
        medium = round(maximum - 0.01, 3)
    return [round(maximum, 3), medium, 0.5]


def _confirm_scale(fits, bound, window=0.03):
    """Largest 0.1%-step scale <= the analytic bound that Orca confirms.

    Tries the bound first (usually right), then bisects the window below it
    (~5 probes), then falls back to coarse 1% steps down to 50%. Orca occasionally
    aborts on a specific scale (boost bad_rational); after one retry such a point
    counts as not fitting, which can only lower the reported maximum.
    """
    errors = []

    def safe(scale):
        for _ in range(2):
            try:
                return fits(scale)
            except RuntimeError as exc:
                errors.append(exc)
        return False

    hi = int(bound * 1000)
    if hi < 500:
        return None
    if safe(hi / 1000.0):
        return hi / 1000.0
    lo = max(500, hi - int(window * 1000))
    if not safe(lo / 1000.0):
        step = lo - 10
        while step >= 500:
            if safe(step / 1000.0):
                lo, hi = step, step + 10
                break
            step -= 10
        else:
            if errors and len(errors) >= 4:
                raise errors[-1]  # Orca is broken, not the model: never report "does not fit".
            return None
    while hi - lo > 1:  # invariant: lo fits, hi does not
        middle = (lo + hi) // 2
        if safe(middle / 1000.0):
            lo = middle
        else:
            hi = middle
    return lo / 1000.0


def scale_label(scale):
    """98.6% / 75% style label: one decimal only when the scale is not a whole percent."""
    pct = round(float(scale) * 100, 1)
    return f'{pct:.1f}'.rstrip('0').rstrip('.') + '%'


# ---- Orientation search: offer rotated alternatives whenever a model does not fit as authored.

IDENTITY_ROT = [1, 0, 0, 0, 1, 0, 0, 0, 1]
# Map each model axis to "up" (Z). Upside-down Z is never a fit improvement, so it is omitted.
UP_AXES = {
    '+Y': ('Rotate 90° on X', [1, 0, 0, 0, 0, -1, 0, 1, 0]),
    '-Y': ('Rotate 90° on X', [1, 0, 0, 0, 0, 1, 0, -1, 0]),
    '+X': ('Rotate 90° on Y', [0, 0, -1, 0, 1, 0, 1, 0, 0]),
    '-X': ('Rotate 90° on Y', [0, 0, 1, 0, 1, 0, -1, 0, 0]),
}
SUPPORT_THRESHOLD_NZ = -0.866  # faces within 30° of a horizontal ceiling (process.json threshold 30°)


def _matmul(a, b):
    return [sum(a[r * 3 + k] * b[k * 3 + c] for k in range(3)) for r in range(3) for c in range(3)]


def _yaw(degrees):
    c, s = math.cos(math.radians(degrees)), math.sin(math.radians(degrees))
    return [c, -s, 0, s, c, 0, 0, 0, 1]


def _unique_points(tris):
    if isinstance(tris, IndexedTriangles):
        # Same set the legacy comprehension builds (points referenced by at
        # least one face, deduplicated by value), without expanding faces.
        import numpy as np
        referenced = np.zeros(len(tris.vertices), dtype=bool)
        referenced[tris.faces.ravel()] = True
        unique = np.unique(tris.vertices[referenced], axis=0)
        points = []
        for start in range(0, len(unique), 8192):
            points.extend(tuple(row) for row in unique[start:start + 8192].tolist())
        return points
    return list({p for tri in tris for p in tri})


def _hull(points):
    pts = sorted(set(points))
    if len(pts) <= 2:
        return pts
    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])
    lower, upper = [], []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def resolved_travel_sweep(state):
    """Conservative XY radius for resolved hop settings; Normal Lift sweeps zero."""
    resolved = state.get('resolved_hop_types') or state.get('filament_z_hop_types') or state.get('z_hop_types')
    if not resolved:
        # AD5X/stock machine templates resolve to Normal Lift; retain safe behavior
        # if the runtime state does not carry per-slot resolved settings yet.
        try:
            machine = json.loads((printers.current().templates_dir() / 'machine.json').read_text())
        except Exception:
            machine = {}
        resolved = machine.get('z_hop_types') or ['Normal Lift']
    if isinstance(resolved, dict):
        resolved = list(resolved.values())
    elif not isinstance(resolved, (list, tuple)):
        resolved = [resolved] if resolved else ['Normal Lift']
    if all(str(kind).strip().lower() in ('normal lift', 'normal') for kind in resolved):
        return 0.0
    try:
        machine = json.loads((printers.current().templates_dir() / 'machine.json').read_text())
    except Exception:
        machine = {}
    z_hop = float(state.get('z_hop', (machine.get('z_hop') or [0.4])[0]))
    slope = float(state.get('travel_slope', (machine.get('travel_slope') or [3.0])[0]))
    return z_hop / math.tan(math.radians(slope)) / (2 * math.pi)


def required_feature_edge(state, brim_policy=None):
    """Edge budget = explicit brim extent + resolved travel sweep."""
    policy = brim_policy or state.get('brim_policy') or {}
    brim = float(policy.get('width_mm', 0)) if policy.get('type') in ('outer_only', 'brim_ears') else 0.0
    return brim + resolved_travel_sweep(state)


def _usable(state, single=True):
    bed = state.get('bed_mm', BED)
    margin = SINGLE_COPY_EDGE_MM if single else MULTI_COPY_EDGE_MM
    edge = margin + required_feature_edge(state)
    return (float(bed[0]) - 2 * edge, float(bed[1]) - 2 * edge,
            float(state.get('max_print_height_mm', MAX_HEIGHT_MM)))


def _orientation_bound(points, state, rotation, yaw_search=True):
    """Largest uniform scale (<=1) at which ``rotation`` (+ best yaw) fits one copy; returns
    (scale, yaw_degrees, dims_mm_at_full_size)."""
    eff_w, eff_d, max_h = _usable(state)
    rotated = [_rotate(p, rotation) for p in points]
    zs = [p[2] for p in rotated]
    height = max(zs) - min(zs)
    hull = _hull([(p[0], p[1]) for p in rotated])
    def footprint(deg):
        c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
        xs = [c * x - s * y for x, y in hull]
        ys = [s * x + c * y for x, y in hull]
        return max(xs) - min(xs), max(ys) - min(ys)
    def fit(deg):
        w, d = footprint(deg)
        xy = min(eff_w / w if w else 9, eff_d / d if d else 9)
        # When height binds, every yaw ties on scale: prefer the most XY clearance.
        return (round(min(xy, max_h / height if height else 9), 4), xy)
    best = 0.0
    if yaw_search:
        best = max((step * 1.0 for step in range(180)), key=fit)
        best = max((best + tenth / 10.0 for tenth in range(-10, 11)), key=fit)
    w, d = footprint(best)
    scale = min(eff_w / w if w else 9, eff_d / d if d else 9, max_h / height if height else 9)
    return min(1.0, scale), round(best % 180, 1), [round(w, 1), round(d, 1), round(height, 1)]


_FACE_CACHE: dict = {}


def _faces(tris):
    """Per-triangle (area, unit normal, vertices), computed once per mesh object."""
    hit = _FACE_CACHE.get(id(tris))
    if hit is None or hit[0] is not tris:
        faces = []
        for a, b, c in tris:
            u = (b[0] - a[0], b[1] - a[1], b[2] - a[2])
            v = (c[0] - a[0], c[1] - a[1], c[2] - a[2])
            n = (u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0])
            length = math.sqrt(n[0] ** 2 + n[1] ** 2 + n[2] ** 2)
            if length:
                faces.append((length / 2, n[0] / length, n[1] / length, n[2] / length, a, b, c))
        _FACE_CACHE.clear()
        hit = _FACE_CACHE[id(tris)] = (tris, faces)
    return hit[1]


def _surface_stats_indexed(tris, rotation):
    """Batched twin of _surface_stats for large indexed meshes.

    Mirrors the scalar path exactly in structure: zero-length faces are
    excluded everywhere (including the floor), the floor is the minimum
    rotated Z over kept faces' points, and a kept face counts as contact when
    its rotated unit normal Z is below the support threshold and its highest
    point is within 0.3 mm of the floor, else as overhang.
    """
    import numpy as np
    r6, r7, r8 = (float(v) for v in rotation[6:9])
    vertices, faces = tris.vertices, tris.faces

    def chunks():
        for start in range(0, len(faces), 8192):
            pts = vertices[faces[start:start + 8192].ravel()].reshape((-1, 3, 3))
            a, b, c = pts[:, 0], pts[:, 1], pts[:, 2]
            u = (b[:, 0] - a[:, 0], b[:, 1] - a[:, 1], b[:, 2] - a[:, 2])
            v = (c[:, 0] - a[:, 0], c[:, 1] - a[:, 1], c[:, 2] - a[:, 2])
            n = (u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0])
            length = np.sqrt(n[0] ** 2 + n[1] ** 2 + n[2] ** 2)
            rz = r6 * pts[:, :, 0] + r7 * pts[:, :, 1] + r8 * pts[:, :, 2]
            yield pts, n, length, rz

    floor = math.inf
    for _, _, length, rz in chunks():
        kept = length > 0
        if np.any(kept):
            floor = min(floor, float(rz[kept].min()))
    if floor == math.inf:
        raise ValueError('min() arg is an empty sequence')
    contact = overhang = 0.0
    for _, n, length, rz in chunks():
        kept = length > 0
        if not np.any(kept):
            continue
        safe_length = np.where(kept, length, 1.0)
        rnz = (r6 * (n[0] / safe_length) + r7 * (n[1] / safe_length)
               + r8 * (n[2] / safe_length))
        facing = kept & (rnz < SUPPORT_THRESHOLD_NZ)
        if not np.any(facing):
            continue
        area = length / 2
        top = rz.max(axis=1)
        contact += float(np.sum(area[facing & (top - floor < 0.3)]))
        overhang += float(np.sum(area[facing & ~(top - floor < 0.3)]))
    return contact, overhang


def _surface_stats(tris, rotation):
    """(bed contact area, unsupported overhang area) in mm² for an orientation."""
    if isinstance(tris, IndexedTriangles) and len(tris) >= 32768:
        return _surface_stats_indexed(tris, rotation)
    r6, r7, r8 = (float(v) for v in rotation[6:9])
    faces = _faces(tris)
    floor = min(r6 * p[0] + r7 * p[1] + r8 * p[2] for f in faces for p in f[4:])
    contact = overhang = 0.0
    for area, nx, ny, nz, a, b, c in faces:
        # Rotating the mesh rotates its normals by the same matrix: only rotated nz matters.
        if r6 * nx + r7 * ny + r8 * nz < SUPPORT_THRESHOLD_NZ:
            top = max(r6 * p[0] + r7 * p[1] + r8 * p[2] for p in (a, b, c))
            if top - floor < 0.3:
                contact += area
            else:
                overhang += area
    return contact, overhang


def _euler_xyz(rotation):
    """Return deterministic XYZ Euler angles for a row-major rigid matrix."""
    r = rotation
    y = math.asin(max(-1.0, min(1.0, r[2])))
    if abs(math.cos(y)) > 1e-9:
        x, z = math.atan2(-r[5], r[8]), math.atan2(-r[1], r[0])
    else:
        x, z = math.atan2(r[7], r[4]), 0.0
    return [round(math.degrees(v), 3) for v in (x, y, z)]


def placement_candidates(path_or_triangles, printer_model=None, bed_mm=None, max_z_mm=None):
    """Pure offline placement report; never invokes OrcaSlicer or printer services."""
    tris = source_triangles(path_or_triangles) if isinstance(path_or_triangles, (str, Path)) else path_or_triangles
    if not tris:
        raise ValueError('placement report requires non-empty triangles')
    points = _unique_points(tris)
    if not all(math.isfinite(component) for point in points for component in point):
        raise ValueError('placement report requires finite mesh coordinates')
    bed = tuple(float(v) for v in (bed_mm or BED))
    height_limit = float(max_z_mm if max_z_mm is not None else MAX_HEIGHT_MM)
    if len(bed) != 2 or not all(math.isfinite(v) for v in (*bed, height_limit)) or min(*bed, height_limit) <= 0:
        raise ValueError('bed_mm and max_z_mm must be positive dimensions')
    rows = []
    orientations = [('Upright', 'identity', IDENTITY_ROT)]
    orientations.extend((name, axis, matrix) for axis, (name, matrix) in UP_AXES.items())
    for name, axis, matrix in orientations:
        rotated = [_rotate(p, matrix) for p in points]
        hull = _hull([(p[0], p[1]) for p in rotated])
        z_extent = max(p[2] for p in rotated) - min(p[2] for p in rotated)
        yaw_set = {0.0, 45.0, 90.0}
        for i, a in enumerate(hull):
            b = hull[(i + 1) % len(hull)]
            yaw_set.add(round(math.degrees(math.atan2(b[1]-a[1], b[0]-a[0])) % 90.0, 6))
        for yaw in sorted(yaw_set):
            rot = _matmul(_yaw(yaw), matrix)
            # Yaw is about Z, so XY extents come from the 2D hull alone and Z is unchanged.
            ym = _yaw(yaw)
            moved = [(ym[0]*x + ym[1]*y, ym[3]*x + ym[4]*y) for x, y in hull]
            dims = [max(p[0] for p in moved) - min(p[0] for p in moved),
                    max(p[1] for p in moved) - min(p[1] for p in moved), z_extent]
            mx, my, mz = bed[0]-dims[0], bed[1]-dims[1], height_limit-dims[2]
            euler = _euler_xyz(rot)
            rows.append({'label': name, 'axis': axis, 'rx_deg': euler[0], 'ry_deg': euler[1], 'rz_deg': round(yaw, 3),
                         'footprint_mm': [round(v, 3) for v in dims], 'margin_x_mm': round(mx, 3),
                         'margin_y_mm': round(my, 3), 'margin_z_mm': round(mz, 3),
                         'min_bed_margin_mm': round(min(mx, my, mz), 3),
                         'fits_bed': dims[0] <= bed[0] and dims[1] <= bed[1] and dims[2] <= height_limit,
                         'diagonal_gain_mm': 0.0, 'rotation': [round(v, 9) for v in rot]})
    for row in rows:
        cardinal = next((r for r in rows if r['axis'] == row['axis'] and r['rz_deg'] == 0.0), row)
        row['diagonal_gain_mm'] = round(max(0.0, cardinal['footprint_mm'][0]-row['footprint_mm'][0],
                                               cardinal['footprint_mm'][1]-row['footprint_mm'][1]), 3)
    rows.sort(key=lambda r: (not r['fits_bed'], 0 if r['axis']=='identity' and r['fits_bed'] else 1,
                             -r['min_bed_margin_mm'], r['footprint_mm'][2], r['label'], r['axis'], r['rz_deg']))
    for rank, row in enumerate(rows, 1):
        row['rank'] = rank
        row['printer_model'] = printer_model
    return rows


def rotation_alternatives(source: Path, state: dict, authored_max: float = 0.0, limit: int = 2) -> list[dict]:
    """Rotated orientations that print larger than the authored orientation can.

    Each result is Orca-verified for one copy and carries an exact rigid rotation matrix
    (axis change plus the best bed yaw) for slicing, a 0.1% precise maximum scale, the
    resulting size, and whether new overhangs likely need supports.
    """
    import tempfile
    source = Path(source)
    if source.suffix.lower() == '.3mf' and _source_plate_count(source) > 1:
        return []  # multi-plate projects are split/merged by the planner, never re-oriented
    tris = source_triangles(source)
    points = _unique_points(tris)
    _, authored_overhang = _surface_stats(tris, IDENTITY_ROT)
    candidates: list[dict] = []
    # Same model, turned on the bed (yaw only): no new overhangs.
    scale, yaw, dims = _orientation_bound(points, state, IDENTITY_ROT)
    # A quarter turn is not redundant on rectangular printable areas.
    if yaw != 0.0 and scale > authored_max + 0.004:
        candidates.append({'name': f'Turn {round(yaw)}° on the bed', 'axis': '+Z', 'rotation': _yaw(yaw),
                           'bound': scale, 'dims_full_mm': dims, 'supports': False})
    for axis_pair in (('+Y', '-Y'), ('+X', '-X')):
        options = []
        for axis in axis_pair:
            name, up = UP_AXES[axis]
            contact, overhang = _surface_stats(tris, up)
            options.append((contact, -overhang, axis, name, up, overhang))
        contact, _, axis, name, up, overhang = max(options)
        scale, yaw, dims = _orientation_bound(points, state, up)
        if scale <= authored_max + 0.004:
            continue
        needs = overhang > authored_overhang * 1.5 + 100.0
        candidates.append({'name': name, 'axis': axis, 'rotation': _matmul(_yaw(yaw), up), 'bound': scale,
                           'dims_full_mm': dims, 'supports': needs, 'bed_contact_mm2': round(contact),
                           'overhang_mm2': round(overhang)})
    # An envelope is not a placement: equal dimensions can hide different
    # contact faces, hulls and support geometry. Only identical transforms are
    # redundant; independently verify every distinct rigid placement.
    unique: list[dict] = []
    for row in candidates:
        if any(all(abs(a - b) < 1e-9 for a, b in zip(u['rotation'], row['rotation']))
               and u['supports'] == row['supports'] for u in unique):
            continue
        unique.append(row)
    candidates = unique
    if not candidates:
        return []
    verified = []
    # Sequential on purpose: concurrent flatpak Orca runs collide (exit 243).
    # The rotation goes straight to the probe, which uses the cached rotated hull
    # instead of writing and re-reading a dense rotated mesh per orientation.
    for row in candidates:
        fits = lambda s, rot=row['rotation'], sup=bool(row['supports']): orca_plate_capacity(
            source, state, scale=s, copy_limit=1, allow_rotations=False, rotation=rot, supports=sup) > 0
        verified.append(_confirm_scale(fits, row['bound']))
    results = []
    for row, scale in zip(candidates, verified):
        if scale is None or scale <= authored_max:
            continue
        row = dict(row, scale=scale, full_size=scale >= 1.0,
                   dims_mm=[round(v * scale, 1) for v in row['dims_full_mm']])
        row['rotation'] = [round(v, 9) for v in row['rotation']]
        contact = row.get('bed_contact_mm2')
        footprint = row['dims_mm'][0] * row['dims_mm'][1]
        # Tall part on a sliver of bed: recommend a brim so it does not topple.
        row['brim'] = contact is not None and (contact < 400 or contact < 0.05 * footprint)
        extras = [x for x, on in (('supports', row['supports']), ('brim', row['brim'])) if on]
        row['label'] = (f"{row['name']}, {'full size' if scale >= 1.0 else scale_label(scale)}"
                        + (f" (needs {' + '.join(extras)})" if extras else ''))
        results.append(row)
    # At equal verified scale/support requirements, preserve the authored contact face.
    results.sort(key=lambda r: (-r['scale'], r['supports'], r['axis'] != '+Z'))
    return results[:limit]


def fit_options(source: Path, state: dict, brim_override=None) -> dict:
    """Everything the intake needs after a no-fit, in one worker call.

    ``scales``: verified authored-orientation ladder (may be empty).
    ``rotations``: Orca-verified rotated orientations that print larger than the
    best authored scale (stood upright etc.), each with supports/brim warnings.
    """
    source = Path(source)
    # Tight fits must not inherit auto_brim: choose a fitting explicit policy.
    # An operator override is authoritative and its width is included in clearance.
    requested = brim_override if brim_override is not None else state.get('brim_override')
    policies = ([{'type': 'outer_only', 'width_mm': float(requested), 'label': f'Outer-only brim, {float(requested):g} mm'}]
                if requested is not None else
                [{'type': 'outer_only', 'width_mm': w, 'label': f'Outer-only brim, {w:g} mm'} for w in (3.0, 2.0, 1.0)]
                + [{'type': 'no_brim', 'width_mm': 0.0, 'label': 'No brim'}])
    policy = policies[-1]
    scales = []
    for candidate in policies:
        candidate_scales = verified_scale_choices(source, state, brim_policy=candidate)
        if candidate_scales:
            policy, scales = candidate, candidate_scales
            break
    try:
        # Explicit operator brim clearance applies to every candidate, including yaw.
        rotation_state = {**state, 'brim_policy': policies[0]} if requested is not None else state
        rotations = rotation_alternatives(source, rotation_state, authored_max=scales[0] if scales else 0.0)
    except (RuntimeError, ValueError, OSError) as exc:
        return {'scales': scales, 'rotations': [], 'rotation_error': str(exc)}
    result = {'scales': scales, 'rotations': rotations,
              'brim_policy': policy if scales and scales[0] < 0.97 else None,
              'fit_search_deferred': bool(source.suffix.lower() == '.3mf' and _source_plate_count(source) > 1)}
    try:
        from .geometry_layout import probe_single_plate_rearrange
        bed = tuple(state.get('bed_mm') or BED)
        max_h = float(state.get('max_print_height_mm', MAX_HEIGHT_MM))
        rearrange = probe_single_plate_rearrange(source, bed=(float(bed[0]), float(bed[1]), max_h))
        if rearrange is not None:
            result['rearrange_100'] = rearrange
    except Exception:
        pass
    if source.suffix.lower() == '.3mf':
        try:
            from . import color_geometry
            from .source_colors import inventory
            manifest = inventory(source)
            # Per-part fit at 100%: every authored object is evaluated on its
            # own against the usable bed, the Z limit and dimension-preserving
            # yaws before any shrink scale is offered (#76). A part that fits
            # no plate alone is blocking: separate plates cannot cure it.
            per_part = {}
            try:
                from . import split_fit
                bed = tuple(state.get('bed_mm') or BED)
                per_part = {row['object_id']: row for row in split_fit.per_part_fit(
                    source, bed=(float(bed[0]), float(bed[1])),
                    height=float(state.get('max_print_height_mm', MAX_HEIGHT_MM)))}
            except Exception:
                per_part = {}
            details = []
            checked = ['authored XY', 'yaw sweep 0-90 degrees'] if per_part else ['authored XY']
            with zipfile.ZipFile(source) as archive:
                for part in manifest.get('parts', []):
                    box = color_geometry._world_bounds(archive, {str(part['object_id'])})[0]
                    dims = [round(box[i][1] - box[i][0], 1) for i in range(3)]
                    row = {'name': part.get('name') or f"Object {part['object_id']}",
                           'object_id': str(part['object_id']),
                           'extent_mm': dims, 'orientations_checked': checked}
                    fit = per_part.get(str(part['object_id']))
                    if fit is not None:
                        row.update({'fits_full_size': fit['fits_full_size'],
                                    'best_yaw_deg': fit['best_yaw_deg'],
                                    'best_extent_mm': fit['best_extent_mm'],
                                    'usable_mm': fit['usable_mm'],
                                    'height_limit_mm': fit['height_limit_mm'],
                                    'blocking_reason': fit['blocking_reason'],
                                    **({'placement_mm': fit['placement_mm']}
                                       if fit.get('placement_mm') else {})})
                    details.append(row)
            result['parts'] = details
            blocking = [row['name'] for row in details if row.get('fits_full_size') is False]
            if blocking:
                result['blocking_parts'] = blocking
        except (RuntimeError, OSError, zipfile.BadZipFile, KeyError, ValueError):
            result['parts'] = []
    return result
