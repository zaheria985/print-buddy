#!/usr/bin/env python3
"""Fetch, inspect, and (only with --approved) slice Printables or MakerWorld jobs.

Normal path, one command:
  python3 -m print_buddy.core.model_job fetch URL
It verifies current hardware against Klipper + Moonraker + Spoolman, inventories every
listed source profile/file, downloads only the selected source, audits the 3MF/STL, and
creates JOB.json. It DOES NOT slice, upload, or start a print.

After the operator selects a numbered source and explicitly approves slicing:
  python3 -m print_buddy.core.model_job fetch URL --select N --approved
The baked site G-code is never used on this printer. Geometry is resliced against
the live-verified nozzle, loaded Spoolman filament, and configured motion limits. Upload and
print stay separate unless --print-now is given, which means the operator clicked
"bed clear" for exactly this job. `python3 -m print_buddy.core.model_job print-now FILE|JOB.json` starts an
already sliced job the same way.
"""
import argparse, hashlib, html, json, math, os, re, shutil, subprocess, sys, time, urllib.parse, urllib.request, zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, NamedTuple

from .. import printers, settings
from ..printers.base import MultiColorUnsupported
from . import _external, launch_state, printer_state

PROFILE = settings.current()["paths"]["hermes_home"]
STATE = settings.state_dir()/"printer_hardware.json"
ARCHIVE = settings.current()["paths"]["archive"]
MOONRAKERCTL = settings.ROOT/"bin"/"moonrakerctl"
TIMEZONE = settings.current()["discord"]["timezone"]
IFS_SLOTS = settings.current()["printer"]["ifs_slots"]
BED_MM = int(min(settings.current()["printer"]["bed_mm"]))
MAX_HEIGHT_MM = settings.current()["printer"]["max_print_height_mm"]

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36"


def request_json(url, *, data=None, headers=None, timeout=25):
    h={"User-Agent":UA, "Accept":"application/json"}; h.update(headers or {})
    body=json.dumps(data).encode() if data is not None else None
    req=urllib.request.Request(url, data=body, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r: return json.load(r)


def download(url, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    req=urllib.request.Request(url, headers={"User-Agent":UA})
    with urllib.request.urlopen(req, timeout=90) as r, open(target,"wb") as out:
        shutil.copyfileobj(r,out)
    if target.stat().st_size == 0: raise RuntimeError("downloaded an empty file")


def maker_headers():
    token=(Path.home()/".config/bambu/token").read_text().strip()
    if not token: raise RuntimeError("MakerWorld token empty")
    return {"Authorization":f"Bearer {token}", "Origin":"https://makerworld.com",
            "Referer":"https://makerworld.com/", "x-bbl-app-source":"makerworld"}


CJK=re.compile(r'[\u3000-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\uff00-\uffef]')
def english(*candidates):
    """First non-empty candidate without CJK text; else the first non-empty one.
    MakerWorld returns the author's original-language title plus an English
    `titleTranslated`. Discord output must stay English."""
    vals=[str(c).strip() for c in candidates if c and str(c).strip()]
    for v in vals:
        if not CJK.search(v): return v
    return vals[0] if vals else ""


def maker_id(url):
    m=re.search(r'/models/(\d+)',url)
    if not m: raise ValueError("MakerWorld URL needs /models/<number>")
    return m.group(1)


def maker_inventory(url):
    mid=maker_id(url)
    d=request_json(f"https://api.bambulab.com/v1/design-service/design/{mid}",headers=maker_headers())
    rows=[]
    for ix, i in enumerate(d.get("instances",[]),1):
        mi=i.get("extention",{}).get("modelInfo",{})
        c=mi.get("compatibility",{})
        plates=mi.get("plates",[])
        fs=i.get("instanceFilaments") or []
        from .makerworld_profiles import annotate as annotate_maker_profiles
        rows.append({"n":ix,"instance_id":i["id"],"title":english(i.get("titleTranslated"), i.get("title")) or "untitled",
                     "site_nozzle_mm":c.get("nozzleDiameter"),"site_printer":c.get("devProductName"),
                     "compatibility":c,"other_compatibility":mi.get("otherCompatibility") or [],
                     "plates":[{"name":p.get("name"),"objects":len(p.get("objects") or []),
                                "weight_g":p.get("weight"),"prediction_s":p.get("prediction")} for p in plates],
                     "materials":[f.get("type") for f in fs],
                     "filaments": [{"source_index": n, "material": f.get("type"), "color": f.get("color")}
                                   for n, f in enumerate(fs, 1)],
                     "source_parts": [{"plate": n, "objects": p.get("objects") or []}
                                      for n, p in enumerate(plates, 1)],
                     "is_default":i.get("isDefault",False)})
    from .makerworld_profiles import annotate as annotate_maker_profiles
    annotate_maker_profiles(rows)
    title=english(d.get("titleTranslated"), d.get("title"), d.get("name")) or f"makerworld-{mid}"
    return {"site":"makerworld","model_id":mid,"title":title,"slug":d.get("slug") or mid,
            "license":d.get("license"),"summary":html.unescape(re.sub('<[^>]+>',' ',english(d.get("summaryTranslated"), d.get("summary")) or "")).strip(),"sources":rows}


def _cached_source(site, key, value, model_id=None):
    """Newest earlier download of the same site file, so re-runs never hit the site again (rate limits, captcha)."""
    if value is None: return None
    best=None
    for jp in ARCHIVE.glob("*/JOB.json"):
        try:
            d=json.loads(jp.read_text())
        except (OSError, ValueError):
            continue
        sel=d.get("selected_source") or {}
        f=Path(d.get("downloaded_file") or "")
        if (d.get("site")==site and str(sel.get(key))==str(value)
                and (model_id is None or str(d.get("model_id"))==str(model_id))
                and f.is_file() and f.stat().st_size>0):
            if best is None or f.stat().st_mtime>best.stat().st_mtime: best=f
    return best


def _cached_maker_source(instance_id):
    return _cached_source("makerworld", "instance_id", instance_id)


def _reuse_cached(cached, target, site):
    target.mkdir(parents=True, exist_ok=True)
    dest=target/cached.name
    if dest.resolve()!=cached.resolve(): shutil.copy2(cached, dest)
    print(f"SOURCE REUSED: {cached} (already downloaded; {site} not contacted)")
    return dest


def maker_download(job, source, target):
    cached=_cached_maker_source(source.get("instance_id"))
    if cached is not None:
        return _reuse_cached(cached, target, "MakerWorld")
    try:
        r=request_json(f"https://api.bambulab.com/v1/design-service/instance/{source['instance_id']}/f3mf?type=download",headers=maker_headers())
    except urllib.error.HTTPError as exc:
        if exc.code == 418:
            # MakerWorld's Cloudflare front answers file downloads with 418 + a GeeTest captcha when it decides the
            # account/IP looks automated. Never try to solve or dodge it; say so plainly.
            raise RuntimeError("MakerWorld blocked the download with a robot check (captcha). Open the model on "
                               "makerworld.com while logged in and pass the check there, or wait a while, then send the link again") from None
        raise
    name=Path(r.get("name") or "model.3mf").name
    target=target/name
    download(r["url"],target)
    return target

PRINTABLES="https://api.printables.com/graphql/"
def printables_id(url):
    m=re.search(r'/model/(\d+)',url)
    if not m: raise ValueError("Printables URL needs /model/<number>")
    return m.group(1)
def gql(query, variables):
    return request_json(PRINTABLES,data={"query":query,"variables":variables},headers={"Content-Type":"application/json","Origin":"https://www.printables.com","Referer":"https://www.printables.com/"})
def printables_inventory(url):
    mid=printables_id(url)
    q='''query($id: ID!) { print(id:$id) { id name slug license{name} stls{id name fileSize} gcodes{id name fileSize} otherFiles{id name fileSize} } }'''
    d=gql(q,{"id":mid}).get("data",{}).get("print")
    if not d: raise RuntimeError("Printables model not found")
    files=[]
    for kind in ("stls","gcodes","otherFiles"):
        for f in d.get(kind,[]): files.append({"id":f["id"],"name":f["name"],"bytes":f["fileSize"],"kind":kind})
    return {"site":"printables","model_id":mid,"title":d["name"],"slug":d.get("slug") or mid,"license":(d.get("license")or{}).get("name"),"sources":files}
def printables_download(job, source, target):
    cached=_cached_source("printables", "id", source.get("id"), job.get("model_id"))
    if cached is not None and cached.name==Path(source.get("name") or "").name:
        return _reuse_cached(cached, target, "Printables")
    q='''mutation($id:ID!,$printId:ID!,$source:DownloadSourceEnum!,$fileType:DownloadFileTypeEnum!){getDownloadLink(id:$id,printId:$printId,source:$source,fileType:$fileType){ok output{link} errors{messages}}}'''
    typ="gcode" if source["kind"] == "gcodes" else "stl"
    d=gql(q,{"id":str(source["id"]),"printId":str(job["model_id"]),"source":"model_detail","fileType":typ})["data"]["getDownloadLink"]
    if not d.get("ok"): raise RuntimeError(str(d.get("errors")))
    target=target/Path(source["name"]).name; download(d["output"]["link"],target); return target


def _timing(job_path, stage, **extra):
    """Launch latency instrumentation: append one line to <job>/TIMING.jsonl. Never raises."""
    try:
        line={"stage":stage,"t":round(time.time(),3),"pid":os.getpid(),**extra}
        print("TIMING "+json.dumps(line), flush=True)
        if job_path is not None:
            with (Path(job_path).parent/"TIMING.jsonl").open("a") as out: out.write(json.dumps(line)+"\n")
    except Exception:
        pass


def file_sha256(path):
    with Path(path).open("rb") as source:
        return hashlib.file_digest(source,"sha256").hexdigest()


def zmod_md5_valid(path):
    """Validate ZMod preprocess format: first-line digest covers remaining bytes."""
    data = Path(path).read_bytes()
    first, sep, payload = data.partition(b"\n")
    match = re.fullmatch(rb"; MD5:([0-9a-fA-F]{32})\r?", first)
    if not sep or not match:
        return False
    return hashlib.md5(payload).hexdigest().encode("ascii") == match.group(1).lower()


def finalize_multicolor(*args, **kwargs):
    """ZMod color/slot-map insertion and MD5, through the printer adapter."""
    return printers.current().native.finalize(*args, **kwargs)


def native_heater_lines_valid(text, bed, hotend):
    """Require the documented native startup prefix, not later layer heaters."""
    valid = printers.current().native.native_startup_valid
    return valid(text, bed, hotend)


def plate_manifest(path):
    """Inventory source plate assignments without slicing or touching the printer."""
    path=Path(path)
    if path.suffix.lower() != ".3mf": raise RuntimeError("plate manifest requires a 3MF source")
    with zipfile.ZipFile(path) as archive:
        names=set(archive.namelist())
        preview_ids={int(m.group(1)) for name in names
                     if (m:=re.fullmatch(r"Metadata/plate_(\d+)\.png",name))}
        if "Metadata/model_settings.config" not in names:
            raise RuntimeError("3MF has no plate assignments in Metadata/model_settings.config")
        try:
            root=ET.fromstring(archive.read("Metadata/model_settings.config"))
            plates=[]
            for plate in root.iter("plate"):
                metadata={m.get("key"):m.get("value") for m in plate.findall("metadata")}
                raw_id=metadata.get("plater_id")
                if raw_id is None: raise RuntimeError("plate has no plater_id assignment")
                plate_id=int(raw_id)
                object_ids=[]
                for instance in plate.findall("model_instance"):
                    for entry in instance.findall("metadata"):
                        if entry.get("key")=="object_id":
                            value=entry.get("value")
                            if value is None: raise RuntimeError(f"plate {plate_id} has empty object assignment")
                            object_ids.append(int(value))
                if not object_ids: raise RuntimeError(f"plate {plate_id} has no object assignments")
                plates.append({"id":plate_id,"name":metadata.get("plater_name") or f"Plate {plate_id}",
                               "object_ids":object_ids,"status":"planned"})
        except (ET.ParseError,KeyError,TypeError,ValueError) as exc:
            raise RuntimeError(f"invalid 3MF plate assignments: {exc}") from exc
        ids=[p["id"] for p in plates]
        if not ids or len(ids)!=len(set(ids)) or (preview_ids and preview_ids!=set(ids)):
            raise RuntimeError("3MF plate assignments missing, duplicated, or inconsistent with previews")
    return {"source_file":str(path.resolve()),"source_sha256":file_sha256(path),
            "plates":sorted(plates,key=lambda p:p["id"])}


def model_info_output(path):
    """Run or read cached geometry extents/manifoldness for a model file."""
    path = Path(path)
    global _INFO_CACHE
    if "_INFO_CACHE" not in globals():
        _INFO_CACHE = {}
    try:
        st = path.stat()
        key = (str(path.resolve()), st.st_size, st.st_mtime)
        if key in _INFO_CACHE:
            return _INFO_CACHE[key]
    except Exception:
        key = None

    # Measured from the printable mesh in-process (Orca-only stack; no Prusa).
    # Cutters and modifiers are already excluded by source_triangles.
    from .orca_adapter import source_triangles
    tris = source_triangles(path)
    if not tris:
        raise RuntimeError("model has no printable geometry")
    lo = [min(p[a] for t in tris for p in t) for a in range(3)]
    hi = [max(p[a] for t in tris for p in t) for a in range(3)]
    vol = 0.0
    for (ax, ay, az), (bx, by, bz), (cx, cy, cz) in tris:
        vol += (ax * (by * cz - bz * cy) - ay * (bx * cz - bz * cx) + az * (bx * cy - by * cx)) / 6.0
    info = "".join(f"{k} = {hi[a] - lo[a]:.2f}\n" for a, k in enumerate(("size_x", "size_y", "size_z")))
    info += f"volume = {abs(vol):.2f}\n"
    if key is not None:
        _INFO_CACHE[key] = info
    return info


def mesh_topology(triangles):
    """Conservative indexed-by-position topology summary; does not repair geometry."""
    vertices = {}
    faces = []
    degenerate = 0
    for tri in triangles:
        ids = []
        for point in tri:
            key = tuple(round(float(v), 6) for v in point)
            ids.append(vertices.setdefault(key, len(vertices)))
        if len(set(ids)) < 3:
            degenerate += 1
        faces.append(ids)
    edges = {}
    adjacency = [[] for _ in faces]
    for fi, face in enumerate(faces):
        for a, b in zip(face, face[1:] + face[:1]):
            key = (min(a, b), max(a, b))
            edges.setdefault(key, []).append((fi, a < b))
    for uses in edges.values():
        for fi, _ in uses:
            for other, _ in uses:
                if fi != other:
                    adjacency[fi].append(other)
    seen = set()
    shells = 0
    for start in range(len(faces)):
        if start in seen:
            continue
        shells += 1
        stack = [start]
        seen.add(start)
        while stack:
            current = stack.pop()
            for other in adjacency[current]:
                if other not in seen:
                    seen.add(other)
                    stack.append(other)
    boundary = sum(len(uses) == 1 for uses in edges.values())
    nonmanifold = sum(len(uses) > 2 for uses in edges.values())
    winding = sum(len(uses) == 2 and uses[0][1] == uses[1][1] for uses in edges.values())
    incident = {}
    for edge, uses in edges.items():
        for vertex in edge:
            incident.setdefault(vertex, []).append(len(uses))
    singular = sum(any(count != 2 for count in counts) for counts in incident.values())
    manifold = not (boundary or nonmanifold or singular or degenerate or winding)
    return {"triangle_count": len(faces), "connected_shells": shells,
            "boundary_edges": boundary, "non_manifold_edges": nonmanifold,
            "singular_vertices": singular, "winding_inconsistencies": winding,
            "degenerate_triangles": degenerate, "watertight": boundary == 0 and nonmanifold == 0,
            "manifold": manifold}


def mesh_audit(path):
    """Read-only topology report for STL/3MF source geometry."""
    from .orca_adapter import source_triangles
    triangles = source_triangles(path)
    if not triangles:
        raise RuntimeError("model has no printable geometry")
    return {"schema_version": 1, "sha256": file_sha256(path), **mesh_topology(triangles)}


def audit(path):
    with open(path,"rb") as source:
        digest=hashlib.file_digest(source,"sha256").hexdigest()
    out={"path":path.name,"bytes":path.stat().st_size,"sha256":digest,"kind":path.suffix.lower()}
    if path.suffix.lower()==".3mf":
        with zipfile.ZipFile(path) as z:
            names=z.namelist(); out["archive_files"]=len(names)
            plate_ids={int(m.group(1)) for n in names if (m:=re.search(r'plate_(\d+)\.(?:gcode|png)$',n))}
            if "Metadata/model_settings.config" in names:
                try:
                    settings = ET.fromstring(z.read("Metadata/model_settings.config"))
                    for plate in settings.iter("plate"):
                        for metadata in plate.iter("metadata"):
                            if metadata.get("key") == "plater_id":
                                plate_id = metadata.get("value")
                                if plate_id is None: raise ValueError("plate has no plater_id value")
                                plate_ids.add(int(plate_id))
                except (ET.ParseError, ValueError) as exc:
                    raise RuntimeError(f"unable to inspect 3MF plate assignments: {exc}") from exc
            out["plates"]=len(plate_ids); out["plate_ids"]=sorted(plate_ids)
            text=""
            for n in names:
                if n.endswith(("3dmodel.model","model_settings.config")):
                    try:text+=z.read(n).decode(errors="ignore")
                    except:pass
            out["objects"]=len(re.findall(r'<object\b',text)); out["components"]=len(re.findall(r'<component\b',text)); out["build_items"]=len(re.findall(r'<item\b',text)); out["named_parts"]=re.findall(r'<metadata key="name" value="([^"]+)',text)[:30]
    from .source_colors import inventory, prompts
    manifest = inventory(path)
    out['color_manifest'] = manifest
    out['color_prompts'] = prompts(manifest)
    try:
        out["mesh_audit"] = mesh_audit(path)
    except Exception as exc:
        out["mesh_audit_error"] = str(exc)
    try:
        info = model_info_output(path)
    except Exception as exc:  # unreadable mesh: slice_job re-measures and refuses clearly
        info = ""; out["geometry_error"] = str(exc)
    for key in ("size_x","size_y","size_z","number_of_parts","manifold"):
        m=re.search(rf'^{key}\s*=\s*(.+)$',info,re.M)
        if m: out[key]=m.group(1).strip()
    return out


def safe_slug(s): return re.sub(r'[^a-z0-9]+','-',s.lower()).strip('-')[:70]
def intake_id_from_message(source, message_id):
    """Return the canonical intake identity for a Discord message, never its snowflake."""
    if not message_id:
        raise ValueError('plain model link requires a stable Discord message ID')
    identity = [getattr(source, 'profile', ''), str(getattr(source, 'chat_id', '')),
                str(getattr(source, 'thread_id', '') or ''), str(message_id)]
    return hashlib.sha256(json.dumps(identity).encode()).hexdigest()


def intake_folder(archive, slug, intake_id=None):
    if intake_id is not None and not re.fullmatch(r'[a-f0-9]{64}', intake_id):
        raise ValueError('intake ID must be a 64 character lowercase SHA256')
    return Path(archive) / (safe_slug(slug) + ('--' + intake_id if intake_id else ''))


def write_job(folder, job):
    # Atomic replace: the background slice and the intake flow read JOB.json
    # concurrently; a plain write_text truncates first, so a reader could see
    # an empty file and fail with "Expecting value: line 1 column 1".
    target = Path(folder) / "JOB.json"
    tmp = target.with_name(f".JOB.json.{os.getpid()}.{time.monotonic_ns()}.tmp")
    tmp.write_text(json.dumps(job, indent=2) + "\n")
    os.replace(tmp, target)
def show(job):
    print(f"{job['title']} [{job['site']} {job['model_id']}]\nHardware: {printer_state.summary(job['hardware'])}")
    print("Sources:")
    for x in job["sources"]:
        if len(x.get('filaments', [])) > 1 or len(x.get('source_parts', [])) > 1:
            print('  Color/part choices required for this profile: ' + json.dumps({
                'filaments': x.get('filaments'), 'parts_by_plate': x.get('source_parts')}))
        if job["site"]=="makerworld":
            from .makerworld_profiles import selection_summary
            print(f"  {x['n']}. {selection_summary(x)} | nozzle {x.get('site_nozzle_mm')} mm | {', '.join(x['materials']) or 'material unstated'}")
        else: print(f"  {x['n']}. {x['name']} | {x['kind']} | {x['bytes']/1048576:.1f} MB")

NUMBER=r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][+-]?\d+)?'


_GCODE_LINE_RE = re.compile(r'(?im)^[ \t]*(G90|G91|G0?0|G0?1|G0?2|G0?3|G92|G28|G53|FORCE_MOVE|MANUAL_STEPPER|STEPPER_BUZZ|SET_KINEMATIC_POSITION)(?=\s|$)([^;\r\n]*)')
_GCODE_XYZ_RE = re.compile(r'([XYZ])\s*('+NUMBER+r')')
_GCODE_SUPPORTED = {"G0", "G00", "G1", "G01"}
_GCODE_FORBIDDEN = {"G28", "G53", "FORCE_MOVE", "MANUAL_STEPPER", "STEPPER_BUZZ", "SET_KINEMATIC_POSITION"}


def gcode_motion_bounds(text):
    """Return fail-closed XYZ bounds for ordinary slicer G-code."""
    absolute = True
    position = {axis: float("nan") for axis in "XYZ"}
    low = {axis: float("inf") for axis in "XYZ"}
    high = {axis: float("-inf") for axis in "XYZ"}
    recognized = 0
    for match in _GCODE_LINE_RE.finditer(text):
        command = match.group(1).upper()
        if command == "G90":
            absolute = True
        elif command == "G91":
            absolute = False
        elif command in _GCODE_FORBIDDEN:
            lineno = text.count("\n", 0, match.start()) + 1
            raise RuntimeError(f"line {lineno}: unsupported motion-affecting command {command}")
        elif command in ("G2", "G02", "G3", "G03"):
            lineno = text.count("\n", 0, match.start()) + 1
            raise RuntimeError(f"line {lineno}: arc motion is unsupported by the bounds verifier")
        elif command == "G92":
            if _GCODE_XYZ_RE.search(match.group(2).upper()):
                lineno = text.count("\n", 0, match.start()) + 1
                raise RuntimeError(f"line {lineno}: G92 changes XYZ coordinates; bounds are ambiguous")
        elif command in _GCODE_SUPPORTED:
            words = match.group(2).upper()
            found = False
            for xyz_match in _GCODE_XYZ_RE.finditer(words):
                axis, number = xyz_match.groups()
                found = True
                value = float(number)
                if absolute:
                    position[axis] = value
                else:
                    if position[axis] != position[axis]:
                        lineno = text.count("\n", 0, match.start()) + 1
                        raise RuntimeError(f"line {lineno}: relative {axis} motion before an absolute {axis} position")
                    position[axis] += value
                low[axis] = min(low[axis], position[axis])
                high[axis] = max(high[axis], position[axis])
            if found:
                recognized += 1
    if not recognized:
        raise RuntimeError("no bounded XYZ motion found")
    missing = [axis for axis in "XYZ" if low[axis] == float("inf")]
    if missing:
        raise RuntimeError("missing bounded motion for axis " + ", ".join(missing))
    return {axis.lower(): (low[axis], high[axis]) for axis in "XYZ"}


BOUNDS_CACHE = Path(os.environ.get("PRINT_BUDDY_BOUNDS_CACHE", settings.state_dir()/"gcode-bounds-cache"))


def cached_motion_bounds(raw, text=None):
    """gcode_motion_bounds keyed by content sha256.

    Slicing verifies offline first; print-now then reverifies the same bytes.
    Reparsing a 24 MB plate costs ~30 s per pass, so identical bytes reuse the
    stored result. Failures are never cached, so a bad file still fails closed.
    """
    digest = hashlib.sha256(raw).hexdigest()
    entry = BOUNDS_CACHE/f"{digest}.json"
    try:
        hit = json.loads(entry.read_text())
        return {axis: tuple(hit[axis]) for axis in "xyz"}
    except (OSError, ValueError, KeyError, TypeError):
        pass
    bounds = gcode_motion_bounds(text if text is not None else raw.decode(errors="ignore"))
    try:
        BOUNDS_CACHE.mkdir(parents=True, exist_ok=True)
        tmp = entry.with_suffix(".tmp")
        tmp.write_text(json.dumps({axis: list(bounds[axis]) for axis in "xyz"}))
        tmp.replace(entry)
    except OSError:
        pass
    return bounds


FILAMENT_SENSOR_OBJECTS = ("filament_switch_sensor head_switch_sensor", "zmod_color", "zmod_ifs", "print_stats", "save_variables")
MAP_PREFIX = "; hermes_tool_slots = "


def format_color_title(c):
    return ' '.join(w.upper() if w.casefold() in {'pla', 'pla+', 'petg', 'petg-cf', 'abs', 'tpu'} else w.capitalize() for w in str(c or '').split())


def build_slot_labels(selections):
    counts = {}
    for s in selections:
        c_title = format_color_title(s.get('color'))
        mat = str(s.get('material', '')).strip()
        name = c_title if mat and c_title.casefold().endswith(mat.casefold()) else f'{c_title} {mat}'.strip()
        counts[name] = counts.get(name, 0) + 1
    labels = []
    for s in selections:
        c_title = format_color_title(s.get('color'))
        mat = str(s.get('material', '')).strip()
        name = c_title if mat and c_title.casefold().endswith(mat.casefold()) else f'{c_title} {mat}'.strip()
        if counts[name] > 1:
            labels.append(f'{name} (Slot {s["slot"]})')
        else:
            labels.append(name)
    return labels


def validate_filament_selection(selection):
    """Validate a job-bound, user-confirmed filament selection."""
    if not isinstance(selection, dict):
        raise ValueError("explicit filament selection is required (use 'filament-select' command)")
    slot = selection.get("slot")
    if not isinstance(slot, int) or isinstance(slot, bool) or not 1 <= slot <= IFS_SLOTS:
        raise ValueError(f"filament slot must be an integer from 1 through {IFS_SLOTS}")
    for key in ("color", "material"):
        if not isinstance(selection.get(key), str) or not selection[key].strip():
            raise ValueError(f"filament {key} is required")
    spool_id = selection.get("spoolman_id")
    if not isinstance(spool_id, int) or isinstance(spool_id, bool) or spool_id <= 0:
        raise ValueError("a positive Spoolman spool ID is required")
    return {"slot": slot, "color": selection["color"].strip(),
            "material": selection["material"].strip(), "spoolman_id": spool_id}


def resolve_filament_selection(state, selection):
    """Resolve metadata from the requested slot, never the globally active spool.

    Configured slot labels are consistency checks, not proof of physical loading.
    An unmapped slot requires an explicit spool ID rather than an inferred one.
    """
    if not isinstance(selection, dict):
        return validate_filament_selection(selection)
    selection = dict(selection)
    slot = selection.get("slot")
    if not isinstance(slot, int) or isinstance(slot, bool) or not 1 <= slot <= IFS_SLOTS:
        raise ValueError(f"filament slot must be an integer from 1 through {IFS_SLOTS}")
    mappings = state.get("ifs_slots", {})
    configured = mappings.get(str(slot), {}) if isinstance(mappings, dict) else {}
    if not isinstance(configured, dict):
        raise ValueError(f"slot {slot} configured mapping is invalid")
    mapped_id = configured.get("spoolman_id")
    if selection.get("spoolman_id") is None:
        if mapped_id is None:
            raise ValueError(f"slot {slot} requires an explicit Spoolman spool ID; no configured mapping")
        selection["spoolman_id"] = mapped_id
    for key in ("color", "material"):
        if selection.get(key) is None and configured.get(key) is not None:
            selection[key] = configured[key]
    selection = validate_filament_selection(selection)
    if mapped_id is not None and selection["spoolman_id"] != mapped_id:
        raise ValueError(f"slot {slot} spool mismatch: requested #{selection['spoolman_id']}, configured #{mapped_id}")
    def canonical_color(value):
        text = value.strip().casefold().lstrip("#")
        return text.upper() if re.fullmatch(r"[0-9a-fA-F]{6}", text) else " ".join(text.split())
    for key in ("color", "material"):
        value = configured.get(key)
        if value is not None and (not isinstance(value, str) or
                                   (canonical_color(value) if key == "color" else value.strip().casefold()) !=
                                   (canonical_color(selection[key]) if key == "color" else selection[key].strip().casefold())):
            raise ValueError(f"slot {slot} {key} does not match the configured selection")
    return selection


def save_filament_selection(job_path, selection):
    """Persist a confirmed selection only in the explicitly named job file."""
    path = Path(job_path)
    job = json.loads(path.read_text())
    if not isinstance(job, dict) or not job.get("model_id"):
        raise ValueError("not a model JOB.json")
    job["filament_selection"] = validate_filament_selection(selection)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(job, indent=2) + "\n")
    os.replace(tmp, path)
    return job["filament_selection"]


READINESS_MAX_AGE_SECONDS = 15


def _native():
    """True when the configured printer has the ZMod native-screen runtime (display_off, offsets, stock API)."""
    return printers.current().has_native_screen


def _gcode_tools(text):
    """Tool indexes a G-code selects with a bare Tn line."""
    return {int(m.group(1)) for line in text.splitlines()
            if (m := re.fullmatch(r"T(\d+)(?:\s*;.*)?", line.strip()))}


def _refuse_multicolor(text):
    """Non-multi-slot families refuse a G-code that selects any tool but T0, through the adapter."""
    adapter = printers.current()
    adapter.normalize_tool_mapping({str(t): t + 1 for t in sorted(_gcode_tools(text) | {0})}, {})
    return adapter


def telemetry_state(value):
    """Classify raw telemetry without coercing strings, numbers, or null to booleans."""
    if value is True:
        return "true"
    if value is False:
        return "false"
    return "unknown"


def check_filament_readiness(state, status, selection=None, *, observed_at=None, now=None):
    """Fail closed unless explicit, fresh telemetry agrees with the job's AD5X selection."""
    problems = []
    now = time.time() if now is None else now
    if (not isinstance(now, (int, float)) or isinstance(now, bool) or
            not isinstance(observed_at, (int, float)) or isinstance(observed_at, bool)):
        problems.append("telemetry freshness is unknown")
    elif (now - observed_at < 0 or now - observed_at > READINESS_MAX_AGE_SECONDS):
        problems.append("telemetry is stale or has an invalid timestamp")

    sensor = status.get("filament_switch_sensor head_switch_sensor") if isinstance(status, dict) else None
    if not isinstance(sensor, dict) or telemetry_state(sensor.get("enabled")) != "true":
        problems.append(f"head filament sensor enabled state is {telemetry_state(sensor.get('enabled') if isinstance(sensor, dict) else None)}")
    head = sensor.get("filament_detected") if isinstance(sensor, dict) else None
    print_stats = status.get('print_stats') if isinstance(status, dict) else None
    phase = print_stats.get('state') if isinstance(print_stats, dict) else None
    staged_idle = phase in ('standby', 'complete', 'cancelled')
    if telemetry_state(head) == "unknown" or (head is False and not staged_idle):
        if telemetry_state(head) == "unknown":
            problems.append(f"head filament sensor is unknown (value {head!r})")
        else:
            problems.append("head filament sensor reports no filament during a non-idle print phase (possible runout)")
    # An enabled, empty head is normal before IFS loading. Unknown execution
    # state and active/paused runout never receive the idle staging exemption.
    if isinstance(selection, dict) and "filament_selection" in selection:
        selection = selection.get("filament_selection")
    if isinstance(selection, dict):
        try:
            selection = validate_filament_selection(selection)
        except ValueError as exc:
            problems.append(str(exc))
            return problems
    color = status.get("zmod_color") if isinstance(status, dict) else None
    slots = color.get("slots") if isinstance(color, dict) else None
    if not isinstance(slots, list):
        problems.append("AD5X IFS slot inventory is unavailable")
        return problems
    if not isinstance(selection, dict):
        problems.append("job has no explicit IFS slot/color selection")
        return problems
    try:
        selection = validate_filament_selection(selection)
    except ValueError as exc:
        problems.append(str(exc))
        return problems
    slot_id = selection["slot"]
    def slot_id_of(item):
        raw = item.get("ID") if isinstance(item, dict) else None
        if isinstance(raw, int) and not isinstance(raw, bool) and 1 <= raw <= IFS_SLOTS:
            return raw
        if isinstance(raw, str) and raw.isdigit() and 1 <= int(raw) <= IFS_SLOTS:
            return int(raw)
        return None

    malformed = [item for item in slots if slot_id_of(item) is None]
    if malformed:
        problems.append("AD5X IFS inventory contains malformed slot IDs")
    matches = [item for item in slots if slot_id_of(item) == selection["slot"]]
    if len(matches) > 1:
        problems.append(f"AD5X IFS inventory contains duplicate ID {slot_id}")
        return problems
    if not matches:
        problems.append(f"selected IFS slot {slot_id} is unavailable")
        return problems
    actual = matches[0]
    feeder = actual.get("hasFilament")
    feeder_state = telemetry_state(feeder)
    if feeder_state == "false":
        problems.append(
            f"selected IFS slot {slot_id} reports false (empty). Check the printer screen's IFS view for slot {slot_id}; "
            f"if it is empty, feed filament into slot {slot_id}'s inlet until the IFS grips it, then retry. "
            "The AD5X has no buffer switch to click")
    elif feeder_state != "true":
        problems.append(
            f"selected IFS slot {slot_id} presence is unknown (raw value {feeder!r}); "
            "this is a telemetry fault, not proof the slot is empty")
    for key, actual_key in (("material", "Material"), ("color", "Color")):
        wanted = selection.get(key)
        observed = actual.get(actual_key)
        if not isinstance(wanted, str) or not wanted.strip():
            problems.append(f"job selection is missing {key}")
        elif not isinstance(observed, str) or observed.casefold() != wanted.casefold():
            problems.append(f"selected IFS slot {slot_id} {key} does not match the job selection")
    return problems


def read_filament_telemetry(state):
    """Read and label filament telemetry without triggering any printer action."""
    objects = urllib.parse.quote("&".join(FILAMENT_SENSOR_OBJECTS), safe="&")
    try:
        response = request_json(f"{state['moonraker_url']}/printer/objects/query?{objects}")
        status = response.get("result", {}).get("status", {}) if isinstance(response, dict) else {}
    except Exception as exc:
        status = {}
        query_error = f"{type(exc).__name__}: {exc}"
    else:
        query_error = None
    if not isinstance(status, dict):
        status = {}
        query_error = query_error or "invalid Moonraker status payload"
    sensor = status.get("filament_switch_sensor head_switch_sensor")
    color = status.get("zmod_color")
    slots = color.get("slots") if isinstance(color, dict) else None
    raw_zmod_slots = [
        {"ID": item.get("ID"), "hasFilament": item.get("hasFilament")}
        for item in slots if isinstance(item, dict)
    ] if isinstance(slots, list) else "unknown"
    ifs = status.get("zmod_ifs")
    ports = ifs.get("Ports") if isinstance(ifs, dict) else None
    variables_obj = status.get("save_variables")
    variables = variables_obj.get("variables") if isinstance(variables_obj, dict) else None
    mode = variables.get("display_off") if isinstance(variables, dict) else None
    if type(mode) is not int or mode not in (0, 1):
        mode = None
    mode_name = "native" if mode == 0 else "screenless" if mode == 1 else "unknown"
    stock_detail_error = None
    stock_detail_used = False
    if mode == 0:
        try:
            native_slots = read_stock_ifs_slots(state)
            # Native authenticated /detail is authoritative for occupancy.
            apply_stock_presence(status, native_slots)
            slots = color.get("slots") if isinstance(color, dict) else None
            stock_detail_used = True
        except Exception as exc:
            # Preserve raw ZMod telemetry, but never present its known-stale
            # native-mode occupancy flags as evidence of empty or loaded slots.
            stock_detail_error = f"{type(exc).__name__}: {exc}"
            if isinstance(slots, list):
                for item in slots:
                    if isinstance(item, dict):
                        item["hasFilament"] = "unknown"
                        item["presence_source"] = "conflicted: stock /detail unavailable; raw zmod_color preserved separately"
    return {
        "queried_at_epoch": time.time(),
        "source": "Moonraker printer/objects/query",
        "printer_mode": mode_name,
        "display_off": mode if mode in (0, 1) else "unknown",
        "stock_detail_authoritative": stock_detail_used,
        "stock_detail_error": stock_detail_error,
        "raw_zmod_color_presence": raw_zmod_slots,
        "freshness_note": "Query time is recorded; the endpoint does not provide per-field sample timestamps, so underlying sensor freshness is unknown.",
        "query_error": query_error,
        "configured_palette": [
            {"ID": item.get("ID"), "Material": item.get("Material"), "Color": item.get("Color")}
            for item in slots if isinstance(item, dict)
        ] if isinstance(slots, list) else "unknown",
        "feeder_slots": [
            {"ID": item.get("ID"), "hasFilament": telemetry_state(item.get("hasFilament"))}
            for item in slots if isinstance(item, dict)
        ] if isinstance(slots, list) else "unknown",
        "feeder_ports_zero_based": [telemetry_state(port) for port in ports]
        if isinstance(ports, list) else "unknown",
        "feeder_slot_mapping": "zmod_color slot IDs are 1-based; zmod_ifs Ports list indices are 0-based",
        "ifs_enabled": telemetry_state(ifs.get("ifs")) if isinstance(ifs, dict) else "unknown",
        "silk_state": ifs.get("Silk") if isinstance(ifs, dict) else "unknown",
        "toolhead": {
            "source_object": "filament_switch_sensor head_switch_sensor",
            "filament_detected": telemetry_state(sensor.get("filament_detected"))
            if isinstance(sensor, dict) else "unknown",
            "enabled": telemetry_state(sensor.get("enabled")) if isinstance(sensor, dict) else "unknown",
        },
    }


# Stock firmware /detail readers live with the ZMod code; these delegate through the printer adapter.
def read_stock_detail(state, *, post=None):
    """Read native status using profile credentials without logging them."""
    return printers.current().native.read_stock_detail(state, post=post)


def validate_native_start(detail, selection):
    """Require native ready and an explicit physical-slot route, not stale feeder state."""
    return printers.current().native.validate_native_start(detail, selection)


def read_stock_ifs_slots_from_detail(detail):
    """Extract native slot presence from one already-fetched authenticated detail sample."""
    return printers.current().native.read_stock_ifs_slots_from_detail(detail)


def read_stock_ifs_slots(state, *, post=None):
    """Native presence, not the screen-mode hardcoded zmod_color flags."""
    return read_stock_ifs_slots_from_detail(read_stock_detail(state, post=post))


def apply_stock_presence(status, stock):
    """Replace zmod_color hasFilament with stock presence; unknown stays unknown."""
    return printers.current().native.apply_stock_presence(status, stock)


def _validate_slot_selection(state, selection):
    """The selected IFS slot, not the globally active spool, owns a single-color job."""
    slot = state.get('ifs_slots', {}).get(str(selection['slot']), {})
    if (not slot.get('physical_color_verified') or not slot.get('spoolman_id')
            or slot.get('spoolman_id') != selection['spoolman_id']
            or str(slot.get('material', '')).casefold() != selection['material'].casefold()):
        raise RuntimeError(f"selected IFS slot {selection['slot']} is unverified or its material/spool mapping differs")
    return slot


def _sync_active_spool(st, slot):
    """Point Moonraker and the hardware record at the launching slot's spool.

    Runs only after every launch preflight has passed and before upload. A
    failed write or readback refuses the launch with nothing uploaded. Nozzle
    and bed temperatures are left alone; per-slot temperatures are Phase 2.
    """
    configured = st.get('ifs_slots', {}).get(str(slot), {})
    sid = configured.get('spoolman_id')
    if not isinstance(sid, int) or isinstance(sid, bool):
        raise RuntimeError(f'IFS slot {slot} has no mapped Spoolman spool; nothing uploaded or started')
    try:
        current = _mr(st, '/server/spoolman/spool_id')['result'].get('spool_id')
    except Exception as exc:
        raise RuntimeError(f'Moonraker active spool read failed: {exc}; nothing uploaded or started') from exc
    if current == sid and st.get('spool', {}).get('spoolman_id') == sid:
        return {'spool_id': sid, 'changed': False}
    try:
        spool = request_json(f"{st['spoolman_url']}/api/v1/spool/{sid}")
    except Exception as exc:
        raise RuntimeError(f'Spoolman spool #{sid} lookup failed: {exc}; nothing uploaded or started') from exc
    filament = spool.get('filament') or {}
    if spool.get('archived') or not printer_state.same_material(filament.get('material'), configured.get('material')):
        raise RuntimeError(f'Spoolman spool #{sid} is archived or its material differs from slot {slot}; nothing uploaded or started')
    try:
        _mr(st, '/server/spoolman/spool_id', data={'spool_id': sid})
        back = _mr(st, '/server/spoolman/spool_id')['result'].get('spool_id')
    except Exception as exc:
        raise RuntimeError(f'Moonraker active spool update failed: {exc}; nothing uploaded or started') from exc
    if back != sid:
        raise RuntimeError(f'Moonraker active spool readback {back!r} != #{sid}; nothing uploaded or started')
    fresh = printer_state.load()
    fresh['spool'] = {'spoolman_id': sid, 'name': filament.get('name'), 'material': filament.get('material'),
                      'diameter_mm': filament.get('diameter', 1.75), 'density': filament.get('density', 1.24)}
    printer_state.save(fresh)
    st['spool'] = dict(fresh['spool'])
    print(f'ACTIVE SPOOL SYNCED: #{current} -> #{sid} (slot {slot})', flush=True)
    return {'spool_id': sid, 'previous': current, 'changed': True}


def _verify_readiness_adapter(adapter, state, selection=None):
    """Families without IFS: one Moonraker sample judged by the adapter's own readiness check."""
    try:
        objects = urllib.parse.quote("&".join(adapter.moonraker_objects()), safe="&")
        status = request_json(f"{state['moonraker_url']}/printer/objects/query?{objects}")["result"]["status"]
        problems = adapter.readiness_problems(state, status, selection)
        if problems:
            raise RuntimeError("; ".join(problems))
    except Exception as exc:
        raise RuntimeError(f"{adapter.label} readiness unavailable; print refused: {exc}") from exc


def _verify_filament_readiness(state, selection=None):
    """Read current mode, then obtain one freshness-bounded sensor/occupancy sample."""
    adapter = printers.current()
    if not adapter.multi_slot:
        return _verify_readiness_adapter(adapter, state, selection)
    try:
        selection = resolve_filament_selection(state, selection)
        _validate_slot_selection(state, selection)
    except ValueError as exc:
        raise RuntimeError(f"filament readiness refused: {exc}") from exc
    # Phase 1: the Moonraker active spool is an accounting pointer that follows
    # the job at launch (_sync_active_spool); it is not a physical feed fact.
    try:
        mode_data = _mr(state, "/printer/objects/query?save_variables")["result"]["status"]
        mode = (mode_data.get("save_variables") or {}).get("variables", {}).get("display_off")
        if mode not in (0, 1):
            raise RuntimeError(f"printer mode unknown: display_off={mode!r}")
        response = request_json(f"{state['moonraker_url']}/printer/objects/query?{urllib.parse.quote('&'.join(FILAMENT_SENSOR_OBJECTS), safe='&')}")
        status = response["result"]["status"]
        observed_at = time.time()
        if mode == 0:
            detail = read_stock_detail(state)
            native_at = time.time()
            native_phase = detail.get("status")
            moonraker_phase, moonraker_file, _ = _printer_phase(state)
            if native_phase == "printing" and moonraker_phase not in ("printing", "paused"):
                raise RuntimeError("native status='printing' conflicts with Moonraker standby; execution state unresolved")
            if native_phase in ("ready", "idle", "standby") and moonraker_phase in ("printing", "paused"):
                raise RuntimeError(f"Moonraker {moonraker_phase} conflicts with native status={native_phase!r}; execution state unresolved")
            apply_stock_presence(status, read_stock_ifs_slots_from_detail(detail))
            observed_at = native_at
        problems = check_filament_readiness(state, status, selection, observed_at=observed_at)
        if problems:
            raise RuntimeError("; ".join(problems))
    except Exception as exc:
        raise RuntimeError(f"filament readiness unavailable; print refused: {exc}") from exc


def verify_gcode_offline(gcode_path, state):
    """Check startup, checksum, and bed bounds without contacting the printer.

    The live sibling reads display mode from Moonraker. This one validates the
    artifact against the already loaded hardware record: MD5 when present,
    native heater prefix, nozzle, material, start and end, and motion bounds.
    """
    p = Path(gcode_path)
    raw = p.read_bytes()
    text = raw.decode(errors='ignore')
    errors = []
    if raw.startswith(b'; MD5:') and not zmod_md5_valid(p):
        errors.append('invalid ZMod MD5 checksum')
    from .nozzle import effective_nozzle_temperature
    nozzle_type = (state.get('nozzle') or {}).get('type') or settings.current().get('printer', {}).get('nozzle_type') or 'hardened_steel'
    try:
        hotend = effective_nozzle_temperature(nozzle_type, float(state['temps_c']['nozzle']))
    except Exception as exc:
        raise RuntimeError("offline G-code verification failed: invalid effective nozzle temperature: " + str(exc)) from exc
    if not native_heater_lines_valid(text, state['temps_c']['bed'], hotend):
        errors.append('startup heater prefix does not match the recorded bed and effective nozzle temperature')
    def config(key):
        match = re.search(rf'^; {re.escape(key)} = (.+)$', text, re.M)
        return match.group(1).strip() if match else None
    nozzle = config('nozzle_diameter')
    if nozzle is None or abs(float(nozzle) - state['nozzle']['diameter_mm']) > 1e-3:
        errors.append(f"nozzle {nozzle} != recorded {state['nozzle']['diameter_mm']}")
    hotend, bed = effective_nozzle_temperature(nozzle_type, float(state['temps_c']['nozzle'])), state['temps_c']['bed']
    start_match = re.search(r'^START_PRINT\s+([^;]*)', text, re.M)
    start_args = dict(re.findall(r'([A-Z_]+)=([+-]?(?:\d+(?:\.\d*)?|\.\d+))', start_match.group(1))) if start_match else {}
    start_matches = (start_match is not None and
                     math.isclose(float(start_args.get('BED_TEMP', 'nan')), float(bed), abs_tol=1e-3) and
                     math.isclose(float(start_args.get('EXTRUDER_TEMP', 'nan')), float(hotend), abs_tol=1e-3))
    if not start_matches:
        errors.append(f'missing START_PRINT matching bed {bed} and effective hotend {hotend}')
    if not re.search(r'^END_PRINT(?:\s|$)', text, re.M):
        errors.append('missing END_PRINT')
    bounds = cached_motion_bounds(raw, text)
    limits = state.get('axis_limits_mm', {'x': [0, BED_MM], 'y': [0, BED_MM], 'z': [0, state.get('max_print_height_mm', MAX_HEIGHT_MM)]})
    for axis in 'xyz':
        low, high = bounds[axis]
        limit_low, limit_high = map(float, limits[axis])
        if low < limit_low - 1e-3 or high > limit_high + 1e-3:
            errors.append(f'{axis.upper()} bounds {low:.3f}..{high:.3f} exceed {limit_low:.3f}..{limit_high:.3f}')
    if errors:
        raise RuntimeError('offline G-code verification failed: ' + '; '.join(errors))
    return {'bounds_mm': bounds, 'nozzle': nozzle, 'offline': True}


def verify_gcode(gcode_path):
    """Check a G-code artifact against live nozzle, spool and temperature state."""
    st=printer_state.load(); ok, probs, _=printer_state.check(st)
    if not ok: raise RuntimeError("live hardware check failed:\n  " + "\n  ".join(probs))
    p=Path(gcode_path); raw=p.read_bytes(); text=raw.decode(errors="ignore")
    errors=[]
    # The installed native screen requires ZMod's documented first-line MD5.
    # Screenless files remain accepted without it, but any present checksum is validated.
    if raw.startswith(b"; MD5:") and not zmod_md5_valid(p):
        errors.append("invalid ZMod MD5 checksum")
    native = _native()
    mode = _mr(st, "/printer/objects/query?save_variables")["result"]["status"]["save_variables"]["variables"].get("display_off") if native else None
    nozzle_type = (st.get("nozzle") or {}).get("type") or settings.current().get("printer", {}).get("nozzle_type") or "hardened_steel"
    from .printer_state import _material_temperature_contract
    try:
        _material_temperature_contract(st)
    except RuntimeError as exc:
        errors.append(str(exc))
    from .nozzle import effective_nozzle_temperature
    effective_hotend = effective_nozzle_temperature(nozzle_type, st["temps_c"]["nozzle"])
    if not native:
        pass  # no native screen: no MD5 or startup-heater contract
    elif mode == 0:
        if not zmod_md5_valid(p):
            errors.append("native screen requires valid ZMod MD5 checksum")
        if not native_heater_lines_valid(text, st["temps_c"]["bed"], effective_hotend):
            errors.append("native screen requires immediate startup M190 Sbed / M104 Snozzle T0 consistent with START_PRINT; later heaters do not count")
    elif mode != 1:
        errors.append(f"unknown display_off mode {mode!r}; refusing hardware-dependent G-code validation")
    def config(key):
        m=re.search(rf'^; {re.escape(key)} = (.+)$', text, re.M)
        return m.group(1).strip() if m else None
    nozzle=config("nozzle_diameter")
    nozzle_value=None
    if nozzle is None:
        errors.append("missing embedded nozzle_diameter")
    else:
        try:
            nozzle_value=float(nozzle)
        except ValueError:
            errors.append(f"invalid nozzle_diameter {nozzle!r}")
        else:
            if abs(nozzle_value-st["nozzle"]["diameter_mm"])>1e-3:
                errors.append(f"G-code nozzle {nozzle} != live {st['nozzle']['diameter_mm']}")
    material=config("filament_type")
    if native:
        validate_materials = printers.current().native.validate_materials
        try:
            validate_materials(text, material, st)
        except RuntimeError as exc:
            errors.append(str(exc))
    else:
        _refuse_multicolor(text)
        if not printer_state.same_material(material, st["spool"]["material"]):
            errors.append(f"G-code filament_type {material!r} != loaded {st['spool']['material']!r}")
    hotend,bed=effective_hotend,st["temps_c"]["bed"]
    def numeric_macro_match(pattern, bed_key, hotend_key):
        match = re.search(pattern, text, re.M)
        args = dict(re.findall(r'([A-Z_]+)=([+-]?(?:\d+(?:\.\d*)?|\.\d+))', match.group(1))) if match else {}
        try:
            return (match is not None and
                    math.isclose(float(args.get(bed_key, 'nan')), float(bed), abs_tol=1e-3) and
                    math.isclose(float(args.get(hotend_key, 'nan')), float(hotend), abs_tol=1e-3))
        except ValueError:
            return False
    octoprint = getattr(printers.current(), 'transport', None) == 'octoprint'
    if octoprint:
        for command, target in (('M190', bed), ('M109', hotend)):
            values = re.findall(rf'^{command}\s+S([\d.]+)', text, re.M)
            if not values or not math.isclose(float(values[0]), float(target), abs_tol=1e-3):
                errors.append(f'missing {command} matching target {target}')
        # Validate the slicer-owned shutdown, never append a guessed end sequence.
        if not all(re.search(rf'^{cmd}\s+S0(?:\s|$)', text, re.M) for cmd in ('M104', 'M140')):
            errors.append('Marlin slicer end G-code must turn off nozzle and bed (M104 S0, M140 S0)')
    elif not native:
        macro = printers.current().start_macro
        if not numeric_macro_match(rf'^{re.escape(macro)}\s+([^;]*)', 'BED', 'HOTEND'):
            errors.append(f"missing {macro} matching bed {bed} and effective hotend {hotend}")
    elif not (numeric_macro_match(r'^PRINT_START\s+([^;]*)', 'BED', 'HOTEND') or
              numeric_macro_match(r'^START_PRINT\s+([^;]*)', 'BED_TEMP', 'EXTRUDER_TEMP')):
        errors.append(f"missing START_PRINT/PRINT_START matching bed {bed} and effective hotend {hotend}")
    if not octoprint and not (re.search(r'^PRINT_END(?:\s|$)',text,re.M) or re.search(r'^END_PRINT(?:\s|$)',text,re.M)
            or (not native and re.search(rf'^{re.escape(printers.current().end_macro)}(?:\s|$)',text,re.M))):
        errors.append("missing END_PRINT/PRINT_END safe end sequence")
    try:
        bounds=cached_motion_bounds(raw, text)
    except RuntimeError as exc:
        errors.append(f"toolpath bounds unavailable: {exc}")
        bounds=None
    if bounds:
        limits=st.get("axis_limits_mm", {"x":[0,223],"y":[0,223],"z":[0,st.get("max_print_height_mm",250)]})
        for axis in "xyz":
            low,high=bounds[axis]
            limit_low,limit_high=map(float,limits[axis])
            if low<limit_low-1e-3 or high>limit_high+1e-3:
                errors.append(f"{axis.upper()} bounds {low:.3f}..{high:.3f} exceed {limit_low:.3f}..{limit_high:.3f}")
    first_layer_height=config("first_layer_height") or config("initial_layer_print_height")
    try:
        if first_layer_height is None or float(first_layer_height)<=0:
            errors.append(f"invalid first_layer_height {first_layer_height!r}")
    except ValueError:
        errors.append(f"invalid first_layer_height {first_layer_height!r}")
    # Lines narrower than 90% of the orifice are refused, first layer and body alike.
    if nozzle_value is not None:
        floor=0.9*nozzle_value
        layers=re.split(r'^;LAYER_CHANGE', text, flags=re.M)
        def structural_widths(block):
            # Gap infill deliberately fills spaces narrower than the nozzle. Its
            # transition count is not a wall-flow measurement; never remove model
            # features merely to raise this metadata median.
            role = None
            widths = []
            for line in block.splitlines():
                if line.startswith(";TYPE:"):
                    role = line[6:].strip().casefold()
                if line.startswith(";WIDTH:") and role not in ("gap infill", "gap fill"):
                    widths.append(float(line[7:]))
            return widths
        first=structural_widths(layers[1] if len(layers)>1 else "")
        body=structural_widths("".join(layers[2:6]))
        for name,ws in (("first layer",first),("layers 2-5",body)):
            if name == "layers 2-5" and len(layers) <= 2:
                # Single-layer test print (e.g. first-layer calibration pad) has no subsequent layers
                continue
            if not ws:
                errors.append(f"missing {name} WIDTH metadata")
                continue
            med=sorted(ws)[len(ws)//2]
            if med<floor: errors.append(f"{name} line width {med:.2f} mm < {floor:.2f} mm (90% of {nozzle} nozzle): starved extrusion, reslice")
    if errors: raise RuntimeError("G-CODE REFUSED: " + "; ".join(errors))
    print(f"G-CODE OK: {p.name} | {printer_state.summary(st)}")
    return True


CONTEXT = Path(os.environ.get("PRINT_BUDDY_CONTEXT_PATH", settings.state_dir()/"print_buddy_context.json"))
RECORD_KEYS = ("layer_height","first_layer_height","perimeters","fill_density","fill_pattern","support_material","brim_width","nozzle_diameter","filament_type","temperature","bed_temperature")

def _job_for_gcode(g):
    """The archived JOB.json that produced G-code g.

    A split job keeps only plate 1 in JOB.json["gcode"]; plates 2..N live in
    derived_plates and multipart_plan.parts, so match those too (#48).
    """
    for jp in ARCHIVE.glob("*/JOB.json"):
        try: j=json.loads(jp.read_text())
        except (OSError, ValueError): continue
        if not isinstance(j, dict): continue
        paths=[j.get("gcode")]
        paths+=[r.get("gcode") for r in j.get("derived_plates") or [] if isinstance(r, dict)]
        paths+=[p.get("gcode_path") for p in (j.get("multipart_plan") or {}).get("parts") or [] if isinstance(p, dict)]
        if any(p and Path(p).resolve()==g for p in paths): return j
    return None


def copies_supported(job, multi_plate=None):
    """Whether slicing this job clones copies into its G-code (#37).

    Only the single plate path passes copies to Orca. Multi-plate, split and
    per-part jobs slice one copy per plate, so they must never be offered more.
    """
    if multi_plate is None:
        try: multi_plate = int((job.get('audit') or {}).get('plates') or 0) > 1
        except (TypeError, ValueError): multi_plate = False
    return not (multi_plate or job.get('full_size_split_plan') or job.get('plate_mode') == 'separate'
                or (job.get('color_decisions') or {}).get('mode') == 'per_part')


def record_start(gcode_path, moonraker_name=None, started_at=None):
    """Write the Obsidian start note plus the completion sidecar for a started print."""
    print_records = _external.load("print_records")
    st=printer_state.load(); g=Path(gcode_path).expanduser().resolve(); text=g.read_text(errors="ignore")
    def cfg(key):
        m=re.search(rf'^; {re.escape(key)} = (.+)$', text, re.M); return m.group(1).strip() if m else None
    job=_job_for_gcode(g)
    key=moonraker_name or g.name
    src=(job or {}).get("selected_source") or {}
    ctx={"job_key":key,"title":(job or {}).get("title") or g.stem,"source_url":(job or {}).get("url"),
         "source_platform":(job or {}).get("site"),"started_at":started_at or print_records.utc_now(),
         "hardware":st.get("printer", "Klipper") + ", "+printer_state.summary(st),
         "printer_profile":"Klipper base ini, resliced locally (site settings not imported)",
         "estimated_time":cfg("estimated printing time (normal mode)"),"estimated_filament_g":cfg("total filament used [g]"),
         "filament_diameter_mm":st["spool"]["diameter_mm"],"filament_density":st["spool"]["density"],
         "slicer_settings":{**{k:cfg(k) for k in RECORD_KEYS if cfg(k) is not None},
                            "source_profile":src.get("title"),"source_file":(job or {}).get("downloaded_file"),"local_gcode":str(g)},
         "active_spool":{"id":st["spool"]["spoolman_id"],"material":st["spool"]["material"],"color":st["spool"]["name"]}}
    plan = (job or {}).get('multipart_plan') or {}
    for part in plan.get('parts') or []:
        if part.get('gcode_path') and Path(part['gcode_path']).resolve() == g:
            # Split plates advance the same plan that /print-next reads.
            ctx.update(multipart_plan_id=plan['plan_id'], part_id=part['id'], required_parts=plan['parts'])
            break
    mapped = printers.current().native.embedded_mapping(text, st) if printers.current().multi_slot else None
    if mapped is not None:
        ctx['tool_to_physical_slot'] = mapped
        ctx['active_spool']['accounting_only_not_job_color'] = True
        ctx['slicer_settings']['mapped_materials'] = {
            tool: st['ifs_slots'][str(slot)] for tool, slot in mapped.items()}
        ctx['printer_profile'] = 'Reviewed Orca AD5X native multicolor'
    ctx["record_path"]=print_records.write_start_record(ctx)
    record_path=ctx["record_path"]
    try: side=json.loads(CONTEXT.read_text())
    except (OSError, ValueError): side={}
    if not isinstance(side.get("jobs"),dict): side={"jobs":{}}
    side["jobs"][key]={**side["jobs"].get(key,{}),**ctx}
    tmp=CONTEXT.with_suffix(".tmp"); tmp.write_text(json.dumps(side,indent=2)+"\n"); os.replace(tmp,CONTEXT)
    print(f"OBSIDIAN START NOTE: {ctx['record_path']}")
    return record_path


# Start announcement ownership. print_now claims the file before starting it so the
# Moonraker listener skips its generic "Print started" post for that same job.
START_CLAIMS = Path(os.environ.get("PRINT_BUDDY_START_CLAIMS", settings.state_dir()/"print_buddy_start_claims.json"))
START_CLAIM_TTL_S = settings.current()["listener"]["start_claim_ttl_s"]


def _write_claims(claims):
    tmp = START_CLAIMS.with_suffix(".tmp"); tmp.write_text(json.dumps(claims)); os.replace(tmp, START_CLAIMS)


def _read_claims():
    try: claims = json.loads(START_CLAIMS.read_text())
    except (OSError, ValueError): return {}
    return claims if isinstance(claims, dict) else {}


def claim_start_announcement(remote, now=None):
    now = time.time() if now is None else now
    claims = {k: v for k, v in _read_claims().items()
              if isinstance(v, (int, float)) and now - v < START_CLAIM_TTL_S}
    claims[remote] = now
    _write_claims(claims)


def release_start_announcement(remote):
    claims = _read_claims()
    if claims.pop(remote, None) is not None:
        try: _write_claims(claims)
        except OSError: pass


def start_print_settings(text):
    """Read the START_PRINT call the printer will actually execute."""
    m = re.search(r'^(?:START_PRINT|PRINT_START)\b(.*)$', text, re.M)
    if not m: return {"line": None, "args": {}}
    return {"line": m.group(0).strip(), "args": dict(re.findall(r'(\w+)=(\S+)', m.group(1)))}


def validate_saved_mesh_start(text, mesh):
    """Both modes must consume the reviewed mesh without new offset/calibration."""
    args = start_print_settings(text)['args']
    expected = mesh.get('profile')
    if (not expected or args.get('MESH') != expected or 'Z_OFFSET' in args or
            args.get('FORCE_LEVELING', 'false').lower() not in ('false', '0') or
            args.get('FORCE_KAMP', 'false').lower() not in ('false', '0')):
        raise RuntimeError(f'saved-mesh flow requires START_PRINT MESH={expected}, no explicit Z_OFFSET and no forced leveling')


Z_OFFSET_NOTICE = ("Z offset: no explicit START_PRINT Z_OFFSET; effective offset depends on the "
                   "installed macros and screen mode. Historical first-layer cause is not established "
                   "by this G-code. Do not inject or change an offset on this evidence alone.")


def z_offset_notice(text):
    """None when START_PRINT passes an explicit Z_OFFSET; otherwise the truthful notice.
    Informational only: with display_off=0 ZMod applies the native screen zProbeOffset, which is correct."""
    return None if "Z_OFFSET" in start_print_settings(text)["args"] else Z_OFFSET_NOTICE


# The 2026-10-03 "too high" prints logged `Printing with bed mesh: ` (blank). The good one loaded `auto`.
MESH_PROFILE = "auto"


def screen_z_offset(st):
    """zProbeOffset from the stock screen's Adventurer5M.json (what ZMod applies with display_off=0). Read-only.

    The captured schema stores it at leftExtruderOffset.zProbeOffset. A top-level or level.zProbeOffset
    is accepted only when the nested field is absent. Non-finite values fail closed as unreadable.
    """
    if not _native(): return None
    try:
        d=_mr(st,"/server/files/config/Adventurer5M.json",timeout=8)
    except Exception: return None
    def numeric(v):
        return isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(float(v))
    nested=((d or {}).get("leftExtruderOffset") or {}).get("zProbeOffset") if isinstance(d,dict) else None
    if "leftExtruderOffset" in (d or {}) and isinstance(d.get("leftExtruderOffset"), dict) and "zProbeOffset" in d["leftExtruderOffset"]:
        return round(float(nested),3) if numeric(nested) else None
    def find(o):
        if isinstance(o,dict):
            v=o.get("zProbeOffset")
            if numeric(v): return v
            for x in o.values():
                r=find(x)
                if r is not None: return r
        return None
    v=find(d)
    return round(float(v),3) if numeric(v) else None


def mesh_readiness(st):
    """Read-only bed mesh check before upload. Returns dict; problem is None when the saved mesh is usable."""
    info: dict = {"profile":None,"saved_at":None,"print_leveling":None,"problem":None,"stale":None}
    if not _native():
        # The printer owns its mesh and offset; there is no saved-mesh contract to enforce.
        info.update(display_off=None, z_offset_mm=None, offset_source=None)
        return info
    try:
        q=_mr(st,"/printer/objects/query?bed_mesh=profiles&save_variables",timeout=8)["result"]["status"]
    except Exception as e:
        info["problem"]=f"could not read the bed mesh from Klipper ({type(e).__name__})"; return info
    profiles=(q.get("bed_mesh") or {}).get("profiles") or {}
    info["print_leveling"]=((q.get("save_variables") or {}).get("variables") or {}).get("print_leveling")
    variables=((q.get("save_variables") or {}).get("variables") or {})
    info["display_off"]=variables.get("display_off")
    saved_z=(variables.get("gcode_offsets") or {}).get("z")
    saved_numeric=not isinstance(saved_z, bool) and isinstance(saved_z, (int, float)) and math.isfinite(saved_z)
    # ZMod stores two offsets. display_off=1 (screenless) loads the numeric
    # save_variables gcode_offsets.z when LOAD_ZOFFSET=1. display_off=0 leaves
    # that value unused and applies the native screen zProbeOffset instead.
    # Neither path has a required magnitude. The two values are never added.
    if info["display_off"]==0:
        native=screen_z_offset(st)
        info["z_offset_mm"]=native
        info["saved_gcode_offset_mm"]=saved_z if saved_numeric else None
        info["offset_source"]="native screen zProbeOffset"
        if not isinstance(native, (int, float)) or isinstance(native, bool):
            info["problem"]="native screen is the offset owner but zProbeOffset is unreadable. Reconcile the screen calibration before starting; do not substitute the screenless saved offset"
            return info
    elif info["display_off"]==1:
        info["z_offset_mm"]=saved_z if saved_numeric else None
        info["offset_source"]="saved gcode offset"
        if variables.get("load_zoffset")!=1 or not saved_numeric:
            info["problem"]="screenless offset is not authoritative; expected LOAD_ZOFFSET=1 and a numeric saved gcode_offsets.z. The native screen offset is a separate store and is not used in this mode"
            return info
    else:
        info["z_offset_mm"]=None
        info["offset_source"]=None
        info["problem"]="display_off is neither 0 nor 1, so the offset owner is unknown. Reconcile before starting"
        return info
    if info["display_off"]==1 and variables.get("mesh_test", 1) not in (0, 1):
        info["problem"]="screenless saved-mesh flow permits MESH_TEST=0 or 1 only; modes 2/4 can rebuild and modes 3/4 auto-tune Z offset. Reconcile before starting"
        return info
    profile = MESH_PROFILE
    if info['display_off'] == 0:
        profile = (st.get('calibration_baseline') or {}).get('native_mesh', MESH_PROFILE)
    if not isinstance(profile, str) or not re.fullmatch(r'[A-Za-z0-9_]+', profile):
        info['problem'] = 'invalid calibration baseline mesh'
        return info
    if profile not in profiles:
        info["problem"]=f"no saved '{profile}' bed mesh. Reconcile the selected calibration baseline before using the saved-mesh print flow"
        return info
    if info["print_leveling"] in (1,True,"1"):
        info["problem"]="PRINT_LEVELING=1 selects per-print rebuilding in screenless ZMod. This saved-mesh workflow requires PRINT_LEVELING=0; reconcile the chosen strategy before starting"
        return info
    info["profile"]=profile
    try:
        files=_mr(st,"/server/files/list?root=config",timeout=8)["result"]
        m=[f.get("modified") for f in files if f.get("path")=="printer.cfg"]
        if m and isinstance(m[0],(int,float)): info["saved_at"]=m[0]
    except Exception: pass
    try:
        inst=(st.get("nozzle") or {}).get("installed")
        if inst and info["saved_at"]:
            from datetime import datetime
            info["stale"]=datetime.fromisoformat(inst).timestamp() > info["saved_at"]
    except (ValueError, TypeError, AttributeError): pass
    return info


def mesh_line(mesh):
    if not isinstance(mesh,dict) or not mesh.get("profile"): return "Bed mesh: unknown"
    when="unknown"
    if mesh.get("saved_at"):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        when=datetime.fromtimestamp(mesh["saved_at"],ZoneInfo(TIMEZONE)).strftime("%-I:%M %p %a %b %-d")
    line=f"Bed mesh: {mesh['profile']} (config saved {when})"
    if mesh.get("stale"): line+=". Nozzle changed after this mesh; rerun full bed leveling"
    return line


def _assign_print_number(job_path):
    """Permanent print number from the shared /print-again registry; None on any failure."""
    if job_path is None:
        return None
    try:
        print_control = _external.load("print_control")
        ARCHIVE, assign_print_numbers = print_control.ARCHIVE, print_control.assign_print_numbers
        key = Path(job_path).resolve()
        if not key.is_relative_to(ARCHIVE.resolve()):
            return None  # scratch and test jobs are not prints
        return assign_print_numbers({key: time.time()}).get(str(key))
    except Exception as e:
        print(f"WARNING: print number not assigned: {e}")
        return None


def _maintenance_detail_line():
    try:
        maintenance = _external.load("maintenance")
        _call = _external.load("print_control")._call
        runs = maintenance.load().get("runs", [])
        hours = None
        try:
            data = _call("status")
            hours = data.get("print_hours") if isinstance(data, dict) else None
        except Exception: pass
        full = next((r for r in reversed(runs) if r.get("kind") == "full"), None)
        if full is None: return "- Maintenance: history unavailable"
        try:
            from datetime import datetime, timezone
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(full["at"])).days
        except Exception: age = "unknown"
        delta = "print hours unknown" if hours is None else f"{max(0,float(hours)-float(full.get('hours_total',0))):.1f} h"
        note = " (history totals unavailable)" if hours is None else ""
        return f"- Maintenance: last full clean {age} days / {delta} ago.{note}"
    except Exception: return "- Maintenance: history unavailable"


def job_details(out, *, hardware=None, selection=None, settings=None, record_path=None, z_notice=None, live=None,
                screen_z=None, mesh=None, printer=None, nozzle_mm=None, calibration=True):
    """Compact public summary; accounting, telemetry and paths stay in structured records."""
    def u(v): return v if v not in (None, "") else "unknown"
    args = (settings or {}).get("args") or {}
    bed = args.get("BED_TEMP") or args.get("BED"); hot = args.get("EXTRUDER_TEMP") or args.get("HOTEND")
    chan = (f"slot {selection.get('slot')}: {u(selection.get('color'))} {u(selection.get('material'))}"
            if isinstance(selection, dict) and selection.get("slot") is not None else None)
    grams = out.get("filament_g")
    # Historical records may include a timezone label; public details always assume New York.
    finish = re.sub(r"\s+(?:ET|EDT|EST)\b", "", str(u(out.get("finish_et"))))
    owner = (mesh or {}).get("offset_source") if isinstance(mesh, dict) else None
    source = owner or ("persisted" if isinstance(mesh, dict) else "screen")
    z = f"{screen_z:.3f} mm ({source})" if isinstance(screen_z, (int, float)) and not isinstance(screen_z, bool) else "unknown"
    return "\n".join([
        "**Job Details:**",
        *([f"- Print #{out['print_number']}"] if out.get("print_number") else []),
        f"- Model: {u(out.get('title'))}",
        f"- Estimated time: {u(out.get('print_time'))} printing + ~5 min warmup; finish {finish}",
        f"- Filament: {f'{grams} g' if grams is not None else 'unknown'}; {u(chan)}",
        f"- Printer: {u(printer)}",
        f"- Stock nozzle: {u(nozzle_mm)} mm; targets nozzle {u(hot)} C, bed {u(bed)} C",
        *([f"- Temperature material: {u((hardware or {}).get('spool', {}).get('material'))}; source: {u((hardware or {}).get('temperature_resolution', {}).get('source'))}"] if hardware else []),
        *([f"- Calibration: Z offset {z}; {mesh_line(mesh)}"] if calibration else []),
        _maintenance_detail_line(),
    ])


def get_author_settings(src_path):
    """Extract author brim and support configurations from a 3MF file."""
    import zipfile
    p = Path(src_path)
    res = {}
    if not p.is_file() or p.suffix.lower() != ".3mf":
        return res
    try:
        with zipfile.ZipFile(p) as z:
            for n in z.namelist():
                if n.endswith("project_settings.config"):
                    data = json.loads(z.read(n).decode("utf-8", errors="ignore"))
                    # Brim settings
                    bt = data.get("brim_type", "")
                    bw = data.get("brim_width")
                    if bt == "no_brim":
                        res["brim_width"] = "0"
                    elif bw is not None:
                        res["brim_width"] = str(bw)
                    # Support settings
                    es = data.get("enable_support")
                    if es is not None:
                        val = int(es) if str(es).isdigit() else (1 if es else 0)
                        res["support_material"] = str(val)
                    st = data.get("support_type")
                    if st and "tree" in str(st).lower():
                        res["support_material_style"] = "organic"
                    elif st:
                        # Bambu normal(auto)/normal(manual) is grid style; never leave the template's organic in place.
                        res["support_material_style"] = "grid"
                    break
    except (zipfile.BadZipFile, KeyError, json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        raise RuntimeError(f"unable to read author settings from {p}: {exc}") from exc
    return res

def decisions_digest(decisions):
    """Canonical geometry-to-filament choice identity, independent of dict order."""
    return hashlib.sha256(json.dumps(decisions, sort_keys=True, separators=(',', ':'),
                                     allow_nan=False).encode()).hexdigest()


def set_color_decisions(job, decisions):
    if decisions_digest(job.get('color_decisions')) != decisions_digest(decisions):
        if job.get('gcode'):
            job.setdefault('artifact_history', []).append({key: job[key] for key in
                ('gcode', 'gcode_verified', 'provenance', 'artifact_binding', 'sliced_at', 'slice_config')
                if key in job})
        for key in ('gcode', 'provenance', 'artifact_binding', 'sliced_at', 'slice_config'):
            job.pop(key, None)
        job['gcode_verified'] = False
    job['color_decisions'] = decisions
    if decisions['mode'] == 'single_color':
        job['filament_selection'] = decisions['selection']
    else:
        job.pop('filament_selection', None)


def require_current_artifact(job, gcode):
    """Offline fail-closed guard before telemetry, approval consumption or upload."""
    if not isinstance(job, dict):
        return
    binding = job.get('artifact_binding')
    if binding is None and not job.get('color_decisions'):
        return  # Preserve legacy jobs without source-color decisions.
    if not binding or job.get('gcode_verified') is not True:
        raise RuntimeError('stale color artifact: reslice current JOB before launch')
    stored_hash = binding.get('gcode_sha256')
    presented = Path(gcode).resolve()
    if isinstance(stored_hash, dict):
        plates = job.get('derived_plates') or []
        normalized_hashes = {str(slot): digest for slot, digest in stored_hash.items()}
        normalized_slots = set()
        for row in plates:
            slot = row.get('slot')
            def _valid_slot(s):
                return not isinstance(s, bool) and (isinstance(s, int) or isinstance(s, str) and s.isdigit())
            # Painted multicolor plates carry every IFS slot they use.
            painted_ok = (row.get('binding_key') and isinstance(slot, list) and slot
                          and all(_valid_slot(s) for s in slot))
            if not painted_ok and not _valid_slot(slot):
                raise RuntimeError('stale color artifact: malformed physical IFS slot binding')
            slot_key = str(row.get('binding_key') or slot)
            if slot_key in normalized_slots:
                raise RuntimeError('stale color artifact: duplicate physical IFS slot binding')
            normalized_slots.add(slot_key)
        if normalized_slots != set(normalized_hashes):
            raise RuntimeError('stale color artifact: derived plates do not match the binding')
        allowed = set()
        for row in plates:
            plate = Path(row['gcode']).resolve()
            allowed.add(plate)
            if file_sha256(plate) != normalized_hashes[str(row.get('binding_key') or row.get('slot'))]:
                raise RuntimeError('stale color artifact: source, decisions or G-code changed; reslice before launch')
        if presented not in allowed or Path(job.get('gcode', '')).resolve() not in allowed:
            raise RuntimeError('stale color artifact: source, decisions or G-code changed; reslice before launch')
        expected_hash = stored_hash
    else:
        expected_hash = file_sha256(gcode)
        if Path(job.get('gcode', '')).resolve() != presented:
            raise RuntimeError('stale color artifact: source, decisions or G-code changed; reslice before launch')
    expected = {'source_sha256': file_sha256(job['downloaded_file']),
                'decisions_sha256': decisions_digest(job.get('color_decisions')),
                'gcode_sha256': expected_hash}
    if binding != expected:
        raise RuntimeError('stale color artifact: source, decisions or G-code changed; reslice before launch')


def _job_adapter(job):
    """Adapter for the model a job is prepared for: its recorded override, else the configured model."""
    key = job.get("printer_model") if isinstance(job, dict) else None
    if not key or key == printers.current().key:
        return printers.current()
    return printers.get(key)


def intake_color_choices(job_path, state, mode=None, slots=None, interactive=False):
    """Normal intake gate: ask per part/palette and persist exact-source choices.

    Noninteractive callers receive a structured button-ready question packet.
    They resume the same JOB via slice; no JSON file authoring is required.
    """
    from .source_colors import inventory, prompts, canonical_prompts, validate_decisions
    path = Path(job_path)
    job = json.loads(path.read_text())
    manifest = inventory(job['downloaded_file'])
    if not manifest.get('source_sha256'):
        manifest['source_sha256'] = file_sha256(job['downloaded_file'])
    adapter = _job_adapter(job)
    if not adapter.multi_slot:
        if manifest['requires_choices']:
            raise MultiColorUnsupported(
                f"{adapter.label} has a single extruder; this source has several parts or colors that need a "
                "slot choice. Slice a single-color model or use a multi-slot printer")
        return True
    questions = canonical_prompts(manifest) if job.get('color_schema') == 'part_slots_v1' else prompts(manifest)
    available = []
    for slot, label in sorted(state.get('ifs_slots', {}).items()):
        selection = resolve_filament_selection(state, dict(label, slot=int(slot)))
        available.append({'label': f"Slot {slot}: {selection['color']} {selection['material']} (configured label)",
                          'selection': selection})
    job['color_manifest'], job['color_prompts'] = manifest, questions
    job['color_questions'] = [dict(q, choices=available) for q in questions]
    write_job(path.parent, job)
    if not manifest['requires_choices']:
        return True
    if mode is None and job.get('color_decisions'):
        validate_decisions(manifest, job['color_decisions'], state, resolve_filament_selection)
        return True
    packet = {'status': 'COLOR_CHOICES_REQUIRED', 'job_json': str(path),
              'source_sha256': manifest['source_sha256'],
              'question': 'Preserve colors per part/source filament, or explicitly print everything in one color?',
              'choices': ['Preserve colors per part/source filament', 'Print everything in one color'],
              'questions': job['color_questions'],
              'next_action': 'Present the source-bound questions once through the internal color workflow and persist selections to this same job. Never ask the user to supply a filesystem path, raw source index or command.',
              'resume': 'slice JOB.json --approved --color-mode per_part --color-slot SLOT (repeat for each question)'}
    if mode is None:
        print(json.dumps(packet, indent=2))
        if not interactive:
            return False
        answer = input('Color mode (per_part or single_color; blank stops): ').strip()
        if not answer:
            return False
        mode = answer
    if mode not in ('per_part', 'single_color'):
        raise ValueError('color mode must be per_part or single_color')
    rows = questions if mode == 'per_part' else [{'question': 'Choose one slot for ALL parts and source colors'}]
    if slots is None and interactive:
        print('\n'.join(choice['label'] for choice in available))
        slots = [int(input(row['question'] + ': ')) for row in rows]
    if not slots or len(slots) != len(rows):
        raise ValueError('supply one --color-slot for every displayed question (one for single_color)')
    selections = [resolve_filament_selection(state, dict(state.get('ifs_slots', {}).get(str(slot), {}), slot=slot)) for slot in slots]
    decisions = {'source_sha256': manifest['source_sha256'], 'mode': mode}
    if job.get('color_schema') == 'part_slots_v1':
        decisions['schema'] = 'part_slots_v1'
    if mode == 'single_color':
        decisions['selection'] = selections[0]
    else:
        decisions['choices'] = {row['key']: selection for row, selection in zip(rows, selections)}
    set_color_decisions(job, validate_decisions(manifest, decisions, state, resolve_filament_selection))
    write_job(path.parent, job)
    return True


class ImplicitPlateSplit(Exception):
    """A single implicit plate could not merge; the caller splits by slot."""

    def __init__(self, verdict):
        super().__init__(verdict.get('reason'))
        self.verdict = verdict


def single_color_groups(job, manifest):
    """Split an exact per-part decision into one group per physical slot.

    A group is one derived plate. Part keys appear once, and a group that
    mixes slots is not representable here. Decisions stay on the source job;
    this only plans the plates.
    """
    decisions = job.get('color_decisions') or {}
    if decisions.get('mode') != 'per_part' or decisions.get('schema') != 'part_slots_v1':
        raise RuntimeError('per-part plate split requires schema part_slots_v1')
    choices = decisions.get('choices') or {}
    groups = {}
    seen = set()
    for part in manifest.get('parts') or []:
        selection = choices.get('part:' + part['key'])
        if not selection:
            raise RuntimeError('per-part decision is missing a source part')
        slot = selection.get('slot')
        if part['key'] in seen:
            raise RuntimeError('source inventory repeats a part key')
        seen.add(part['key'])
        groups.setdefault(slot, []).append(part['key'])
    if not groups:
        raise RuntimeError('per-part decision has no printable parts')
    return [{'slot': slot, 'part_keys': keys} for slot, keys in sorted(groups.items(), key=lambda row: row[0])]


def _per_part_view(manifest, decisions):
    """Exact per-part decisions for the geometry exporters.

    A single_color decision is expanded in memory only; the persisted job
    decisions (and their digest) stay exactly what the user chose.
    """
    if decisions['mode'] == 'per_part':
        return decisions
    from .source_colors import canonical_prompts
    selection = decisions['selection']
    return {'schema': 'part_slots_v1', 'mode': 'per_part', 'source_sha256': manifest['source_sha256'],
            'choices': {row['key']: selection for row in canonical_prompts(manifest)}}


def _part_slots(view, manifest):
    if manifest.get('painted'):
        # Painted regions pick slots per source filament, not per part. Every
        # chosen filament slot is in play; part keys map to their base filament.
        choices = view['choices']
        out = {part['key']: choices[f"filament:{part['source_extruder']}"]['slot']
               for part in manifest['parts'] if f"filament:{part['source_extruder']}" in choices}
        out.update({key: row['slot'] for key, row in choices.items() if key.startswith('filament:')})
        return out
    return {part['key']: view['choices']['part:' + part['key']]['slot'] for part in manifest['parts']}


def _job_fit(job, brim_override):
    """Operator-chosen scale/orientation for per-part plates.

    Returns (fit, supports, brim). fit is None at authored size. The same
    verified option the whole-model path honors is applied to every slot
    plate: rotation, uniform scale, and the supports/brim it was offered with.
    """
    scale = job.get('scale')
    orientation = job.get('orientation') or {}
    rotation = orientation.get('rotation')
    if not rotation and (scale is None or float(scale) >= 1.0):
        if brim_override is None and job.get('fit_brim_policy'):
            brim_override = job['fit_brim_policy'].get('width_mm', 0)
        return None, None, brim_override
    fit = {'scale': float(scale) if scale is not None else 1.0, 'rotation': rotation}
    supports = bool(orientation.get('supports')) if rotation else None
    if brim_override is None and job.get('fit_brim_policy'):
        brim_override = job['fit_brim_policy'].get('width_mm', 0)
    if rotation and brim_override is None and orientation.get('brim'):
        brim_override = 5
    return fit, supports, brim_override


def per_part_fit_options(source, decisions, options, bed=BED_MM):
    """Keep only the fit options every per-part slot plate can honor.

    Each slot group is exported with the option applied, exactly as slicing
    will. An option that crops, lifts a part off the bed, or otherwise fails
    for any slot is dropped, so intake never offers a choice it cannot slice.
    """
    import tempfile
    from .color_geometry import export_single_color_plate
    from .source_colors import inventory
    manifest = inventory(source)
    view = _per_part_view(manifest, decisions)
    plates = plate_manifest(source).get('plates', []) if Path(source).suffix.lower() == '.3mf' else []
    object_plate = {str(o): plate['id'] for plate in plates for o in plate.get('object_ids', [])} if plates else {}
    groups = {}
    for part in manifest['parts']:
        p_id = object_plate.get(str(part['object_id']), 1) if plates else 1
        slot = view['choices']['part:' + part['key']]['slot']
        groups.setdefault((p_id, slot) if plates else slot, []).append(part['key'])

    def honored(fit):
        with tempfile.TemporaryDirectory(prefix='per-part-fit-') as tmp:
            for group_key, keys in groups.items():
                tag = f"p{group_key[0]}_s{group_key[1]}" if isinstance(group_key, tuple) else f"slot{group_key}"
                try:
                    export_single_color_plate(source, Path(tmp) / f'{tag}.3mf', view, keys, bed=bed, fit=fit)
                except RuntimeError:
                    return False
        return True

    scales = [s for s in options.get('scales') or [] if honored({'scale': s})]
    rotations = [r for r in options.get('rotations') or []
                 if r.get('rotation') and honored({'scale': r.get('scale'), 'rotation': r['rotation']})]
    details = []
    try:
        with zipfile.ZipFile(source) as archive:
            for part in manifest.get('parts', []):
                box = __import__('print_buddy.core.color_geometry', fromlist=['_world_bounds'])._world_bounds(
                    archive, {str(part['object_id'])})[0]
                details.append({'name': part.get('name') or f"Object {part['object_id']}",
                                'extent_mm': [round(box[i][1] - box[i][0], 1) for i in range(3)]})
    except (OSError, zipfile.BadZipFile, RuntimeError, KeyError):
        details = []
    return {**options, 'scales': scales, 'rotations': rotations, 'parts': details}


def _gcode_object_count(text):
    return len(set(re.findall(r'^; printing object (.+?)(?: id:\d+)?(?: copy \d+)?\s*$', text, re.M)))


def slice_multi_plate(job, job_path, src, manifest, decisions, st, brim_override):
    """Slice every object of a multi-plate 3MF. Disk only, never the printer.

    job['plate_mode'] 'merge' (default) packs all objects on one bed and
    slices once. 'separate' (or any split reason) slices one G-code per
    source plate and slot. Every split carries a reason in job['plate_merge'].
    """
    from . import plate_merge
    if not decisions:
        raise RuntimeError('part/color choices required; run color-plan and color-select before slicing')
    view = _per_part_view(manifest, decisions)
    slots = _part_slots(view, manifest)
    implicit = False
    try:
        plates = plate_manifest(src)['plates']
    except RuntimeError:
        # A single-plate 3MF may carry no plate assignments; it is one plate
        # holding every object. A multi-plate source must name its plates.
        if job.get('audit', {}).get('plates', 0) and int(job['audit']['plates']) > 1:
            raise
        implicit = True
        plates = [{'id': 1, 'name': 'Plate 1', 'status': 'planned',
                   'object_ids': sorted({int(p['object_id']) for p in manifest['parts']})}]
    assigned = {str(o) for plate in plates for o in plate['object_ids']}
    missing = sorted({part['object_id'] for part in manifest['parts']} - assigned, key=str)
    if missing:
        raise RuntimeError(f'multi-plate 3MF objects {",".join(missing)} belong to no plate; no slicing attempted')
    work = Path(job_path).parent
    mode = job.get('plate_mode', 'merge')
    if mode not in ('merge', 'separate'):
        raise RuntimeError("plate_mode must be 'merge' or 'separate'")
    verdict = {'mode': 'split', 'reason': 'Print parts separately was chosen', 'plates_in': len(plates),
               'objects_in': sorted({p['object_id'] for p in manifest['parts']}, key=str)}
    fit, _, _ = _job_fit(job, brim_override)
    if fit is not None:
        # Merge shifts are planned at authored size. A resized or rotated job
        # slices one fitted plate per slot; the whole model was verified to fit.
        verdict['reason'] = 'Resized per-part job: one fitted plate per slot'
    elif mode == 'merge':
        verdict = plate_merge.plan(src, manifest, plates, slots, st, job=job)
        if verdict['mode'] == 'merge':
            try:
                result = _slice_merged(job, job_path, src, manifest, decisions, view, verdict, st, brim_override, work)
            except plate_merge.SplitNeeded as exc:
                verdict = {**verdict, 'mode': 'split', 'reason': str(exc)}
            else:
                return result
    job['plate_merge'] = {k: v for k, v in verdict.items() if k not in ('shifts', 'placed_bounds_mm')}
    if manifest.get('painted') and len(set(slots.values())) > 1:
        # Painted colors live inside one mesh; a per-slot split would print the
        # same body twice. Split by authored plate instead, each one multicolor.
        print('SPLIT painted 3MF into per-plate multicolor G-code: ' + str(verdict['reason']))
        return _slice_painted_plates(job, job_path, src, manifest, decisions, view, plates, st, brim_override, work)
    if implicit or len(plates) == 1:
        # One plate that cannot merge splits by slot, the legacy single-color path.
        raise ImplicitPlateSplit(job['plate_merge'])
    print('SPLIT multi-plate 3MF into per-plate G-code: ' + str(verdict['reason']))
    return _slice_per_plate(job, job_path, src, manifest, decisions, view, plates, st, brim_override, work)


PAINTED_SCALE_STEPS = (1.0, 0.95, 0.9, 0.87, 0.85, 0.8, 0.75, 0.7, 0.65, 0.6, 0.55, 0.5)


def painted_plate_scale(src, view, object_ids, ceiling=1.0):
    """Largest ladder scale <= ceiling at which this plate plus its prime tower
    fits the bed. Pure geometry export, no slicer, no printer."""
    import tempfile
    from .color_geometry import export_color_geometry
    with tempfile.TemporaryDirectory(prefix='painted-fit-') as tmp:
        for scale in PAINTED_SCALE_STEPS:
            if scale > ceiling + 1e-9:
                continue
            try:
                _, geometry = export_color_geometry(src, Path(tmp) / 'g.3mf', view, object_ids=object_ids, scale=scale)
            except RuntimeError:
                continue
            if all(0 <= ax[0] and ax[1] <= BED_LIMIT_MM for row in geometry['parts'] for ax in row['bounds_mm'][:2]):
                return scale
    return None


BED_LIMIT_MM = BED_MM


def painted_fit(src, view, plates, ceiling=1.0):
    """One shared scale for every authored plate of a painted source."""
    scale, blocking = ceiling, None
    for plate in plates:
        ids = {str(o) for o in plate['object_ids']}
        fitted = painted_plate_scale(src, view, ids, scale)
        if fitted is None:
            return {'scale': None, 'blocking_plate': plate['id'], 'plates': len(plates)}
        if fitted < scale:
            scale, blocking = fitted, plate['id']
    return {'scale': scale, 'blocking_plate': blocking, 'plates': len(plates)}


def painted_fit_for_job(src, decisions):
    """Intake helper: shared painted-plate scale for an exact-source decision."""
    from .source_colors import inventory
    manifest = inventory(src)
    view = _per_part_view(manifest, decisions)
    try:
        plates = plate_manifest(src)['plates']
    except RuntimeError:
        plates = [{'id': 1, 'object_ids': sorted({str(p['object_id']) for p in manifest['parts']})}]
    plates = [p for p in plates if any(str(o) in {str(x['object_id']) for x in manifest['parts']} for o in p['object_ids'])]
    return painted_fit(src, view, plates)


def _slice_painted_plates(job, job_path, src, manifest, decisions, view, plates, st, brim_override, work):
    """One multicolor G-code per authored plate of a triangle-painted 3MF.

    Paint is remapped to the chosen slots inside every mesh, so no plate ever
    prints a body in the wrong color. Every plate uses one scale so parts
    still fit together: job['scale'] caps it (default full size), otherwise it
    is the largest ladder scale at which every plate and its prime tower fit.
    """
    import uuid
    from .orca_adapter import slice_model
    source_sha = file_sha256(src); decision_sha = decisions_digest(decisions)
    fit = painted_fit(src, view, plates, float(job.get('scale') or 1.0))
    scale = fit['scale']
    if scale is None:
        raise RuntimeError(f"painted plate {fit['blocking_plate']} does not fit the bed with a prime tower at 50% or larger; no slicing attempted")
    artifacts = []
    for plate in plates:
        ids = {str(o) for o in plate['object_ids']}
        keys = [p['key'] for p in manifest['parts'] if str(p['object_id']) in ids]
        if not keys:
            continue
        slice_dir = work / 'sliced' / 'orca' / uuid.uuid4().hex
        out, provenance, configs = slice_model(src, slice_dir, st, brim_override, color_decisions=view,
                                               object_ids=ids, scale=None if scale >= 1.0 else scale)
        geometry = provenance.get('geometry') or {}
        if sorted(r['part_key'] for r in geometry.get('parts', [])) != sorted(keys):
            raise RuntimeError(f"painted plate {plate['id']} dropped or duplicated a source part")
        check = verify_gcode_offline(out, st)
        tools = provenance.get('tool_mapping') or {}
        artifacts.append({'plate_id': plate['id'], 'slot': sorted(set(tools.values())),
                          'binding_key': f"{plate['id']}:painted", 'part_keys': keys,
                          'scale': scale, 'gcode': str(out), 'offline_check': check,
                          'estimate_s': gcode_estimate_s(Path(out).read_text(errors='ignore')),
                          'provenance': provenance, 'slice_config': [str(p) for p in configs]})
    if not artifacts:
        raise RuntimeError('painted source produced no plates')
    latest = json.loads(Path(job_path).read_text())
    if file_sha256(src) != source_sha or decisions_digest(latest.get('color_decisions')) != decision_sha:
        raise RuntimeError('source or decisions changed during slicing; artifact not made ready')
    job['derived_plates'] = artifacts
    job['plate_merge'] = {'mode': 'split', 'painted': True, 'plates_in': len(plates), 'plates_out': len(artifacts),
                          'reason': 'painted multicolor plates sliced separately, one multicolor G-code each',
                          'scales': {str(a['plate_id']): a['scale'] for a in artifacts}, 'fits_bed': True,
                          'bed_mm': int(st['bed_mm'][0])}
    job['artifact_binding'] = {'source_sha256': source_sha, 'decisions_sha256': decision_sha,
                               'gcode_sha256': {a['binding_key']: file_sha256(a['gcode']) for a in artifacts}}
    job.update({'gcode': artifacts[0]['gcode'], 'gcode_verified': True,
                'sliced_at': time.strftime('%Y-%m-%dT%H:%M:%S%z')})
    write_job(work, job)
    for a in artifacts:
        print(f"SLICED painted plate {a['plate_id']} at {round(a['scale'] * 100)}% (not uploaded): {a['gcode']}")
    return [a['gcode'] for a in artifacts]


def _slice_merged(job, job_path, src, manifest, decisions, view, verdict, st, brim_override, work):
    import uuid
    from . import plate_merge
    from .orca_adapter import slice_model
    source_sha = file_sha256(src); decision_sha = decisions_digest(decisions)
    slice_dir = work / 'sliced' / 'orca' / uuid.uuid4().hex
    slots = sorted(set(_part_slots(view, manifest).values()))
    if len(slots) == 1 and not manifest.get('painted'):
        from .color_geometry import export_single_color_plate
        derived = work / 'derived'; derived.mkdir(exist_ok=True)
        plate = derived / 'merged.3mf'
        report = export_single_color_plate(src, plate, view, [p['key'] for p in manifest['parts']], bed=int(st['bed_mm'][0]),
                                           slot_colors=st.get('ifs_slots') or {}, item_shifts=verdict['shifts'])
        out, provenance, configs = slice_model(plate, slice_dir, st, brim_override, preserve_layout=True, filament_slot=slots[0])
        material = st.get('ifs_slots', {}).get(str(slots[0]), {}).get('material')
        if not material:
            raise RuntimeError(f'merged plate physical IFS slot {slots[0]} has no verified material')
        finalize_multicolor(out, None, st, {'0': slots[0]})
        placed_ids = sorted(report['object_ids'], key=str)
        extra = {'plate': str(plate), 'plate_report': report}
    else:
        # Painted single-color choices still go through the paint-preserving
        # exporter, so pass the per-part view (it is exactly the user's choice).
        out, provenance, configs = slice_model(src, slice_dir, st, brim_override,
                                               color_decisions=view if manifest.get('painted') else decisions,
                                               item_shifts=verdict['shifts'])
        geometry = provenance.get('geometry') or {}
        placed_keys = sorted(row['part_key'] for row in geometry.get('parts', []))
        if placed_keys != sorted(p['key'] for p in manifest['parts']):
            raise RuntimeError('merged geometry dropped or duplicated a source part')
        placed_ids = sorted({p['object_id'] for p in manifest['parts'] if p['key'] in placed_keys}, key=str)
        extra = {'geometry_report': geometry}
    text = Path(out).read_text(errors='ignore')
    estimate = gcode_estimate_s(text)
    if estimate is not None and estimate > plate_merge.MAX_MERGED_S:
        raise plate_merge.SplitNeeded(f'merged estimate {human_dur(estimate)} exceeds the {plate_merge.MAX_MERGED_S / 3600:g} h limit')
    if placed_ids != verdict['objects_in']:
        raise RuntimeError('merged output does not account for every source object')
    labelled = _gcode_object_count(text)
    if labelled and labelled != len(manifest['parts']):
        raise RuntimeError(f'merged G-code prints {labelled} objects but the source has {len(manifest["parts"])}')
    check = verify_gcode_offline(out, st)
    latest = json.loads(Path(job_path).read_text())
    if file_sha256(src) != source_sha or decisions_digest(latest.get('color_decisions')) != decision_sha:
        raise RuntimeError('source or decisions changed during slicing; artifact not made ready')
    summary = {'mode': 'merge', 'reason': None, 'plates_in': verdict['plates_in'], 'objects_in': len(verdict['objects_in']),
               'objects_out': len(placed_ids), 'parts_in': len(manifest['parts']), 'bed_mm': verdict['bed_mm'],
               'spacing_mm': verdict['spacing_mm'], 'fits_bed': True, 'slots': slots,
               'placed_bounds_mm': verdict['placed_bounds_mm'], 'estimate_s': estimate,
               'estimate': human_dur(estimate) if estimate else None, 'gcode_objects_labelled': labelled or None}
    job.pop('derived_plates', None); job.pop('refused_plates', None); job.pop('multipart_plan', None)
    job['plate_merge'] = summary
    job['artifact_binding'] = {'source_sha256': source_sha, 'decisions_sha256': decision_sha, 'gcode_sha256': file_sha256(out)}
    job.update({'slice_config': [str(p) for p in configs], 'gcode': str(out), 'provenance': provenance,
                'gcode_verified': True, 'offline_check': check, 'merged': extra,
                'sliced_at': time.strftime('%Y-%m-%dT%H:%M:%S%z')})
    write_job(work, job)
    print(f"SLICED merged {summary['objects_in']} objects from {summary['plates_in']} plates onto one bed (not uploaded): {out}")
    return out


def _slice_per_plate(job, job_path, src, manifest, decisions, view, plates, st, brim_override, work):
    import uuid
    fit, supports, brim_override = _job_fit(job, brim_override)
    from .color_geometry import export_single_color_plate
    from .orca_adapter import slice_model
    object_plate = {str(o): plate['id'] for plate in plates for o in plate['object_ids']}
    if fit is not None and len(plates) <= 1:
        # The chosen fit was verified for the whole model on one bed, so the
        # source plate boundaries no longer split it: one plate per slot.
        object_plate = {key: 1 for key in object_plate}
    slots = _part_slots(view, manifest)
    groups = {}
    for part in manifest['parts']:
        groups.setdefault((object_plate[str(part['object_id'])], slots[part['key']]), []).append(part['key'])
    derived = work / 'derived'; derived.mkdir(exist_ok=True)
    source_sha = file_sha256(src); decision_sha = decisions_digest(decisions)
    single_plate = len({plate for plate, _ in groups}) == 1
    artifacts = []
    refused = []
    plate_scales = job.get('plate_scales') or {}
    for (plate_id, slot), keys in sorted(groups.items()):
        current_fit = fit
        if str(plate_id) in plate_scales:
            psc = float(plate_scales[str(plate_id)])
            current_fit = {'scale': psc} if psc < 1.0 else None
        plate = derived / f'plate{plate_id}_slot{slot}.3mf'
        try:
            if not st.get('ifs_slots', {}).get(str(slot), {}).get('material'):
                raise RuntimeError(f'derived plate physical IFS slot {slot} has no verified material')
            report = export_single_color_plate(src, plate, view, keys, bed=int(st['bed_mm'][0]), slot_colors=st.get('ifs_slots') or {}, **({'fit': current_fit} if current_fit is not None else {}))
            slice_dir = work / 'sliced' / 'orca' / uuid.uuid4().hex
            out, provenance, configs = slice_model(plate, slice_dir, st, brim_override, preserve_layout=True, filament_slot=slot, **({'supports': supports} if supports is not None else {}))
            finalize_multicolor(out, None, st, {'0': slot})
            check = verify_gcode_offline(out, st)
        except RuntimeError as exc:
            # One plate failing a safety guard (e.g. an off-bed move) must not discard the
            # other plates that sliced and verified cleanly. The failed plate is recorded,
            # never published, and never launchable; it needs its own fix and reslice.
            refused.append({'slot': slot, 'plate_id': plate_id, 'binding_key': f'{plate_id}:{slot}',
                            'part_keys': keys, 'reason': ' '.join(str(exc).split())[:500]})
            print(f'REFUSED plate {plate_id} slot {slot}: {" ".join(str(exc).split())[:300]}')
            continue
        artifacts.append({'slot': slot, 'plate_id': plate_id, 'binding_key': f'{plate_id}:{slot}', 'part_keys': keys,
                          'plate': str(plate), 'gcode': str(out), 'offline_check': check, 'plate_report': report,
                          'provenance': provenance, 'slice_config': [str(path) for path in configs]})
    if not artifacts:
        raise RuntimeError('every derived plate was refused: ' + '; '.join(
            f"plate {r['plate_id']} slot {r['slot']}: {r['reason']}" for r in refused))
    covered = sorted(k for row in artifacts + refused for k in row['part_keys'])
    if covered != sorted(p['key'] for p in manifest['parts']):
        raise RuntimeError('per-plate output does not account for every source part')
    latest = json.loads(Path(job_path).read_text())
    if file_sha256(src) != source_sha or decisions_digest(latest.get('color_decisions')) != decision_sha:
        raise RuntimeError('source or decisions changed during slicing; artifacts not made ready')
    job['derived_plates'] = artifacts
    if refused:
        job['refused_plates'] = refused
    else:
        job.pop('refused_plates', None)
    job['artifact_binding'] = {'source_sha256': source_sha, 'decisions_sha256': decision_sha,
                               'gcode_sha256': {row['binding_key']: file_sha256(row['gcode']) for row in artifacts}}
    job.update({'gcode': artifacts[0]['gcode'], 'gcode_verified': True,
                'sliced_at': time.strftime('%Y-%m-%dT%H:%M:%S%z')})
    write_job(work, job)
    print('SLICED separate plates (not uploaded): ' + ', '.join(row['gcode'] for row in artifacts))
    return [row['gcode'] for row in artifacts]


def _slice_full_size_split(job, job_path, src, manifest, decisions, st):
    """Slice the exact intake-approved authored-size part groups, disk only."""
    import uuid
    from .color_geometry import export_single_color_plate
    from .orca_adapter import slice_model
    plan = job.get('full_size_split_plan') or {}
    plates = plan.get('plates') or []
    expected = {part['key'] for part in manifest.get('parts') or []}
    flattened = [key for group in plates for key in group]
    if len(flattened) < 2 or len(flattened) != len(set(flattened)) or set(flattened) != expected:
        raise RuntimeError('full-size split plan does not cover every source part exactly once')
    view = _per_part_view(manifest, decisions)
    slots = sorted(set(_part_slots(view, manifest).values()))
    if len(slots) != 1:
        raise RuntimeError('full-size split plan requires one physical filament slot')
    slot = slots[0]
    if not (st.get('ifs_slots') or {}).get(str(slot), {}).get('material'):
        raise RuntimeError(f'full-size split physical IFS slot {slot} has no verified material')
    work = Path(job_path).parent
    derived = work / 'derived'
    derived.mkdir(exist_ok=True)
    source_sha = file_sha256(src)
    decision_sha = decisions_digest(decisions)
    artifacts = []
    for index, keys in enumerate(plates, 1):
        plate = derived / f'full_size_split_{index}.3mf'
        report = export_single_color_plate(src, plate, view, keys, bed=int(st['bed_mm'][0]),
                                           slot_colors=st.get('ifs_slots') or {})
        slice_dir = work / 'sliced' / 'orca' / uuid.uuid4().hex
        out, provenance, configs = slice_model(plate, slice_dir, st, preserve_layout=True, filament_slot=slot)
        finalize_multicolor(out, None, st, {'0': slot})
        check = verify_gcode_offline(out, st)
        artifacts.append({'slot': slot, 'plate_id': index, 'binding_key': f'{index}:{slot}',
                          'part_keys': keys, 'plate': str(plate), 'gcode': str(out),
                          'offline_check': check, 'plate_report': report, 'provenance': provenance,
                          'slice_config': [str(path) for path in configs]})
    latest = json.loads(Path(job_path).read_text())
    if file_sha256(src) != source_sha or decisions_digest(latest.get('color_decisions')) != decision_sha:
        raise RuntimeError('source or decisions changed during full-size split slicing')
    job['derived_plates'] = artifacts
    job['plate_merge'] = {'mode': 'split', 'reason': f"operator chose full-size split into {len(artifacts)} plates",
                          'plates_in': len(artifacts), 'parts_in': len(expected), 'fits_bed': True,
                          'bed_mm': int(st['bed_mm'][0]), 'full_size': True}
    job['artifact_binding'] = {'source_sha256': source_sha, 'decisions_sha256': decision_sha,
                               'gcode_sha256': {row['binding_key']: file_sha256(row['gcode']) for row in artifacts}}
    job.update({'gcode': artifacts[0]['gcode'], 'gcode_verified': True,
                'sliced_at': time.strftime('%Y-%m-%dT%H:%M:%S%z')})
    write_job(work, job)
    return [row['gcode'] for row in artifacts]


def slice_job(job_path, brim_override=None, copies=None, scale=None):
    # Disk only. Launch still runs printer_state.check inside print_now and
    # verify_gcode. A file that never leaves disk does not need Moonraker or Spoolman.
    job = json.loads(Path(job_path).read_text()); st = printer_state.load()
    adapter = _job_adapter(job)
    generic = not adapter.multi_slot
    job["printer_model"] = adapter.key  # print-now refuses a job sliced for another model
    _timing(job_path, 'slice_start')
    if copies is None:
        copies = job.get('copies')
    if copies:
        job['copies'] = int(copies)
    if not generic and job.get("filament_selection") is not None and (job.get('color_decisions') or {}).get('mode') != 'per_part':
        selection = resolve_filament_selection(st, job["filament_selection"])
        _validate_slot_selection(st, selection)
        job["filament_selection"] = selection
    src=Path(job["downloaded_file"]); ad=job["audit"]; multi_plate=False
    if src.suffix.lower() not in (".3mf",".stl"): raise RuntimeError("only STL/3MF can be sliced; selected source is not geometry")
    # A Bambu/Orca multi-plate project is not one PrusaSlicer bed. Its aggregate
    # dimensions and center are not meaningful for an individual printable plate.
    if src.suffix.lower() == ".3mf":
        with zipfile.ZipFile(src) as archive:
            actual_plates = {int(match.group(1)) for name in archive.namelist()
                             if (match := re.fullmatch(r"Metadata/plate_(\d+)\.(?:png|gcode)", name))}
            if "Metadata/model_settings.config" in archive.namelist():
                try:
                    settings = ET.fromstring(archive.read("Metadata/model_settings.config"))
                    for plate in settings.iter("plate"):
                        for metadata in plate.iter("metadata"):
                            if metadata.get("key") == "plater_id":
                                plate_id = metadata.get("value")
                                if plate_id is None:
                                    raise RuntimeError("3MF plate assignment has no plater_id value")
                                actual_plates.add(int(plate_id))
                except (ET.ParseError, ValueError, TypeError) as exc:
                    raise RuntimeError(f"unable to inspect 3MF plate assignments: {exc}") from exc
        multi_plate = len(actual_plates) > 1 or int(ad.get("plates", 0)) > 1
    from .source_colors import inventory, prompts, require_single_color, validate_decisions
    try:
        manifest = inventory(src)
    except RuntimeError as exc:
        if multi_plate:
            raise RuntimeError(f'multi-plate 3MF cannot be merged or split: {exc}; no slicing attempted') from exc
        raise
    if generic and (multi_plate or manifest['requires_choices'] or job.get('full_size_split_plan')):
        raise MultiColorUnsupported(
            f"{adapter.label} has a single extruder; multi-plate, multi-part or multi-color sources need a "
            "multi-slot printer. Slice one single-color plate instead")
    # STL has no source palette; bind decisions to the exact file bytes, as intake does.
    if not manifest.get('source_sha256'):
        manifest['source_sha256'] = file_sha256(src)
    job['color_manifest'], job['color_prompts'] = manifest, prompts(manifest)
    write_job(Path(job_path).parent, job)
    _timing(job_path, 'inventory_done')
    decisions = None if generic else job.get('color_decisions')
    if decisions is not None:
        decisions = validate_decisions(manifest, decisions, st, resolve_filament_selection)
    if manifest['requires_choices'] and not decisions:
        raise RuntimeError('part/color choices required; run color-plan and color-select before slicing')
    if copies and int(copies) > 1 and not generic and not copies_supported(job, multi_plate):
        # Split, multi-plate and per-part paths never clone copies (#37); keep JOB.json truthful.
        print(f'NOTE: {int(copies)} copies requested, but split and per-part jobs slice one copy per plate')
        copies = 1
        job['copies'] = 1
    if job.get('full_size_split_plan'):
        result = _slice_full_size_split(job, job_path, src, manifest, decisions, st)
        _timing(job_path, 'slice_done', path='full_size_split')
        return result
    if multi_plate:
        # Several source plates: merge onto one bed by default, otherwise one
        # derived plate per source plate. Never an unexplained refusal.
        result = slice_multi_plate(job, job_path, src, manifest, decisions, st, brim_override)
        _timing(job_path, 'slice_done', path='multi_plate')
        return result
    if decisions and decisions['mode'] == 'single_color':
        selection = decisions['selection']
        _validate_slot_selection(st, selection)
        job['filament_selection'] = selection
    elif decisions and decisions['mode'] == 'per_part':
        if src.suffix.lower() == '.3mf':
            # Single-plate per-part decisions use the same merge planner as
            # multi-plate sources: one multicolor G-code unless a stated reason splits.
            try:
                return slice_multi_plate(job, job_path, src, manifest, decisions, st, brim_override)
            except ImplicitPlateSplit as exc:
                print('SPLIT single-plate 3MF into per-slot G-code: ' + str(exc.verdict.get('reason')))
                job['plate_merge'] = exc.verdict
        # Non-mergeable and non-3MF per-part sources use the single-slot plate split.
        groups = single_color_groups(job, manifest)
        if not groups:
            raise RuntimeError('per-part decision did not resolve any single-color plate')
        # Every slot group is sliced. The loaded spool is a launch fact, not a
        # reason to drop a plate that already names its own slot.
        from .color_geometry import export_single_color_plate
        from .orca_adapter import slice_model
        import uuid
        work = Path(job_path).parent
        derived = work / 'derived'
        derived.mkdir(exist_ok=True)
        source_sha = file_sha256(src)
        decision_sha = decisions_digest(decisions)
        fit, supports, brim_override = _job_fit(job, brim_override)
        artifacts = []
        for group in groups:
            plate = derived / f"slot{group['slot']}.3mf"
            report = export_single_color_plate(src, plate, decisions, group['part_keys'], bed=int(st['bed_mm'][0]), slot_colors=st.get('ifs_slots') or {}, **({'fit': fit} if fit is not None else {}))
            slice_dir = work / 'sliced' / 'orca' / uuid.uuid4().hex
            out, provenance, configs = slice_model(plate, slice_dir, st, brim_override, preserve_layout=True, filament_slot=group['slot'], **({'supports': supports} if supports is not None else {}))
            # Keep the plate's physical IFS slot explicit. Orca may retain
            # project-wide unused filament_type entries although all geometry
            # is remapped to logical T0 on this single-color derivative.
            material = st.get('ifs_slots', {}).get(str(group['slot']), {}).get('material')
            if not material:
                raise RuntimeError(f"derived plate physical IFS slot {group['slot']} has no verified material")
            finalize_multicolor(out, None, st, {'0': group['slot']})
            # Verify the exact final bytes (after map, color and MD5 rewrite).
            check = verify_gcode_offline(out, st)
            artifacts.append({'slot': group['slot'], 'part_keys': group['part_keys'], 'plate': str(plate),
                              'gcode': str(out), 'offline_check': check, 'plate_report': report,
                              'provenance': provenance, 'slice_config': [str(path) for path in configs]})
        latest = json.loads(Path(job_path).read_text())
        if file_sha256(src) != source_sha or decisions_digest(latest.get('color_decisions')) != decision_sha:
            raise RuntimeError('source or decisions changed during slicing; artifacts not made ready')
        job['derived_plates'] = artifacts
        job.pop('refused_plates', None)
        job['artifact_binding'] = {'source_sha256': source_sha, 'decisions_sha256': decision_sha,
                                   'gcode_sha256': {row['slot']: file_sha256(row['gcode']) for row in artifacts}}
        job.update({'gcode': artifacts[0]['gcode'], 'gcode_verified': True,
                    'sliced_at': time.strftime('%Y-%m-%dT%H:%M:%S%z')})
        _timing(job_path, 'slice_done')
        write_job(work, job)
        print('SLICED separate single-color plates (not uploaded): ' + ', '.join(row['gcode'] for row in artifacts))
        return [row['gcode'] for row in artifacts]
    scale = scale if scale is not None else job.get("scale")
    explicit_scale = scale is not None
    if scale is not None:
        scale = float(scale)
        if not scale > 0:
            raise RuntimeError('scale must be greater than 0')
    orientation = job.get("orientation") or {}
    rotation = orientation.get("rotation")
    if not all(ad.get(k) for k in ("size_x", "size_y", "size_z")):
        # Jobs audited by the retired Prusa probe can lack extents; re-measure.
        for line in model_info_output(src).splitlines():
            k, _, v = line.partition(" = ")
            if k in ("size_x", "size_y", "size_z"): ad[k] = v
    sx,sy,sz=(float(ad[k]) for k in ("size_x","size_y","size_z"))
    if rotation and orientation.get("dims_full_mm"):
        # Operator chose a rotated orientation: check the rotated envelope, not the authored one.
        sx, sy, sz = (float(v) for v in orientation["dims_full_mm"])
    auto_scale = None
    if (not explicit_scale and src.suffix.lower() == '.stl' and max(sx, sy, sz) < 10.0):
        inches_scale = 25.4
        inch_dims = tuple(round(value * inches_scale, 6) for value in (sx, sy, sz))
        if (inch_dims[0] > st["bed_mm"][0] or inch_dims[1] > st["bed_mm"][1]
                or inch_dims[2] > st["max_print_height_mm"]):
            measured = f'{sx:g}x{sy:g}x{sz:g}'
            final = f'{inch_dims[0]:g}x{inch_dims[1]:g}x{inch_dims[2]:g}'
            raise RuntimeError(f'model measures {measured} mm and treating it as inches would be {final} mm, which exceeds printer volume')
        scale = inches_scale
        auto_scale = {'factor': inches_scale, 'reason': 'inch units',
                      'original_mm': [sx, sy, sz], 'final_mm': list(inch_dims)}
    eff_sx, eff_sy, eff_sz = sx * (float(scale) if scale else 1.0), sy * (float(scale) if scale else 1.0), sz * (float(scale) if scale else 1.0)
    if eff_sx>st["bed_mm"][0] or eff_sy>st["bed_mm"][1] or eff_sz>st["max_print_height_mm"]: raise RuntimeError(f"model {sx}x{sy}x{sz} mm exceeds printer volume")
    if brim_override is None and job.get('fit_brim_policy'):
        brim_override = job['fit_brim_policy'].get('width_mm', 0)
    if rotation and brim_override is None and orientation.get("brim"):
        brim_override = 5
    # Canonical slicing is Orca only.
    from .orca_adapter import slice_model
    work = Path(job_path).parent
    import uuid
    # Preserve historical artifact bytes on every reslice.
    slice_dir = work / "sliced" / "orca" / uuid.uuid4().hex
    source_sha = file_sha256(src)
    decision_sha = decisions_digest(decisions)
    selected_slot = None if generic else (job.get("filament_selection") or {}).get("slot")
    scale_kw = {'scale': scale} if scale is not None else {}
    if adapter.key != printers.current().key:
        scale_kw['adapter'] = adapter
    if rotation:
        scale_kw.update(rotation=rotation, supports=bool(orientation.get("supports")))
    _timing(job_path, 'orca_begin')
    if selected_slot is not None:
        # Single-color jobs are bound to their named IFS slot exactly like
        # derived per-slot plates, so launch uses the mapped native route.
        out, provenance, configs = slice_model(src, slice_dir, st, brim_override,
                                              color_decisions=decisions, filament_slot=selected_slot,
                                              copies=copies, **scale_kw)
        finalize_multicolor(out, None, st, {"0": selected_slot})
    else:
        out, provenance, configs = slice_model(src, slice_dir, st, brim_override,
                                              color_decisions=decisions, copies=copies, **scale_kw)
    _timing(job_path, 'orca_finalize_done')
    if adapter.key == printers.current().key:
        verify_gcode(out)
    latest = json.loads(Path(job_path).read_text())
    _timing(job_path, 'verify_done')
    if file_sha256(src) != source_sha or decisions_digest(latest.get("color_decisions")) != decision_sha:
        raise RuntimeError("source or decisions changed during slicing; artifact not made ready")
    job["artifact_binding"] = {"source_sha256": source_sha, "decisions_sha256": decision_sha,
                               "gcode_sha256": file_sha256(out)}
    if auto_scale is not None:
        job['auto_scale'] = auto_scale
    else:
        job.pop('auto_scale', None)
    job.update({"printer_model": adapter.key, "slice_config": [str(p) for p in configs], "gcode": str(out),
                "provenance": provenance, "gcode_verified": True,
                # Only this path clones copies into the G-code; estimates read it, never the request.
                "sliced_copies": {"gcode": str(out), "copies": max(1, int(copies or 1))},
                "sliced_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")})
    write_job(work, job)
    print(f"SLICED (not uploaded): {out}")
    _timing(job_path, 'slice_done')
    return out


def prepare_first_layer(slot, color, material, spoolman_id, slicer="orca"):
    """Offline geometry + real slicer output. No printer contact or start permission."""
    st = printer_state.load()
    selection = {"slot": slot, "color": color, "material": material, "spoolman_id": spoolman_id}
    mapped = st.get("ifs_slots", {}).get(str(slot), {})
    if (mapped.get("spoolman_id") != spoolman_id or
            str(mapped.get("color", "")).casefold() != color.casefold() or
            str(mapped.get("material", "")).casefold() != material.casefold() or
            st["spool"]["spoolman_id"] != spoolman_id):
        raise ValueError("selected filament differs from recorded slot/active spool; live reconciliation required")
    folder = ARCHIVE / "ad5x-first-layer-sliced"
    source = folder / "source" / "first_layer_50x50.stl"
    source.parent.mkdir(parents=True, exist_ok=True)
    # Closed 50 x 50 x 0.2 mm rectangular solid, one printable layer.
    vertices = [(0,0,0), (50,0,0), (50,50,0), (0,50,0),
                (0,0,0.2), (50,0,0.2), (50,50,0.2), (0,50,0.2)]
    faces = [(0,2,1),(0,3,2),(4,5,6),(4,6,7),(0,1,5),(0,5,4),
             (1,2,6),(1,6,5),(2,3,7),(2,7,6),(3,0,4),(3,4,7)]
    lines = ["solid first_layer"]
    for face in faces:
        lines += ["facet normal 0 0 0", "outer loop"]
        lines += ["vertex " + " ".join(map(str, vertices[i])) for i in face]
        lines += ["endloop", "endfacet"]
    source.write_text("\n".join(lines + ["endsolid first_layer"]) + "\n")
    from .orca_adapter import slice_model
    if slicer != "orca":
        raise ValueError("only Orca slicing is supported; legacy Prusa is retired")
    output, provenance, configs = slice_model(source, folder / "sliced", st, first_layer=True)
    text = output.read_text()
    if text.count(";LAYER_CHANGE") != 1:
        raise RuntimeError("first-layer output must contain exactly one layer")
    config_desc = [str(p) for p in configs]
    command = provenance["slicer_command"]
    job = {"title": "AD5X First Layer Test Square 50x50mm", "slug": folder.name,
           "downloaded_file": str(source), "gcode": str(output), "slice_config": config_desc,
           "filament_selection": selection, "gcode_verified": False,
           "audit": {"size_x": 50, "size_y": 50, "size_z": 0.2, "parts": 1},
           "provenance": provenance,
           "next_action": "Live readiness and verify-gcode required before authorized print-now"}
    write_job(folder, job)
    print("PREPARED OFFLINE (not verified live, not uploaded): " + str(output))
    return output


def gcode_estimate_s(text):
    m=re.search(r'^; estimated printing time \(normal mode\) = (.+)$',text,re.M)
    if not m: return None
    return sum(int(n)*{"d":86400,"h":3600,"m":60,"s":1}[u] for n,u in re.findall(r'(\d+)\s*([dhms])',m.group(1))) or None


def human_dur(sec):
    h,m=divmod(int(round(sec/60)),60)
    return f"{h}h {m}m" if h else f"{m}m"


def _mr(st, path, data=None, timeout=30):
    return request_json(st["moonraker_url"]+path, data=data, timeout=timeout,
                        headers={"Content-Type":"application/json"} if data is not None else None)


def remote_name(gcode, sha, job=None):
    """Moonraker file name: readable model title first, then the plate stem and exact SHA256.

    The title makes Moonraker, Fluidd and the printer screen show what is printing; the
    stem plus full hash keeps the name unique and lets discord_print_summary.find_job
    bind it back to the exact local artifact.
    """
    g = Path(gcode)
    title = job.get("title") if isinstance(job, dict) else None
    slug = re.sub(r"[^A-Za-z0-9]+", "-", str(title or "")).strip("-")[:40].strip("-")
    stem = g.stem if not slug or slug.lower() == g.stem.lower() else f"{slug}_{g.stem}"
    return f"{stem}_{sha}{g.suffix}"


def _announce_print_name(st, job, slots=None, gcode=None):
    """Best effort: publish "<title> (<colors>)" as Klipper display_status.message.

    The Moonraker Home Assistant integration already exposes that field as the
    "Current Display Message" sensor, so no listener or HA change is needed.
    Sent before the start command because a script queued during START_PRINT
    heating would block on the gcode mutex. Never blocks or fails a print.
    """
    title = job.get("title") if isinstance(job, dict) else None
    if not title:
        title = Path(str((job or {}).get("downloaded_file") or "")).stem if isinstance(job, dict) else ""
    name = re.sub(r"[^\w .,&+#@:/-]", "", str(title or "")).strip()[:50]
    if not name:
        return None
    colors = []
    for slot in slots or []:
        row = (st.get("ifs_slots") or {}).get(str(slot)) or {}
        label = " ".join(w.capitalize() for w in str(row.get("color") or "").split())
        if label and label not in colors:
            colors.append(label)
    if not colors and isinstance(job, dict):
        sel = job.get("filament_selection") or {}
        if sel.get("color"):
            colors.append(" ".join(w.capitalize() for w in str(sel["color"]).split()))
    plates = (job.get("derived_plates") or []) if isinstance(job, dict) else []
    if gcode is not None and len(plates) > 1:
        paths = [str(r.get("gcode") or "") for r in plates]
        if str(gcode) in paths:
            name = f"{name} {paths.index(str(gcode)) + 1}/{len(plates)}"
    if colors:
        name = f"{name} ({' + '.join(colors)})"
    try:
        request_json(st["moonraker_url"] + "/printer/gcode/script",
                     data={"script": f'SET_DISPLAY_TEXT MSG="{name}"'}, timeout=5,
                     headers={"Content-Type": "application/json"})
        print("PRINT NAME PUBLISHED " + json.dumps({"name": name}), flush=True)
        return name
    except Exception as exc:
        print(f"WARNING: print name not published: {exc}", flush=True)
        return None


def _usage_for_source_extruder(manifest, source_extruder):
    stats = (manifest or {}).get('filament_stats') or {}
    extruders = stats.get('extruders') if isinstance(stats, dict) else {}
    if not isinstance(extruders, dict):
        return None
    row = extruders.get(source_extruder, extruders.get(str(source_extruder)))
    grams = row.get('g') if isinstance(row, dict) else None
    return float(grams) if isinstance(grams, (int, float)) and not isinstance(grams, bool) and grams > 0 else None


def source_site_estimate(row):
    """(seconds, grams) summed over the author's plates from the model page (MakerWorld), else None."""
    plates = (row or {}).get('plates') if isinstance(row, dict) else None
    if not isinstance(plates, list) or not plates:
        return None
    seconds = grams = 0.0
    for plate in plates:
        if not isinstance(plate, dict):
            return None
        s, g = plate.get('prediction_s'), plate.get('weight_g')
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 < v < 1e7 for v in (s, g)):
            return None
        seconds += float(s)
        grams += float(g)
    return seconds, grams


def _reference_manifest(job, manifest):
    """Replace a volume-guessed total with the model page's own weight when one exists.

    A guessed total also lands entirely on extruder 1, so every part shares it equally.
    """
    stats = (manifest or {}).get('filament_stats') or {}
    parts = [p for p in (manifest or {}).get('parts') or [] if isinstance(p, dict)]
    extruders = stats.get('extruders') if isinstance(stats.get('extruders'), dict) else {}
    used = {str(p.get('source_extruder')) for p in parts if p.get('source_extruder') is not None}
    guessed = bool(stats.get('estimated')) or bool(used - {str(k) for k in extruders})
    if not guessed:
        return manifest, False
    site = source_site_estimate((job or {}).get('selected_source'))
    total = site[1] if site else stats.get('total_g')
    if not isinstance(total, (int, float)) or isinstance(total, bool) or total <= 0:
        return manifest, False
    flat = {**manifest, 'parts': [{**p, 'source_extruder': 1} for p in parts],
            'filament_stats': {**stats, 'total_g': float(total), 'extruders': {'1': {'g': float(total)}}}}
    return flat, True


def filament_usage_by_selection(job, manifest=None):
    """Aggregate source estimates by confirmed selection, without duplicating multi-color totals."""
    manifest = manifest or (job or {}).get('color_manifest') or {}
    manifest, _ = _reference_manifest(job, manifest)
    decisions = (job or {}).get('color_decisions') or {}
    if decisions.get('mode') == 'single_color':
        selection = decisions.get('selection') or (job or {}).get('filament_selection')
        total = ((manifest.get('filament_stats') or {}).get('total_g')
                 if isinstance(manifest, dict) else None)
        if isinstance(selection, dict) and isinstance(total, (int, float)) and not isinstance(total, bool) and total > 0:
            return [(selection, float(total))]
        return []
    choices = decisions.get('choices') if isinstance(decisions, dict) else None
    if not isinstance(choices, dict):
        return []
    # Painted sources contain both geometry and palette choice rows. The palette
    # rows are the authoritative per-extruder allocation, so do not count both.
    palette_choices = {key: value for key, value in choices.items()
                       if isinstance(key, str) and key.startswith('filament:')}
    part_extruders = {'part:' + str(part.get('key')): part.get('source_extruder')
                      for part in manifest.get('parts') or [] if isinstance(part, dict)}
    choice_rows = palette_choices or choices
    # An extruder total belongs to every part on that extruder together, never to each one.
    # Split it across those parts (by volume when known) so N parts never count N x the total.
    sharers = {}
    for key in choice_rows:
        if isinstance(key, str) and not key.startswith('filament:'):
            sharers.setdefault(str(part_extruders.get(key)), []).append(key)
    volumes = {'part:' + str(p.get('key')): p.get('volume_mm3')
               for p in manifest.get('parts') or [] if isinstance(p, dict)}
    totals = {}
    for key, selection in choice_rows.items():
        if not isinstance(selection, dict) or not isinstance(key, str):
            continue
        source_text = (key.split(':', 1)[1] if key.startswith('filament:') else part_extruders.get(key))
        try:
            source_extruder = int(source_text)
        except (TypeError, ValueError):
            continue
        grams = _usage_for_source_extruder(manifest, source_extruder)
        if grams is None:
            continue
        if not key.startswith('filament:'):
            group = sharers.get(str(source_text)) or [key]
            vols = [volumes.get(k) for k in group]
            if all(isinstance(v, (int, float)) and v > 0 for v in vols):
                grams *= float(volumes[key]) / sum(vols)
            else:
                grams /= len(group)
        identity = (selection.get('slot'), selection.get('spoolman_id'),
                    str(selection.get('color') or '').casefold(), str(selection.get('material') or '').casefold())
        item = totals.setdefault(identity, [selection, 0.0])
        item[1] += grams
    return [(selection, grams) for selection, grams in totals.values()]


def intake_spool_projection(job, spool_weights, manifest=None, copies=1):
    """Remaining-weight projection from exact source estimates and saved choices."""
    try:
        copies = max(1, int(copies or 1))
    except (TypeError, ValueError):
        copies = 1
    rows = []
    for selection, grams in filament_usage_by_selection(job, manifest):
        grams *= copies
        remaining = spool_weights.get(selection.get('spoolman_id')) if isinstance(spool_weights, dict) else None
        projected = (float(remaining) - grams if isinstance(remaining, (int, float))
                     and not isinstance(remaining, bool) else None)
        rows.append({'slot': selection.get('slot'), 'color': selection.get('color'),
                     'material': selection.get('material'), 'spoolman_id': selection.get('spoolman_id'),
                     'estimated_used_g': round(grams, 1),
                     'remaining_g': round(float(remaining), 1) if isinstance(remaining, (int, float))
                     and not isinstance(remaining, bool) else None,
                     'projected_remaining_g': round(projected, 1) if projected is not None else None})
    return rows


def intake_spool_weights(state, spool_ids, fetch=None):
    """Currently available grams per Spoolman ID: Spoolman first, then the slot record, else absent.

    Display only. Never raises; an unreachable Spoolman just falls back.
    """
    slots = {s.get('spoolman_id'): s for s in ((state or {}).get('ifs_slots') or {}).values()
             if isinstance(s, dict) and s.get('spoolman_id') is not None}
    weights = {}
    for sid in spool_ids:
        grams = None
        if fetch is not None and isinstance(sid, int) and not isinstance(sid, bool):
            try:
                rem = (fetch(sid) or {}).get('remaining_weight')
                if isinstance(rem, (int, float)) and not isinstance(rem, bool) and rem >= 0:
                    grams = float(rem)
            except Exception:
                grams = None
        if grams is None:
            rem = (slots.get(sid) or {}).get('remaining_grams')
            if isinstance(rem, (int, float)) and not isinstance(rem, bool) and rem >= 0:
                grams = float(rem)
        if grams is not None:
            weights[sid] = grams
    return weights


def format_spool_projection(rows):
    """One short line per spool: needed, available, left after. Unknowns stay unknown."""
    lines = []
    for r in rows:
        label = ' '.join(str(x) for x in (r.get('color'), r.get('material')) if x) or 'filament'
        if r.get('slot') is not None:
            label = f"Slot {r['slot']} {label}"
        line = f"{label}: needs about {r['estimated_used_g']:g} g"
        if r.get('remaining_g') is None:
            line += '; spool weight unknown.'
        elif r['projected_remaining_g'] < 0:
            line += (f"; spool has {r['remaining_g']:g} g, short by about "
                     f"{-r['projected_remaining_g']:g} g.")
        else:
            line += (f"; spool has {r['remaining_g']:g} g, about "
                     f"{r['projected_remaining_g']:g} g left after.")
        lines.append(line)
    return '\n'.join(lines)


def job_card(job, remote, gcode_text=""):
    """Copies and spools for the Home Assistant print card: one JSON document, no secrets."""
    job = job if isinstance(job, dict) else {}
    dec = job.get("color_decisions") or {}
    picks = [dec["selection"]] if isinstance(dec.get("selection"), dict) else list((dec.get("choices") or {}).values())
    if not picks and isinstance(job.get("filament_selection"), dict):
        picks = [job["filament_selection"]]
    spools, seen = [], set()
    for row in picks:
        if isinstance(row, dict) and row.get("slot") not in seen:
            seen.add(row.get("slot"))
            spools.append({k: row.get(k) for k in ("slot", "color", "material", "spoolman_id")})
    m = re.search(r"^; total filament used \[g\] = ([\d.]+)", gcode_text or "", re.M)
    return {"title": job.get("title") or Path(str(remote)).stem, "file": remote,
            "copies": int(job.get("copies") or 1), "spools": spools,
            "filament_g": round(float(m[1]), 1) if m else None,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}


def _publish_job_card(st, job, remote, gcode_text=""):
    """Best effort: Moonraker database hermes/current_job, read by Home Assistant. Never blocks a print."""
    try:
        card = job_card(job, remote, gcode_text)
        request_json(st["moonraker_url"] + "/server/database/item",
                     data={"namespace": "hermes", "key": "current_job", "value": card}, timeout=5,
                     headers={"Content-Type": "application/json"})
        return card
    except Exception as exc:
        print(f"WARNING: job card not published: {exc}", flush=True)
        return None


def _fetch_remote_gcode_sha256(moonraker_url, remote_name, timeout=60):
    escaped = urllib.parse.quote(remote_name)
    url = f"{moonraker_url}/server/files/gcodes/{escaped}"
    hasher = hashlib.sha256()
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        total_read = 0
        while True:
            chunk = resp.read(65536)
            if not chunk:
                break
            hasher.update(chunk)
            total_read += len(chunk)
            if total_read > 1024 * 1024 * 1024:
                raise RuntimeError(f"Remote file {remote_name} exceeds 1GB limit")
    return hasher.hexdigest()


def _printer_phase(st):
    """Return a single adapter's reported phase; native 'printing' needs corroboration."""
    native=_native()
    q=_mr(st,"/printer/objects/query?"+("save_variables&" if native else "")+"print_stats&idle_timeout&virtual_sdcard&webhooks")["result"]["status"]
    variables=(q.get("save_variables") or {}).get("variables") or {}
    mode=variables.get("display_off")
    if native and mode not in (0,1):
        raise RuntimeError(f"live display_off mode is {mode!r}; refusing ambiguous hardware behavior")
    st["display_off"]=mode
    st["force_md5"]=variables.get("force_md5")
    stats=q.get("print_stats") or {}
    state=stats.get("state")
    filename=stats.get("filename")
    idle=(q.get("idle_timeout") or {}).get("state")
    virtual_sd = q.get("virtual_sdcard") or {}
    webhooks = q.get("webhooks") or {}
    # print_stats can remain standby/empty while PRINT_START is heating or
    # otherwise preparing. These independent Klipper signals still prove the
    # submitted job has been accepted; do not infer failure or retry it.
    accepted = (virtual_sd.get("is_active") is True or bool(virtual_sd.get("file_path"))
                or webhooks.get("state") == "Printing" or idle == "Printing")
    if state not in ("standby", "printing", "paused", "complete", "error", "cancelled"):
        raise RuntimeError(f"Moonraker print state unknown: {state!r}")
    if state in ("printing", "paused"):
        return state, filename, "Moonraker reports active execution"
    return state, filename, "Printing" if accepted else idle


class StartReadback(NamedTuple):
    outcome: str
    evidence: Any
    message: str


def classify_start_readback(status_snapshot) -> StartReadback:
    """Pure function classifying printer status after submission.

    Outcomes:
    - 'preparing': accepted, with heating, homing or prep evidence.
    - 'printing': state printing.
    - 'failed': error or cancelled.
    - 'unconfirmed': no evidence.
    """
    if not isinstance(status_snapshot, dict):
        status = {}
    elif "result" in status_snapshot and isinstance(status_snapshot["result"], dict) and "status" in status_snapshot["result"]:
        status = status_snapshot["result"]["status"]
    elif "status" in status_snapshot and isinstance(status_snapshot["status"], dict):
        status = status_snapshot["status"]
    else:
        status = status_snapshot

    ps = status.get("print_stats") if isinstance(status.get("print_stats"), dict) else {}
    ext = status.get("extruder") if isinstance(status.get("extruder"), dict) else {}
    bed = status.get("heater_bed") if isinstance(status.get("heater_bed"), dict) else {}
    vsd = status.get("virtual_sdcard") if isinstance(status.get("virtual_sdcard"), dict) else {}
    idle = status.get("idle_timeout") if isinstance(status.get("idle_timeout"), dict) else {}
    hooks = status.get("webhooks") if isinstance(status.get("webhooks"), dict) else {}
    toolhead = status.get("toolhead") if isinstance(status.get("toolhead"), dict) else {}

    raw_state = ps.get("state") or status.get("state")
    state = raw_state.lower() if isinstance(raw_state, str) else None

    # 1. Failed
    hook_state = (hooks.get("state") or "").lower() if isinstance(hooks.get("state"), str) else ""
    if state in ("error", "cancelled") or hook_state in ("error", "shutdown"):
        fail_state = state or hook_state or "error"
        return StartReadback(
            outcome="failed",
            evidence={"state": fail_state},
            message=f"print failed: state is {fail_state}",
        )

    # 2. Printing
    if state == "printing":
        filament_used = None
        for k in ("filament_used", "filament_used_mm", "filament_length"):
            if k in ps:
                filament_used = ps[k]
                break
            if k in status:
                filament_used = status[k]
                break

        extruder_delta = None
        for candidate in (
            status.get("extruder_position_delta"),
            status.get("extruder_delta"),
            ext.get("position_delta"),
            ext.get("delta"),
            toolhead.get("extruder_position_delta"),
            toolhead.get("extruder_delta"),
        ):
            if candidate is not None:
                extruder_delta = candidate
                break

        extrusion_confirmed = False
        if filament_used is not None:
            try:
                if float(filament_used) > 0:
                    extrusion_confirmed = True
            except (ValueError, TypeError):
                pass
        if extruder_delta is not None:
            try:
                if float(extruder_delta) > 0:
                    extrusion_confirmed = True
            except (ValueError, TypeError):
                pass

        msg = "printing" if extrusion_confirmed else "printing (extrusion not yet confirmed)"
        evidence = {
            "state": "printing",
            "filament_used": filament_used,
            "extruder_position_delta": extruder_delta,
            "extrusion_confirmed": extrusion_confirmed,
            "info": ps.get("info") or status.get("info"),
        }
        return StartReadback(outcome="printing", evidence=evidence, message=msg)

    # 3. Preparing
    prep_evidence = {}
    heater_targets = {}
    for name, obj in (("extruder", ext), ("heater_bed", bed)):
        t = obj.get("target")
        if t is not None:
            try:
                if float(t) > 0:
                    heater_targets[name] = float(t)
            except (ValueError, TypeError):
                pass
    for name, key in (("extruder", "extruder_target"), ("heater_bed", "heater_bed_target")):
        if key in status:
            try:
                if float(status[key]) > 0:
                    heater_targets[name] = float(status[key])
            except (ValueError, TypeError):
                pass
    if heater_targets:
        prep_evidence["heating"] = heater_targets

    homed = toolhead.get("homed_axes") or status.get("homed_axes")
    if homed:
        prep_evidence["homing"] = homed
    elif toolhead.get("homing") or status.get("homing"):
        prep_evidence["homing"] = True

    if vsd.get("is_active") is True or vsd.get("file_path"):
        prep_evidence["virtual_sdcard"] = vsd.get("file_path") or True
    if idle.get("state") == "Printing" or status.get("idle_state") == "Printing":
        prep_evidence["idle_timeout"] = "Printing"
    if hook_state == "printing":
        prep_evidence["webhooks"] = "Printing"
    if ps.get("filename"):
        prep_evidence["filename"] = ps.get("filename")
    if status.get("preparation_evidence") is True or status.get("prep") is True:
        prep_evidence["preparation_evidence"] = True
    if status.get("ifs_loading") or status.get("ifs_load"):
        prep_evidence["ifs_loading"] = True
    ifs_obj = status.get("zmod_ifs") if isinstance(status.get("zmod_ifs"), dict) else {}
    if ifs_obj.get("state") or ifs_obj.get("feed") or ifs_obj.get("action"):
        prep_evidence["zmod_ifs"] = ifs_obj

    if prep_evidence:
        return StartReadback(
            outcome="preparing",
            evidence=prep_evidence,
            message="printer accepted the job and is preparing (heating/loading); not a failure; do not resubmit",
        )

    # 4. Unconfirmed
    return StartReadback(
        outcome="unconfirmed",
        evidence=None,
        message="submitted, awaiting confirmation",
    )


def _preparation_evidence(st):
    """True when a heater target is set (native start preparation), False when all are 0,
    None when unreadable. Read-only; used only after exactly one submission."""
    try:
        q = _mr(st, "/printer/objects/query?extruder=target&heater_bed=target")["result"]["status"]
        targets = [float((q.get(k) or {}).get("target")) for k in ("extruder", "heater_bed")]
    except Exception:
        return None
    return any(v > 0 for v in targets)


def _wait_printing(st, remote, wait_s):
    """Wait until print_stats is printing, or the start sequence settles without printing."""
    t0=time.monotonic(); state=None
    while time.monotonic()-t0 < wait_s:
        state,fn,busy=_printer_phase(st)
        if state=="printing" and fn==remote: return "printing"
        if state in ("error","cancelled"): return state
        if time.monotonic()-t0 > 20 and busy != "Printing": return state  # settled, not printing
        time.sleep(1)
    return state


def live_snapshot(st):
    """Current temps and every IFS slot for Job Details. Read-only; unknown stays unknown."""
    snap={"nozzle_c":None,"bed_c":None,"slots":"unknown"}
    try:
        q=_mr(st,"/printer/objects/query?extruder&heater_bed")["result"]["status"]
        snap["nozzle_c"]=q.get("extruder",{}).get("temperature")
        snap["bed_c"]=q.get("heater_bed",{}).get("temperature")
    except Exception: pass
    if not _native(): return snap
    try:
        tel=read_filament_telemetry(st); palette=tel.get("configured_palette")
        presence={}
        try: presence=read_stock_ifs_slots(st)
        except Exception: pass
        if isinstance(palette,list):
            snap["slots"]=[{"slot":p.get("ID"),"color":p.get("Color"),"material":p.get("Material"),
                            "loaded":presence.get(int(p["ID"])) if str(p.get("ID")).isdigit() else None}
                           for p in palette]
    except Exception: pass
    return snap


def _fmt_slots(slots):
    if not isinstance(slots,list) or not slots: return "unknown"
    def u(v): return v if v not in (None,"") else "unknown"
    def loaded(v): return "loaded" if v is True else "empty" if v is False else "presence unknown"
    return "; ".join(f"{u(s.get('slot'))}: {u(s.get('color'))} {u(s.get('material'))} ({loaded(s.get('loaded'))})" for s in slots)


# Typical durations only, from common Klipper guidance; never a measurement of this printer.
ROUTINE_TYPICAL_MIN = {
    "home": (1, 2), "bed_mesh": (3, 6), "leveling": (5, 10), "z_offset": (3, 5),
    "pid_hotend": (5, 8), "pid_bed": (8, 15), "input_shaper": (5, 10),
}
ROUTINE_NAMES = {"home": "homing", "bed_mesh": "bed mesh probe", "leveling": "full bed leveling",
                 "z_offset": "Z offset calibration", "pid_hotend": "hotend PID tuning",
                 "pid_bed": "bed PID tuning", "input_shaper": "input shaper calibration"}


def routine_receipt(routine):
    """Deterministic immediate receipt; the estimate comes only from the static typical table."""
    if routine not in ROUTINE_TYPICAL_MIN: raise ValueError(f"unknown routine {routine!r}; known: {', '.join(ROUTINE_TYPICAL_MIN)}")
    lo,hi=ROUTINE_TYPICAL_MIN[routine]
    return f"Running {ROUTINE_NAMES[routine]}, about {lo} to {hi} min (typical, not measured on this printer)."


def routine_phase(st, accepted=True):
    """accepted -> running -> completed, read from Klipper idle_timeout. Never inferred from time."""
    try: busy=_mr(st,"/printer/objects/query?idle_timeout")["result"]["status"]["idle_timeout"].get("state")
    except Exception: return "unknown"
    if busy=="Printing": return "running"
    if busy in ("Ready","Idle"): return "completed" if accepted else "idle"
    return "unknown"


def _consume_approval(job_path, job, gcode, reprint=False):
    """Single-use bed-clear approval per G-code. Refuses reuse unless an explicit new reprint approval.
    Check and record run in one launch-state transaction on the current JOB.json, never the caller's
    possibly stale snapshot; the database copy survives another writer saving an older JOB.json."""
    sha=file_sha256(gcode); key=str(Path(job_path).resolve())
    with launch_state.transaction() as con:
        current=json.loads(Path(job_path).read_text())
        require_current_artifact(current, gcode)
        now=time.strftime("%Y-%m-%dT%H:%M:%S%z")
        used=[a for a in current.get("approvals",[]) if a.get("gcode_sha256")==sha and a.get("consumed_at")]
        used+=launch_state.consumed(con, key, sha)
        if used and not reprint:
            raise SystemExit(f"REFUSED: the bed-clear approval for this G-code was already used at {used[-1]['consumed_at']}. "
                             "Ask the operator for a new bed-clear confirmation, then rerun with --reprint. Nothing uploaded or started.")
        kind="reprint" if used else "bed_clear"
        launch_state.record_consumed(con, key, sha, kind, now)
        current.setdefault("approvals",[]).append({"gcode_sha256":sha,"granted_at":now,"consumed_at":now,"kind":kind})
        write_job(Path(job_path).parent, current)
    job.clear(); job.update(current)


def _printer_key(st):
    """One submission lease per printer this profile drives."""
    return st.get("moonraker_url") or printers.current().key


def _require_idle_before_mapped_launch(require_idle_settled, read_moonraker, read_native):
    """Report only an explicit idle-guard refusal as a clean pre-upload refusal."""
    try:
        require_idle_settled(read_moonraker, read_native)
    except RuntimeError as exc:
        message = str(exc)
        if not message.startswith("REFUSED: idle guard failed:"):
            raise
        raise SystemExit(message + ". Nothing uploaded or started.") from None


def print_mapped_native(g, st, mesh, mapping, job, job_path, wait_s, reprint):
    """Native explicit mapping route; exactly one SET_ZCOLOR submission, no fallback."""
    require_idle_settled = printers.current().native.require_idle_settled
    require_current_artifact(job, g)
    if mesh.get('display_off') != 0:
        raise RuntimeError('explicit mapped launch currently requires native screen mode')
    text = g.read_text()
    _embedded = printers.current().native.embedded_mapping
    _embedded(text, st)  # refuses a spool swapped since slicing, before any printer I/O
    validate_saved_mesh_start(text, mesh)
    response = _mr(st, '/printer/objects/query?webhooks&print_stats&extruder&heater_bed&zmod_color&filament_switch_sensor%20head_switch_sensor')
    status = response['result']['status']
    def read_moonraker():
        current = _mr(st, '/printer/objects/query?webhooks&print_stats&extruder&heater_bed&zmod_color&filament_switch_sensor%20head_switch_sensor')['result']['status']
        return {'state': current['webhooks']['state'], 'print': current['print_stats'],
                'extruder': current['extruder'], 'bed': current['heater_bed']}
    def read_native():
        return read_stock_detail(st)
    _require_idle_before_mapped_launch(require_idle_settled, read_moonraker, read_native)
    detail = read_native()
    status = response['result']['status']
    sensor = status.get('filament_switch_sensor head_switch_sensor', {})
    if sensor.get('enabled') is not True:
        raise RuntimeError('head filament sensor protection is disabled or unknown')
    # Idle nozzle engagement is not feeder occupancy. Genuine native errors and
    # installed runout/jam protections stay intact; do not actuate sensor overrides.
    configured = status.get('zmod_color', {}).get('slots', [])
    occupancy = read_stock_ifs_slots_from_detail(detail)
    normalize_mapping = printers.current().native.normalize_mapping
    mapped = normalize_mapping(mapping, st)
    for tool, slot in mapped.items():
        expected = st['ifs_slots'][str(slot)]
        matches = [item for item in configured if str(item.get('ID')) == str(slot)]
        # Family match (PLA == PLA+): the native slot label is coarser than Spoolman's material,
        # so an exact compare refused a correctly loaded PLA+ spool.
        if len(matches) != 1 or not printer_state.same_material(matches[0].get('Material'), expected['material']):
            raise RuntimeError(f'T{tool} slot {slot} live material mapping mismatch or unknown')
        if str(matches[0].get('HEX', '')).lstrip('#').upper() != expected['hex'].lstrip('#').upper():
            raise RuntimeError(f'T{tool} slot {slot} live RGB mapping mismatch or unknown')
        if occupancy.get(slot) is not True:
            # Operator-confirmed loaded feeders supersede idle false occupancy,
            # never unknown telemetry or an actual native error/activity.
            if occupancy.get(slot) is not False or expected.get('physical_loading_verified') is not True:
                raise RuntimeError(f'T{tool} slot {slot} occupancy is unknown or unconfirmed')
    sha = file_sha256(g)
    remote = remote_name(g, sha, job)
    lease = launch_state.acquire(_printer_key(st), job_json=job_path, gcode_sha256=sha, remote=remote,
                                 observed_file=(status.get('print_stats') or {}).get('filename'))
    try:
        # Again inside the lease: the first read may predate another job's start.
        _require_idle_before_mapped_launch(require_idle_settled, read_moonraker, read_native)
        if job_path is not None:
            _consume_approval(job_path, job, g, reprint=reprint)
        slots_used = sorted(set(mapped.values()))
        if len(slots_used) > 1:
            print('WARNING: MULTI-SLOT JOB: automatic Spoolman usage deduction disabled; aggregate usage cannot be safely apportioned to selected spools.', flush=True)
        else:
            _sync_active_spool(st, slots_used[0])
        _timing(job_path, 'upload_begin')
        uploaded = subprocess.run([str(MOONRAKERCTL), 'upload', str(g), remote, sha],
                                  text=True, capture_output=True, timeout=300)
        if uploaded.returncode:
            raise RuntimeError('UPLOAD FAILED: ' + (uploaded.stderr or uploaded.stdout)[-500:])
        upload_json = json.loads(uploaded.stdout)
        g.with_suffix('.upload.json').write_text(json.dumps(upload_json, indent=2) + '\n')
        result = upload_json.get('result', upload_json)
        item = result.get('item', {}) if isinstance(result, dict) else {}
        if item.get('root') != 'gcodes' or item.get('path') != remote:
            raise RuntimeError('uploaded target mismatch: ' + json.dumps(item))
        # Integrity: Moonraker checked the SHA256 we sent and refuses the upload (HTTP 400) on mismatch,
        # so the file is never downloaded back to hash it.
        _timing(job_path, 'upload_verified')
        command = (f'SET_ZCOLOR FILENAME={remote} SILENT=1 AUTO_ASSIGN=0 LEVELING=0 ' +
                   ' '.join(f'T{tool}={slot}' for tool, slot in sorted(mapping.items())))
        checkpoint = g.with_suffix('.launch.json')
        if checkpoint.exists():
            raise RuntimeError('mapped submission checkpoint exists; reconcile instead of replaying')
        checkpoint.write_text(json.dumps({'phase': 'submission_pending', 'command': command, 'sha256': sha}) + '\n')
        # This path posts its own start card, so the listener must not post a second one
        # when native preparation (heating, leveling) finishes minutes later (2026-10-11 duplicate).
        try: claim_start_announcement(remote)
        except OSError as e: print(f"WARNING: start announcement claim failed, listener may also post: {e}")
        _announce_print_name(st, job, slots_used, g)
        _publish_job_card(st, job, remote, g.read_text(errors="ignore"))
    except BaseException:
        lease.release()  # nothing was sent to start
        raise
    lease.submitting()
    try:
        receipt = _mr(st, '/printer/gcode/script', data={'script': command})
    except BaseException as exc:
        release_start_announcement(remote)
        lease.uncertain(f'start request did not complete: {exc!r}')
        raise
    _timing(job_path, 'start_submitted')
    # Number at submission, not at "printing": native preparation can outlast the readback
    # and the listener's completion card only reads the registry (2026-10-14: 6 unnumbered).
    print_no = _assign_print_number(job_path)
    out = {'accepted': False, 'started': False, 'phase': 'submitted_unconfirmed',
           'file': remote, 'artifact': str(g), 'sha256': sha, 'mapping': mapping,
           'command': command, 'receipt': receipt, 'submission_count': 1, 'print_number': print_no}
    checkpoint = g.with_suffix('.launch.json')
    checkpoint.write_text(json.dumps(out, indent=2) + '\n')
    print('MAPPED START SUBMITTED ' + json.dumps(out), flush=True)
    # Record immediately so a long native preparation cannot lose its dated note.
    out['record_path'] = str(record_start(g, remote))
    state = _wait_printing(st, remote, wait_s)
    readback = None
    try:
        snap = _mr(st, "/printer/objects/query?print_stats&extruder&heater_bed&virtual_sdcard&idle_timeout&webhooks&toolhead")
        readback = classify_start_readback(snap)
    except Exception:
        pass

    if state == 'printing' or (readback and readback.outcome == 'printing'):
        out.update(accepted=True, started=True, phase='executing', moonraker_state='printing',
                   outcome='printing', message=(readback.message if readback else 'printing'),
                   extrusion_confirmed=bool(readback and (readback.evidence or {}).get('extrusion_confirmed')))
    else:
        # The AD5X native route heats and loads the IFS slot for several minutes before
        # print_stats reports printing (2026-10-16: ~7 min). Heater targets set by that
        # preparation prove acceptance; report preparing, never a failure, never retry.
        preparing = _preparation_evidence(st)
        is_prep = preparing is True or (readback and readback.outcome == 'preparing')
        outcome_val = 'preparing' if is_prep else (readback.outcome if readback else 'unconfirmed')
        msg = readback.message if readback else 'submitted, awaiting confirmation'
        out.update(accepted=is_prep, started=is_prep, moonraker_state=state,
                   phase='preparing' if is_prep else 'preparing_or_unresolved',
                   outcome=outcome_val,
                   message=msg, preparation_evidence=preparing)
    if out['accepted']:
        lease.release()  # accepted; the busy printer now refuses another start
    else:
        lease.uncertain(out['message'])  # reconciled when the printer reports this file
    checkpoint.write_text(json.dumps(out, indent=2) + '\n')
    print('MAPPED START READBACK ' + json.dumps(out), flush=True)
    return out


def resolve_print_target(target, archive=None):
    """A bare printer file name (what Moonraker shows) maps to the archived local artifact
    that was uploaded under that name. Never searches outside the archive, never downloads."""
    raw = str(target)
    p = Path(raw).expanduser()
    if p.is_absolute() or "/" in raw or p.exists():
        return raw
    archive = Path(archive or ARCHIVE)
    for pat in ("*/sliced/*/*/*.launch.json", "*/sliced/*/*/*.upload.json"):
        for rec in sorted(archive.glob(pat), key=lambda x: x.stat().st_mtime, reverse=True):
            try:
                d = json.loads(rec.read_text())
            except (OSError, ValueError):
                continue
            item = d.get("item") if isinstance(d.get("item"), dict) else {}
            if raw in (d.get("file"), d.get("remote"), d.get("remote_name"), item.get("path")):
                artifact = rec.with_name(rec.name.split(".", 1)[0] + ".gcode")
                if artifact.is_file():
                    return str(artifact)
    raise SystemExit(f"REFUSED: {raw} is a printer file name, not a local path, and no archived job uploaded a "
                     "file with that name. Pass the job's JOB.json instead. Nothing uploaded or started.")


def remap_slot(target, new_slot, tool="0", state=None):
    """Remap a single-color job or G-code to a different physical IFS slot.

    Rewrites tool mapping metadata and checksum, updating job filament_selection
    when given a JOB.json, allowing printing with a different physical spool
    without physical changes or screen interaction.
    """
    st = state or printer_state.load()
    resolved = resolve_print_target(target)
    target_path = Path(resolved).expanduser().resolve()

    job = None
    job_path = None
    if target_path.name == "JOB.json":
        job_path = target_path
        job = json.loads(job_path.read_text())
        if not job.get("gcode"):
            raise RuntimeError("stale color artifact: job has no sliced gcode")
        gcode_path = Path(job["gcode"]).expanduser().resolve()
    else:
        gcode_path = target_path
        temp_root = Path(os.environ.get("TMPDIR") or "/tmp").resolve()
        for parent in list(gcode_path.parents)[:5]:
            if parent in (Path("/"), temp_root) or temp_root in parent.parents:
                break
            if (parent / "JOB.json").is_file():
                job_path = parent / "JOB.json"
                job = json.loads(job_path.read_text())
                break

    slot_int = int(new_slot)
    if slot_int not in range(1, IFS_SLOTS + 1):
        raise ValueError(f"physical IFS slot must be an integer from 1 through {IFS_SLOTS}")

    selection = resolve_filament_selection(st, {"slot": slot_int})

    raw_tools = {}
    if gcode_path.is_file():
        for line in gcode_path.read_text().splitlines():
            if line.startswith(MAP_PREFIX):
                try:
                    data = json.loads(line[len(MAP_PREFIX):])
                    for t, v in data.items():
                        raw_tools[str(t)] = v.get("physical_slot") if isinstance(v, dict) else v
                except Exception:
                    pass
                break
    mapping = dict(raw_tools) if raw_tools else {}
    mapping[str(tool)] = slot_int
    normalize_mapping = printers.current().native.normalize_mapping
    mapping = normalize_mapping(mapping, st)

    new_sha = None
    if gcode_path.is_file():
        finalize = printers.current().native.finalize
        new_sha = finalize(gcode_path, None, st, mapping)
        try:
            verify_gcode(gcode_path)
        except Exception:
            offline_st = dict(st)
            offline_st.setdefault("temps_c", {"bed": 60, "nozzle": 210})
            verify_gcode_offline(gcode_path, offline_st)

    if job is not None and job_path is not None:
        job["filament_selection"] = selection
        if new_sha:
            job["gcode_sha256"] = new_sha
            job["gcode_verified"] = True
            if "artifact_binding" in job and isinstance(job["artifact_binding"], dict):
                job["artifact_binding"]["gcode_sha256"] = new_sha
                job["artifact_binding"]["decisions_sha256"] = decisions_digest(job.get("color_decisions"))
        write_job(job_path.parent, job)

    return {
        "ok": True,
        "slot": slot_int,
        "tool": str(tool),
        "mapping": mapping,
        "selection": selection,
        "gcode": str(gcode_path) if gcode_path.is_file() else None,
        "gcode_sha256": new_sha,
        "job_json": str(job_path) if job_path else None,
    }


def _print_octoprint(adapter, state, gcode, job, job_path, wait_s, reprint):
    """A separate transport path, retaining artifact and approval guards above."""
    client = adapter.client()
    try:
        status = client.status()
        client.require_ready(status)
        observed = adapter.normalize_status(status).get('filename')
        verify_gcode(gcode)
        _refuse_multicolor(gcode.read_text())
    except RuntimeError as exc:
        raise SystemExit(f'REFUSED: {exc}. Nothing uploaded or started.') from exc
    sha = file_sha256(gcode)
    remote = remote_name(gcode, sha, job)
    lease = launch_state.acquire(_printer_key(state), job_json=job_path, gcode_sha256=sha, remote=remote,
                                 observed_file=observed)
    try:
        try:
            client.require_ready(client.status())  # again inside the lease
        except RuntimeError as exc:
            raise SystemExit(f'REFUSED: {exc}. Nothing uploaded or started.') from exc
        if job_path is not None:
            _consume_approval(job_path, job, gcode, reprint=reprint)
    except BaseException:
        lease.release()
        raise
    # Upload and start are one uncertain step here: the client reports either failure the same way.
    lease.submitting()
    try:
        remote = client.upload(gcode, remote)
        from .nozzle import effective_nozzle_temperature
        hotend = effective_nozzle_temperature((state.get('nozzle') or {}).get('type', 'hardened_steel'), state['temps_c']['nozzle'])
        client.start(remote, state['temps_c']['bed'], hotend)
    except BaseException as exc:
        lease.uncertain(f'OctoPrint submission did not complete: {exc!r}')
        if isinstance(exc, RuntimeError):
            raise SystemExit(f'OctoPrint submission failed or uncertain: {exc}. Do not resubmit without checking printer status.') from exc
        raise
    # Accepted is not proof of printing. No resubmission on delayed readback.
    live = {}
    try:
        live = adapter.normalize_status(client.status())
    except RuntimeError:
        pass
    confirmed = live.get('state') == 'printing' and live.get('filename') == remote
    if confirmed:
        lease.release()
    else:
        lease.uncertain('submitted, awaiting confirmation')
    out = {'started': confirmed, 'accepted': True, 'file': remote, 'method': 'octoprint',
           'phase': 'executing' if confirmed else 'awaiting_confirmation',
           'outcome': 'printing' if confirmed else 'unconfirmed', 'live': live,
           'message': 'printing' if confirmed else 'submitted, awaiting confirmation; do not resubmit'}
    if confirmed:
        try:
            out['record_path'] = record_start(gcode, remote)
        except Exception as exc:
            print(f'WARNING: start record failed: {exc}')
    print(('PRINT STARTED ' if confirmed else 'START SUBMITTED; AWAITING CONFIRMATION ') + json.dumps(out))
    return out


def _require_idle(st):
    state,fn,busy=_printer_phase(st)
    if state == "printing" or busy == "Printing":
        raise SystemExit(f"REFUSED: printer is busy ({state}, {fn}). Nothing uploaded or started.")
    if st.get("display_off") == 0 and state not in ("standby", "complete", "cancelled", "error"):
        raise SystemExit(f"REFUSED: native printer state is {state!r} for {fn}; nothing uploaded or started.")
    return state,fn,busy


def print_now(target, wait_s=25, accept_default_z_offset=False, reprint=False, remap_slot_num=None):
    """Bed is confirmed clear: verify, upload, start, confirm, record. One command."""
    if remap_slot_num is not None:
        remap_res = remap_slot(target, remap_slot_num, tool="0")
        print(f"SLOT REMAPPED: T0 -> slot {remap_res['slot']} ({remap_res['selection']['color']} {remap_res['selection']['material']})", flush=True)
    g=Path(resolve_print_target(target)).expanduser().resolve()
    job=None; job_path=None
    if g.name=="JOB.json":
        job_path=g; job=json.loads(g.read_text())
        if not job.get("gcode"):
            raise RuntimeError("stale color artifact: reslice current JOB before launch")
        g=Path(job["gcode"]).expanduser().resolve()
    else:
        # Slices live at <job>/sliced/orca/<uuid>/plate_1.gcode (four levels). Search every
        # nearby ancestor so an older slice is always bound to its JOB and the stale guard runs.
        # Stop at the temp root too. This profile sets TMPDIR inside itself, so a fixture
        # such as <scratch>/tmpXXX/x.gcode otherwise climbs into the real profile JOB.json.
        temp_root = Path(os.environ.get("TMPDIR") or "/tmp").resolve()
        for parent in list(g.parents)[:5]:
            if parent in (Path("/"), temp_root) or temp_root in parent.parents:
                break
            if (parent/"JOB.json").is_file():
                job_path=parent/"JOB.json"; job=json.loads(job_path.read_text()); break
    cur = Path(job["gcode"]).expanduser().resolve() if isinstance(job, dict) and job.get("gcode") else None
    if (cur and cur.parent != g.parent and cur.is_file() and isinstance(job, dict)
            and not job.get("derived_plates")):
        # A file from an earlier slice of this job (2026-10-10: the bot reran the pre-reslice name).
        raise SystemExit(f"REFUSED: {g.name} is an older slice of this job. The current slice is {cur}; "
                         f"run print-now {job_path} instead. Nothing uploaded or started.")
    require_current_artifact(job, g)
    if not g.is_file(): raise SystemExit(f"REFUSED: no G-code at {g}")
    try:
        if isinstance(job, dict): printers.check_job_model(job)
    except printers.JobModelMismatch as exc:
        raise SystemExit(f"REFUSED: {exc}. Reslice it for the configured printer; nothing uploaded or started.")
    adapter = printers.current()
    _timing(job_path, 'print_now_start')
    # accept_default_z_offset is kept for CLI compatibility only. With display_off=0 ZMod applies the
    # native screen zProbeOffset, which is the correct calibrated value; no Z_OFFSET in START_PRINT is fine.
    st=printer_state.load()
    if getattr(adapter, 'transport', None) == 'octoprint':
        return _print_octoprint(adapter, st, g, job, job_path, wait_s, reprint)
    state,fn,busy=_require_idle(st)
    mesh=mesh_readiness(st)
    if mesh["problem"]:
        raise SystemExit(f"REFUSED: {mesh['problem']}. Nothing uploaded or started.")
    try:
        # Pre-upload checks. Report them as one REFUSED line so print_next releases the plate's
        # claim and the operator can retry after fixing the cause (a traceback kept it held).
        verify_gcode(g)
        selection = job.get("filament_selection") if isinstance(job, dict) else None
        _timing(job_path, 'preflight_verify_done')
        if adapter.multi_slot:
            embedded_mapping = printers.current().native.embedded_mapping
            normalize_mapping = printers.current().native.normalize_mapping
            raw_mapping = embedded_mapping(g.read_text(), st)
            mapping = normalize_mapping(raw_mapping, st) if raw_mapping is not None else None
        else:
            _refuse_multicolor(g.read_text())
            mapping = None
    except (RuntimeError, MultiColorUnsupported) as exc:
        raise SystemExit("REFUSED: " + " ".join(str(exc).split()) + ". Nothing uploaded or started.")
    if mapping is not None:
        return print_mapped_native(g, st, mesh, mapping, job, job_path, wait_s, reprint)
    try:
        _verify_filament_readiness(st, selection)
    except RuntimeError as exc:
        if adapter.multi_slot: raise
        raise SystemExit("REFUSED: " + " ".join(str(exc).split()) + ". Nothing uploaded or started.")
    sha=file_sha256(g); remote=remote_name(g, sha, job)
    text=g.read_text(errors="ignore")
    if mesh.get('display_off') in (0, 1):
        try:
            validate_saved_mesh_start(text, mesh)
        except RuntimeError as exc:
            raise SystemExit(f'REFUSED: {exc}. Reslice with the reviewed machine profile; nothing uploaded.') from exc
    if mesh.get('display_off') == 0:
        try:
            validate_native_start(read_stock_detail(st), selection)
        except Exception as exc:
            raise SystemExit(f'REFUSED: native preflight: {exc}. Nothing uploaded or started.') from exc
    lease=launch_state.acquire(_printer_key(st), job_json=job_path, gcode_sha256=sha, remote=remote, observed_file=fn)
    try:
        _require_idle(st)  # again inside the lease: the first read may predate another job's start
        if job_path is not None: _consume_approval(job_path, job, g, reprint=reprint)
        _timing(job_path, 'upload_begin')
        r=subprocess.run([str(MOONRAKERCTL),"upload",str(g),remote,sha],text=True,capture_output=True,timeout=300)
        if r.returncode: raise SystemExit(f"UPLOAD FAILED: {(r.stderr or r.stdout)[-500:]}")
        try:
            up_json=json.loads(r.stdout)
        except Exception as e:
            raise SystemExit(f"UPLOAD FAILED: malformed upload response: {e}")
        res=up_json.get("result", up_json) if isinstance(up_json, dict) else {}
        item=res.get("item") if isinstance(res, dict) else None
        if not isinstance(item, dict) or item.get("root")!="gcodes" or item.get("path")!=remote:
            raise SystemExit(f"UPLOAD FAILED: upload target mismatch or invalid response: {up_json}")
        # Moonraker verified the SHA256 sent with the upload (mismatch = HTTP 400 = UPLOAD FAILED above).
        _timing(job_path, 'upload_verified')
        try: claim_start_announcement(remote)
        except OSError as e: print(f"WARNING: start announcement claim failed, listener may also post: {e}")
        _announce_print_name(st, job, gcode=g)
        _publish_job_card(st, job, remote, text)
    except BaseException:
        lease.release()  # nothing was sent to start
        raise
    lease.submitting()
    try:
        for command in adapter.moonraker_start_commands(remote):
            _mr(st, command["path"], data=command["body"])
    except BaseException as exc:
        lease.uncertain(f"start request did not complete: {exc!r}")
        raise
    _timing(job_path, 'start_submitted')
    print("START REQUEST ACCEPTED " + json.dumps({"file": remote, "printing_confirmed": False}), flush=True)
    method="print/start"; state=_wait_printing(st,remote,wait_s)
    readback = None
    try:
        snap = _mr(st, "/printer/objects/query?print_stats&extruder&heater_bed&virtual_sdcard&idle_timeout&webhooks&toolhead")
        readback = classify_start_readback(snap)
    except Exception:
        pass
    if state!="printing" and not (readback and readback.outcome == "printing"):
        if readback and readback.outcome == "preparing":
            out = {"started": True, "accepted": True, "phase": "preparing", "outcome": "preparing", "file": remote,
                   "message": readback.message}
            lease.release()  # accepted; the busy printer now refuses another start
            print("START SUBMITTED; AWAITING CONFIRMATION " + json.dumps(out), flush=True)
            return out
        # Do not retry a still-running preparation sequence or call it a failure.
        pending_state,pending_file,pending_busy=_printer_phase(st)
        if pending_busy == "Printing" and pending_state not in ("error", "cancelled"):
            # Keep the claim: the start card is already posted and the listener would
            # otherwise post a duplicate when preparation reaches "printing".
            out = {"started": True, "accepted": True, "phase": "preparing", "outcome": "preparing", "file": remote,
                   "message": "printer accepted the job and is preparing (heating/loading); not a failure; do not resubmit"}
            lease.release()
            print("START SUBMITTED; AWAITING CONFIRMATION " + json.dumps(out), flush=True)
            return out
        # An unreadable/ambiguous delayed readback after the one start request is
        # not evidence of rejection. Preserve the claim and report uncertainty.
        if pending_state not in ("error", "cancelled") and pending_file in (None, ""):
            out = {"started": False, "accepted": True, "phase": "awaiting_confirmation", "outcome": "unconfirmed", "file": remote,
                   "message": "submitted, awaiting confirmation"}
            lease.uncertain("submitted, awaiting confirmation")  # reconciled when the printer reports this file
            print("START SUBMITTED; AWAITING CONFIRMATION " + json.dumps(out), flush=True)
            return out
    if state!="printing" and not (readback and readback.outcome == "printing"):
        release_start_announcement(remote)
        lease.release()  # the printer reports the start failed
        fail_msg = readback.message if (readback and readback.outcome == "failed") else f"printer state is {state!r} after {method}"
        raise SystemExit(f"START FAILED: {fail_msg}. Not retrying; check the printer screen.")
    lease.release()  # printing; the busy printer now refuses another start
    record_path=None
    try: record_path=record_start(g, remote)
    except Exception as e: print(f"WARNING: Obsidian start note failed: {e}")
    if not isinstance(record_path,(str,os.PathLike)): record_path=None
    elif record_path is not None: record_path=str(record_path)
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    est=gcode_estimate_s(text); m=re.search(r'^; total filament used \[g\] = ([\d.]+)',text,re.M)
    warm=5*60; now=datetime.now(ZoneInfo(TIMEZONE))
    title=(job or {}).get("title") or g.stem
    print_no=_assign_print_number(job_path)
    out={"started":True,"title":title,"print_number":print_no,"file":remote,"method":method,
         "phase":"executing","outcome":"printing","message":(readback.message if readback else "printing"),
         "extrusion_confirmed":bool(readback and (readback.evidence or {}).get("extrusion_confirmed")),
         "print_time":human_dur(est) if est else None,"filament_g":round(float(m.group(1))) if m else None,
         "finish_et":(now+timedelta(seconds=(est or 0)+warm)).strftime("%-I:%M %p %a") if est else None}
    settings=start_print_settings(text); z_notice=z_offset_notice(text)
    try: hardware=st.get("printer","Klipper")+", "+printer_state.summary(st)
    except (KeyError, TypeError): hardware=None
    out["z_offset_notice"]=z_notice; out["record_path"]=record_path
    live=live_snapshot(st); out["live"]=live
    screen_z=mesh.get("z_offset_mm"); out["screen_z_offset_mm"]=screen_z; out["native_screen_z_offset_mm"]=screen_z_offset(st); out["bed_mesh"]=mesh
    out["details"]=job_details(out, hardware=st, selection=selection, settings=settings,
                               record_path=record_path, z_notice=z_notice, live=live,
                               screen_z=screen_z, mesh=mesh, printer=st.get("printer") or (None if adapter.multi_slot else adapter.label),
                               nozzle_mm=(st.get("nozzle") or {}).get("diameter_mm"), calibration=adapter.has_native_screen)
    print("PRINT STARTED " + json.dumps(out))
    print(out["details"])
    return out


def main():
    try:
        _main()
    except MultiColorUnsupported as exc:
        raise SystemExit("REFUSED: " + " ".join(str(exc).split()) + ". Nothing sliced, uploaded or started.")


def _main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch")
    f.add_argument("url")
    f.add_argument("--intake-id", help="fresh Discord request identity; isolates history")
    f.add_argument("--inspect-only", action="store_true", help="download selected geometry and audit without slicing approval or slicing")
    f.add_argument("--select", type=int)
    f.add_argument("--approved", action="store_true", help="the operator approved exactly the selected source and any supports")
    f.add_argument("--brim", type=int, default=None, help="override brim width mm (e.g. 0 to disable)")
    f.add_argument("--printer", choices=printers.MODELS, help="printer model this job is prepared for (default: [printer] model)")
    f.add_argument("--filament-slot", type=int, help="AD5X IFS feeder slot (1-4)")
    f.add_argument("--filament-color", type=str, help="filament color matching IFS slot")
    f.add_argument("--filament-material", type=str, help="filament material matching IFS slot")
    f.add_argument("--filament-spoolman-id", "--spoolman-id", dest="filament_spoolman_id", type=int, default=None, help="Spoolman spool ID")
    s = sub.add_parser("slice")
    s.add_argument("job_json")
    s.add_argument("--approved", action="store_true")
    s.add_argument("--printer", choices=printers.MODELS, help="printer model to slice this job for (recorded on the job)")
    s.add_argument("--brim", type=int, default=None, help="override brim width mm (e.g. 0 to disable)")
    s.add_argument("--separate", action="store_true", help="multi-plate source: slice one G-code per plate instead of merging all parts onto one bed")
    filament_select=sub.add_parser("filament-select", help="record a user-confirmed slot/color choice in a model JOB.json")
    filament_select.add_argument("job_json")
    filament_select.add_argument("--slot", type=int, required=True)
    filament_select.add_argument("--color", required=True)
    filament_select.add_argument("--material", required=True)
    filament_select.add_argument("--spoolman-id", type=int, required=True)
    cp = sub.add_parser('color-plan', help='offline per-part and source-filament selection questions')
    cp.add_argument('job_json')
    cs = sub.add_parser('color-select', help='save source-bound color decisions; no slicing or start')
    cs.add_argument('job_json')
    cs.add_argument('decisions_json')
    filament_status = sub.add_parser("filament-status", help="read-only labeled AD5X filament telemetry")
    final=sub.add_parser("finalize-gcode", help="finalize reviewed multicolor G-code metadata and native checksum; no upload")
    final.add_argument("gcode")
    final.add_argument("--tool-map", required=True, help="JSON tool-to-physical-slot mapping")
    v=sub.add_parser("verify-gcode"); v.add_argument("gcode")
    remap=sub.add_parser("remap-slot", help="remap single-color job or G-code tool to another physical IFS slot")
    remap.add_argument("target", help="JOB.json or plate.gcode")
    remap.add_argument("--slot", type=int, required=True, help="physical IFS slot (1-4)")
    remap.add_argument("--tool", type=str, default="0", help="logical tool index (default: 0)")
    pn=sub.add_parser("print-now",help="bed confirmed clear: verify, upload, start, confirm, record (G-code or JOB.json)"); pn.add_argument("target")
    pn.add_argument("--accept-spool-change",action="store_true",help="operator confirmed printing with the spool now in a slot that changed since slicing (same material family only)")
    pn.add_argument("--remap-slot", "--slot", dest="remap_slot", type=int, default=None, help="remap T0 to another physical IFS slot (1-4) without physical spool changes")
    for x in (pn,f,s):
        x.add_argument("--accept-default-z-offset",action="store_true",help="the operator accepted the printer default Z offset for this start")
        x.add_argument("--reprint",action="store_true",help="the operator gave a NEW bed-clear approval to reprint the same G-code")
    rl=sub.add_parser("reconcile-launch",help="show, or after checking the printer settle, an uncertain start; never resubmits")
    outcome=rl.add_mutually_exclusive_group()
    outcome.add_argument("--started",action="store_true",help="the operator saw that start reach the printer")
    outcome.add_argument("--not-started",action="store_true",help="the operator saw that the printer never started it")
    rt=sub.add_parser("routine-receipt",help="immediate typical-duration receipt for a calibration routine (no printer contact)"); rt.add_argument("routine",choices=sorted(ROUTINE_TYPICAL_MIN))
    sub.add_parser("routine-status",help="read-only accepted/running/completed phase from Klipper idle_timeout")
    for x in (f,s):
        x.add_argument("--print-now",action="store_true",help="bed confirmed clear: start the print right after slicing")
        x.add_argument('--color-mode', choices=['per_part', 'single_color'])
        x.add_argument('--color-slot', type=int, action='append', help='slot for each displayed part/filament question, in order')
    ma=sub.add_parser("audit",help="read-only mesh topology audit; no repair or slicing")
    ma.add_argument("source")
    m=sub.add_parser("plate-manifest",help="read-only source plate inventory; does not authorize slicing")
    m.add_argument("source_3mf")
    r=sub.add_parser("record-start",help="after a confirmed print start: write the dated Obsidian note"); r.add_argument("gcode"); r.add_argument("--moonraker-name"); r.add_argument("--started-at",help="ISO UTC, default now")
    prep = sub.add_parser("prepare-first-layer", help="offline: generate and slice a one-layer test; never upload or start")
    prep.add_argument("--slot", type=int, required=True)
    prep.add_argument("--color", required=True)
    prep.add_argument("--material", required=True)
    prep.add_argument("--spoolman-id", type=int, required=True)
    prep.add_argument("--slicer", choices=["orca"], default="orca", help="canonical Orca engine; no legacy fallback")
    a=ap.parse_args();
    if a.cmd in ('color-plan', 'color-select'):
        from .source_colors import inventory, prompts, validate_decisions
        path = Path(a.job_json)
        job = json.loads(path.read_text())
        manifest = inventory(job['downloaded_file'])
        job['color_manifest'], job['color_prompts'] = manifest, prompts(manifest)
        if a.cmd == 'color-select':
            set_color_decisions(job, validate_decisions(manifest, json.loads(Path(a.decisions_json).read_text()),
                                                        printer_state.load(), resolve_filament_selection))
        write_job(path.parent, job)
        print(json.dumps({'manifest': manifest, 'questions': prompts(manifest),
                          'decisions': job.get('color_decisions')}, indent=2))
        return
    if a.cmd=="audit":
        print(json.dumps(mesh_audit(a.source), indent=2)); return
    if a.cmd=="prepare-first-layer":
        prepare_first_layer(a.slot, a.color, a.material, a.spoolman_id, slicer=a.slicer); return
    if a.cmd=="plate-manifest":
        print(json.dumps(plate_manifest(a.source_3mf),indent=2)); return
    if a.cmd=="record-start":
        record_start(a.gcode, a.moonraker_name, a.started_at); return
    if a.cmd=="filament-select":
        selection = resolve_filament_selection(printer_state.load(), vars(a))
        selection = save_filament_selection(a.job_json, selection)
        print("FILAMENT SELECTION RECORDED " + json.dumps(selection))
        return
    if a.cmd=="finalize-gcode":
        finalize = printers.current().native.finalize
        st = printer_state.load()
        ok, probs, _ = printer_state.check(st)
        if not ok:
            raise RuntimeError('live hardware check failed: ' + '; '.join(probs))
        path = Path(a.gcode)
        backup = path.with_suffix(path.suffix + '.before-finalize')
        if not backup.exists():
            shutil.copy2(path, backup)
        sha = finalize(path, None, st, json.loads(Path(a.tool_map).read_text()))
        verify_gcode(path)
        print('FINALIZED ' + json.dumps({'path': str(path), 'sha256': sha, 'backup': str(backup)}))
        return
    if a.cmd=="verify-gcode":
        verify_gcode(a.gcode); return
    if a.cmd=="filament-status":
        st = printer_state.load()
        print(json.dumps(read_filament_telemetry(st), indent=2))
        return
    if a.cmd=="reconcile-launch":
        key=_printer_key(printer_state.load())
        if a.started or a.not_started:
            launch_state.resolve(key, "started" if a.started else "not_started")
        else:
            print(json.dumps(launch_state.pending(key), indent=2))
        return
    if a.cmd=="routine-receipt":
        print(routine_receipt(a.routine)); return
    if a.cmd=="routine-status":
        print(routine_phase(printer_state.load())); return
    pn_kw=dict(accept_default_z_offset=getattr(a,"accept_default_z_offset",False),reprint=getattr(a,"reprint",False),remap_slot_num=getattr(a,"remap_slot",None))
    if a.cmd=="remap-slot":
        res = remap_slot(a.target, a.slot, a.tool)
        print("SLOT REMAPPED " + json.dumps(res, indent=2))
        return
    if a.cmd=="print-now":
        if a.accept_spool_change:
            os.environ["PRINT_ACCEPT_SPOOL_CHANGE"] = "1"
            print("SPOOL CHANGE ACCEPTED: slots may hold a different spool than at slicing (same material family)", flush=True)
        print_now(a.target, **pn_kw); return
    st=printer_state.load()
    if a.cmd=="slice":
        if not a.approved: raise SystemExit("REFUSED: explicit slicing approval required")
        if a.printer:
            _job = json.loads(Path(a.job_json).read_text()); _job["printer_model"] = a.printer
            write_job(Path(a.job_json).parent, _job)
        if not intake_color_choices(a.job_json, st, a.color_mode, a.color_slot, sys.stdin.isatty()):
            return
        if a.separate:
            _job = json.loads(Path(a.job_json).read_text()); _job["plate_mode"] = "separate"
            write_job(Path(a.job_json).parent, _job)
        out=slice_job(a.job_json, brim_override=a.brim)
        if a.print_now: print_now(out, **pn_kw)
        return
    ok,probs,_=printer_state.check(st)
    if not ok: raise SystemExit("REFUSED: live hardware check failed:\n  " + "\n  ".join(probs))
    selection = None
    filament_specified = (a.filament_slot is not None or a.filament_color is not None or
                          a.filament_material is not None or a.filament_spoolman_id is not None)
    if filament_specified:
        try:
            selection = resolve_filament_selection(st, {
                "slot": a.filament_slot, "color": a.filament_color,
                "material": a.filament_material, "spoolman_id": a.filament_spoolman_id,
            })
        except ValueError as exc:
            raise SystemExit(f"REFUSED: invalid filament selection: {exc}")
    p=urllib.parse.urlparse(a.url); job=maker_inventory(a.url) if "makerworld.com" in p.netloc else printables_inventory(a.url) if "printables.com" in p.netloc else (_ for _ in ()).throw(ValueError("supported sites: MakerWorld, Printables"))
    if not job.get("sources"):
        raise SystemExit("REFUSED: source inventory contains no selectable files or profiles")
    job["url"],job["hardware"],job["fetched_at"]=a.url,st,time.strftime("%Y-%m-%dT%H:%M:%S%z")
    if a.printer: job["printer_model"]=a.printer
    for n,x in enumerate(job["sources"],1): x["n"]=n
    if selection is not None:
        job["filament_selection"] = selection
    folder=intake_folder(ARCHIVE, job["slug"], a.intake_id)
    if a.intake_id:
        job['intake_id'] = a.intake_id
        if (folder/'JOB.json').exists():
            previous = json.loads((folder/'JOB.json').read_text())
            if (previous.get('url') != a.url or previous.get('intake_id') != a.intake_id
                    or previous.get('downloaded_file') or previous.get('gcode')
                    or previous.get('color_decisions') or previous.get('approvals')):
                raise SystemExit('REFUSED: intake already has a prepared job; resume its exact JOB.json explicitly, never overwrite history')
    folder.mkdir(parents=True,exist_ok=True); write_job(folder,job); show(job)
    if a.select is None:
        print(f"\nSTOPPED: inventory saved at {folder/'JOB.json'}. Choose --select N; slicing requires --approved."); return
    if a.inspect_only and (a.approved or a.print_now):
        raise SystemExit('REFUSED: inspect-only cannot approve slicing or printing')
    if not a.approved and not a.inspect_only: raise SystemExit("REFUSED: --select requires the operator's explicit --approved slicing authorization; use --inspect-only for preparation")
    if a.select<1 or a.select>len(job["sources"]): raise SystemExit("invalid --select")
    if a.print_now and printers.get(job.get("printer_model") or printers.current().key).multi_slot and not job.get("filament_selection"):
        raise SystemExit("REFUSED: --print-now requires explicit filament selection (--filament-slot, --filament-color, --filament-material, or use 'filament-select' command)")
    chosen=job["sources"][a.select-1]
    # Site profile settings are never imported. The selected 3MF/STL supplies geometry
    # only, and is resliced to the live verified local nozzle and spool. Preserve the
    # metadata as a warning so a PLA CF or 0.4 Bambu preset cannot masquerade as local
    # compatibility.
    if job["site"]=="makerworld":
        notes=[]
        if chosen.get("site_nozzle_mm") not in (None, st["nozzle"]["diameter_mm"]): notes.append(f"site profile declares {chosen['site_nozzle_mm']} mm")
        site_mats=set(chosen.get("materials") or [])
        if site_mats and st["spool"]["material"] not in site_mats: notes.append(f"site profile material is {', '.join(sorted(site_mats))}; loaded material is {st['spool']['material']}")
        if notes: print("SITE PROFILE NOT REUSED: " + "; ".join(notes) + ". Reslicing geometry to live local state.")
    srcdir=folder/"source"; path=maker_download(job,chosen,srcdir) if job["site"]=="makerworld" else printables_download(job,chosen,srcdir)
    job["selected_source"],job["downloaded_file"],job["audit"]=chosen,str(path),audit(path); write_job(folder,job)
    print("\nAUDIT: "+json.dumps(job["audit"],indent=2))
    if a.inspect_only:
        print('SOURCE_INSPECTED ' + json.dumps({'job_json': str(folder/'JOB.json'), 'sliced': False, 'uploaded': False, 'started': False}))
        return
    if not intake_color_choices(folder/'JOB.json', st, a.color_mode, a.color_slot, sys.stdin.isatty()):
        return
    out=slice_job(folder/"JOB.json", brim_override=a.brim)
    if a.print_now: print_now(out, **pn_kw)
if __name__=="__main__": main()
