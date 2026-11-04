"""Desired-state reconciliation with durable ownership and deletion inventory."""
from __future__ import annotations

from datetime import datetime, timezone
import ipaddress
import logging
import os
import threading

from backend.server import storage as servers
from backend.utils.time import iso_z_now
from . import models, storage
from .client import ProviderError, Webhook

logger = logging.getLogger(__name__)
_sync_lock = threading.Lock()


def key(record: dict) -> tuple[str, str]:
    return models.name(record["dnsName"]), record["recordType"]


def equivalent(a: dict, b: dict) -> bool:
    return (
        key(a) == key(b)
        and sorted(a.get("targets") or []) == sorted(b.get("targets") or [])
        and a.get("recordTTL", 0) == b.get("recordTTL", 0)
    )


def marker(record: dict, owner: str) -> dict:
    host, kind = key(record)
    return {
        "dnsName": f"_fabricator-{kind.lower()}.{host}", "recordType": "TXT",
        "recordTTL": 300,
        "targets": [f"heritage=external-dns,external-dns/owner={owner},external-dns/resource=fabricator/{kind.lower()}/{host}"],
    }


def _is_owned(value: dict | None, expected: dict) -> bool:
    if not value:
        return False
    # DNS servers can return TXT strings in presentation-format quotes.
    return sorted(t.strip('"') for t in value.get("targets", [])) == expected["targets"]


def desired(data: dict) -> list[dict]:
    cfg = data["config"]
    if not cfg or not cfg["enabled"]:
        return []
    records = list(data["records"])
    current = {s["id"]: s for s in servers.get_all_servers()}
    for server_id, binding in data["bindings"].items():
        if not binding.get("enabled") or server_id not in current:
            continue
        server = current[server_id]
        host, ttl = binding["hostname"], binding["ttl"]
        for version, kind in ((4, "A"), (6, "AAAA")):
            addresses = [a for a in binding["addresses"] if ipaddress.ip_address(a).version == version]
            if addresses:
                records.append({"dnsName": host, "recordType": kind, "recordTTL": ttl, "targets": addresses})
        if binding["target"]:
            records.append({"dnsName": host, "recordType": "CNAME", "recordTTL": ttl, "targets": [binding["target"] + "."]})
        if binding["srv"]:
            port = int(server.get("port", 25565))
            models.integer(port, 1, 65535, "Server port")
            records.append({
                "dnsName": f"_minecraft._tcp.{host}", "recordType": "SRV", "recordTTL": ttl,
                "targets": [f"{binding['priority']} {binding['weight']} {port} {host}."],
            })
    return models.records(records, cfg["domain"])


def _changes(data: dict, cfg: dict, old: list[dict], wanted: list[dict], pending: list[dict]) -> dict:
    client = Webhook(cfg["url"])
    client.negotiate(cfg["domain"])
    adjusted = client.adjust(wanted)
    # Provider normalization may change TTL or target presentation but not names/types.
    adjusted = models.records([
        {k: r[k] for k in ("dnsName", "recordType", "recordTTL", "targets")}
        for r in adjusted
    ], cfg["domain"])
    if {key(r) for r in adjusted} != {key(r) for r in wanted}:
        raise ProviderError("Provider changed the requested record identities")
    live = {}
    for r in client.records():
        k = key(r)
        if k in live:
            raise ProviderError("Provider returned duplicate DNS record sets")
        live[k] = r
    previous = {key(r): r for r in old}
    intents = {key(r): r for r in pending}
    wanted_map = {key(r): r for r in adjusted}
    for (host, kind), record in wanted_map.items():
        for live_key in live:
            if live_key[0] != host or live_key == (host, kind):
                continue
            if (kind == "CNAME" or live_key[1] == "CNAME") and not (
                live_key in previous and live_key not in wanted_map
            ):
                raise ValueError(f"Existing CNAME/data conflict for {host}")
        if kind == "SRV":
            for value in record["targets"]:
                destination = value.split()[-1]
                alias_key = (models.name(destination), "CNAME") if destination != "." else None
                if alias_key in live and not (alias_key in previous and alias_key not in wanted_map):
                    raise ValueError(f"SRV target is a CNAME: {destination}")
    changes = {"Create": [], "Delete": [], "UpdateOld": [], "UpdateNew": []}
    for k in sorted(previous.keys() | wanted_map.keys() | intents.keys()):
        record = wanted_map.get(k) or previous.get(k) or intents[k]
        claim = marker(record, data["owner"])
        existing, ownership = live.get(k), live.get(key(claim))
        owned = _is_owned(ownership, claim)
        if ownership and not owned:
            raise ValueError(f"Ownership conflict for {k[0]} {k[1]}")
        if existing and not owned:
            raise ValueError(f"Existing unmanaged record conflicts with {k[0]} {k[1]}")
        if existing and owned:
            known = [r for r in (previous.get(k), intents.get(k), wanted_map.get(k)) if r]
            if not any(equivalent(existing, r) for r in known):
                raise ValueError(f"Record changed outside Fabricator: {k[0]} {k[1]}")
        target = wanted_map.get(k)
        if target:
            if not ownership:
                changes["Create"].append(claim)
            if not existing:
                changes["Create"].append(target)
            elif not equivalent(existing, target):
                changes["UpdateOld"].append(existing)
                changes["UpdateNew"].append(target)
        else:
            if existing:
                changes["Delete"].append(existing)
            if ownership:
                changes["Delete"].append(ownership)
    return {"config": cfg, "changes": changes, "desired": adjusted}


def _groups(data: dict, wanted: list[dict]):
    cfg = data["config"]
    active_key = (cfg["url"], cfg["domain"]) if cfg else None
    found = False
    for entry in data["applied"]:
        entry_key = (entry["config"]["url"], entry["config"]["domain"])
        active = entry_key == active_key
        found = found or active
        yield entry, wanted if active else []
    if cfg and cfg["enabled"] and not found:
        yield {"config": dict(cfg), "records": [], "pending": []}, wanted


def preview() -> dict:
    with _sync_lock, storage.lock:
        data = storage.load()
        wanted = desired(data)
        groups = [_changes(data, e["config"], e["records"], w, e.get("pending", [])) for e, w in _groups(data, wanted)]
        return {"desired": wanted, "groups": groups}


def sync() -> dict:
    with _sync_lock, storage.lock:
        data = storage.load()
        if not data["config"] and not data["applied"]:
            return data["status"]
        try:
            wanted = desired(data)
            data["status"] = {"state": "pending", "error": None, "attemptedAt": iso_z_now()}
            storage.save(data)
            for entry, target in list(_groups(data, wanted)):
                group = _changes(data, entry["config"], entry["records"], target, entry.get("pending", []))
                if entry not in data["applied"]:
                    data["applied"].append(entry)
                entry["pending"] = group["desired"]
                storage.save(data)
                if any(group["changes"].values()):
                    Webhook(entry["config"]["url"]).apply(group["changes"])
                # Verify authoritative provider state after applying, before claiming success.
                confirmed = _changes(data, entry["config"], entry["records"], target, entry["pending"])
                if any(confirmed["changes"].values()):
                    raise ProviderError("Provider did not converge; retry synchronization")
                entry["records"] = confirmed["desired"]
                entry["pending"] = []
                if not entry["records"]:
                    data["applied"].remove(entry)
                storage.save(data)
            data["status"] = {
                "state": "applied" if data["config"] and data["config"]["enabled"] else "disabled",
                "error": None, "syncedAt": iso_z_now(),
            }
        except (ProviderError, ValueError, KeyError, TypeError) as exc:
            logger.warning("DNS synchronization failed: %s", exc)
            data["status"] = {"state": "failed", "error": str(exc), "attemptedAt": iso_z_now()}
        storage.save(data)
        return data["status"]


def request_sync() -> None:
    from backend.backups.scheduler import scheduler
    if scheduler.running and scheduler.get_job("fabricator-dns"):
        scheduler.modify_job("fabricator-dns", next_run_time=datetime.now(timezone.utc))


def init_dns() -> None:
    if os.environ.get("FABRICATOR_DISABLE_SCHEDULER") == "1":
        return
    from backend.backups.scheduler import scheduler
    if scheduler.running:
        scheduler.add_job(
            id="fabricator-dns", func=sync, trigger="interval", seconds=60,
            next_run_time=datetime.now(timezone.utc), replace_existing=True,
            max_instances=1, coalesce=True,
        )
