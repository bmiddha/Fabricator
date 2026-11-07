"""Fixed-resolution SQLite retention; no console output or report contents."""
from contextlib import closing, contextmanager
import json
import os
import sqlite3
import time

RETENTION_SECONDS = 86400
MAX_SAMPLES = 2880
MAX_CAPTURES = 20


@contextmanager
def database():
    from backend.server.storage import _resolve_servers_file
    path = _resolve_servers_file().parent / "diagnostics.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    os.close(fd)
    with closing(sqlite3.connect(path, timeout=5)) as db, db:
        db.execute("PRAGMA auto_vacuum=FULL")
        db.execute("PRAGMA secure_delete=ON")
        db.executescript("""
            CREATE TABLE IF NOT EXISTS samples (
                server_id TEXT NOT NULL, ts INTEGER NOT NULL, data TEXT NOT NULL,
                PRIMARY KEY(server_id, ts));
            CREATE TABLE IF NOT EXISTS captures (
                id TEXT PRIMARY KEY, server_id TEXT NOT NULL,
                created REAL NOT NULL, data TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS capture_server ON captures(server_id, created);
        """)
        yield db


def append(server_id, sample):
    with database() as db:
        db.execute("INSERT OR REPLACE INTO samples VALUES (?, ?, ?)",
                   (server_id, sample["ts"], json.dumps(sample, allow_nan=False)))
        db.execute("DELETE FROM samples WHERE ts < ?", (time.time() - RETENTION_SECONDS,))
        db.execute("""DELETE FROM samples WHERE server_id=? AND ts NOT IN
                    (SELECT ts FROM samples WHERE server_id=? ORDER BY ts DESC LIMIT ?)""",
                   (server_id, server_id, MAX_SAMPLES))


def history(server_id, seconds):
    with database() as db:
        return [json.loads(row[0]) for row in db.execute(
            "SELECT data FROM samples WHERE server_id=? AND ts>=? ORDER BY ts",
            (server_id, time.time() - seconds))]


def save_capture(server_id, capture):
    with database() as db:
        db.execute("INSERT OR REPLACE INTO captures VALUES (?, ?, ?, ?)",
                   (capture["id"], server_id, capture["created"], json.dumps(capture)))
        db.execute("""DELETE FROM captures WHERE server_id=? AND id NOT IN
                    (SELECT id FROM captures WHERE server_id=? ORDER BY created DESC LIMIT ?)""",
                   (server_id, server_id, MAX_CAPTURES))


def captures(server_id):
    with database() as db:
        return [json.loads(row[0]) for row in db.execute(
            "SELECT data FROM captures WHERE server_id=? AND created>=? ORDER BY created DESC LIMIT ?",
            (server_id, time.time() - RETENTION_SECONDS, MAX_CAPTURES))]


def cleanup(server_ids):
    with database() as db:
        db.execute("DELETE FROM samples WHERE ts < ?", (time.time() - RETENTION_SECONDS,))
        db.execute("DELETE FROM captures WHERE created < ?", (time.time() - RETENTION_SECONDS,))
        for table in ("samples", "captures"):
            for (sid,) in db.execute(f"SELECT DISTINCT server_id FROM {table}").fetchall():
                if sid not in server_ids:
                    db.execute(f"DELETE FROM {table} WHERE server_id=?", (sid,))


def delete(server_id):
    with database() as db:
        db.execute("DELETE FROM samples WHERE server_id=?", (server_id,))
        db.execute("DELETE FROM captures WHERE server_id=?", (server_id,))
