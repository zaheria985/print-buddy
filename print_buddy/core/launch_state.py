"""Transactional launch state: consumed bed-clear approvals and one submission lease per printer.

A small SQLite database in the profile state folder, so the check and the write happen in one
transaction across processes (the bot, print_next children, a CLI print-now). JOB.json keeps its
human-readable approvals list; model archives stay ordinary files.

Lease phases: ``leased`` (checks and upload, nothing started), ``submitting`` (persisted before the
start request), ``uncertain`` (sent, outcome unknown). A confirmed or failed start deletes the row.
An unfinished ``leased`` row whose process is gone is released: nothing was sent. A ``submitting``
or ``uncertain`` row is never released by guessing and never replayed. It is reconciled when the
printer reports that exact file, or by the operator with ``model_job reconcile-launch``.
"""
from __future__ import annotations

import json
import os
import socket
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .. import settings

SCHEMA = (
    "CREATE TABLE IF NOT EXISTS approvals (job_json TEXT NOT NULL, gcode_sha256 TEXT NOT NULL, "
    "kind TEXT NOT NULL, consumed_at TEXT NOT NULL, pid INTEGER)",
    "CREATE TABLE IF NOT EXISTS leases (printer TEXT PRIMARY KEY, token TEXT NOT NULL, phase TEXT NOT NULL, "
    "job_json TEXT, gcode_sha256 TEXT, remote TEXT, pid INTEGER, host TEXT, leased_at TEXT, updated_at TEXT, "
    "detail TEXT)",
)


def db_path():
    return Path(os.environ.get("PRINT_BUDDY_LAUNCH_DB") or settings.state_dir()/"print_buddy_launches.sqlite3")


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


@contextmanager
def transaction():
    """One exclusive write transaction. Every launch-state check-then-write runs inside one."""
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=60, isolation_level=None)
    con.row_factory = sqlite3.Row
    try:
        con.execute("BEGIN IMMEDIATE")
        for statement in SCHEMA:
            con.execute(statement)
        try:
            yield con
        except BaseException:
            con.execute("ROLLBACK")
            raise
        con.execute("COMMIT")
    finally:
        con.close()


def consumed(con, job_json, gcode_sha256):
    return [dict(r) for r in con.execute("SELECT * FROM approvals WHERE job_json=? AND gcode_sha256=? ORDER BY rowid",
                                         (str(job_json), gcode_sha256))]


def record_consumed(con, job_json, gcode_sha256, kind, when):
    con.execute("INSERT INTO approvals VALUES (?,?,?,?,?)", (str(job_json), gcode_sha256, kind, when, os.getpid()))


def _owner_alive(row, unknown=True):
    """Whether the lease's process still runs. Another host or container cannot be checked: unknown."""
    if row["host"] != socket.gethostname():
        return unknown
    try:
        os.kill(int(row["pid"]), 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError, TypeError, ValueError):
        return True
    return True


def _describe(row):
    return f"job {row['job_json'] or 'unknown'}, file {row['remote'] or 'unknown'}, since {row['leased_at']}"


class Lease:
    """The one launch allowed on a printer right now. Every update is scoped to this lease's token."""

    def __init__(self, printer, token):
        self.printer, self.token = printer, token

    def _set(self, phase, detail=None):
        with transaction() as con:
            con.execute("UPDATE leases SET phase=?, detail=?, updated_at=? WHERE printer=? AND token=?",
                        (phase, detail, _now(), self.printer, self.token))

    def submitting(self):
        """Persist before the external start request: from here a crash is uncertain, not retried."""
        self._set("submitting")

    def uncertain(self, detail):
        self._set("uncertain", str(detail)[-500:])

    def release(self):
        with transaction() as con:
            con.execute("DELETE FROM leases WHERE printer=? AND token=?", (self.printer, self.token))


def acquire(printer, *, job_json=None, gcode_sha256=None, remote=None, observed_file=None):
    """Take the printer's lease or refuse. observed_file is the file the printer reports right now."""
    with transaction() as con:
        row = con.execute("SELECT * FROM leases WHERE printer=?", (printer,)).fetchone()
        if row is not None:
            if row["phase"] == "leased" and not _owner_alive(row):
                print(f"LAUNCH LEASE RELEASED: an earlier launch stopped before submission ({_describe(row)}).", flush=True)
            elif row["phase"] != "leased" and row["remote"] and observed_file == row["remote"]:
                print(f"LAUNCH RECONCILED: the printer reports {row['remote']}, so the earlier {row['phase']} "
                      "submission reached it. Not resubmitted.", flush=True)
            elif row["phase"] == "leased" or (row["phase"] == "submitting" and _owner_alive(row)):
                raise SystemExit(f"REFUSED: another launch on this printer is in progress ({_describe(row)}). "
                                 "Nothing uploaded or started.")
            else:
                raise SystemExit(f"REFUSED: an earlier start on this printer is uncertain ({_describe(row)}). "
                                 "Check the printer, then run `python3 -m print_buddy.core.model_job reconcile-launch "
                                 "--started` or `--not-started`. It is never resubmitted automatically. "
                                 "Nothing uploaded or started.")
            con.execute("DELETE FROM leases WHERE printer=?", (printer,))
        token = uuid.uuid4().hex
        now = _now()
        con.execute("INSERT INTO leases VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (printer, token, "leased", str(job_json) if job_json else None, gcode_sha256, remote,
                     os.getpid(), socket.gethostname(), now, now, None))
    return Lease(printer, token)


def pending(printer=None):
    with transaction() as con:
        rows = con.execute("SELECT * FROM leases" + (" WHERE printer=?" if printer else ""),
                           (printer,) if printer else ()).fetchall()
    return [dict(r) for r in rows]


def resolve(printer, outcome):
    """Operator reconciliation of an uncertain start. Clears the lease; consumed approvals stay consumed."""
    if outcome not in ("started", "not_started"):
        raise ValueError("outcome must be started or not_started")
    with transaction() as con:
        row = con.execute("SELECT * FROM leases WHERE printer=?", (printer,)).fetchone()
        if row is None:
            raise SystemExit("No launch is pending on this printer; nothing to reconcile.")
        if row["phase"] == "leased" and _owner_alive(row, unknown=False):  # the operator vouches for other hosts
            raise SystemExit(f"REFUSED: that launch is still running ({_describe(row)}); nothing changed.")
        con.execute("DELETE FROM leases WHERE printer=?", (printer,))
    out = dict(row, reconciled=outcome, reconciled_at=_now())
    print("LAUNCH RECONCILED " + json.dumps(out), flush=True)
    return out
