"""Secret destination storage, bounded delivery attempts and disk-pressure sampling."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import shutil
import sqlite3
import threading
import time
import uuid

from cryptography.fernet import Fernet, InvalidToken

from backend.events import store, transport

logger = logging.getLogger(__name__)
_workers = set()
_init_lock = threading.Lock()
RETRY_DELAYS = (30, 120, 600, 1800)  # five attempts total, including the first


def _cipher():
    path = store.db_path().parent / "webhooks.key"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        os.chmod(path, 0o600)
        key = path.read_bytes()
    else:
        key = Fernet.generate_key()
        with os.fdopen(fd, "wb") as handle:
            handle.write(key)
            handle.flush()
            os.fsync(handle.fileno())
    return Fernet(key)


def destinations():
    with store.connection() as db:
        return [
            {"id": row["id"], "name": row["name"], "kind": row["kind"],
             "enabled": bool(row["enabled"]), "alerts": json.loads(row["alerts"]),
             "has_url": True}
            for row in db.execute("SELECT id,name,kind,enabled,alerts FROM destinations ORDER BY name,id")
        ]


def save_destination(data, destination_id=None):
    if not isinstance(data, dict) or set(data) - {"name", "kind", "url", "enabled", "alerts"}:
        raise ValueError("Expected name, kind, url, enabled and alerts")
    name = data.get("name")
    kind = data.get("kind")
    enabled = data.get("enabled", True)
    alerts = data.get("alerts", sorted(store.ALERTS))
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
        raise ValueError("Destination name must be 1-80 characters")
    if not isinstance(kind, str) or kind not in {"webhook", "discord"} or type(enabled) is not bool:
        raise ValueError("Invalid destination type or enabled flag")
    if not isinstance(alerts, list) or not alerts or any(not isinstance(a, str) or a not in store.ALERTS for a in alerts):
        raise ValueError("Select at least one supported alert")
    with store.connection() as db:
        old = db.execute("SELECT * FROM destinations WHERE id=?", (destination_id,)).fetchone() if destination_id else None
        if destination_id and not old:
            raise LookupError("Destination not found")
        if not old and db.execute("SELECT count(*) FROM destinations").fetchone()[0] >= 20:
            raise ValueError("At most 20 destinations are supported")
        url = data.get("url")
        if url is not None:
            parsed, host, _, _ = transport.target(url)
            if kind == "discord" and (host not in {"discord.com", "discordapp.com"} or not parsed.path.startswith("/api/webhooks/")):
                raise ValueError("Discord destinations require a Discord webhook URL")
            secret = _cipher().encrypt(url.encode()).decode()
        elif old:
            if kind != old["kind"]:
                raise ValueError("Provide a new URL when changing destination type")
            secret = old["secret"]
        else:
            raise ValueError("Webhook URL is required")
        destination_id = destination_id or uuid.uuid4().hex
        db.execute("INSERT OR REPLACE INTO destinations VALUES(?,?,?,?,?,?)",
                   (destination_id, name.strip(), kind, secret, int(enabled), json.dumps(sorted(set(alerts)))))
        # Do not send queued metadata to a changed recipient or subscription.
        if old:
            db.execute("UPDATE deliveries SET state='cancelled',error='destination_changed' WHERE destination_id=? AND state='pending'", (destination_id,))
    return destination_id


def delete_destination(destination_id):
    with store.connection() as db:
        changed = db.execute("DELETE FROM destinations WHERE id=?", (destination_id,)).rowcount
        db.execute("UPDATE deliveries SET state='cancelled',error='destination_deleted' WHERE destination_id=? AND state='pending'", (destination_id,))
    return bool(changed)


def test_destination(destination_id):
    with store.connection() as db:
        row = db.execute("SELECT enabled FROM destinations WHERE id=?", (destination_id,)).fetchone()
        if not row:
            raise LookupError("Destination not found")
        if not row["enabled"]:
            raise ValueError("Enable the destination before testing")
        now = time.time()
        key = f"test:{destination_id}"
        last = db.execute("SELECT at FROM cooldowns WHERE key=?", (key,)).fetchone()
        if last and now - last["at"] < 60:
            raise ValueError("Wait 60 seconds between test notifications")
        db.execute("INSERT OR REPLACE INTO cooldowns VALUES(?,?)", (key, now))
        event_id = store._insert(db, server_id=None, action="notification.test", outcome="accepted", who=store.actor())
        payload = json.dumps({"id": event_id, "at": now, "alert": "notification.test", "server_id": None,
                              "action": "notification.test", "outcome": "accepted", "path": "/settings/notifications", "ref": None})
        store._enqueue(db, destination_id, event_id, payload, now)
        return event_id


def deliveries():
    with store.connection() as db:
        store._prune(db, time.time())
        return [dict(row) for row in db.execute(
            "SELECT d.id,d.destination_id,c.name,d.event_id,d.created,d.state,d.attempts,d.next_at,d.error "
            "FROM deliveries d LEFT JOIN destinations c ON c.id=d.destination_id ORDER BY d.id DESC LIMIT 200"
        )]


def deliver_due(now=None):
    now = time.time() if now is None else now
    with store.connection() as db:
        db.execute("UPDATE deliveries SET state=CASE WHEN attempts>=5 THEN 'failed' ELSE 'pending' END, "
                   "error='delivery_interrupted' WHERE state='sending' AND next_at<=?", (now,))
        store._prune(db, now)
        row = db.execute(
            "SELECT d.*,c.secret,c.kind,c.enabled FROM deliveries d "
            "JOIN destinations c ON c.id=d.destination_id "
            "WHERE d.state='pending' AND d.next_at<=? ORDER BY d.id LIMIT 1", (now,)
        ).fetchone()
        if row is None:
            return False
        # Claim before network IO; no database lock is held during delivery.
        if db.execute("UPDATE deliveries SET state='sending',attempts=attempts+1,next_at=? WHERE id=? AND state='pending'",
                      (now + 30, row["id"])).rowcount != 1:
            return False
    attempts = row["attempts"] + 1
    error = None
    try:
        if not row["enabled"]:
            raise transport.DeliveryError("destination_disabled")
        url = _cipher().decrypt(row["secret"].encode()).decode()
        transport.deliver(url, row["kind"], json.loads(row["payload"]))
    except (InvalidToken, ValueError, UnicodeError):
        error = "invalid_destination"
    except transport.DeliveryError as exc:
        error = str(exc)
    except Exception:
        # Keep unforeseen failures visible without logging secret-bearing text.
        logger.error("Unexpected notification transport failure")
        error = "delivery_internal_error"
    state = "sent"
    next_at = now
    if error:
        permanent = error in {"target_blocked", "invalid_destination", "destination_disabled"} or (
            error.startswith("http_") and error not in {"http_408", "http_429"} and not error.startswith("http_5")
        )
        state = "failed" if permanent or attempts > len(RETRY_DELAYS) else "pending"
        if state == "pending":
            next_at = now + RETRY_DELAYS[attempts - 1]
        logger.warning("Notification delivery %s: %s (%s)", row["id"], state, error)
    with store.connection() as db:
        db.execute("UPDATE deliveries SET state=?,next_at=?,error=? WHERE id=? AND state='sending'",
                   (state, next_at, error, row["id"]))
    return True


def _existing_directory(path):
    path = Path(path).expanduser()
    while not path.exists() and path != path.parent:
        path = path.parent
    return path


def sample_disks():
    from backend.core.config import get_config
    from backend.server import storage
    from backend.backups import storage as backup_storage
    config = get_config()
    scopes = [("panel", None, "panel", store.db_path().parent)]
    for server in storage.get_all_servers():
        sid = server["id"]
        root = Path(server.get("installPath") or sid)
        root = root if root.is_absolute() else Path(config.SERVERS_ROOT) / root
        scopes.append((f"server:{sid}", sid, "server", root))
    for cfg in backup_storage.all_configs():
        if cfg.get("storagePath") and (cfg.get("destination") or {}).get("type", "local") == "local":
            sid = cfg.get("serverId")
            scopes.append((f"backup:{cfg['id']}", sid, "backup", cfg["storagePath"]))
    for scope, sid, kind, path in scopes:
        try:
            usage = shutil.disk_usage(_existing_directory(path))
            percent = 100 * usage.used / usage.total
            with store.connection() as db:
                old = db.execute("SELECT pressured FROM disk_state WHERE scope=?", (scope,)).fetchone()
                pressured = bool(old and old["pressured"])
                new = (percent > 85 or usage.free < 3 * 1024**3) if pressured else (percent >= 90 or usage.free < 2 * 1024**3)
                now = time.time()
                db.execute("INSERT OR REPLACE INTO disk_state VALUES(?,?,?)", (scope, int(new), now))
                if new and not pressured:
                    last = db.execute("SELECT at FROM cooldowns WHERE key=?", (scope,)).fetchone()
                    if not last or now - last["at"] >= 3600:
                        db.execute("INSERT OR REPLACE INTO cooldowns VALUES(?,?)", (scope, now))
                        store._insert(db, server_id=sid, action=f"disk.{kind}.pressure",
                                      outcome="succeeded", alert="disk.pressure", who={"kind": "system", "id": "disk-monitor"})
                elif pressured and not new:
                    store._insert(db, server_id=sid, action=f"disk.{kind}.recovered",
                                  outcome="succeeded", alert="disk.recovered", who={"kind": "system", "id": "disk-monitor"})
        except (OSError, sqlite3.Error):
            logger.error("Disk pressure sample failed for a configured storage scope")


def init_events(app):
    if os.environ.get("FABRICATOR_DISABLE_SCHEDULER") == "1" or os.environ.get("FABRICATOR_DISABLE_NOTIFICATIONS") == "1":
        return
    path = str(store.db_path())
    with _init_lock:
        if path in _workers:
            return
        store.recover()
        _workers.add(path)

    def worker():
        last_disk = 0
        with app.app_context():
            while True:
                try:
                    now = time.time()
                    if now - last_disk >= 60:
                        sample_disks()
                        last_disk = now
                    if deliver_due(now):
                        continue
                except Exception:
                    logger.error("Notification worker iteration failed; will retry")
                time.sleep(2)
    threading.Thread(target=worker, name="fabricator-notifications", daemon=True).start()
