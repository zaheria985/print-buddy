"""Sovol vendor profile: Sovol printers run stock Klipper, so this is the Klipper profile under the Sovol name."""
from ..klipper import VENDOR as KLIPPER

VENDOR = KLIPPER.derive(key="sovol", label="Sovol")
