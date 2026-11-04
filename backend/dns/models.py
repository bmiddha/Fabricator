"""Validation shared by DNS settings, bindings, and reconciliation."""
from __future__ import annotations

import ipaddress
import re
import shlex
from urllib.parse import urlparse

TYPES = frozenset({"A", "AAAA", "CNAME", "SRV", "TXT", "MX", "CAA", "NS"})
_LABEL = re.compile(r"^[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?$")


def name(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("A DNS name is required")
    result = value.strip().rstrip(".").lower()
    if len(result) > 253 or not all(_LABEL.fullmatch(p) for p in result.split(".")):
        raise ValueError("Invalid DNS name")
    return result


def inside(host: str, zone: str) -> bool:
    return host == zone or host.endswith("." + zone)


def integer(value: object, low: int, high: int, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f"{field} must be an integer between {low} and {high}")
    return value


def config(data: object) -> dict:
    if not isinstance(data, dict) or set(data) - {"enabled", "url", "zone", "domain"}:
        raise ValueError("Expected enabled, url, zone, and optional domain")
    enabled = data.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError("enabled must be a boolean")
    url = data.get("url", "")
    if not isinstance(url, str):
        raise ValueError("Webhook URL must be a string")
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("An http(s) webhook URL is required")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Webhook credentials, query strings, and fragments are not allowed in URLs")
    zone = name(data.get("zone"))
    domain = name(data.get("domain") or zone)
    if not inside(domain, zone):
        raise ValueError("Managed domain must be within the provider zone")
    return {"enabled": enabled, "url": url.rstrip("/"), "zone": zone, "domain": domain}


def record(data: object, domain: str) -> dict:
    if not isinstance(data, dict) or set(data) - {"dnsName", "recordType", "recordTTL", "targets"}:
        raise ValueError("Expected dnsName, recordType, recordTTL, and targets")
    host = name(data.get("dnsName"))
    if not inside(host, domain):
        raise ValueError(f"Record must be within {domain}")
    if host.startswith("_fabricator-"):
        raise ValueError("The _fabricator- prefix is reserved for ownership records")
    kind = data.get("recordType")
    if kind not in TYPES:
        raise ValueError("Unsupported DNS record type")
    ttl = integer(data.get("recordTTL", 300), 60, 2147483647, "TTL")
    targets = data.get("targets")
    if not isinstance(targets, list) or not 1 <= len(targets) <= 100:
        raise ValueError("Each record requires between 1 and 100 targets")
    normalized = []
    for raw in targets:
        if not isinstance(raw, str) or not raw or len(raw) > 4096 or any(c in raw for c in "\x00\r\n"):
            raise ValueError("Invalid DNS target")
        target = raw.strip() if kind != "TXT" else raw
        if kind in {"A", "AAAA"}:
            try:
                address = ipaddress.ip_address(target)
            except ValueError as exc:
                raise ValueError(f"{kind} requires an IP address") from exc
            if address.version != (4 if kind == "A" else 6):
                raise ValueError(f"Wrong IP family for {kind}")
            target = str(address)
        elif kind in {"CNAME", "NS"}:
            target = name(target) + "."
            if kind == "CNAME" and name(target) == host:
                raise ValueError("CNAME cannot target itself")
        elif kind in {"SRV", "MX"}:
            parts = target.split()
            fields = 3 if kind == "SRV" else 1
            if len(parts) != fields + 1 or not all(p.isascii() and p.isdigit() for p in parts[:fields]):
                raise ValueError(f"{kind} requires {'priority weight port target' if kind == 'SRV' else 'priority target'}")
            values = [integer(int(p), 0, 65535, kind) for p in parts[:fields]]
            destination = "." if parts[-1] == "." else name(parts[-1]) + "."
            target = " ".join([*(str(p) for p in values), destination])
        elif kind == "CAA":
            parts = shlex.split(target)
            if len(parts) != 3 or not parts[0].isdigit() or int(parts[0]) > 255 or not re.fullmatch(r"[a-zA-Z0-9]+", parts[1]):
                raise ValueError('CAA requires flags tag "value"')
            import json
            target = f"{int(parts[0])} {parts[1]} {json.dumps(parts[2])}"
        normalized.append(target)
    if kind == "CNAME" and (len(targets) != 1 or host == domain):
        raise ValueError("CNAME requires one target and cannot be the managed domain apex")
    if kind == "NS" and host == domain:
        raise ValueError("Apex NS records are managed by the authoritative provider")
    return {"dnsName": host, "recordType": kind, "recordTTL": ttl, "targets": sorted(set(normalized))}


def records(data: object, domain: str) -> list[dict]:
    if not isinstance(data, list) or len(data) > 1000:
        raise ValueError("Expected a list of at most 1000 record sets")
    result = [record(r, domain) for r in data]
    keys = [(r["dnsName"], r["recordType"]) for r in result]
    if len(keys) != len(set(keys)):
        raise ValueError("Duplicate record set")
    aliases = {r["dnsName"] for r in result if r["recordType"] == "CNAME"}
    for r in result:
        if r["dnsName"] in aliases and r["recordType"] != "CNAME":
            raise ValueError("CNAME cannot coexist with other records")
        if r["recordType"] == "SRV" and any(t.split()[-1] != "." and name(t.split()[-1]) in aliases for t in r["targets"]):
            raise ValueError("SRV targets must have address records, not CNAME aliases")
    return result


def binding(data: object, domain: str) -> dict:
    allowed = {"enabled", "hostname", "addresses", "target", "srv", "ttl", "priority", "weight"}
    if not isinstance(data, dict) or set(data) - allowed:
        raise ValueError("Unknown DNS binding field")
    if not isinstance(data.get("enabled", False), bool):
        raise ValueError("enabled must be a boolean")
    if not data.get("enabled"):
        return {"enabled": False}
    host = name(data.get("hostname"))
    if not inside(host, domain) or host.startswith("_fabricator-"):
        raise ValueError("Hostname must be within the managed domain")
    addresses = data.get("addresses", [])
    if not isinstance(addresses, list) or len(addresses) > 20:
        raise ValueError("addresses must be a list of at most 20 IP addresses")
    try:
        addresses = sorted({str(ipaddress.ip_address(a)) for a in addresses if isinstance(a, str)})
    except ValueError as exc:
        raise ValueError("Invalid public IP address") from exc
    if any(not isinstance(a, str) for a in data.get("addresses", [])):
        raise ValueError("Invalid public IP address")
    target = name(data["target"]) if data.get("target") else ""
    if bool(addresses) == bool(target):
        raise ValueError("Supply public IP addresses or a hostname target, not both")
    srv = data.get("srv", True)
    if not isinstance(srv, bool):
        raise ValueError("srv must be a boolean")
    if srv and not addresses:
        raise ValueError("SRV bindings require explicit public IP addresses to avoid alias targets")
    return {
        "enabled": True, "hostname": host, "addresses": addresses, "target": target,
        "srv": srv, "ttl": integer(data.get("ttl", 300), 60, 2147483647, "TTL"),
        "priority": integer(data.get("priority", 0), 0, 65535, "priority"),
        "weight": integer(data.get("weight", 0), 0, 65535, "weight"),
    }
