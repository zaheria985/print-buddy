"""Sourced hardware facts and template provenance for Bambu Lab printers. Supplemental metadata only.

Connection, start code, slicers, delivery, defaults and registration are decided by VENDOR (bambulab/__init__.py)
and each MODEL; nothing here overrides them. These are the values the A1 guard enforces, each with its source:

  ORCA     OrcaSlicer's own Bambu Lab (BBL) vendor profiles at ORCA_RELEASE, read directly from that commit,
           or OrcaSlicer's output with those profiles. Only the values below are recorded; the profiles
           themselves are never copied into this repository (profiles.py generates them on the operator's
           machine from their own OrcaSlicer installation).
  BAMBU    Bambu Lab product and tech-spec pages. Not reachable from the sandbox that wrote these files; read
           from search-result extracts. Re-check against bambulab.com before relying on them.
  DERIVED  Computed from the ORCA G-code templates, not published by Bambu. Not a hardware certification.
verified=True means the value was read from its source directly; False means it must be re-checked.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Fact:
    value: object
    source: str
    verified: bool


# OrcaSlicer release whose BBL vendor profiles were checked: (tag, commit). A provenance reference only; the
# profiles (AGPL-3.0) are not in this repository. See docs/BAMBU-LAB.md "Provenance and licence".
ORCA_RELEASE = ("v2.4.2", "8500fcdccaa10b5099ac20d252af3a7c560046f1")
ORCA_PROFILE_ROOT = "resources/profiles/BBL"
# Print Buddy's only changes to the flattened vendor profiles, by kind. The shared post-slice guard allows only
# Normal Lift and no arc fitting; settings IDs let it match the effective settings Orca writes back.
ORCA_OVERRIDES = {
    "machine": {"z_hop_types": ["Normal Lift"]},
    # The plate is chosen explicitly: Orca's CLI otherwise resolves "Cool Plate", and the vendor start G-code's
    # Textured PEI nozzle offset branch depends on it. The guard refuses any other plate.
    "process": {"enable_arc_fitting": "0", "enable_prime_tower": "0", "brim_type": "outer_only",
                "curr_bed_type": "Textured PEI Plate"},
    "filament": {"filament_colour": ["#FFFFFF"], "filament_z_hop_types": ["Normal Lift"]},
}
SETTINGS_IDS = {"machine": "printer_settings_id", "process": "print_settings_id", "filament": "filament_settings_id"}

# Vendor-wide claims, documented here only; VENDOR holds the behaviour.
VENDOR_FACTS = {
    # Orca writes Bambu-dialect G-code for BBL profiles: "; FEATURE:" role tags and "; CHANGE_LAYER" layers.
    "gcode_dialect": Fact("bambu", "ORCA", True),
    # Bambu Studio, LAN and cloud jobs are .gcode.3mf. Whether an A1 runs Print Buddy's package, or a plain .gcode
    # from microSD, has not been tested.
    "native_job_format": Fact(".gcode.3mf", "BAMBU", False),
    "connection_types": Fact(("LAN (MQTT status, FTPS upload, access code)", "Bambu cloud"), "BAMBU", False),
}

A1 = {
    # printer_model as Orca writes it into G-code.
    "printer_model": Fact("Bambu Lab A1", "ORCA", True),
    "orca_profiles": Fact({"machine": "Bambu Lab A1 0.4 nozzle", "process": "0.20mm Standard @BBL A1",
                           "filament": "Generic PLA @BBL A1"}, "ORCA", True),
    # The vendor profiles Print Buddy was checked against: SHA-256 of each profile above with its inherits and
    # includes resolved (profiles.sha256), from an installed OrcaSlicer. Hashes only, no profile content. A
    # different OrcaSlicer or vendor profile version is refused until it has been checked and added here.
    # One checked vendor filament profile per base polymer (#264): slicing picks the one for the hardware record's
    # material and refuses a material that has none. PLA is the default profile above. Bambu's ideal materials only.
    "orca_filaments": Fact({"PLA": "Generic PLA @BBL A1", "PETG": "Generic PETG @BBL A1",
                            "TPU": "Generic TPU @BBL A1", "PVA": "Generic PVA @BBL A1"}, "ORCA", True),
    "orca_identity": Fact(({
        "label": "OrcaSlicer 2.4.2 (BBL vendor profiles 02.01.00.19)",
        "vendor_version": "02.01.00.19",
        "sha256": {"machine": "41a75dfaca7903b95f3a9bc81fb8d1a8e6684c449f151e74963119f959e50a72",
                   "process": "7cfbed1af4c0310589fab4857a86eb83b84bd5db71f7d34bbe2201933b9c9f3c",
                   "filament": "dc7ed678c86bc8fb2bb5e61ff8264ed211dc9e25b50badd650bdd9bccd10e81e"},
        # The other orca_filaments profiles, hashed the same way.
        "filaments": {"Generic PETG @BBL A1": "eb5c733468d29eb92ae2f0176b65880a6880e1ccc38ea91cd7de9e536fffc4d4",
                      "Generic TPU @BBL A1": "d30fd112cfbfdcc79e8946c6874354d20f41a72353e65566bf5888c1eaaea8c9",
                      "Generic PVA @BBL A1": "1974432a9cc01611daea0b34759fb3284ae1aa4d204114b3ead8ee2ded28dc80"},
    },), "ORCA", True),
    # printable_area 0x0..256x256 and printable_height 256; Bambu also publishes 256 x 256 x 256 mm.
    "bed_mm": Fact((256.0, 256.0), "ORCA", True),
    "max_print_height_mm": Fact(256.0, "ORCA", True),
    # The A1 profile empties the X1 family's corner exclusion: no printable-area exclusion.
    "bed_exclude_area": Fact((), "ORCA", True),
    # Extremes of the absolute XY moves in the vendor G-code (wiper X-48.2, cutter X267, purge lane Y-0.5/262.5),
    # plus the highest travel Z allowed above the print (z-hop and timelapse lift). Not hardware certification.
    "motion_envelope_mm": Fact((-48.2, -0.5, 267.0, 262.5), "DERIVED", False),
    "travel_z_headroom_mm": Fact(1.0, "DERIVED", False),
    # The vendor profile's default plate; the start G-code lowers the nozzle 0.02 mm only for this plate type.
    "default_plate": Fact("Textured PEI Plate", "ORCA", True),
    # Stock 0.4 mm stainless steel; 0.2, 0.6 and 0.8 mm hotends exist. Only 0.4 has a template here.
    "stock_nozzle": Fact((0.4, "stainless_steel"), "ORCA", True),
    "nozzle_sizes_mm": Fact((0.2, 0.4, 0.6, 0.8), "BAMBU", False),
    "max_hotend_c": Fact(300, "BAMBU", False),
    "max_bed_c": Fact(100, "BAMBU", False),
    # Bambu lists PLA, PETG, TPU and PVA as ideal; ABS, ASA, PC, PA, PET and CF/GF fills as not recommended
    # (open frame). CF/GF and glow fills need a hardened steel nozzle.
    "ideal_materials": Fact(("PLA", "PETG", "TPU", "PVA"), "BAMBU", False),
    "not_recommended_materials": Fact(("ABS", "ASA", "PC", "PA", "PET"), "BAMBU", False),
    # AMS Lite: four spool holders feeding one hotend (A1 Combo). Not driven by Print Buddy.
    "ams": Fact(("AMS Lite", 4), "BAMBU", False),
}


# P1S system profiles selected from the checked OrcaSlicer BBL index. The selected process and filament are the
# P1S machine profile's actual 0.20 mm / PLA defaults; the X1C profiles explicitly list P1S compatibility.
P1S = {
    "printer_model": Fact("Bambu Lab P1S", "ORCA", True),
    "orca_profiles": Fact({"machine": "Bambu Lab P1S 0.4 nozzle", "process": "0.20mm Standard @BBL X1C",
                           "filament": "Bambu PLA Basic @BBL X1C"}, "ORCA", True),
    # As on the A1 (#264), from the same Bambu-branded X1C family as the default PLA profile; each lists the P1S.
    "orca_filaments": Fact({"PLA": "Bambu PLA Basic @BBL X1C", "PETG": "Bambu PETG Basic @BBL X1C",
                            "TPU": "Bambu TPU 95A @BBL X1C", "PVA": "Bambu PVA @BBL X1C"}, "ORCA", True),
    "orca_identity": Fact(({
        "label": "OrcaSlicer 2.4.2 (BBL vendor profiles 02.01.00.19)",
        "vendor_version": "02.01.00.19",
        "sha256": {"machine": "61c68e30dd084e185ec0cf75beab03195130dabf11702cacb168205770a4782f",
                   "process": "bc4e02dad296e7584bf12143276bbbe2debf73f390f7e6dd2ec2aeac9e3d3c14",
                   "filament": "ff10a3d1ad9e7ed43f5dec36996248b6abb934eed6692c480732670848e0a403"},
        "filaments": {"Bambu PETG Basic @BBL X1C": "dbf3b8e6a26bd2a9202b97e3f0501219c4883caf639b950757050ef6a0004d60",
                      "Bambu TPU 95A @BBL X1C": "e01de656a3c239354f989b625c0411f209f5779ed60c2190f0532cf5de0ee245",
                      "Bambu PVA @BBL X1C": "619f13393e86b02f8613c8a9c5bf7cd79793a4262068f1355bb935765702dfcf"},
    },), "ORCA", True),
    "bed_mm": Fact((256.0, 256.0), "BAMBU", True),
    "max_print_height_mm": Fact(256.0, "BAMBU", True),
    # Orca's conservative machine profile limits slices to Z=250 although Bambu lists a 256 mm volume.
    "orca_profile_height_mm": Fact(250.0, "ORCA", True),
    "bed_exclude_area": Fact(((0.0, 0.0), (18.0, 0.0), (18.0, 28.0), (0.0, 28.0)), "ORCA", True),
    # The literal vendor-profile start, end and timelapse moves include the front purge lane through Y=265.
    "motion_envelope_mm": Fact((0.0, -3.0, 256.0, 265.0), "DERIVED", False),
    "travel_z_headroom_mm": Fact(1.0, "DERIVED", False),
    "default_plate": Fact("Textured PEI Plate", "ORCA", True),
    "stock_nozzle": Fact((0.4, "stainless_steel"), "ORCA", True),
    "nozzle_sizes_mm": Fact((0.2, 0.4, 0.6, 0.8), "BAMBU", True),
    "max_hotend_c": Fact(300, "BAMBU", True),
    "max_bed_c": Fact(100, "BAMBU", True),
    "ideal_materials": Fact(("PLA", "PETG", "TPU", "PVA"), "BAMBU", True),
    "not_recommended_materials": Fact(("ABS", "ASA", "PC", "PA", "PET"), "BAMBU", True),
    # P1S's standard AMS has four slots; Print Buddy deliberately enables only single-color offline jobs.
    "ams": Fact(("AMS", 4), "BAMBU", True),
    "enclosed": Fact(True, "BAMBU", True),
}


# --- #127 Stage 1: the remaining single-nozzle models ---------------------------------------------
# Same derivation as A1/P1S: profile names and identities computed by running profiles.flatten +
# profiles.sha256 over OrcaSlicer v2.4.2 at ORCA_RELEASE (the pipeline reproduces the A1 and P1S
# identities above exactly). Motion envelopes are DERIVED the A1 way: extremes of the literal XY
# moves in the flattened vendor start/end/timelapse/change-filament blocks, widened to cover the
# bed. Dual-nozzle models (H2D, H2D Pro, X2D, H2C) are not here: their machine profiles carry two
# nozzle_diameter entries and a narrower dual-tool width, which the single-path adapter refuses;
# they are Stage 2 on #127.

_IDENTITY_LABEL = "OrcaSlicer 2.4.2 (BBL vendor profiles 02.01.00.19)"


def _identity(machine, process, filament):
    return Fact(({"label": _IDENTITY_LABEL, "vendor_version": "02.01.00.19",
                  "sha256": {"machine": machine, "process": process, "filament": filament}},), "ORCA", True)


A1MINI = {
    "printer_model": Fact("Bambu Lab A1 mini", "ORCA", True),
    "orca_profiles": Fact({"machine": "Bambu Lab A1 mini 0.4 nozzle", "process": "0.20mm Standard @BBL A1M",
                           "filament": "Bambu PLA Basic @BBL A1M"}, "ORCA", True),
    "orca_identity": _identity("044aaabe61a992dd5611ac4b58d91ab3f17f71213fb07d6d61092b87d55a7d7a",
                               "6e04aa942c3afb3f112ae98c9b418e67c2a8c1f50f655b29394b01748fe24a17",
                               "1567b1027fe09e6adac7347b77eca107e1677aae9935b9d2e076fb034649ef2e"),
    "bed_mm": Fact((180.0, 180.0), "ORCA", True),
    "max_print_height_mm": Fact(180.0, "ORCA", True),
    "bed_exclude_area": Fact((), "ORCA", True),
    "motion_envelope_mm": Fact((-30.0, -4.0, 187.0, 185.0), "DERIVED", False),
    "travel_z_headroom_mm": Fact(1.0, "DERIVED", False),
    "default_plate": Fact("Textured PEI Plate", "ORCA", True),
    "stock_nozzle": Fact((0.4, "stainless_steel"), "ORCA", True),
    "nozzle_sizes_mm": Fact((0.2, 0.4, 0.6, 0.8), "BAMBU", False),
    "max_hotend_c": Fact(300, "BAMBU", False),
    "max_bed_c": Fact(80, "BAMBU", False),
    "ideal_materials": Fact(("PLA", "PETG", "TPU", "PVA"), "BAMBU", False),
    "not_recommended_materials": Fact(("ABS", "ASA", "PC", "PA", "PET"), "BAMBU", False),
    # AMS Lite, as on the A1; not driven by Print Buddy.
    "ams": Fact(("AMS Lite", 4), "BAMBU", False),
    "enclosed": Fact(False, "BAMBU", False),
}

P1P = {
    "printer_model": Fact("Bambu Lab P1P", "ORCA", True),
    "orca_profiles": Fact({"machine": "Bambu Lab P1P 0.4 nozzle", "process": "0.20mm Standard @BBL P1P",
                           "filament": "Bambu PLA Basic @BBL P1P"}, "ORCA", True),
    "orca_identity": _identity("511d2d5a976e36d2e2d2009efe352668a40113a3207b1f40a3970d4e28d52706",
                               "359b0093641490765cd659320eb8ddfcaf72904c4ee7e9b39758da932754e92f",
                               "228c186abd2a0e25cc30dd41d19a55fc88e0fa8c89870eed51924efe4db1249e"),
    "bed_mm": Fact((256.0, 256.0), "ORCA", True),
    "max_print_height_mm": Fact(256.0, "BAMBU", False),
    "orca_profile_height_mm": Fact(250.0, "ORCA", True),
    "bed_exclude_area": Fact(((0.0, 0.0), (18.0, 0.0), (18.0, 28.0), (0.0, 28.0)), "ORCA", True),
    "motion_envelope_mm": Fact((0.0, -3.0, 256.0, 265.0), "DERIVED", False),
    "travel_z_headroom_mm": Fact(1.0, "DERIVED", False),
    "default_plate": Fact("Textured PEI Plate", "ORCA", True),
    "stock_nozzle": Fact((0.4, "stainless_steel"), "ORCA", True),
    "nozzle_sizes_mm": Fact((0.2, 0.4, 0.6, 0.8), "BAMBU", False),
    "max_hotend_c": Fact(300, "BAMBU", False),
    "max_bed_c": Fact(100, "BAMBU", False),
    "ideal_materials": Fact(("PLA", "PETG", "TPU", "PVA"), "BAMBU", False),
    "not_recommended_materials": Fact(("ABS", "ASA", "PC", "PA", "PET"), "BAMBU", False),
    "ams": Fact(("AMS", 4), "BAMBU", False),
    # Open frame as shipped (enclosure panels are an optional upgrade).
    "enclosed": Fact(False, "BAMBU", False),
}

# The X1 family shares the P1 platform geometry (256 mm bed, 250 mm Orca height, the same front-left
# exclusion and purge-lane envelope) and the X1C process/filament profiles; the X1 and X1E machine
# profiles differ, so each model keeps its own identity.
_X1_FAMILY = {
    "bed_mm": Fact((256.0, 256.0), "ORCA", True),
    "max_print_height_mm": Fact(256.0, "BAMBU", False),
    "orca_profile_height_mm": Fact(250.0, "ORCA", True),
    "bed_exclude_area": Fact(((0.0, 0.0), (18.0, 0.0), (18.0, 28.0), (0.0, 28.0)), "ORCA", True),
    "motion_envelope_mm": Fact((0.0, -3.0, 256.0, 265.0), "DERIVED", False),
    "travel_z_headroom_mm": Fact(1.0, "DERIVED", False),
    "default_plate": Fact("Textured PEI Plate", "ORCA", True),
    "nozzle_sizes_mm": Fact((0.2, 0.4, 0.6, 0.8), "BAMBU", False),
    "max_hotend_c": Fact(300, "BAMBU", False),
    "max_bed_c": Fact(110, "BAMBU", False),
    "ideal_materials": Fact(("PLA", "PETG", "TPU", "ABS", "ASA", "PC", "PA"), "BAMBU", False),
    "not_recommended_materials": Fact((), "BAMBU", False),
    "ams": Fact(("AMS", 4), "BAMBU", False),
    "enclosed": Fact(True, "BAMBU", False),
}

X1 = {
    **_X1_FAMILY,
    "printer_model": Fact("Bambu Lab X1", "ORCA", True),
    "orca_profiles": Fact({"machine": "Bambu Lab X1 0.4 nozzle", "process": "0.20mm Standard @BBL X1C",
                           "filament": "Bambu PLA Basic @BBL X1"}, "ORCA", True),
    "orca_identity": _identity("8ab80fbf8f84e2962e5dd6f9b2c41955152ccfb27ad1bd592f860d7430720346",
                               "bc4e02dad296e7584bf12143276bbbe2debf73f390f7e6dd2ec2aeac9e3d3c14",
                               "df9074c5777e7a2cf0f3b73c54ebd8c9cf8dc2782f04b4698b10974039f5db19"),
    "stock_nozzle": Fact((0.4, "stainless_steel"), "ORCA", True),
}

X1C = {
    **_X1_FAMILY,
    "printer_model": Fact("Bambu Lab X1 Carbon", "ORCA", True),
    "orca_profiles": Fact({"machine": "Bambu Lab X1 Carbon 0.4 nozzle", "process": "0.20mm Standard @BBL X1C",
                           "filament": "Bambu PLA Basic @BBL X1C"}, "ORCA", True),
    "orca_identity": _identity("c3ba8d3d84ed20abe5783cf5a54798ae35ef9cd06f84da3a60272ce08cb38396",
                               "bc4e02dad296e7584bf12143276bbbe2debf73f390f7e6dd2ec2aeac9e3d3c14",
                               "ff10a3d1ad9e7ed43f5dec36996248b6abb934eed6692c480732670848e0a403"),
    # The X1 Carbon ships with a hardened steel hotend (Carbon = CF-ready).
    "stock_nozzle": Fact((0.4, "hardened_steel"), "ORCA", True),
}

X1E = {
    **_X1_FAMILY,
    "printer_model": Fact("Bambu Lab X1E", "ORCA", True),
    "orca_profiles": Fact({"machine": "Bambu Lab X1E 0.4 nozzle", "process": "0.20mm Standard @BBL X1C",
                           "filament": "Bambu PLA Basic @BBL X1C"}, "ORCA", True),
    "orca_identity": _identity("75d3b7457906753319d74f2efc4d4d020154da2373ad73ca8a56a3fa506ef980",
                               "bc4e02dad296e7584bf12143276bbbe2debf73f390f7e6dd2ec2aeac9e3d3c14",
                               "ff10a3d1ad9e7ed43f5dec36996248b6abb934eed6692c480732670848e0a403"),
    "stock_nozzle": Fact((0.4, "hardened_steel"), "ORCA", True),
}

P2S = {
    "printer_model": Fact("Bambu Lab P2S", "ORCA", True),
    "orca_profiles": Fact({"machine": "Bambu Lab P2S 0.4 nozzle", "process": "0.20mm Standard @BBL P2S",
                           "filament": "Bambu PLA Basic @BBL P2S"}, "ORCA", True),
    "orca_identity": _identity("85e88eba88cb41d79620b99e7483bed8c6e94106089611f7ba17f2b998b3615e",
                               "363db4359eb29d59313749d0d56b5985041ecd56cb5ed6f75626223b1c4199e9",
                               "9dae1470940aaa6dfdd354644342722b675c8ae8c6805c3b109def21aba50d7c"),
    "bed_mm": Fact((256.0, 256.0), "ORCA", True),
    # Unlike the P1/X1 platform, the P2S Orca profile uses the full listed 256 mm height.
    "max_print_height_mm": Fact(256.0, "ORCA", True),
    "bed_exclude_area": Fact((), "ORCA", True),
    "motion_envelope_mm": Fact((0.0, -16.0, 256.0, 256.0), "DERIVED", False),
    "travel_z_headroom_mm": Fact(1.0, "DERIVED", False),
    "default_plate": Fact("Textured PEI Plate", "ORCA", True),
    "stock_nozzle": Fact((0.4, "hardened_steel"), "ORCA", True),
    "nozzle_sizes_mm": Fact((0.2, 0.4, 0.6, 0.8), "BAMBU", False),
    "max_hotend_c": Fact(300, "BAMBU", False),
    "max_bed_c": Fact(110, "BAMBU", False),
    "ideal_materials": Fact(("PLA", "PETG", "TPU", "ABS", "ASA", "PC", "PA"), "BAMBU", False),
    "not_recommended_materials": Fact((), "BAMBU", False),
    # AMS 2 Pro on the combo unit; Print Buddy drives a single path only.
    "ams": Fact(("AMS 2 Pro", 4), "BAMBU", False),
    "enclosed": Fact(True, "BAMBU", False),
}

H2S = {
    "printer_model": Fact("Bambu Lab H2S", "ORCA", True),
    "orca_profiles": Fact({"machine": "Bambu Lab H2S 0.4 nozzle", "process": "0.20mm Standard @BBL H2S",
                           "filament": "Bambu PLA Basic @BBL H2S"}, "ORCA", True),
    "orca_identity": _identity("5956872ea1d691853fd08158134c67da1291fe7b89252e32be281e09a3541f23",
                               "c7b55397a79d3bf03c2389ce868b50c49943f08b9e2f39e97e6e5f3bdf2db2c0",
                               "781806ef94f4bdbcb30525df9d265e80a20e0858d92fe7511e20ac10dcb2ec10"),
    # Single-nozzle H2 variant: one 0.4 nozzle in the profile (the dual-nozzle H2D/H2C are Stage 2).
    "bed_mm": Fact((340.0, 320.0), "ORCA", True),
    "max_print_height_mm": Fact(340.0, "ORCA", True),
    "bed_exclude_area": Fact((), "ORCA", True),
    "motion_envelope_mm": Fact((0.0, -16.0, 340.0, 320.0), "DERIVED", False),
    "travel_z_headroom_mm": Fact(1.0, "DERIVED", False),
    "default_plate": Fact("Textured PEI Plate", "ORCA", True),
    "stock_nozzle": Fact((0.4, "hardened_steel"), "ORCA", True),
    "nozzle_sizes_mm": Fact((0.2, 0.4, 0.6, 0.8), "BAMBU", False),
    "max_hotend_c": Fact(350, "BAMBU", False),
    "max_bed_c": Fact(120, "BAMBU", False),
    "ideal_materials": Fact(("PLA", "PETG", "TPU", "ABS", "ASA", "PC", "PA"), "BAMBU", False),
    "not_recommended_materials": Fact((), "BAMBU", False),
    "ams": Fact(("AMS 2 Pro", 4), "BAMBU", False),
    "enclosed": Fact(True, "BAMBU", False),
}
