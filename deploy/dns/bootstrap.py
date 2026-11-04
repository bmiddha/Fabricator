"""Initialize a new authoritative zone without modifying any existing zone."""

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request


def fqdn(value):
    value = value.strip().rstrip(".").lower()
    labels = value.split(".")
    if len(value) > 253 or not all(
        re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
        for label in labels
    ):
        raise ValueError("invalid DNS name in bootstrap configuration")
    return value + "."


def main():
    key = os.environ.get("POWERDNS_API_KEY", "")
    if not key:
        raise ValueError("POWERDNS_API_KEY is required")
    zone = fqdn(os.environ["DNS_ZONE"])
    nameservers = [
        fqdn(value)
        for value in os.environ.get("DNS_NAMESERVERS", "").split(",")
        if value.strip()
    ]
    if not nameservers:
        raise ValueError("DNS_NAMESERVERS must list the real authoritative nameservers")
    mailbox = fqdn(os.environ.get("DNS_SOA_MAILBOX") or "hostmaster." + zone)
    base = os.environ["POWERDNS_API_URL"].rstrip("/") + "/api/v1/servers/localhost/zones"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(method, url, payload=None):
        body = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(
            url,
            body,
            {"X-API-Key": key, "Content-Type": "application/json"},
            method=method,
        )
        return opener.open(req, timeout=20)

    try:
        with request("GET", base + "/" + urllib.parse.quote(zone, safe="")):
            print("Authoritative zone already exists; SOA/NS left unchanged.")
            return
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise RuntimeError(f"PowerDNS lookup failed: HTTP {error.code}") from None
    payload = {
        "name": zone,
        "kind": "Native",
        "nameservers": [],
        "soa_edit_api": "DEFAULT",
        "rrsets": [
            {
                "name": zone,
                "type": "SOA",
                "ttl": 300,
                "records": [
                    {
                        "content": f"{nameservers[0]} {mailbox} 1 10800 3600 604800 300",
                        "disabled": False,
                    }
                ],
            },
            {
                "name": zone,
                "type": "NS",
                "ttl": 300,
                "records": [
                    {"content": name, "disabled": False} for name in nameservers
                ],
            },
        ],
    }
    try:
        with request("POST", base, payload):
            print("Created authoritative zone with bootstrap SOA and NS.")
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"PowerDNS zone creation failed: HTTP {error.code}") from None


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, RuntimeError, OSError) as error:
        print(f"DNS bootstrap failed: {error}", file=sys.stderr)
        sys.exit(1)
