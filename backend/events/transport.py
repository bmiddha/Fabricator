"""Pinned-address webhook POSTs: no redirects, proxies, DNS rebinding or URL logs."""
from __future__ import annotations

import http.client
import ipaddress
import json
import os
import socket
import ssl
import threading
import time
from urllib.parse import urlsplit


class DeliveryError(Exception):
    """A fixed safe error code, not an upstream response or secret-bearing URL."""


DNS_TIMEOUT = 5
_dns_slots = threading.BoundedSemaphore(2)


def _addresses(host, port):
    # libc DNS has no per-call timeout. Bound both the caller's wait and the
    # number of outstanding resolver threads, without accumulating queued work.
    if not _dns_slots.acquire(blocking=False):
        raise DeliveryError("dns_busy")
    finished = threading.Event()
    result = []

    def lookup():
        try:
            result.extend(socket.getaddrinfo(host, port, type=socket.SOCK_STREAM))
        except (OSError, UnicodeError):
            pass
        finally:
            _dns_slots.release()
            finished.set()
    threading.Thread(target=lookup, name="webhook-dns", daemon=True).start()
    if not finished.wait(DNS_TIMEOUT):
        raise DeliveryError("dns_timeout")
    if not result:
        raise DeliveryError("dns_failed")
    return list(dict.fromkeys(info[4][0] for info in result))


def target(url):
    if not isinstance(url, str) or not 1 <= len(url) <= 2048 or any(ord(c) <= 32 or ord(c) >= 127 for c in url):
        raise ValueError("Webhook URL must be a valid ASCII URL of at most 2048 characters")
    try:
        parsed = urlsplit(url)
        port = parsed.port if parsed.port is not None else (443 if parsed.scheme == "https" else 80)
    except ValueError:
        raise ValueError("Invalid webhook URL") from None
    if (not 1 <= port <= 65535 or parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.fragment or "\\" in url):
        raise ValueError("Webhook URL must have a host and no userinfo, fragment or backslash")
    hostname = parsed.hostname.lower()
    host = f"[{hostname}]" if ":" in hostname else hostname
    origin = f"{parsed.scheme}://{host}:{port}"
    try:
        configured = json.loads(os.environ.get("FABRICATOR_WEBHOOK_PRIVATE_TARGETS", "{}"))
        if not isinstance(configured, dict):
            raise ValueError
        allowed = {str(ipaddress.ip_address(ip)) for ip in configured.get(origin, [])}
    except (ValueError, TypeError):
        raise ValueError("Invalid operator webhook target configuration") from None
    if parsed.scheme != "https" and not allowed:
        raise ValueError("Public webhooks require HTTPS")
    if port != 443 and not allowed:
        raise ValueError("Nonstandard ports require an operator-configured target")
    return parsed, hostname, port, allowed


def resolve(url):
    parsed, host, port, allowed = target(url)
    addresses = _addresses(host, port)
    for raw in addresses:
        ip = ipaddress.ip_address(raw)
        # Mapped IPv4 must pass the same checks as ordinary IPv4.
        check = ip.ipv4_mapped if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped else ip
        if (check.is_unspecified or check.is_multicast or check.is_link_local
                or (not check.is_global and str(ip) not in allowed)
                or (allowed and str(ip) not in allowed)):
            raise DeliveryError("target_blocked")
    return parsed, host, port, addresses


def _connect_checked(addresses, port, timeout):
    deadline = time.monotonic() + timeout
    for address in addresses:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            return socket.create_connection((address, port), remaining)
        except OSError:
            continue
    raise OSError("Checked addresses could not be connected")


class PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, port, addresses):
        super().__init__(host, port, timeout=5, context=ssl.create_default_context())
        self.addresses = addresses

    def connect(self):
        raw = _connect_checked(self.addresses, self.port, self.timeout)
        self.sock = raw  # The total-deadline timer can interrupt the TLS handshake.
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host, do_handshake_on_connect=False)
            self.sock.do_handshake()
        except BaseException:
            raw.close()
            raise


class PinnedHTTP(http.client.HTTPConnection):
    def __init__(self, host, port, addresses):
        super().__init__(host, port, timeout=5)
        self.addresses = addresses

    def connect(self):
        self.sock = _connect_checked(self.addresses, self.port, self.timeout)


def deliver(url, kind, event):
    parsed, host, port, addresses = resolve(url)  # Revalidate on EVERY attempt.
    base = os.environ.get("FABRICATOR_PUBLIC_URL", "").rstrip("/")
    if base:
        public = urlsplit(base)
        if (public.scheme not in {"https", "http"} or not public.hostname or public.username is not None
                or public.password is not None or public.query or public.fragment or "\\" in base
                or any(ord(c) <= 32 for c in base)):
            raise DeliveryError("invalid_destination")
    path = event["path"]
    link = base + path
    if kind == "discord":
        payload = {
            "content": f"Fabricator: {event['alert']} ({event['action']}, {event['outcome']})\n{link}",
            "allowed_mentions": {"parse": []},
        }
    else:
        payload = {**event, "url": link}
    body = json.dumps(payload).encode("utf-8")
    connection = (PinnedHTTPS if parsed.scheme == "https" else PinnedHTTP)(host, port, addresses)
    timed_out = threading.Event()

    def expire():
        timed_out.set()
        if connection.sock:
            try:
                connection.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass  # The connection may already have completed.
    deadline = threading.Timer(10, expire)
    deadline.daemon = True
    deadline.start()
    try:
        connection.request("POST", (parsed.path or "/") + ("?" + parsed.query if parsed.query else ""),
                           body, {"Content-Type": "application/json", "User-Agent": "Fabricator-alerts/1"})
        response = connection.getresponse()
        # Do not consume unbounded/untrusted response bodies. Redirects fail.
        if not 200 <= response.status < 300:
            raise DeliveryError(f"http_{response.status}")
    except DeliveryError:
        raise
    except (OSError, http.client.HTTPException):
        raise DeliveryError("connection_timeout" if timed_out.is_set() else "connection_failed") from None
    finally:
        deadline.cancel()
        connection.close()
