"""Explicit, owner-bound grants to one immutable archive. Never log tokens."""
from __future__ import annotations

import re
import math
import secrets
import time
import uuid

from backend.modrinth import client_pack as packs
from backend.server import storage

MIN_TTL = 3600
MAX_TTL = 7 * 24 * 3600
MAX_LINKS = 20
TOKEN_PATTERN = re.compile(r"[a-f0-9]{64}\.[A-Za-z0-9_-]{43}")


def _path(server, store=packs):
    return store._directory(server) / "shares.json"


def _records(path, store=packs):
    records = store._read(path, [])
    if (not isinstance(records, list) or len(records) > MAX_LINKS
            or any(not isinstance(record, dict)
                   or not all(isinstance(record.get(key), str) for key in ("id", "server_id", "artifact_id", "token_hash"))
                   or "owner" not in record
                   or not re.fullmatch(r"[a-f0-9]{32}", record["id"])
                   or not re.fullmatch(r"[a-f0-9]{32}", record["artifact_id"])
                   or not re.fullmatch(r"[a-f0-9]{64}", record["token_hash"])
                   or not all(type(record.get(key)) in (int, float) and math.isfinite(record[key])
                              for key in ("created_at", "expires_at"))
                   or not MIN_TTL <= record["expires_at"] - record["created_at"] <= MAX_TTL
                   for record in records)):
        raise store.PackError("Invalid archive share state; check server storage")
    return records


def active(server, store=packs):
    records = _records(_path(server, store), store)
    now = time.time()
    return [record for record in records if record["expires_at"] > now]


def public(record):
    return {key: record[key] for key in ("id", "artifact_id", "created_at", "expires_at")}


def listing(server, owner, store=packs):
    records = active(server, store)
    store._atomic_json(_path(server, store), records)
    return [public(record) for record in records if record["owner"] == owner]


def create(server, artifact_id, ttl, owner, store=packs):
    if type(ttl) is not int or not MIN_TTL <= ttl <= MAX_TTL:
        raise store.PackError("Share expiry must be an integer from 3600 to 604800 seconds (1 hour-7 days)")
    records = active(server, store)
    if len(records) >= MAX_LINKS:
        raise store.PackError("This server has 20 active share links; revoke a link or wait for expiry")
    store.artifact_path(server, artifact_id)
    token = f'{store._key(server["id"])}.{secrets.token_urlsafe(32)}'
    now = time.time()
    record = {
        "id": uuid.uuid4().hex, "server_id": server["id"], "artifact_id": artifact_id,
        "owner": owner, "token_hash": store._key(token),
        "created_at": now, "expires_at": now + ttl,
    }
    store._atomic_json(_path(server, store), [*records, record])
    return {**public(record), "url": getattr(store, "SHARE_PAGE", "/client-pack-share") + "#" + token}


def revoke(server, share_id, owner, store=packs):
    records = active(server, store)
    matching = [record for record in records if record["id"] == share_id and record["owner"] == owner]
    if not matching:
        raise FileNotFoundError("Share link not found")
    store._atomic_json(_path(server, store), [record for record in records if record not in matching])


def resolve(token, store=packs):
    if not isinstance(token, str) or not TOKEN_PATTERN.fullmatch(token):
        return None
    directory = store._root() / token.split(".", 1)[0]
    records = _records(directory / "shares.json", store)
    digest = store._key(token)
    for record in records:
        if (secrets.compare_digest(record["token_hash"], digest)
                and record["expires_at"] > time.time()
                and store._key(record["server_id"]) == directory.name):
            server = storage.get_server(record["server_id"])
            if server:
                return server, record
    return None


def remove_artifact(server, artifact_id, *, confirmed=False, store=packs):
    records = active(server, store)
    if any(record["artifact_id"] == artifact_id for record in records) and not confirmed:
        raise store.PackError("This archive has active share links. Confirm deletion to invalidate all of them.")
    path, _ = store.artifact_path(server, artifact_id)
    store._atomic_json(_path(server, store), [record for record in records if record["artifact_id"] != artifact_id])
    path.unlink()
    metadata_path = getattr(store, "artifact_metadata_path", lambda p: p.with_suffix(".json"))
    metadata_path(path).unlink(missing_ok=True)
