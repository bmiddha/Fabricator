"""Read-only Minecraft identities, aggregated strictly after server authorization."""
import logging
from datetime import datetime
from pathlib import Path
from uuid import UUID

from backend.auth.permissions import can
from backend.server.players import files, lastseen, observed
from backend.utils.time import iso_z_from_timestamp

logger = logging.getLogger(__name__)


def identity(uid, server_id):
    try:
        value = UUID(str(uid))
    except ValueError:
        return None
    namespace = "minecraft" if value.version == 4 else (
        f"offline:{server_id}" if value.version == 3 else f"unknown:{server_id}"
    )
    return f"{namespace}:{value}", namespace, str(value)


def _read(path, filename):
    target = files._resolve_within(path, filename)
    if not target.is_file():
        return [], "unavailable"
    entries = files.read_json_list(path, filename)
    if any(not isinstance(entry, dict) for entry in entries):
        raise ValueError("Invalid player entry")
    return entries, "available"


def aggregate(registry, servers):
    players = {}
    scopes = []
    for server in servers:
        sid = str(server["id"])
        if not can("server.view", sid) or not can("players.read", sid):
            continue
        if str(server.get("loader", "")).lower() == "velocity":
            continue
        scope = {"id": sid, "name": server.get("name", sid),
                 "history": "partial", "whitelist": "unavailable", "online": "unavailable"}
        scopes.append(scope)
        try:
            path = Path(registry.resolve_install_path(server))
            history, _ = _read(path, observed.HISTORY_FILE)
            for entry in history:
                if (not isinstance(entry.get("names"), list)
                        or any(not isinstance(name, str) for name in entry["names"])
                        or not isinstance(entry.get("lastSeen"), str)):
                    raise ValueError("Invalid observed history")
                seen = datetime.fromisoformat(entry["lastSeen"])
                if seen.tzinfo is None:
                    raise ValueError("Observed history timestamp needs a timezone")
                entry["lastSeen"] = iso_z_from_timestamp(seen.timestamp())
            whitelist, scope["whitelist"] = _read(path, "whitelist.json")
            known, _ = _read(path, "usercache.json")
            level = lastseen.read_level_name(path, server)
            world = files._resolve_within(path, level)
            # Playerdata is evidence of a saved session, never a usercache TTL.
            saved = []
            for relative in ("playerdata", "players/data"):
                folder = files._resolve_within(world, relative)
                if folder.is_dir():
                    saved.extend({"uuid": p.name.split(".")[0]} for p in folder.iterdir()
                                 if p.name.endswith((".dat", ".dat_old")))
            manager = registry.get_manager(sid)
            online = []
            if manager and registry.get_status(sid).get("status") == "running":
                with manager._lock:
                    online = [{"uuid": p.observed_uuid, "name": p.name,
                               "joinedAt": iso_z_from_timestamp(p.joined_at.timestamp())}
                              for p in manager._players.values() if p.observed_uuid]
                    unresolved = any(not p.observed_uuid for p in manager._players.values())
                scope["online"] = "partial" if unresolved else "available"
        except (OSError, ValueError, TypeError):
            logger.warning("Player directory source unavailable for server %s", sid)
            scope.update(history="unavailable", whitelist="unavailable", online="unavailable")
            continue

        local = {}
        for source, entries in (("known", known), ("whitelist", whitelist), ("saved", saved),
                                ("history", history), ("online", online)):
            for entry in entries:
                key = identity(entry.get("uuid"), sid)
                if key is None:
                    scope["history" if source != "whitelist" else "whitelist"] = "partial"
                    continue
                pid, namespace, uid = key
                row = local.setdefault(pid, {"id": pid, "namespace": namespace, "uuid": uid,
                                            "names": [], "name": uid, "joined": None,
                                            "lastSeen": None, "online": None,
                                            "whitelisted": False if scope["whitelist"] == "available" else None})
                names = entry.get("names", []) if source == "history" else []
                names = [n for n in names if isinstance(n, str)] if isinstance(names, list) else []
                if isinstance(entry.get("name"), str):
                    names = [*names, entry["name"]]
                    row["name"] = entry["name"]
                row["names"] = list(dict.fromkeys([*row["names"], *names]))
                if source == "whitelist":
                    row["whitelisted"] = True
                if source in {"saved", "history", "online"}:
                    row["joined"] = True
                if source == "history":
                    row["lastSeen"] = entry.get("lastSeen")
                if source == "online":
                    row["online"] = True
                    row["lastSeen"] = max(row["lastSeen"] or "", entry["joinedAt"])
        for pid, row in local.items():
            if scope["whitelist"] != "available" and row["whitelisted"] is False:
                row["whitelisted"] = None
            saved_seen = lastseen.last_seen_iso(path, level, row["uuid"])
            if saved_seen and (not row["lastSeen"] or saved_seen > row["lastSeen"]):
                row["lastSeen"] = saved_seen
            # An empty in-memory list is not a complete census after a panel restart.
            if row["online"] is None and registry.get_status(sid).get("status") == "stopped":
                row["online"] = False
            player = players.setdefault(pid, {"id": pid, "namespace": row["namespace"],
                                              "uuid": row["uuid"], "name": row["name"],
                                              "names": [], "servers": [], "lastSeen": None})
            player["names"] = list(dict.fromkeys([*player["names"], *row["names"]]))
            player["servers"].append({"id": sid, "name": scope["name"],
                                      **{k: row[k] for k in ("joined", "whitelisted", "online", "lastSeen")}})
            if row["lastSeen"] and (not player["lastSeen"] or row["lastSeen"] > player["lastSeen"]):
                player["lastSeen"] = row["lastSeen"]
                player["name"] = row["name"]
    return {"players": sorted(players.values(), key=lambda p: (p["name"].casefold(), p["id"])),
            "servers": scopes}
