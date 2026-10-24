"""Adversarial offline tests for transactional launch state: approval consumption and the printer lease.

No transport: upload, start and printer reads are mocked. Nothing here contacts a printer.
"""
import json
import os
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path
from unittest import mock

import pytest

from print_buddy.core import model_job

PRINTER = "http://m"  # the moonraker_url the mocked hardware record reports


class Crash(BaseException):
    """Stands in for the process dying mid-launch: no ordinary exception handler sees it."""


def upload_ok(cmd, *args, **kwargs):
    if len(cmd) >= 4 and cmd[1] == "upload":
        return mock.Mock(returncode=0, stdout=json.dumps({"result": {"item": {"root": "gcodes", "path": cmd[3]}}}), stderr="")
    return mock.Mock(returncode=0, stdout="", stderr="")


def make_job(root, name):
    folder = Path(root) / name
    (folder / "out").mkdir(parents=True)
    g = folder / "out" / "x.gcode"
    g.write_text(f"; {name}\nSTART_PRINT EXTRUDER_TEMP=220 BED_TEMP=60\n")
    (folder / "JOB.json").write_text(json.dumps({"title": name, "gcode": str(g)}))
    return folder / "JOB.json", g


def dead_pid():
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def leases():
    db = os.environ["PRINT_BUDDY_LAUNCH_DB"]
    if not Path(db).exists():
        return []
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute("SELECT * FROM leases")]
    except sqlite3.OperationalError:
        return []
    finally:
        con.close()


def simulate_restart():
    """The owner of every lease is gone, as after a crash or host restart."""
    con = sqlite3.connect(os.environ["PRINT_BUDDY_LAUNCH_DB"])
    with con:
        con.execute("UPDATE leases SET pid=?", (dead_pid(),))
    con.close()


@pytest.fixture
def printer(tmp_path):
    """print_now with every gate passing and the transport mocked. Returns the mocks to steer."""
    phase = mock.Mock(return_value=("standby", "", "Ready"))
    run = mock.Mock(side_effect=upload_ok)
    mr = mock.Mock(return_value={"result": "ok"})
    patches = [mock.patch.object(model_job, "START_CLAIMS", tmp_path / "claims.json"),
               mock.patch.object(model_job.printer_state, "load", return_value={"moonraker_url": PRINTER}),
               mock.patch.object(model_job, "_printer_phase", phase),
               mock.patch.object(model_job, "verify_gcode"),
               mock.patch.object(model_job, "_verify_filament_readiness"),
               mock.patch.object(model_job, "_mr", mr),
               mock.patch.object(model_job, "_wait_printing", return_value="printing"),
               mock.patch.object(model_job, "record_start", return_value="/vault/x.md"),
               mock.patch.object(model_job, "live_snapshot", return_value={"nozzle_c": None, "bed_c": None, "slots": "unknown"}),
               mock.patch.object(model_job, "mesh_readiness", return_value={"profile": "auto", "saved_at": None, "print_leveling": 0,
                                                                              "problem": None, "stale": None, "z_offset_mm": 0.0,
                                                                              "offset_source": "saved gcode offset"}),
               mock.patch.object(model_job, "screen_z_offset", return_value=0.0),
               mock.patch.object(model_job, "_announce_print_name"),
               mock.patch.object(model_job, "_publish_job_card"),
               mock.patch.object(model_job.subprocess, "run", run)]
    for p in patches:
        p.start()
    yield mock.Mock(phase=phase, run=run, mr=mr, root=tmp_path)
    for p in reversed(patches):
        p.stop()


def uploads(run):
    return [c.args[0][3] for c in run.call_args_list if c.args and len(c.args[0]) >= 4 and c.args[0][1] == "upload"]


def start_requests(mr):
    paths = {c["path"] for c in model_job.printers.current().moonraker_start_commands("f")}
    return [c for c in mr.call_args_list if len(c.args) > 1 and c.args[1] in paths]


# 1. One approval, two stale snapshots.

def test_stale_snapshots_cannot_consume_one_approval_twice(tmp_path):
    jp, g = make_job(tmp_path, "a")
    first, second = json.loads(jp.read_text()), json.loads(jp.read_text())
    model_job._consume_approval(jp, first, g)
    with pytest.raises(SystemExit, match="already used"):
        model_job._consume_approval(jp, second, g)
    assert len(json.loads(jp.read_text())["approvals"]) == 1


def test_simultaneous_callers_consume_one_approval_exactly_once(tmp_path):
    jp, g = make_job(tmp_path, "a")
    snapshots = [json.loads(jp.read_text()) for _ in range(8)]
    gate = threading.Barrier(len(snapshots))
    won, refused = [], []

    def caller(job):
        gate.wait()
        try:
            model_job._consume_approval(jp, job, g)
            won.append(job)
        except SystemExit as exc:
            refused.append(str(exc))

    threads = [threading.Thread(target=caller, args=(s,)) for s in snapshots]
    for t in threads: t.start()
    for t in threads: t.join()
    assert len(won) == 1 and len(refused) == len(snapshots) - 1
    assert all("already used" in r for r in refused)
    assert len(json.loads(jp.read_text())["approvals"]) == 1


def test_a_stale_job_write_cannot_resurrect_a_consumed_approval(tmp_path):
    """Another JOB.json writer (slice, remap) saving an older snapshot must not erase the consumption."""
    jp, g = make_job(tmp_path, "a")
    stale = json.loads(jp.read_text())
    model_job._consume_approval(jp, json.loads(jp.read_text()), g)
    model_job.write_job(jp.parent, stale)  # lost update: approvals list gone from JOB.json
    with pytest.raises(SystemExit, match="already used"):
        model_job._consume_approval(jp, json.loads(jp.read_text()), g)


def test_reprint_is_still_a_new_explicit_approval(tmp_path):
    jp, g = make_job(tmp_path, "a")
    model_job._consume_approval(jp, json.loads(jp.read_text()), g)
    model_job._consume_approval(jp, json.loads(jp.read_text()), g, reprint=True)
    assert [a["kind"] for a in json.loads(jp.read_text())["approvals"]] == ["bed_clear", "reprint"]


def test_consumption_still_runs_the_artifact_guard_on_current_state(tmp_path):
    jp, g = make_job(tmp_path, "a")
    stale = json.loads(jp.read_text())
    current = dict(stale, artifact_binding={"gcode_sha256": "0" * 64}, gcode_verified=True,
                   downloaded_file=str(g))
    model_job.write_job(jp.parent, current)  # resliced/rebound since the caller read it
    with pytest.raises(RuntimeError, match="stale color artifact"):
        model_job._consume_approval(jp, stale, g)
    assert "approvals" not in json.loads(jp.read_text())


# 2. Two different jobs, one printer.

def test_second_job_is_refused_while_the_first_launch_is_in_flight(printer):
    a, _ = make_job(printer.root, "a")
    b, _ = make_job(printer.root, "b")
    seen = {}

    def upload_a_then_race_b(cmd, *args, **kwargs):
        if cmd[1] == "upload" and "b" not in seen:
            seen["b"] = "raced"
            with pytest.raises(SystemExit, match="REFUSED: another launch .*in progress"):
                model_job.print_now(b)  # B also saw an idle printer before A submitted
        return upload_ok(cmd, *args, **kwargs)

    printer.run.side_effect = upload_a_then_race_b
    assert model_job.print_now(a)["started"] is True
    assert len(uploads(printer.run)) == 1 and len(start_requests(printer.mr)) == 1
    assert "approvals" not in json.loads(b.read_text())  # a refused lease consumes nothing


def test_stale_idle_check_cannot_start_a_second_job_after_the_first_started(printer):
    a, _ = make_job(printer.root, "a")
    b, _ = make_job(printer.root, "b")
    model_job.print_now(a)
    remote = uploads(printer.run)[0]
    calls = []

    def idle_once_then_busy(st):
        calls.append(1)
        return ("standby", "", "Ready") if len(calls) == 1 else ("printing", remote, "Printing")

    printer.phase.side_effect = idle_once_then_busy  # B's early check predates A's start
    with pytest.raises(SystemExit, match="REFUSED: printer is busy"):
        model_job.print_now(b)
    assert len(uploads(printer.run)) == 1 and len(start_requests(printer.mr)) == 1
    assert "approvals" not in json.loads(b.read_text())


# 3. A crash after submission is uncertain until reconciled, never replayed.

def crash_on_start(mr):
    paths = {c["path"] for c in model_job.printers.current().moonraker_start_commands("f")}

    def fake(st, path, data=None, timeout=30):
        if path in paths:
            raise Crash("process died after sending the start request")
        return {"result": "ok"}
    mr.side_effect = fake


def test_crash_after_submission_leaves_the_printer_uncertain(printer):
    a, _ = make_job(printer.root, "a")
    b, _ = make_job(printer.root, "b")
    crash_on_start(printer.mr)
    with pytest.raises(Crash):
        model_job.print_now(a)
    simulate_restart()
    rows = leases()
    assert len(rows) == 1 and rows[0]["phase"] in ("submitting", "uncertain")
    printer.mr.side_effect = None
    for target, kw in ((a, {"reprint": True}), (b, {})):
        with pytest.raises(SystemExit, match="REFUSED: .*uncertain"):
            model_job.print_now(target, **kw)
    assert len(uploads(printer.run)) == 1  # nothing replayed
    assert "approvals" not in json.loads(b.read_text())


def test_uncertain_submission_reconciles_from_printer_evidence_not_replay(printer):
    a, _ = make_job(printer.root, "a")
    b, _ = make_job(printer.root, "b")
    crash_on_start(printer.mr)
    with pytest.raises(Crash):
        model_job.print_now(a)
    simulate_restart()
    remote = uploads(printer.run)[0]
    printer.mr.side_effect = None
    printer.phase.return_value = ("complete", remote, "Ready")  # the printer did run A's exact file
    with pytest.raises(SystemExit, match="already used"):
        model_job.print_now(a)  # reconciled as submitted; the approval stays consumed
    assert model_job.print_now(b)["started"] is True
    assert leases() == []
    assert len(uploads(printer.run)) == 2  # A once, then B; A was never resubmitted


def test_operator_reconciles_an_uncertain_submission_without_replaying_it(printer):
    from print_buddy.core import launch_state
    a, _ = make_job(printer.root, "a")
    crash_on_start(printer.mr)
    with pytest.raises(Crash):
        model_job.print_now(a)
    simulate_restart()
    printer.mr.side_effect = None
    assert launch_state.resolve(PRINTER, "not_started")["phase"] in ("submitting", "uncertain")
    assert leases() == []
    with pytest.raises(SystemExit, match="already used"):
        model_job.print_now(a)  # a new start needs a new explicit approval
    assert model_job.print_now(a, reprint=True)["started"] is True


def test_crash_before_submission_is_released_after_restart_and_approval_stays_used(printer):
    a, _ = make_job(printer.root, "a")

    def crash_during_upload(cmd, *args, **kwargs):
        raise Crash("process died while uploading")
    printer.run.side_effect = crash_during_upload
    with pytest.raises(Crash):
        model_job.print_now(a)
    printer.run.side_effect = upload_ok
    simulate_restart()
    with pytest.raises(SystemExit, match="already used"):
        model_job.print_now(a)  # the approval from before the restart is not reusable
    assert model_job.print_now(a, reprint=True)["started"] is True
    assert leases() == []


def test_upload_failure_releases_the_lease(printer):
    a, _ = make_job(printer.root, "a")
    printer.run.side_effect = lambda cmd, *a_, **k: mock.Mock(returncode=1, stdout="", stderr="boom")
    with pytest.raises(SystemExit, match="UPLOAD FAILED"):
        model_job.print_now(a)
    assert leases() == []


def test_hard_crash_before_submission_is_released_after_restart():
    from print_buddy.core import launch_state
    launch_state.acquire(PRINTER, job_json="/jobs/a/JOB.json", gcode_sha256="a" * 64, remote="a.gcode")
    with pytest.raises(SystemExit, match="in progress"):
        launch_state.acquire(PRINTER, remote="b.gcode")
    simulate_restart()  # the owner died during checks or upload: nothing was sent to start
    launch_state.acquire(PRINTER, remote="b.gcode").release()
    assert leases() == []


# OctoPrint: the same lease, with upload and start as one uncertain step.

class FakeOctoPrint:
    transport, key, multi_slot = "octoprint", "octoprint.marlin", False

    def __init__(self):
        self.statuses, self.uploads = [], []
        outer = self

        class Client:
            def status(self):
                status = outer.statuses.pop(0) if len(outer.statuses) > 1 else outer.statuses[0]
                return status() if callable(status) else status
            def require_ready(self, status):
                if status["state"] != "standby": raise RuntimeError(f"printer is {status['state']}")
            def upload(self, gcode, remote): outer.uploads.append(remote); return remote
            def start(self, remote, bed, hotend): pass
        self._client = Client()

    def client(self): return self._client
    def normalize_status(self, raw): return raw
    def normalize_tool_mapping(self, mapping, state): return mapping


@pytest.fixture
def octoprint(tmp_path):
    fake = FakeOctoPrint()
    st = {"temps_c": {"bed": 60, "nozzle": 210}, "nozzle": {"type": "hardened_steel"}}
    with mock.patch.object(model_job.printers, "current", return_value=fake), \
         mock.patch.object(model_job.printer_state, "load", return_value=st), \
         mock.patch.object(model_job, "verify_gcode"), \
         mock.patch.object(model_job, "record_start", return_value="/vault/x.md"):
        fake.root = tmp_path
        yield fake


def octo_job(root, name):
    jp, g = make_job(root, name)
    jp.write_text(json.dumps(dict(json.loads(jp.read_text()), printer_model="octoprint.marlin")))
    return jp


def test_octoprint_unconfirmed_start_is_uncertain_until_the_printer_reports_it(octoprint):
    a, b = octo_job(octoprint.root, "a"), octo_job(octoprint.root, "b")
    idle = {"state": "standby", "filename": ""}
    octoprint.statuses = [idle, idle, idle]  # pre-check, in-lease check, readback: never confirmed
    assert model_job.print_now(a)["outcome"] == "unconfirmed"
    assert [r["phase"] for r in leases()] == ["uncertain"]
    octoprint.statuses = [idle]
    with pytest.raises(SystemExit, match="uncertain"):
        model_job.print_now(b)
    done = {"state": "standby", "filename": octoprint.uploads[0]}  # OctoPrint shows A's file: it ran
    octoprint.statuses = [done, done, lambda: {"state": "printing", "filename": octoprint.uploads[-1]}]
    assert model_job.print_now(b)["outcome"] == "printing"
    assert len(octoprint.uploads) == 2 and leases() == []


# Native mapped route (SET_ZCOLOR): the idle guard runs again inside the lease.

@pytest.fixture
def mapped(tmp_path):
    native = mock.Mock()
    native.normalize_mapping.side_effect = lambda m, st: {"0": 1}
    native.require_idle_settled.return_value = True
    adapter = mock.Mock(native=native)
    scripts = []

    def fake_mr(st, path, data=None, timeout=30):
        if path == "/printer/gcode/script":
            scripts.append(data["script"])
            return {"result": "ok"}
        return {"result": {"status": {
            "webhooks": {"state": "ready"}, "print_stats": {"state": "standby", "filename": ""},
            "extruder": {}, "heater_bed": {},
            "zmod_color": {"slots": [{"ID": 1, "Material": "PLA", "HEX": "000000"}]},
            "filament_switch_sensor head_switch_sensor": {"enabled": True}}}}

    st = {"moonraker_url": PRINTER, "ifs_slots": {"1": {"material": "PLA", "hex": "#000000"}}}
    with mock.patch.object(model_job.printers, "current", return_value=adapter), \
         mock.patch.object(model_job, "START_CLAIMS", tmp_path / "claims.json"), \
         mock.patch.object(model_job, "validate_saved_mesh_start"), \
         mock.patch.object(model_job, "_mr", side_effect=fake_mr), \
         mock.patch.object(model_job, "read_stock_detail", return_value={}), \
         mock.patch.object(model_job, "read_stock_ifs_slots_from_detail", return_value={1: True}), \
         mock.patch.object(model_job, "_sync_active_spool"), \
         mock.patch.object(model_job, "_announce_print_name"), \
         mock.patch.object(model_job, "_publish_job_card"), \
         mock.patch.object(model_job, "_assign_print_number", return_value=None), \
         mock.patch.object(model_job, "record_start", return_value="/vault/x.md"), \
         mock.patch.object(model_job, "_wait_printing", return_value="standby"), \
         mock.patch.object(model_job, "_preparation_evidence", return_value=False), \
         mock.patch.object(model_job.subprocess, "run", side_effect=upload_ok) as run:
        yield mock.Mock(native=native, scripts=scripts, run=run, st=st, root=tmp_path)


def launch_mapped(m, name):
    jp, g = make_job(m.root, name)
    job = json.loads(jp.read_text())
    return jp, lambda: model_job.print_mapped_native(g, m.st, {"display_off": 0}, {"0": 1}, job, jp, 0, False)


def test_mapped_launch_rechecks_idle_inside_the_lease(mapped):
    jp, launch = launch_mapped(mapped, "a")
    mapped.native.require_idle_settled.side_effect = [True, RuntimeError("REFUSED: idle guard failed: Moonraker reports active execution")]
    with pytest.raises(SystemExit, match="idle guard failed"):
        launch()
    assert uploads(mapped.run) == [] and mapped.scripts == [] and leases() == []
    assert "approvals" not in json.loads(jp.read_text())


def test_mapped_unresolved_start_is_uncertain_and_blocks_the_next_job(mapped):
    _, launch_a = launch_mapped(mapped, "a")
    out = launch_a()
    assert out["phase"] == "preparing_or_unresolved" and len(mapped.scripts) == 1
    assert [r["phase"] for r in leases()] == ["uncertain"]
    _, launch_b = launch_mapped(mapped, "b")
    with pytest.raises(SystemExit, match="uncertain"):
        launch_b()
    assert len(mapped.scripts) == 1 and len(uploads(mapped.run)) == 1


def test_operator_can_clear_a_lease_left_by_another_host_but_not_a_live_local_one():
    from print_buddy.core import launch_state
    lease = launch_state.acquire(PRINTER, remote="a.gcode")
    with pytest.raises(SystemExit, match="still running"):
        launch_state.resolve(PRINTER, "not_started")
    con = sqlite3.connect(os.environ["PRINT_BUDDY_LAUNCH_DB"])
    with con:
        con.execute("UPDATE leases SET host='another-container'")
    con.close()
    with pytest.raises(SystemExit, match="in progress"):
        launch_state.acquire(PRINTER, remote="b.gcode")  # unknown liveness fails closed
    assert launch_state.resolve(PRINTER, "not_started")["remote"] == "a.gcode"
    lease.release()  # a stale token changes nothing
    assert leases() == []
