import json

import pytest

from backend.dns.client import MEDIA_TYPE, ProviderError, Webhook


class Response:
    def __init__(self, value, code=200, content_type=MEDIA_TYPE):
        self.payload = json.dumps(value).encode()
        self.status_code = code
        self.headers = {"Content-Type": content_type}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def iter_content(self, size):
        yield self.payload


def test_negotiation_wire_headers_and_domain_filter(monkeypatch):
    client = Webhook("http://dns:8888")
    calls = []

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return Response({"include": ["mc.example.com"], "exclude": ["private.mc.example.com"]})

    monkeypatch.setattr(client.session, "request", request)
    assert client.negotiate("mc.example.com")["include"] == ["mc.example.com"]
    method, url, options = calls[0]
    assert (method, url) == ("GET", "http://dns:8888/")
    assert options["headers"]["Accept"] == MEDIA_TYPE
    assert not options["allow_redirects"]
    with pytest.raises(ProviderError, match="outside"):
        client.negotiate("private.mc.example.com")
    with pytest.raises(ProviderError, match="outside"):
        client.negotiate("notmc.example.com")


def test_reject_incompatible_media_type(monkeypatch):
    client = Webhook("http://dns")
    monkeypatch.setattr(client.session, "request", lambda *a, **k: Response({"include": ["example.com"]}, content_type="application/json"))
    with pytest.raises(ProviderError, match="version 1"):
        client.negotiate("example.com")


@pytest.mark.parametrize("value", [
    {"filters": ["example.com"]},
    {"include": []},
    {"include": [None]},
    {"include": ["example.com"], "exclude": "private.example.com"},
])
def test_reject_invalid_or_legacy_domain_filters(monkeypatch, value):
    client = Webhook("http://dns")
    monkeypatch.setattr(client.session, "request", lambda *a, **k: Response(value))
    with pytest.raises(ProviderError, match="explicit domain filter"):
        client.negotiate("example.com")


def test_provider_errors_do_not_echo_secret_body(monkeypatch):
    client = Webhook("http://dns")
    monkeypatch.setattr(client.session, "request", lambda *a, **k: Response({"secret": "credentials"}, code=500))
    with pytest.raises(ProviderError) as error:
        client.records()
    assert "HTTP 500" in str(error.value)
    assert "credentials" not in str(error.value)


def test_mutual_tls_environment(monkeypatch):
    monkeypatch.setenv("FABRICATOR_DNS_CA_FILE", "/test/ca.pem")
    monkeypatch.setenv("FABRICATOR_DNS_CLIENT_CERT", "/test/cert.pem")
    monkeypatch.setenv("FABRICATOR_DNS_CLIENT_KEY", "/test/key.pem")
    client = Webhook("https://dns")
    assert client.session.verify == "/test/ca.pem"
    assert client.session.cert == ("/test/cert.pem", "/test/key.pem")
    assert client.session.trust_env is False
