"""ExternalDNS webhook client. DNS provider credentials never enter Flask."""
from __future__ import annotations

import os
import requests

MEDIA_TYPE = "application/external.dns.webhook+json;version=1"


class ProviderError(RuntimeError):
    pass


class Webhook:
    def __init__(self, url: str):
        self.url = url
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.verify = os.environ.get("FABRICATOR_DNS_CA_FILE") or True
        cert = os.environ.get("FABRICATOR_DNS_CLIENT_CERT")
        key = os.environ.get("FABRICATOR_DNS_CLIENT_KEY")
        if key and not cert:
            raise ProviderError("FABRICATOR_DNS_CLIENT_KEY requires FABRICATOR_DNS_CLIENT_CERT")
        if cert:
            self.session.cert = (cert, key) if key else cert

    def call(self, method: str, path: str = "", data=None, expected: int = 200):
        try:
            with self.session.request(
                method, self.url + "/" + path, json=data,
                headers={"Accept": MEDIA_TYPE, "Content-Type": MEDIA_TYPE},
                timeout=(5, 20), allow_redirects=False, stream=True,
            ) as response:
                if response.status_code != expected:
                    raise ProviderError(f"DNS provider returned HTTP {response.status_code}; check provider logs")
                if path == "" and response.headers.get("Content-Type") != MEDIA_TYPE:
                    raise ProviderError("DNS provider does not support ExternalDNS webhook version 1")
                if response.headers.get("Content-Length", "").isdigit() and int(response.headers["Content-Length"]) > 4 * 1024 * 1024:
                    raise ProviderError("DNS provider response exceeds 4 MiB")
                body = bytearray()
                for chunk in response.iter_content(65536):
                    body.extend(chunk)
                    if len(body) > 4 * 1024 * 1024:
                        raise ProviderError("DNS provider response exceeds 4 MiB")
                if expected == 204:
                    return None
                import json
                return json.loads(body)
        except requests.RequestException as exc:
            raise ProviderError("Cannot reach DNS provider; check connection and provider logs") from exc
        except (ValueError, UnicodeError) as exc:
            raise ProviderError("DNS provider returned invalid JSON") from exc

    def negotiate(self, domain: str):
        result = self.call("GET")
        if not isinstance(result, dict):
            raise ProviderError("Invalid DNS provider negotiation")
        filters = result.get("include", [])
        exclude = result.get("exclude", [])
        from .models import inside, name
        if (
            not isinstance(filters, list) or not filters
            or not all(isinstance(value, str) for value in filters)
            or not isinstance(exclude, list)
            or not all(isinstance(value, str) for value in exclude)
        ):
            raise ProviderError("DNS provider must advertise an explicit domain filter")
        if not any(inside(domain, name(f)) for f in filters) or any(inside(domain, name(f)) for f in exclude):
            raise ProviderError("Managed domain is outside the DNS provider filter")
        return result

    def records(self) -> list[dict]:
        data = self.call("GET", "records")
        if not isinstance(data, list) or any(not isinstance(r, dict) for r in data):
            raise ProviderError("DNS provider returned invalid records")
        return data

    def adjust(self, records: list[dict]) -> list[dict]:
        result = self.call("POST", "adjustendpoints", records)
        if not isinstance(result, list):
            raise ProviderError("Invalid adjusted DNS endpoints")
        return result

    def apply(self, changes: dict):
        self.call("POST", "records", changes, expected=204)
