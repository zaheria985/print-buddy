"""Plate previews for the Bambu handoff package (#275).

A Bambu Lab printer, Bambu Handy and Bambu Studio show a job's preview from Metadata/plate_<n>.png
(512 px) and Metadata/plate_<n>_small.png (128 px) inside the .gcode.3mf. The G-code carries none
for Bambu printers: Orca embeds thumbnails only for other vendors. Two offline sources, in order:

  1. Orca's own base64 PNG blocks when the G-code has them ("; thumbnail begin WxH N" ... "; thumbnail
     end"), resized to the Bambu sizes.
  2. A render of the geometry the slicer was given (the slicer command's input, else the placed
     geometry next to the G-code, else the job's source file): the model on the printer's bed, drawn
     with numpy and Pillow. No OpenSCAD, no display, no network.

Nothing is fabricated: with neither source the package is built without previews, as before #275.
Studio's top_<n>.png and pick_<n>.png (its object-picking colour map) are not written: they are not what
the printer or the apps show, and the pick map cannot be derived honestly here.
"""
from __future__ import annotations

import base64
import binascii
import io
import math
import re
from dataclasses import dataclass, field
from pathlib import Path

PLATE_PX, SMALL_PX = 512, 128
_SUPERSAMPLE = 2
# Orca: "; thumbnail begin 300x300 34716", base64 lines each prefixed "; ", then "; thumbnail end".
_BLOCK = re.compile(r'^; thumbnail begin (\d+)x(\d+) \d+[ \t]*\r?\n((?:;[^\n]*\r?\n)*?); thumbnail end', re.M)
_GEOMETRY = ('clones.stl', 'geometry.3mf', 'geometry.stl')


@dataclass
class Thumbnails:
    """PNG bytes by role ('plate', 'plate_small') and where they came from, for the log."""
    images: dict = field(default_factory=dict)
    source: str = 'none'


def embedded_thumbnails(text):
    """Orca's base64 PNG thumbnails in a G-code text, largest first: [(width, height, png_bytes)]."""
    found = []
    for match in _BLOCK.finditer(text):
        data = ''.join(line.lstrip(';').strip() for line in match.group(3).splitlines())
        try:
            png = base64.b64decode(data, validate=True)
        except (binascii.Error, ValueError):
            continue
        if png.startswith(b'\x89PNG\r\n\x1a\n'):
            found.append((int(match.group(1)), int(match.group(2)), png))
    return sorted(found, key=lambda item: -(item[0] * item[1]))


def _triangle_array(triangles):
    import numpy as np
    if hasattr(triangles, 'vertices') and hasattr(triangles, 'faces'):  # IndexedTriangles
        vertices, faces = np.asarray(triangles.vertices, dtype=float), np.asarray(triangles.faces)
        return vertices[faces] if len(faces) else np.zeros((0, 3, 3))
    if hasattr(triangles, 'coordinates'):  # CompactTriangles
        return np.asarray(triangles.coordinates, dtype=float).reshape(-1, 3, 3)
    return np.asarray(list(triangles), dtype=float).reshape(-1, 3, 3)


def _basis(top):
    """Camera: yaw about Z, then an elevation above the plate (straight down for the top view)."""
    yaw = 0.0 if top else math.radians(35)
    elevation = math.pi / 2 if top else math.radians(30)
    cy, sy = math.cos(yaw), math.sin(yaw)
    ce, se = math.cos(elevation), math.sin(elevation)
    right = (cy, sy, 0.0)
    forward = (-sy * ce, cy * ce, -se)       # into the scene; larger dot = farther away
    up = (-sy * se, cy * se, ce)
    return right, up, forward


def render_plate(triangles, size=PLATE_PX, bed_mm=None, top=False):
    """Render triangles (mm, bed coordinates) as a PNG: the model on its bed, three-quarter or top view.

    Painter's algorithm over every triangle with flat shading; transparent background like Bambu
    Studio's own plate renders. Pure numpy + Pillow.
    """
    import numpy as np
    from PIL import Image, ImageDraw
    tris = _triangle_array(triangles)
    if not len(tris):
        raise ValueError('no triangles to render')
    right, up, forward = (np.array(v) for v in _basis(top))
    canvas = int(size) * _SUPERSAMPLE
    points = tris.reshape(-1, 3)
    if bed_mm:
        bx, by = float(bed_mm[0]), float(bed_mm[1])
        bed = np.array([[0, 0, 0], [bx, 0, 0], [bx, by, 0], [0, by, 0]], dtype=float)
        points = np.concatenate([points, bed])
    sx, sy = points @ right, points @ up
    span = max(float(sx.max() - sx.min()), float(sy.max() - sy.min()), 1e-6)
    margin = canvas * 0.06
    scale = (canvas - 2 * margin) / span
    ox = (canvas - (sx.max() - sx.min()) * scale) / 2 - sx.min() * scale
    oy = (canvas - (sy.max() - sy.min()) * scale) / 2 + sy.max() * scale
    image = Image.new('RGBA', (canvas, canvas), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image, 'RGBA')
    if bed_mm:
        quad = [(float(p @ right * scale + ox), float(-(p @ up) * scale + oy)) for p in bed]
        draw.polygon(quad, fill=(214, 218, 224, 255), outline=(150, 156, 165, 255))
    screen_x = tris @ right * scale + ox
    screen_y = -(tris @ up) * scale + oy
    depth = (tris @ forward).mean(axis=1)
    normals = np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0])
    lengths = np.linalg.norm(normals, axis=1)
    lengths[lengths == 0] = 1.0
    normals /= lengths[:, None]
    light = np.array([-0.35, -0.5, 0.8])
    light /= np.linalg.norm(light)
    shade = 0.45 + 0.55 * np.abs(normals @ light)
    base = np.array([38, 132, 212], dtype=float)
    colours = np.clip(base * shade[:, None], 0, 255).astype(int)
    for index in np.argsort(-depth, kind='stable'):
        polygon = [(float(screen_x[index, j]), float(screen_y[index, j])) for j in range(3)]
        draw.polygon(polygon, fill=(int(colours[index, 0]), int(colours[index, 1]), int(colours[index, 2]), 255))
    return _png(image.resize((int(size), int(size)), Image.LANCZOS))


def _png(image):
    buffer = io.BytesIO()
    image.save(buffer, format='PNG')
    return buffer.getvalue()


def _square(image, size):
    from PIL import Image, ImageOps
    return ImageOps.pad(image.convert('RGBA'), (size, size), method=Image.LANCZOS, color=(0, 0, 0, 0))


def _provenance_for(job, gcode):
    """The slice provenance recorded for this G-code (single slice or one derived plate)."""
    if not isinstance(job, dict):
        return {}
    if str(job.get('gcode') or '') == str(gcode):
        return job.get('provenance') or {}
    for plate in job.get('derived_plates') or []:
        if isinstance(plate, dict) and str(plate.get('gcode') or '') == str(gcode):
            return plate.get('provenance') or {}
    return {}


def geometry_candidates(gcode, job=None):
    """Geometry files that may be what the slicer was given for this G-code, best first.

    The recorded slicer command's input (exact), then the placed geometry Print Buddy wrote next to the
    G-code, then the job's downloaded source. Only existing STL/3MF files; a candidate without
    triangles is skipped by plate_thumbnails, so a later one can still serve.
    """
    gcode = Path(gcode)
    command = _provenance_for(job, gcode).get('slicer_command') or []
    candidates = [command[-1]] if command else []
    candidates += [gcode.parent / name for name in _GEOMETRY]
    if isinstance(job, dict) and job.get('downloaded_file'):
        candidates.append(job['downloaded_file'])
    found = []
    for candidate in candidates:
        path = Path(candidate)
        if path.suffix.lower() in ('.stl', '.3mf') and path.is_file() and path not in found:
            found.append(path)
    return found


def plate_thumbnails(gcode, job=None, bed_mm=None, text=None):
    """Previews for one plate's G-code: Orca's embedded thumbnail, else a render of the sliced geometry."""
    import zipfile
    from PIL import Image
    gcode = Path(gcode)
    text = gcode.read_text(errors='replace') if text is None else text
    embedded = embedded_thumbnails(text)
    if embedded:
        width, height, png = embedded[0]
        image = Image.open(io.BytesIO(png))
        return Thumbnails({'plate': _png(_square(image, PLATE_PX)), 'plate_small': _png(_square(image, SMALL_PX))},
                          f'Orca thumbnail {width}x{height} embedded in the G-code')
    from .orca_adapter import source_triangles
    for geometry in geometry_candidates(gcode, job):
        try:
            triangles = source_triangles(geometry)
        except (OSError, RuntimeError, ValueError, zipfile.BadZipFile):
            continue
        if not len(triangles):
            continue
        plate = Image.open(io.BytesIO(render_plate(triangles, PLATE_PX, bed_mm)))
        return Thumbnails({'plate': _png(plate), 'plate_small': _png(plate.resize((SMALL_PX, SMALL_PX), Image.LANCZOS))},
                          f'rendered from {geometry.name}')
    return Thumbnails({}, 'no thumbnail in the G-code and no sliced geometry to render')
