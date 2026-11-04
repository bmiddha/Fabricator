"""Atomic DNS state, separate from server configuration."""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import tempfile
import threading
import uuid

from backend.core.config import get_config

lock = threading.RLock()


def path() -> Path:
    return Path(os.environ.get("FABRICATOR_DNS_FILE") or Path(get_config().SERVERS_FILE).with_name("dns.json"))


def load() -> dict:
    target = path()
    if not target.exists():
        return {
            "version": 1, "owner": f"fabricator-{uuid.uuid4().hex}",
            "config": None, "records": [], "bindings": {}, "applied": [],
            "status": {"state": "disabled", "error": None},
        }
    data = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("version") != 1:
        raise ValueError("Unsupported or damaged DNS state")
    return data


def save(data: dict) -> None:
    target = path()
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".dns-", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)


@contextmanager
def transaction():
    with lock:
        data = load()
        yield data
        save(data)
