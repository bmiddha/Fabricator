"""Persist only UUID-attributed joins observed by the existing stdout reader."""
import logging
import threading
from pathlib import Path
from uuid import UUID

from backend.server.players import files
from backend.utils.time import iso_z_now

HISTORY_FILE = ".fabricator/player-history.json"
_lock = threading.Lock()
logger = logging.getLogger(__name__)


def record(install_path, uid, name, *, joined=False):
    try:
        uid = str(UUID(uid))
        with _lock:
            entries = files.read_json_list(install_path, HISTORY_FILE)
            entry = next((e for e in entries if e.get("uuid") == uid), None)
            if entry is None:
                if not joined:
                    return
                entry = {"uuid": uid, "names": [], "firstJoined": iso_z_now()}
                entries.append(entry)
            if name not in entry["names"]:
                entry["names"].append(name)
            entry["name"] = name
            entry["lastSeen"] = iso_z_now()
            if joined:
                entry["lastJoined"] = entry["lastSeen"]
            files.write_json_list(Path(install_path), HISTORY_FILE, entries)
    except (OSError, ValueError, KeyError, TypeError):
        # History failure must not interrupt the server's stdout drain.
        logger.error("Could not persist observed player history")
