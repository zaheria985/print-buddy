"""What happens to a validated slice: the post-slice choices offered by the printer's vendor profile.

Offline and printer-neutral. Nothing here talks to Discord or a printer: prepared_intake asks the
question and sends the messages this module builds. Decisions stay separate, as issue #123 asks:

  slicer          [printer] slicer, chosen before slicing (orca_adapter.slicer_choice)
  delivery        discord / bambu_handoff, any number, each at most once per slice
  print           printer, only through the per-job bed-clear Start button (print_next.start)

A delivery never grants, consumes or implies print authorization. Every delivered file is first bound
to the exact JOB.json and the SHA256 recorded when the slice returned; a changed or unknown file is
refused, not sent.
"""
from __future__ import annotations

import hashlib
import zipfile
from dataclasses import dataclass
from pathlib import Path

from .. import printers, settings
from ..printers.base import start_code_problems

PRINTER, DISCORD, BAMBU_HANDOFF = "printer", "discord", "bambu_handoff"
LABELS = {
    DISCORD: "Send the files here",
    BAMBU_HANDOFF: "Package for Bambu (no upload)",
}
# "don't print" keeps link_intake.ask from adding its own Cancel, which would discard the slice receipt.
DONE_LABEL = "Done, don't print"
# Already-compressed formats gain nothing from a zip.
_COMPRESSED = (".3mf", ".zip")


class DeliveryRefused(RuntimeError):
    """A file cannot be delivered: unknown, changed since slicing, or wrong for the target printer."""


def current_model(config=None):
    """The configured PrinterModel, with its vendor profile."""
    config = config or settings.current()
    return printers.MODELS[config["printer"]["model"]]


def methods(config=None):
    """Delivery methods enabled for the configured printer, in vendor order ([delivery] methods)."""
    config = config or settings.current()
    model = current_model(config)
    enabled = config.get("delivery", {}).get("methods") or list(model.vendor.default_delivery)
    return tuple(m for m in model.vendor.delivery if m in enabled)


def only_printer(enabled):
    """True when the profile offers nothing but printing: the post-slice question stays the Start button."""
    return tuple(enabled) == (PRINTER,)


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def bound_artifacts(job, gcodes, recorded):
    """The sliced files, refused unless each is in this JOB.json and unchanged since the slice returned.

    recorded maps each G-code path to the SHA256 taken when slicing finished.
    """
    listed = {Path(job.get("gcode") or "").resolve()} if job.get("gcode") else set()
    listed |= {Path(row["gcode"]).resolve() for row in job.get("derived_plates") or [] if row.get("gcode")}
    out = []
    for gcode in gcodes:
        path = Path(gcode)
        if path.resolve() not in listed:
            raise DeliveryRefused(f"{path.name} is not a G-code of this job; nothing was sent")
        if not path.is_file():
            raise DeliveryRefused(f"{path.name} is gone; reslice before delivery")
        if recorded.get(str(gcode)) != sha256(path):
            raise DeliveryRefused(f"{path.name} changed after slicing; reslice before delivery")
        out.append(path)
    return out


@dataclass
class Attachment:
    source: Path  # the sliced or packaged file
    file: Path | None  # what is attached: the source, a zip of it, or None when too big
    size: int  # bytes of the source
    sent_size: int | None = None  # bytes of `file`
    mode: str = "attach"  # attach | zip | too_big


def _fmt_mb(size):
    return f"{size / 1_000_000:.1f} MB"


def upload_limit(config=None):
    """Largest Discord attachment in bytes ([discord] upload_max_bytes)."""
    return int((config or settings.current())["discord"]["upload_max_bytes"])


def discord_plan(files, limit, workdir):
    """How each file reaches Discord under the upload limit (bytes): as is, zipped, or not at all.

    G-code compresses well, so a file over the limit is deflated into workdir and sent when the zip
    fits. A file still too big is not sent; the message names its size and where it stays.
    """
    plan = []
    for source in map(Path, files):
        size = source.stat().st_size
        if size <= limit:
            plan.append(Attachment(source, source, size, size, "attach"))
            continue
        if source.suffix.lower() in _COMPRESSED:
            plan.append(Attachment(source, None, size, None, "too_big"))
            continue
        workdir = Path(workdir)
        workdir.mkdir(parents=True, exist_ok=True)
        packed = workdir / (source.name + ".zip")
        with zipfile.ZipFile(packed, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            archive.write(source, source.name)
        packed_size = packed.stat().st_size
        if packed_size <= limit:
            plan.append(Attachment(source, packed, size, packed_size, "zip"))
        else:
            packed.unlink()
            plan.append(Attachment(source, None, size, packed_size, "too_big"))
    return plan


def discord_message(item, limit, label=None):
    """One Discord message for one planned attachment. MEDIA: lines attach a file through the gateway."""
    name = label or item.source.name
    if item.mode == "attach":
        return f"{name} ({_fmt_mb(item.size)})\nMEDIA:{item.file}"
    if item.mode == "zip":
        return (f"{name} is {_fmt_mb(item.size)}, over the {_fmt_mb(limit)} Discord upload limit; "
                f"sent zipped ({_fmt_mb(item.sent_size)}).\nMEDIA:{item.file}")
    zipped = f" and {_fmt_mb(item.sent_size)} zipped" if item.sent_size else ""
    return (f"{name} is {_fmt_mb(item.size)}{zipped}, over the {_fmt_mb(limit)} Discord upload limit, so it "
            f"was not sent. It stays on the Print Buddy host at {item.source}.")


# ----------------------------------------------------------------------------- Bambu handoff

_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
 <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
 <Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>
 <Default Extension="gcode" ContentType="text/x.gcode"/>
</Types>
"""
_RELS = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Target="/3D/3dmodel.model" Id="rel-1" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>
</Relationships>
"""
_MODEL = """<?xml version="1.0" encoding="UTF-8"?>
<model unit="millimeter" xml:lang="en-US" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">
 <metadata name="Application">Print Buddy</metadata>
 <resources/>
 <build/>
</model>
"""
# Fixed timestamp: the same G-code always packs to the same bytes.
_ZIP_TIME = (2020, 1, 1, 0, 0, 0)


def _slice_info(plate, nozzle_mm):
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<config>\n <header>\n'
            '  <header_item key="X-BBL-Client-Type" value="slicer"/>\n'
            '  <header_item key="X-Print-Buddy" value="offline handoff"/>\n </header>\n <plate>\n'
            f'  <metadata key="index" value="{int(plate)}"/>\n'
            f'  <metadata key="nozzle_diameters" value="{float(nozzle_mm):g}"/>\n </plate>\n</config>\n')


def bambu_package(gcode, target, plate=1, nozzle_mm=0.4):
    """Write a .gcode.3mf holding one plate's G-code, in the layout Bambu Studio exports.

    Metadata/plate_<n>.gcode with its MD5 in Metadata/plate_<n>.gcode.md5, a slice_info.config naming
    the plate, and an empty 3D model. Built offline from the validated G-code; not yet verified on
    Bambu hardware (see docs/PRINTER-PROFILES.md).
    """
    raw = Path(gcode).read_bytes()
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    entries = [
        ("[Content_Types].xml", _CONTENT_TYPES.encode()),
        ("_rels/.rels", _RELS.encode()),
        ("3D/3dmodel.model", _MODEL.encode()),
        (f"Metadata/plate_{int(plate)}.gcode", raw),
        (f"Metadata/plate_{int(plate)}.gcode.md5", hashlib.md5(raw).hexdigest().upper().encode()),
        ("Metadata/slice_info.config", _slice_info(plate, nozzle_mm).encode()),
    ]
    tmp = target.with_name(target.name + ".part")
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries:
            info = zipfile.ZipInfo(name, date_time=_ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data)
    tmp.replace(target)
    return target


def bambu_handoff(job, gcodes, config=None):
    """Package each validated plate as a .gcode.3mf for a Bambu Lab printer. Uploads and starts nothing.

    Refused unless the configured printer and the job's printer are Bambu Lab, and unless every
    G-code line is a plain G/M/T code: Klipper or ZMod start code never reaches Bambu hardware.
    """
    config = config or settings.current()
    model = current_model(config)
    if BAMBU_HANDOFF not in model.vendor.delivery:
        raise DeliveryRefused(f"{model.label} is not a Bambu Lab printer; no Bambu package was made")
    job_key = printers.job_model(job)
    if job_key != model.key:
        raise DeliveryRefused(f"this job was sliced for {job_key}, not {model.key}; no Bambu package was made")
    nozzle = config["printer"].get("nozzle_diameter_mm", 0.4)
    packages = []
    for index, gcode in enumerate(map(Path, gcodes), 1):
        problems = start_code_problems(gcode.read_text(errors="replace"), model.vendor)
        if problems:
            raise DeliveryRefused("; ".join(problems) + "; no Bambu package was made")
        target = gcode.parent / "bambu" / (gcode.name[:-len(".gcode")] if gcode.name.endswith(".gcode")
                                           else gcode.stem)
        packages.append(bambu_package(gcode, target.with_name(target.name + ".gcode.3mf"), index, nozzle))
    return packages


def handoff_note(model):
    """What a Bambu handoff does and does not do, for the Discord reply."""
    return (f"Nothing was uploaded to the {model.label} and no print was started: Print Buddy has no live "
            "connection to Bambu printers (no LAN, cloud or MQTT upload, status or print control). Open the "
            ".gcode.3mf in Bambu Studio or Bambu Handy, or copy it to the printer's SD card, and start it there.")


def files_for_discord(job, artifacts, config=None):
    """Files the Discord delivery sends: the vendor's primary format. Bambu Lab gets .gcode.3mf packages."""
    config = config or settings.current()
    model = current_model(config)
    if model.vendor.file_formats[0] == ".gcode.3mf":
        return bambu_handoff(job, artifacts, config)
    return list(artifacts)


def record(receipt, method, items, complete, detail=""):
    """Append one delivery attempt to the intake receipt.

    items: [{'source', 'sent', 'ok'}] per file. complete means nothing is left to retry: every file was
    delivered or reported as impossible (too big). A transient failure leaves the method on offer.
    """
    receipt.setdefault("deliveries", []).append({
        "method": method, "complete": bool(complete), "detail": detail,
        "files": [{**item, "source": str(item["source"]), "sent": str(item["sent"]) if item.get("sent") else None}
                  for item in items],
    })
    return receipt


def done(receipt, method):
    """True when this method already finished for this slice: it is not offered or delivered twice."""
    return any(d.get("method") == method and d.get("complete") for d in receipt.get("deliveries") or [])


def delivered(receipt, method):
    """Sources this method already delivered for this slice, so a retry sends only the rest."""
    return {f["source"] for d in receipt.get("deliveries") or [] if d.get("method") == method
            for f in d.get("files") or [] if f.get("ok")}
