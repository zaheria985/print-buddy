"""Contract tests for every registered printer model, plus the stock Klipper no-AD5X guarantees."""
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from print_buddy import printers, settings
from print_buddy.printers.bambulab.a1 import A1Adapter
from print_buddy.printers.base import BuildVolume, MultiColorUnsupported, PrinterAdapter

from . import a1_fixture

AD5X_ARTIFACTS = re.compile(
    r"SET_EXTRUDER_SLOT|\bIFS\b|zmod|ZMOD|CHANGE_ZCOLOR|display_off|LOAD_ZOFFSET|START_PRINT|MESH=|8898|AD5X|/detail",
    re.I)


MODELS = tuple(printers.MODELS)
AD5X, GENERIC, SV06 = "flashforge.ad5x", "klipper.generic", "sovol.sv06"
OCTOPRINT = "octoprint.marlin"
BAMBU_A1 = "bambulab.a1"


def config_for(family):
    return settings.load(local=False, environ={"PRINT_BUDDY__PRINTER__MODEL": family,
                                               "PRINT_BUDDY__PATHS__HERMES_HOME": str(a1_fixture.HERMES)})


def adapter_for(family):
    return printers.get(family, config_for(family))


class ContractTests(unittest.TestCase):
    def setUp(self):
        # The A1 ships no templates: generate them from the invented fixture tree, trusted for this test only.
        patcher = mock.patch.object(A1Adapter, "facts", a1_fixture.fixture_facts())
        patcher.start()
        self.addCleanup(patcher.stop)
        a1_fixture.install(adapter_for(BAMBU_A1))

    def test_registry_finds_every_model_file(self):
        self.assertEqual(set(MODELS), {AD5X, GENERIC, SV06, OCTOPRINT, BAMBU_A1})
        for key, model in printers.MODELS.items():
            self.assertEqual(model.key, key)
            shipped = settings.ROOT / "print_buddy" / "templates" / "orca" / model.templates
            # Generated templates (third-party profiles) are never shipped; every other model ships its own.
            self.assertEqual(shipped.is_dir(), not model.generated_templates, key)
            self.assertTrue(issubclass(model.adapter, PrinterAdapter))

    def test_every_family_has_an_adapter(self):
        for family in MODELS:
            with self.subTest(family=family):
                adapter = adapter_for(family)
                self.assertIsInstance(adapter, PrinterAdapter)
                self.assertEqual(adapter.key, family)

    def test_contract_for_every_adapter(self):
        for family in MODELS:
            with self.subTest(family=family):
                a = adapter_for(family)
                vol = a.build_volume()
                self.assertIsInstance(vol, BuildVolume)
                self.assertTrue(all(v > 0 for v in (*vol.bed_mm, vol.max_height_mm)))
                self.assertGreaterEqual(a.slot_count, 1)
                self.assertEqual(a.multi_slot, a.slot_count > 1)
                folder = a.templates_dir()
                for name in ("machine", "process", "filament"):
                    json.loads((folder / f"{name}.json").read_text())
                if family not in (OCTOPRINT, BAMBU_A1):
                    self.assertIn("[bed_temperature_initial_layer_single]", a.machine_start_gcode("X MESH=auto", "auto"))
                plan = a.start_plan(60, 215)
                self.assertTrue(plan and all(isinstance(line, str) for line in plan))
                if family == BAMBU_A1:
                    # Offline only: the vendor start runs first, and nothing live is available.
                    machine = json.loads((a.pinned_templates() / "machine.json").read_text())
                    self.assertEqual(plan, machine["machine_start_gcode"].splitlines())
                    self.assertRaises(Exception, a.machine_start_gcode, "X MESH=auto", "auto")
                    self.assertRaises(Exception, a.moonraker_start_commands, "part.gcode")
                    self.assertRaises(Exception, a.moonraker_objects)
                    self.assertEqual(a.normalize_status({"print_stats": {"state": "printing"}})["state"], "unknown")
                    continue
                if family == OCTOPRINT:
                    self.assertEqual(plan, ["M190 S60", "M109 S215"])
                    self.assertEqual(a.machine_end_gcode("END_PRINT"), "END_PRINT")
                    self.assertRaises(Exception, a.moonraker_start_commands, "part.gcode")
                    self.assertRaises(Exception, a.moonraker_objects)
                    self.assertEqual(a.normalize_status({})["state"], "unknown")
                    continue
                cmds = a.moonraker_start_commands("part.gcode")
                self.assertEqual(cmds[-1]["path"], "/printer/print/start")
                self.assertEqual(cmds[-1]["body"], {"filename": "part.gcode"})
                self.assertIn("print_stats", a.moonraker_objects())
                status = a.normalize_status({"print_stats": {"state": "standby", "filename": "a.gcode"},
                                             "extruder": {"temperature": 21.5}})
                self.assertEqual((status["state"], status["filename"], status["hotend_c"]), ("standby", "a.gcode", 21.5))
                self.assertEqual(a.normalize_status({})["state"], "unknown")

    def test_default_model_is_the_ad5x_and_current_returns_it(self):
        home = settings.load(local=False, environ={})
        self.assertEqual(home["printer"]["model"], AD5X)
        self.assertEqual(printers.current(home).key, AD5X)

    def test_unknown_model_is_rejected(self):
        with self.assertRaises(ValueError):
            printers.get("prusa.mk4")


class ZmodTests(unittest.TestCase):
    def setUp(self):
        self.a = adapter_for(AD5X)

    def test_ad5x_start_matches_the_template(self):
        home = settings.load(local=False, environ={})
        machine = json.loads((home["orca"]["templates"] / "machine.json").read_text())
        out = self.a.machine_start_gcode(machine["machine_start_gcode"], "bed_a")
        self.assertTrue(out.startswith("M190 S[bed_temperature_initial_layer_single]\nM104 S[nozzle_temperature_initial_layer] T0\nSTART_PRINT "))
        self.assertIn("MESH=bed_a", out)
        self.assertEqual(self.a.machine_end_gcode("END_PRINT"), "END_PRINT")
        self.assertEqual(self.a.slot_count, 4)
        self.assertTrue(self.a.multi_slot)

    def test_ad5x_screen_and_stock_api(self):
        self.assertEqual(self.a.screen_command(2, "PLA", "ff0000"), "CHANGE_ZCOLOR SLOT=2 TYPE=PLA HEX=FF0000")
        self.assertEqual(self.a.stock_api_url("printer.test"), "http://printer.test:8898/detail")
        self.assertIn("START_PRINT BED_TEMP=60 EXTRUDER_TEMP=215 MESH=auto", self.a.start_plan(60, 215)[-1])


class GenericKlipperTests(unittest.TestCase):
    FAMILIES = (GENERIC, SV06)

    def test_start_plan_and_commands_have_no_ad5x_artifacts(self):
        for family in self.FAMILIES:
            with self.subTest(family=family):
                a = adapter_for(family)
                texts = [*a.start_plan(60, 215), a.machine_start_gcode("START_PRINT MESH=auto", "auto"),
                         a.machine_end_gcode("END_PRINT"), json.dumps(a.moonraker_start_commands("a.gcode")),
                         " ".join(a.moonraker_objects()), json.dumps(a.normalize_status({}))]
                for text in texts:
                    self.assertIsNone(AD5X_ARTIFACTS.search(text), text)
                self.assertEqual(a.start_plan(60, 215)[0], "PRINT_START BED=60 HOTEND=215")
                self.assertEqual(a.machine_end_gcode("END_PRINT"), "PRINT_END")

    def test_templates_have_no_ad5x_artifacts(self):
        for family in self.FAMILIES:
            folder = adapter_for(family).templates_dir()
            for name in ("machine", "process", "filament"):
                text = (folder / f"{name}.json").read_text()
                self.assertIsNone(AD5X_ARTIFACTS.search(text.replace("SET_PRINT_STATS_INFO", "")), (family, name))
                self.assertNotIn("TIMELAPSE", text)

    def test_macro_names_come_from_settings(self):
        cfg = config_for(GENERIC)
        cfg["printer"].update(start_macro="START_MY_PRINT", end_macro="END_MY_PRINT")
        a = printers.get(GENERIC, cfg)
        self.assertEqual(a.start_plan(70, 240)[0], "START_MY_PRINT BED=70 HOTEND=240")
        self.assertEqual(a.machine_end_gcode(""), "END_MY_PRINT")

    def test_no_screen_no_vendor_api_single_slot(self):
        for family in self.FAMILIES:
            a = adapter_for(family)
            self.assertIsNone(a.screen_command(1, "PLA", "FF0000"))
            self.assertIsNone(a.stock_api_url("printer.test"))
            self.assertEqual((a.slot_count, a.multi_slot), (1, False))

    def test_multicolor_is_refused_and_single_is_accepted(self):
        a = adapter_for(GENERIC)
        self.assertEqual(a.normalize_tool_mapping({"0": 1}, {}), {"0": 1})
        with self.assertRaises(MultiColorUnsupported):
            a.normalize_tool_mapping({"0": 1, "1": 2}, {})

    def test_readiness(self):
        a = adapter_for(GENERIC)
        self.assertEqual(a.readiness_problems({}, {"print_stats": {"state": "standby"}, "webhooks": {"state": "ready"}}), [])
        self.assertTrue(a.readiness_problems({}, {"print_stats": {"state": "printing"}}))
        self.assertTrue(a.readiness_problems({}, {}))

    def test_sovol_has_its_own_templates(self):
        self.assertNotEqual(adapter_for(SV06).templates_dir(), adapter_for(GENERIC).templates_dir())
        machine = json.loads((adapter_for(SV06).templates_dir() / "machine.json").read_text())
        self.assertIn("SV06", machine["name"])

    def test_stock_klipper_does_not_import_zmod_code(self):
        code = (f"import sys\nsys.path.insert(0, {str(settings.ROOT)!r})\nfrom print_buddy import printers\n"
                "printers.get('klipper.generic'); printers.get('sovol.sv06')\n"
                "bad=[m for m in sys.modules if m.startswith(('print_buddy.printers.flashforge.zmod',"
                "'print_buddy.hermes.spool_sync'))]\n"
                "print(bad)\n")
        root = Path(settings.ROOT)
        out = subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True, text=True,
                             env={"PATH": ""})
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), "[]")


class SettingsModelTests(unittest.TestCase):
    def load(self, **printer):
        env = {f"PRINT_BUDDY__PRINTER__{k.upper()}": str(v) for k, v in printer.items()}
        return settings.load(local=False, environ=env)

    def test_selection_accepts_registered_models(self):
        for key in MODELS:
            self.assertEqual(printers.current(self.load(model=key)).key, key)

    def test_unknown_model_rejected(self):
        with self.assertRaisesRegex(settings.SettingsError, "model must be one of"):
            self.load(model="prusa.mk4")

    def test_family_key_is_rejected_by_name(self):
        with self.assertRaisesRegex(settings.SettingsError, r"unknown setting \[printer\] family .*model"):
            self.load(family="flashforge_zmod")
        local = Path(tempfile.mkdtemp()) / "local.toml"
        local.write_text('[printer]\nfamily = "flashforge_zmod"\n')
        with self.assertRaisesRegex(settings.SettingsError, r"unknown setting \[printer\] family"):
            settings.load(local=local, environ={})

    def test_model_defaults_come_from_the_model_file(self):
        for key, model in printers.MODELS.items():
            data = self.load(model=key)
            for section, values in model.settings.items():
                for name, value in values.items():
                    expected = Path(settings.ROOT / value) if section == "orca" and name == "templates" else value
                    self.assertEqual(data[section][name], expected, (key, section, name))

    def test_local_toml_overrides_the_model_defaults_with_type_checks(self):
        folder = Path(tempfile.mkdtemp())
        good = folder / "good.toml"
        good.write_text('[printer]\nbed_mm = [250.0, 250.0]\n[layout]\nsingle_copy_edge_mm = 2.5\n')
        data = settings.load(local=good, environ={})
        self.assertEqual((data["printer"]["bed_mm"], data["layout"]["single_copy_edge_mm"]), ([250.0, 250.0], 2.5))
        self.assertEqual(data["printer"]["model"], AD5X)
        bad = folder / "bad.toml"
        bad.write_text('[printer]\nnozzle_diameter_mm = "big"\n')
        with self.assertRaisesRegex(settings.SettingsError, "nozzle_diameter_mm should be like"):
            settings.load(local=bad, environ={})

    def test_local_toml_can_pick_the_model(self):
        local = Path(tempfile.mkdtemp()) / "local.toml"
        local.write_text('[printer]\nmodel = "sovol.sv06"\n')
        data = settings.load(local=local, environ={})
        self.assertEqual((data["printer"]["model"], data["printer"]["ifs_slots"]), (SV06, 1))
        self.assertEqual(data["orca"]["templates"].name, "sovol_sv06")

    def test_klipper_models_need_one_slot(self):
        with self.assertRaisesRegex(settings.SettingsError, "ifs_slots must be 1"):
            self.load(model=GENERIC, ifs_slots=4)

    def test_each_model_uses_its_template_folder_by_default(self):
        for key, model in printers.MODELS.items():
            self.assertEqual(self.load(model=key)["orca"]["templates"].name, model.templates)

    def test_bad_macro_name_rejected(self):
        with self.assertRaisesRegex(settings.SettingsError, "start_macro"):
            self.load(start_macro="PRINT START; M112")

    def test_doctor_reports_the_model(self):
        from print_buddy import doctor
        self.assertEqual(doctor.check_model(self.load()), "flashforge.ad5x (Flashforge AD5X (ZMod)), 4 slots (multi-slot)")
        self.assertIn("single extruder", doctor.check_model(self.load(model=SV06)))


if __name__ == "__main__":
    unittest.main()
