"""Bounded event journal. No request bodies, console output or exceptions belong here."""
from __future__ import annotations

from contextlib import contextmanager
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
import uuid

from flask import current_app, g, has_request_context

from backend.auth.service import data_dir

logger = logging.getLogger(__name__)
_lock = threading.RLock()
ALERTS = frozenset({"server.crashed", "backup.failed", "job.failed", "disk.pressure", "disk.recovered"})
MAX_EVENTS = 10_000
MAX_DELIVERIES = 5_000
MAX_PENDING = 2_000
RETENTION = 30 * 86400
DELIVERY_TTL = 7 * 86400

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
 id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, actor TEXT NOT NULL,
 server_id TEXT, action TEXT NOT NULL, outcome TEXT NOT NULL, alert TEXT, ref TEXT
);
CREATE INDEX IF NOT EXISTS events_scope ON events(server_id, id);
CREATE TABLE IF NOT EXISTS destinations (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL,
 secret TEXT NOT NULL, enabled INTEGER NOT NULL, alerts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS deliveries (
 id INTEGER PRIMARY KEY AUTOINCREMENT, destination_id TEXT NOT NULL,
 event_id INTEGER NOT NULL, payload TEXT NOT NULL, created REAL NOT NULL,
 state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
 next_at REAL NOT NULL, error TEXT, UNIQUE(destination_id, event_id)
);
CREATE INDEX IF NOT EXISTS deliveries_due ON deliveries(state, next_at);
CREATE TABLE IF NOT EXISTS cooldowns (key TEXT PRIMARY KEY, at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS jobs (
 key TEXT PRIMARY KEY, run_id TEXT NOT NULL, server_id TEXT, action TEXT NOT NULL,
 actor TEXT NOT NULL, at REAL NOT NULL, terminal INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS disk_state (
 scope TEXT PRIMARY KEY, pressured INTEGER NOT NULL, at REAL NOT NULL
);
"""


def db_path() -> Path:
    return data_dir() / "events" / "events.sqlite3"


@contextmanager
def connection():
    path = db_path()
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(path.parent, 0o700)
        # Restrict the file before SQLite creates it (including on first boot).
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        os.chmod(path, 0o600)
        db = sqlite3.connect(path, timeout=0.25)
        db.row_factory = sqlite3.Row
        try:
            db.executescript(_SCHEMA)
            with db:
                yield db
        finally:
            db.close()


def actor():
    if not has_request_context():
        return {"kind": "system", "id": "scheduler"}
    if g.get("audit_actor"):
        return g.audit_actor
    user = g.get("principal")
    if g.get("audit_token_id"):
        return {"kind": "token", "id": g.audit_token_id, **({"subject": user.subject} if user else {})}
    if user:
        return {"kind": "oidc", "id": user.subject, "issuer": user.issuer}
    if not current_app.config.get("FABRICATOR_AUTH_ENABLED"):
        return {"kind": "unattributed", "id": "authentication-disabled"}
    return {"kind": "shared-password", "id": "panel"}


def best_effort(func):
    """Journal faults are explicit in service logs, never rollback source operations."""
    def wrapped(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception:
            # Exception text may contain credentials.
            logger.error("Activity journal write failed (%s); operation was not rolled back", func.__name__)
            return None
    return wrapped


def _insert(db, *, server_id, action, outcome, who, alert=None, ref=None, now=None):
    now = time.time() if now is None else now
    if not re.fullmatch(r"[a-z][a-z0-9_.-]{0,79}", action):
        raise ValueError("Invalid event action")
    if alert is not None and alert not in ALERTS | {"notification.test"}:
        raise ValueError("Invalid alert kind")
    if outcome not in {"accepted", "succeeded", "failed", "interrupted", "skipped"}:
        raise ValueError("Invalid event outcome")
    if ref is not None and not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", ref):
        raise ValueError("Invalid job reference")
    cursor = db.execute(
        "INSERT INTO events(at,actor,server_id,action,outcome,alert,ref) VALUES(?,?,?,?,?,?,?)",
        (now, json.dumps(who), server_id, action, outcome, alert, ref),
    )
    event_id = cursor.lastrowid
    if alert:
        payload = json.dumps({
            "id": event_id, "at": now, "server_id": server_id,
            "action": action, "outcome": outcome, "alert": alert, "ref": ref,
            "path": f"/server/{server_id}/activity?event={event_id}" if server_id else "/settings/notifications",
        })
        for dest in db.execute("SELECT id,alerts FROM destinations WHERE enabled=1").fetchall():
            if alert not in json.loads(dest["alerts"]):
                continue
            _enqueue(db, dest["id"], event_id, payload, now)
    _prune(db, now)
    return event_id


def _enqueue(db, destination_id, event_id, payload, now):
    event = json.loads(payload)
    key = f"notify:{destination_id}:{event['alert']}:{event.get('server_id')}"
    last = db.execute("SELECT at FROM cooldowns WHERE key=?", (key,)).fetchone()
    if event["alert"] != "notification.test" and last and now - last["at"] < 300:
        state, error = "suppressed", "cooldown"
    elif db.execute("SELECT count(*) FROM deliveries WHERE state IN ('pending','sending')").fetchone()[0] >= MAX_PENDING:
        # Keep a visible failed delivery, rather than silently dropping an alert.
        state, error = "failed", "queue_capacity"
        logger.error("Notification queue capacity reached; delivery recorded as failed")
    else:
        state, error = "pending", None
        if event["alert"] != "notification.test":
            db.execute("INSERT OR REPLACE INTO cooldowns VALUES(?,?)", (key, now))
    db.execute(
        "INSERT OR IGNORE INTO deliveries(destination_id,event_id,payload,created,state,next_at,error) VALUES(?,?,?,?,?,?,?)",
        (destination_id, event_id, payload, now, state, now, error),
    )


def _prune(db, now):
    db.execute("UPDATE deliveries SET state='failed',error='attempt_limit' WHERE state='pending' AND attempts>=5")
    db.execute("UPDATE deliveries SET state='failed',error='expired' WHERE state IN ('pending','sending') AND created<?", (now - DELIVERY_TTL,))
    db.execute("DELETE FROM events WHERE at<? OR id NOT IN (SELECT id FROM events ORDER BY id DESC LIMIT ?)", (now - RETENTION, MAX_EVENTS))
    db.execute("DELETE FROM deliveries WHERE state IN ('sent','failed','cancelled','suppressed') AND (created<? OR id NOT IN (SELECT id FROM deliveries ORDER BY id DESC LIMIT ?))", (now - RETENTION, MAX_DELIVERIES))
    db.execute("DELETE FROM cooldowns WHERE at<?", (now - 86400,))
    db.execute("DELETE FROM jobs WHERE terminal=1 AND (at<? OR key NOT IN (SELECT key FROM jobs ORDER BY at DESC LIMIT ?))", (now - DELIVERY_TTL, MAX_EVENTS))
    db.execute("DELETE FROM disk_state WHERE at<?", (now - RETENTION,))


@best_effort
def emit(action, *, server_id=None, outcome="succeeded", alert=None, who=None, ref=None, cooldown=0, key=None):
    with connection() as db:
        now = time.time()
        if cooldown:
            key = key or f"{alert}:{server_id}"
            row = db.execute("SELECT at FROM cooldowns WHERE key=?", (key,)).fetchone()
            if row and now - row["at"] < cooldown:
                return None
            db.execute("INSERT OR REPLACE INTO cooldowns VALUES(?,?)", (key, now))
        return _insert(db, server_id=server_id, action=action, outcome=outcome, who=who or actor(), alert=alert, ref=ref, now=now)


@best_effort
def track_job(key, server_id, action, phase, *, ref=None, related_server=None, restart_error=False):
    """Capture actor on submission and terminal outcome once, across worker threads."""
    if not phase and not restart_error:
        return
    with connection() as db:
        row = db.execute("SELECT * FROM jobs WHERE key=?", (key,)).fetchone()
        terminal = phase in {"done", "failed", "completed", "error", "cancelled"}
        if row is None or (row["terminal"] and phase in {"starting", "queued"}):
            if phase == "queued" and row and row["terminal"]:
                # Queue notifications may be repeated by a worker after its
                # submitter; only explicit install keys are reused across runs.
                if not key.startswith(("install:", "modpack:")):
                    return
            db.execute("INSERT OR REPLACE INTO jobs VALUES(?,?,?,?,?,?,0)",
                       (key, uuid.uuid4().hex, server_id, action, json.dumps(actor()), time.time()))
            row = db.execute("SELECT * FROM jobs WHERE key=?", (key,)).fetchone()
        if not row["server_id"] and server_id:
            db.execute("UPDATE jobs SET server_id=?,action=? WHERE key=?", (server_id, action, key))
            row = db.execute("SELECT * FROM jobs WHERE key=?", (key,)).fetchone()
        if row["terminal"]:
            # Restart errors may arrive after the main job reported success.
            if restart_error:
                _insert(db, server_id=row["server_id"], action="server.restart", outcome="failed",
                        who=json.loads(row["actor"]), alert="job.failed", ref=ref)
            return
        if terminal:
            failed = phase in {"failed", "error"} or restart_error
            outcome = "failed" if failed else "skipped" if phase == "cancelled" else "succeeded"
            who = json.loads(row["actor"])
            alert = ("backup.failed" if row["action"] == "backups.create" else "job.failed") if failed else None
            _insert(db, server_id=row["server_id"], action=row["action"],
                    outcome=outcome, who=who, alert=alert, ref=ref)
            if related_server and related_server != row["server_id"]:
                _insert(db, server_id=related_server, action=row["action"],
                        outcome=outcome, who=who, ref=ref)
            db.execute("UPDATE jobs SET terminal=1 WHERE key=?", (key,))


def recover():
    with connection() as db:
        db.execute("UPDATE deliveries SET state=CASE WHEN attempts>=5 THEN 'failed' ELSE 'pending' END, "
                   "next_at=?,error='delivery_interrupted' WHERE state='sending'", (time.time(),))
        for row in db.execute("SELECT * FROM jobs WHERE terminal=0").fetchall():
            _insert(db, server_id=row["server_id"], action=row["action"], outcome="interrupted",
                    who=json.loads(row["actor"]), alert="backup.failed" if row["action"] == "backups.create" else "job.failed")
        db.execute("UPDATE jobs SET terminal=1 WHERE terminal=0")
        _prune(db, time.time())


@best_effort
def record_job_issue(key, server_id, action):
    """A sub-operation failed without failing its enclosing loader install."""
    with connection() as db:
        row = db.execute("SELECT actor FROM jobs WHERE key=?", (key,)).fetchone()
        return _insert(db, server_id=server_id, action=action, outcome="failed",
                       who=json.loads(row["actor"]) if row else actor(), alert="job.failed")


def history(server_id=None, *, before=None, alerts_only=False, limit=50, instance=False):
    clauses, args = [], []
    if not instance:
        clauses.append("server_id=?")
        args.append(server_id)
    if before:
        clauses.append("id<?")
        args.append(before)
    if alerts_only:
        clauses.append("alert IS NOT NULL")
    with connection() as db:
        _prune(db, time.time())
        rows = db.execute("SELECT * FROM events" + (" WHERE " + " AND ".join(clauses) if clauses else "") + " ORDER BY id DESC LIMIT ?", (*args, limit + 1)).fetchall()
    items = [{**dict(row), "actor": json.loads(row["actor"])} for row in rows[:limit]]
    return {"items": items, "next": items[-1]["id"] if len(rows) > limit else None}
