"""Private bounded chat journal, storing observed messages rather than raw logs."""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
import uuid

from backend.auth.service import data_dir
from backend.utils.time import iso_z_from_timestamp

MAX_PER_SERVER = 10_000
MAX_TOTAL = 100_000
RETENTION_SECONDS = 7 * 86400
MAX_PAGE = 200
_lock = threading.RLock()
_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 server_id TEXT NOT NULL, at REAL NOT NULL, ts TEXT NOT NULL,
 kind TEXT NOT NULL, sender TEXT NOT NULL, message TEXT NOT NULL,
 status TEXT, actor TEXT, source_id TEXT NOT NULL,
 UNIQUE(server_id, source_id)
);
CREATE INDEX IF NOT EXISTS chat_scope ON messages(server_id, id);
CREATE INDEX IF NOT EXISTS chat_expiry ON messages(at);
"""


class ChatStorageError(RuntimeError):
    pass


def db_path() -> Path:
    return data_dir() / "chat" / "chat.sqlite3"


@contextmanager
def _connection():
    db = None
    with _lock:
        try:
            path = db_path()
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(path.parent, 0o700)
            fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
            os.close(fd)
            os.chmod(path, 0o600)
            db = sqlite3.connect(path, timeout=1)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA secure_delete=ON")
            db.executescript(_SCHEMA)
            with db:
                yield db
        except (OSError, sqlite3.Error):
            raise ChatStorageError("Chat history storage is unavailable") from None
        finally:
            if db is not None:
                db.close()


def _prune(db, now):
    db.execute("DELETE FROM messages WHERE at <= ?", (now - RETENTION_SECONDS,))
    db.execute(
        "DELETE FROM messages WHERE id IN "
        "(SELECT id FROM messages ORDER BY id DESC LIMIT -1 OFFSET ?)",
        (MAX_TOTAL,),
    )


def _item(row):
    value = {key: row[key] for key in ("id", "ts", "kind", "sender", "message")}
    if row["status"]:
        value["status"] = row["status"]
    return value


def append(server_id, item, *, source_id=None, who=None, now=None):
    now = time.time() if now is None else now
    with _connection() as db:
        _prune(db, now)
        source_id = source_id or uuid.uuid4().hex
        db.execute(
            "INSERT INTO messages(server_id,at,ts,kind,sender,message,status,actor,source_id) "
            "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(server_id,source_id) DO NOTHING",
            (server_id, now, iso_z_from_timestamp(now), item["kind"], item["sender"],
             item["message"], item.get("status"), json.dumps(who) if who else None, source_id),
        )
        row = db.execute("SELECT * FROM messages WHERE server_id=? AND source_id=?",
                         (server_id, source_id)).fetchone()
        db.execute(
            "DELETE FROM messages WHERE id IN "
            "(SELECT id FROM messages WHERE server_id=? ORDER BY id DESC LIMIT -1 OFFSET ?)",
            (server_id, MAX_PER_SERVER),
        )
        _prune(db, now)
        if row is None:
            raise ChatStorageError("Chat history could not be recorded")
        return _item(row)


def finish_send(server_id, item_id, status):
    if status not in {"submitted", "failed"}:
        raise ValueError("Invalid chat submission status")
    with _connection() as db:
        changed = db.execute(
            "UPDATE messages SET status=? WHERE server_id=? AND id=? AND kind='fabricator'",
            (status, server_id, item_id),
        ).rowcount
        if changed != 1:
            raise ChatStorageError("Chat history submission outcome could not be recorded")
        return _item(db.execute("SELECT * FROM messages WHERE server_id=? AND id=?",
                                (server_id, item_id)).fetchone())


def history(server_id, *, before=None, after=None, limit=100, now=None):
    now = time.time() if now is None else now
    with _connection() as db:
        _prune(db, now)
        where, values = "server_id=?", [server_id]
        if before is not None:
            where += " AND id<?"
            values.append(before)
        if after is not None:
            where += " AND id>?"
            values.append(after)
        order = "ASC" if after is not None else "DESC"
        rows = db.execute(f"SELECT * FROM messages WHERE {where} ORDER BY id {order} LIMIT ?",
                          (*values, limit + 1)).fetchall()
        more = len(rows) > limit
        rows = rows[:limit]
        if after is None:
            rows.reverse()
        earliest, latest = db.execute("SELECT MIN(id), MAX(id) FROM messages WHERE server_id=?",
                                      (server_id,)).fetchone()
        outcomes = []
        if after is not None:
            outcomes = [
                {"id": row["id"], "status": row["status"]}
                for row in db.execute(
                    "SELECT id,status FROM messages WHERE server_id=? AND id<=? AND kind='fabricator' "
                    "ORDER BY id DESC LIMIT ?", (server_id, after, MAX_PAGE),
                ).fetchall()
            ]
        return {
            "items": [_item(row) for row in rows],
            "nextBefore": rows[0]["id"] if after is None and more else None,
            "hasMoreAfter": more if after is not None else False,
            "latestId": latest or 0,
            "earliestId": earliest or 0,
            "outcomes": outcomes,
        }
