"""klipper_report: offline, no printer traffic."""
from unittest.mock import patch

from print_buddy.hermes import klipper_report as kr
from print_buddy.hermes import moonraker_event_listener as mel

NOW = 1792070000.0
MSG = "Nozzle hit bed or part detachment. Weight 1512>1500.0. Z=10. FIRMWARE_RESTART."
STATUS = {"webhooks": {"state": "shutdown", "state_message": MSG},
          "print_stats": {"filename": "f.gcode", "print_duration": 3085, "total_duration": 33486,
                          "filament_used": 3936.2, "state": "cancelled", "info": {"current_layer": 42, "total_layer": 300}},
          "virtual_sdcard": {"progress": 0.31}, "extruder": {"temperature": 23, "target": 0},
          "heater_bed": {"temperature": 24, "target": 60}, "gcode_move": {"gcode_position": [128.3, 118.2, 8.4, 0]}}
STORE = [{"message": "!! " + MSG, "time": NOW - 100}, {"message": "// " + MSG, "time": NOW - 99},
         {"message": "ok", "time": NOW - 50}, {"message": "old thing", "time": NOW - 999999}]


def test_report_shutdown_cancel_from_screen():
    out = kr.build("f.gcode", STATUS, STORE, "partial\nStats 1: x mcu_awake=0\nTransition to shutdown state: " + MSG, by_bot=False, now=NOW)
    assert "printer screen" in out and "layer 42/300" in out and "Z=8.40" in out and "51m" in out
    assert "1512 g" in out and "/print-reboot" in out and "target 60" in out and "23°C (off)" in out
    assert out.count("Nozzle hit bed") <= 3 and "old thing" not in out and "Stats 1" not in out
    assert len(out) <= 1990


def test_report_plain_manual_cancel():
    st = {"webhooks": {"state": "ready"}, "print_stats": {"state": "cancelled", "print_duration": 60}}
    out = kr.build("f.gcode", st, [], "", by_bot=True, now=NOW)
    assert "print bot" in out and "plain manual cancel" in out and "Why it stopped" not in out


def test_bot_marker_window(tmp_path):
    with patch.object(kr, "BOT_CANCEL", tmp_path / "m.json"):
        assert not kr.cancelled_by_bot(NOW)
        kr.mark_bot_cancel(NOW)
        assert kr.cancelled_by_bot(NOW + 30) and not kr.cancelled_by_bot(NOW + 500)


def test_report_trims_long_output():
    store = [{"message": f"error line {i} " + "x" * 200, "time": NOW - 10} for i in range(40)]
    tail = "\n".join(f"error {i} " + "y" * 200 for i in range(40))
    assert len(kr.build("f", STATUS, store, tail, by_bot=False, now=NOW)) <= 1990


def test_listener_cancel_posts_report_and_falls_back():
    with patch("print_buddy.hermes.klipper_report.report", return_value="REPORT"), patch.object(mel, "post", return_value=True) as p:
        mel.post_cancel_report("f.gcode")
    assert "REPORT" in p.call_args[0][0]
    with patch("print_buddy.hermes.klipper_report.report", side_effect=RuntimeError("boom")), patch.object(mel, "post", return_value=True) as p:
        mel.post_cancel_report("f.gcode")
    assert "REPORT" not in p.call_args[0][0]


def test_screen_cancel_reboot_paths():
    posts = []
    ok, _ = mel.screen_cancel_reboot("f.gcode", status_reader=lambda: {"state": "printing", "klippy": "ready"},
                                     rebooter=lambda: 1 / 0, clearer=lambda: 1 / 0, poster=posts.append)
    assert ok is False and posts == []
    ok, msg = mel.screen_cancel_reboot("f.gcode", status_reader=lambda: {"state": "cancelled", "klippy": "ready"},
                                       rebooter=lambda: (True, "Restart complete."), clearer=lambda: 1 / 0,
                                       poster=posts.append)
    assert ok and "Force restarting" in posts[0] and posts[-1] == "Restart complete."
    posts.clear()
    ok, _ = mel.screen_cancel_reboot("f.gcode", status_reader=lambda: {"state": "paused", "klippy": "shutdown"},
                                     rebooter=lambda: 1 / 0, clearer=lambda: (True, "Cleared."), poster=posts.append)
    assert ok and posts == ["Cleared."]  # the cancel report already said it; one result line only


def test_native_screen_cancel_detection():
    assert mel.native_screen_cancel("printing", "cancel")
    assert mel.native_screen_cancel("error", "ready")
    assert not mel.native_screen_cancel("ready", "ready")
    assert not mel.native_screen_cancel(None, "cancel")


def test_listener_skips_auto_restart_for_bot_cancel(tmp_path):
    import asyncio
    lst = mel.Listener()
    with patch.object(kr, "BOT_CANCEL", tmp_path / "m.json"):
        kr.mark_bot_cancel()
        assert lst.start_screen_cancel_reboot("test") is False
        (tmp_path / "m.json").unlink()

        async def run():
            with patch.object(mel, "screen_cancel_reboot", return_value=(True, "ok")) as scr:
                assert lst.start_screen_cancel_reboot("test") is True
                assert lst.start_screen_cancel_reboot("dupe") is False
                await lst.screen_reboot_task
            return scr.call_count
        assert asyncio.run(run()) == 1
