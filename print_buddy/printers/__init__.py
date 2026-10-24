"""Printer profiles, vendor first, then printer. See base.py for the contract and docs/PRINTER-PROFILES.md.

    from print_buddy import printers
    printers.current().build_volume()
    printers.MODELS["flashforge.ad5x"].defaults()
    printers.VENDORS["bambulab"].delivery

Every vendor package (flashforge/, klipper/, sovol/, octoprint/, bambulab/) is scanned. Its __init__ defines
VENDOR; each module in it that defines MODEL is registered under its "vendor.printer" key and inherits VENDOR.
Adding a printer is one new file in its vendor package plus its templates.
"""
from __future__ import annotations

import importlib
import pkgutil

from dataclasses import replace

from .base import MultiColorUnsupported, PrinterAdapter, PrinterModel, VendorProfile

# Jobs written before the model was recorded were sliced for the AD5X.
DEFAULT_MODEL = "flashforge.ad5x"


# Other spellings of a vendor key that config may use. "bambu" reads as "bambulab".
VENDOR_ALIASES = {"bambu": "bambulab", "bambu_lab": "bambulab", "bambulabs": "bambulab"}


def _discover():
    vendors, found = {}, {}
    for brand in pkgutil.iter_modules(__path__):
        if not brand.ispkg:
            continue
        package = importlib.import_module(f"{__name__}.{brand.name}")
        vendor = getattr(package, "VENDOR", None)
        if not isinstance(vendor, VendorProfile):
            raise RuntimeError(f"{package.__name__}: a vendor package must define VENDOR = VendorProfile(...)")
        if vendor.key != brand.name:
            raise RuntimeError(f"{package.__name__}: VENDOR key {vendor.key!r} must be {brand.name}")
        vendors[vendor.key] = vendor
        for module in pkgutil.iter_modules(package.__path__):
            if module.ispkg:
                continue
            model = getattr(importlib.import_module(f"{package.__name__}.{module.name}"), "MODEL", None)
            if model is None:
                continue
            if model.key != f"{brand.name}.{module.name}":
                raise RuntimeError(f"{package.__name__}.{module.name}: MODEL key {model.key!r} must be "
                                   f"{brand.name}.{module.name}")
            if model.vendor is None:
                model = replace(model, vendor=vendor)  # a printer module that does not name its vendor
            elif model.vendor != vendor:
                raise RuntimeError(f"{package.__name__}.{module.name}: MODEL vendor {model.vendor.key!r} must be "
                                   f"the {brand.name} package VENDOR")
            found[model.key] = model
    return dict(sorted(vendors.items())), dict(sorted(found.items()))


VENDORS: dict[str, VendorProfile]
MODELS: dict[str, PrinterModel]
VENDORS, MODELS = _discover()


def vendor_key(name):
    """Canonical vendor key for a configured vendor name or alias."""
    name = str(name or "").strip().lower()
    return VENDOR_ALIASES.get(name, name)


def resolve_key(model, vendor=None):
    """The registered "vendor.printer" key for a [printer] model value, with an optional [printer] vendor.

    Accepts the full key ("flashforge.ad5x", what every existing config has), a vendor alias in it
    ("bambu.a1"), or the printer part alone ("ad5x"), taken with `vendor` or, when it names exactly one
    printer, on its own. A vendor that disagrees with a full key is refused rather than guessed.
    """
    raw = str(model or "").strip()
    want_vendor = vendor_key(vendor) if vendor else None
    if want_vendor and want_vendor not in VENDORS:
        raise ValueError(f"[printer] vendor must be one of {', '.join(VENDORS)}, got {vendor!r}")
    if "." in raw:
        head, tail = raw.split(".", 1)
        key = f"{vendor_key(head)}.{tail}"
        if want_vendor and vendor_key(head) != want_vendor:
            raise ValueError(f"[printer] vendor {vendor!r} does not match model {raw!r}")
    elif want_vendor:
        key = f"{want_vendor}.{raw}"
    else:
        matches = [k for k, m in MODELS.items() if m.printer_key == raw]
        if len(matches) > 1:
            raise ValueError(f"[printer] model {raw!r} exists for several vendors ({', '.join(matches)}); "
                             "set [printer] vendor or use the full vendor.printer key")
        key = matches[0] if matches else raw
    if key not in MODELS:
        raise ValueError(f"[printer] model must be one of {', '.join(MODELS)}, got {model!r}")
    return key


def model(key):
    """The PrinterModel registered under a "brand.model" key."""
    if key not in MODELS:
        raise ValueError(f"unknown printer model {key!r}; expected one of {', '.join(MODELS)}")
    return MODELS[key]


def get(key, config=None):
    """Adapter for a model key, built on `config` (default: the process settings)."""
    selected = model(key)
    return selected.adapter(config, selected)


def current(config=None):
    """The adapter selected by [printer] model."""
    from .. import settings
    config = config or settings.current()
    return get(config["printer"]["model"], config)


class JobModelMismatch(RuntimeError):
    """A job was prepared for a different printer model than the one configured."""


# JOB.json written by 2.0.2 and 2.0.3 records printer_family instead of printer_model.
LEGACY_FAMILY = {"flashforge_zmod": "flashforge.ad5x", "sovol_klipper": "sovol.sv06",
                 "generic_klipper": "klipper.generic"}


def job_model(job):
    """Model a JOB.json was prepared for. Jobs older than either field are flashforge.ad5x."""
    if not isinstance(job, dict):
        return DEFAULT_MODEL
    return job.get("printer_model") or LEGACY_FAMILY.get(job.get("printer_family")) or DEFAULT_MODEL


def check_job_model(job, config=None):
    """Raise JobModelMismatch unless the job's model is the configured one."""
    want = current(config).key
    have = job_model(job)
    if have != want:
        raise JobModelMismatch(f"this job was prepared for {have}, but the configured printer model is {want}")
