"""Read-side accessor for the online-players dict maintained by ServerManager.

The stdout-stream loop in manager.py writes OnlinePlayer entries under
ServerManager._lock. This module reads them out and optionally enriches each
with a UUID from the Mojang cache — the enrichment write happens under the
same lock to stay consistent with the existing add/discard sites.

Mojang HTTP / file-cache lookups happen *outside* the lock; only the final
attribute assignment is locked. The lock is fine-grained (microseconds), so
this keeps the stdout-stream loop unblocked even under load.
"""
from typing import Any, Dict, List
from itertools import islice

from backend.server.manager import OnlinePlayer
from backend.server.players import mojang
from backend.server.players.directory import identity


def list_online(registry, server: Dict[str, Any]) -> List[dict]:
    """Snapshot the server's online players as JSON-serialisable dicts."""
    server_id = str(server["id"])
    manager = registry.get_manager(server_id)
    if not manager:
        return []

    try:
        install_path = registry.resolve_install_path(server)
    except (ValueError, OSError):
        # Misconfigured server entry — return the player snapshot without UUIDs.
        install_path = None

    # Snapshot inside the lock to avoid iteration-mid-mutation.
    with manager._lock:  # noqa: SLF001 — mirrors existing in-module locking
        snapshot = list(manager._players.values())

    # Resolve any missing UUIDs from the cache (no network) outside the lock.
    enriched: List[OnlinePlayer] = []
    for player in snapshot:
        if player.uuid is None and install_path is not None:
            cached = mojang.lookup_cached(install_path, player.name)
            if cached:
                # Backfill under the lock so a parallel reader sees a
                # consistent UUID value.
                with manager._lock:
                    current = manager._players.get(player.name)
                    if current is not None and current.uuid is None:
                        current.uuid = cached
                        enriched.append(current)
                        continue
        enriched.append(player)

    return [
        {
            "name": p.name,
            "uuid": p.uuid,
            "joinedAt": p.joined_at.isoformat(),
        }
        for p in enriched
    ]


def preview_online(registry, server: Dict[str, Any]) -> dict:
    """At most ten observed sessions; never resolve a missing UUID by name."""
    sid = str(server["id"])
    result = {"players": [], "total": 0, "status": "unavailable",
              "offlineMode": server.get("onlineMode") is False}
    if str(server.get("loader", "")).lower() == "velocity":
        return result
    status = registry.get_status(sid).get("status")
    if status == "stopped":
        return {**result, "status": "offline"}
    if status != "running":
        return result
    manager = registry.get_manager(sid)
    if manager is None:
        return result
    with manager._lock:
        total = len(manager._players)
        snapshot = [(p.name, p.observed_uuid) for p in islice(manager._players.values(), 10)]
    players = []
    for name, uid in snapshot:
        key = identity(uid, sid)
        players.append({"name": name, "uuid": key[2] if key else None,
                        "id": key[0] if key else None, "namespace": key[1] if key else None})
    # The tracker is not a complete census after a panel restart.
    return {**result, "players": players, "total": total, "status": "partial"}
