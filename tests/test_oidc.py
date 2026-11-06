"""OIDC protocol, role customization and owner-bound token regressions."""
from __future__ import annotations

import json
import time
from unittest.mock import MagicMock
from urllib.parse import parse_qs, urlsplit

import pytest
from joserfc import jwt
from joserfc.jwk import RSAKey

from backend.auth import oidc, permissions, service

ISSUER = "https://identity.example"
CLIENT_ID = "fabricator"
USERS = {
    "root": {"roles": ["admin"]},
    "alice": {"roles": ["token-user"], "servers": {"a": ["operator"], "b": ["viewer"]}},
    "bob": {"roles": ["token-user"], "servers": {"b": ["viewer"]}},
}


@pytest.fixture(autouse=True)
def live_auth_modules():
    # test_app_factory purges backend modules; patch the serving instances.
    global oidc, permissions, service
    from backend.auth import oidc, permissions, service


@pytest.fixture
def oidc_app(tmp_servers_root, monkeypatch):
    monkeypatch.delenv("FABRICATOR_DISABLE_AUTH", raising=False)
    monkeypatch.delenv("FABRICATOR_AUTH_PASSWORD_HASH", raising=False)
    monkeypatch.setenv("FABRICATOR_SESSION_COOKIE_SECURE", "1")
    for key, value in {
        "ISSUER": ISSUER, "CLIENT_ID": CLIENT_ID, "CLIENT_SECRET": "client-secret-canary",
        "REDIRECT_URI": "https://panel.example/api/auth/oidc/callback", "USERS": json.dumps(USERS),
    }.items():
        monkeypatch.setenv("FABRICATOR_OIDC_" + key, value)
    import backend.server.registry as registry
    registry.reset_for_tests()
    from backend.core.app import create_app
    app = create_app()
    app.config["TESTING"] = True
    return app


def sign_in(client, sub="alice"):
    with client.session_transaction() as state:
        state.update(authenticated=True, auth_method="oidc", issuer=ISSUER, sub=sub)


@pytest.mark.parametrize("method,path,capability", [
    ("post", "/backup-configs", "backups.config.write"),
    ("put", "/backup-configs/cfg", "backups.config.write"),
    ("delete", "/backup-configs/cfg?purge=1", "backups.delete"),
    ("get", "/snapshots?check_remote=1", "backups.list"),
    ("get", "/snapshots/snp/download", "backups.download"),
    ("post", "/snapshots/snp/restore", "backups.restore"),
    ("delete", "/snapshots/snp", "backups.delete"),
])
def test_s3_surfaces_remain_capability_scoped(oidc_app, monkeypatch, method, path, capability):
    from backend.backups import destinations
    # All checks must fail before resolving profiles or contacting storage.
    spy = MagicMock(side_effect=AssertionError("S3 must not be contacted"))
    monkeypatch.setattr(destinations, "_client", spy)
    grants = {"server.view", "backups.config.write"} if "purge=1" in path else {"server.view"}
    user = permissions.Principal(
        "backup-reader", ISSUER, (), frozenset(),
        {"a": frozenset(grants)}, {"a": ()},
    )
    oidc_app.extensions["oidc"].users[user.subject] = user
    client = oidc_app.test_client()
    sign_in(client, user.subject)
    response = getattr(client, method)("/api/servers/a" + path)
    assert response.status_code == 403
    spy.assert_not_called()
    assert capability not in grants


def test_oidc_skips_password_setup_and_rejects_old_sessions(oidc_app):
    client = oidc_app.test_client()
    assert not oidc_app.config["FABRICATOR_NEEDS_SETUP"]
    assert client.get("/api/servers").status_code == 401
    with client.session_transaction() as state:
        state["authenticated"] = True
    assert client.get("/api/servers").status_code == 401
    for route in ["setup", "login", "enable", "disable", "change-password"]:
        assert client.post("/api/auth/" + route, json={"password": "irrelevant"}).status_code == 409
    body = client.get("/api/auth/status").get_json()
    assert body["auth_mode"] == "oidc" and not body["authenticated"]
    assert "user" not in body and "root" not in json.dumps(body)
    assert client.get("/api/auth/status").headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize("role,allowed", [
    ("alice", True), ("bob", False),
])
def test_per_server_permissions(oidc_app, role, allowed):
    client = oidc_app.test_client()
    sign_in(client, role)
    # Check before handlers: nonexistent servers still must not grant writes.
    assert client.post("/api/servers/a/console", json={"command": "stop"}).status_code == (403 if allowed else 404)
    assert client.put("/api/servers/a/settings", json={}).status_code == (403 if allowed else 404)
    assert client.get("/api/metrics/system").status_code == 403
    assert client.get("/api/servers/private").status_code == 404
    assert client.get("/api/integrations/mcp").status_code == 200
    assert client.put("/api/integrations/mcp", json={"enabled": True}).status_code == 403


def test_session_and_grants_are_not_cached_in_cookie(oidc_app):
    client = oidc_app.test_client()
    sign_in(client)
    assert client.get("/api/auth/status").get_json()["user"]["servers"]["a"]["roles"] == ["operator"]
    del oidc_app.extensions["oidc"].users["alice"]
    assert client.get("/api/servers").status_code == 401
    assert client.get("/api/auth/status").get_json()["authenticated"] is False


def test_route_permission_coverage(oidc_app):
    exceptions = {
        ("GET", "/api/health"), ("GET", "/api/auth/status"), ("POST", "/api/auth/logout"),
        ("GET", "/api/auth/oidc/login"), ("GET", "/api/auth/oidc/callback"),
        *(("POST", "/api/auth/" + suffix) for suffix in ["setup", "login", "enable", "disable", "change-password"]),
        *((method, "/api/<path:_>") for method in ["GET", "POST", "PUT", "DELETE", "PATCH"]),
    }
    live = {
        (method, rule.rule) for rule in oidc_app.url_map.iter_rules()
        if rule.rule.startswith("/api/")
        for method in rule.methods - {"OPTIONS", "HEAD"}
    }
    assert live == set(permissions.RULES) | exceptions


def test_custom_role_union_overrides_and_scope(monkeypatch):
    monkeypatch.setenv("FABRICATOR_AUTH_ROLES", json.dumps({
        "admin": ["server.view"], "superuser": sorted(permissions.CATALOG),
        "starter": ["server.view", "server.start", "java.install"],
    }))
    users = permissions.load_users(ISSUER, json.dumps({
        "root": {"roles": ["superuser"]},
        "limited-admin": {"servers": {"a": ["admin", "starter", "viewer"]}},
    }))
    user = users["limited-admin"]
    assert user.can("server.start", "a")
    assert user.can("server.logs.read", "a")
    assert not user.can("server.stop", "a")
    assert not user.can("server.start", "b")
    assert not user.can("java.install", "a")
    assert not user.can("tokens.all.manage")


def test_dns_permission_is_global_and_tokens_remain_excluded(oidc_app):
    client = oidc_app.test_client()
    sign_in(client)
    assert client.get("/api/dns").status_code == 403
    assert client.put("/api/servers/a/dns", json={}).status_code == 403
    sign_in(client, "root")
    assert client.get("/api/dns").status_code == 200
    service.set_mcp_enabled(True)
    token = client.post("/api/integrations/mcp/tokens", json={"name": "dns", "scope": "manage"}).json
    headers = {"Authorization": "Bearer " + token["token"]}
    assert client.get("/api/dns", headers=headers).status_code == 403
    assert client.put("/api/servers/a/dns", headers=headers, json={}).status_code == 403


@pytest.mark.parametrize("operation", ["preview", "apply"])
def test_forwarding_checks_every_backend_permission(oidc_app, monkeypatch, operation):
    routes = oidc_app.view_functions["velocity." + operation].__globals__
    monkeypatch.setitem(routes, "_proxy", lambda sid: ({"id": sid}, None))
    monkeypatch.setattr(routes["forwarding"], "configure", lambda *args, **kwargs: {"ok": True})
    client = oidc_app.test_client()
    grants = frozenset({"server.view", "server.settings.read", "server.settings.write"})
    user = permissions.Principal("proxy-admin", ISSUER, (), frozenset(), {"a": grants}, {"a": ()})
    oidc_app.extensions["oidc"].users[user.subject] = user
    sign_in(client, user.subject)
    url = "/api/servers/a/velocity/forwarding/" + operation
    assert client.post(url, json={"serverIds": ["b"]}).status_code == 404
    user.servers["b"] = frozenset({"server.view"})
    assert client.post(url, json={"serverIds": ["b"]}).status_code == 403
    user.servers["b"] |= {"server.settings.read"}
    assert client.post(url, json={"serverIds": ["b"]}).status_code == (200 if operation == "preview" else 403)
    user.servers["b"] = grants
    assert client.post(url, json={"serverIds": ["b"]}).status_code == 200


@pytest.mark.parametrize("roles,users", [
    ('{"bad":["not.real"]}', USERS),
    ('{"admin":[]}', USERS),
    ('{}', {"x": {"roles": ["unknown"]}}),
    ('{}', {"root": {"roles": ["admin"]}, "x": {"servers": {"a": ["bad"]}}}),
    ('{"bad":["server.start"]}', {"root": {"roles": ["admin"]}, "x": {"servers": {"a": ["bad"]}}}),
    ('{"a":[],"a":[]}', USERS),
])
def test_invalid_role_configuration(monkeypatch, roles, users):
    monkeypatch.setenv("FABRICATOR_AUTH_ROLES", roles)
    with pytest.raises(ValueError):
        permissions.load_users(ISSUER, json.dumps(users))


def test_collection_and_detail_projection(oidc_app, monkeypatch):
    view_globals = oidc_app.view_functions["server.get_servers"].__globals__
    storage = view_globals["storage"]
    records = [
        {"id": sid, "name": sid, "rconPassword": "credential-canary", "installPath": "/private/canary"}
        for sid in ("a", "b", "private")
    ]
    monkeypatch.setattr(storage, "get_all_servers", lambda: records)
    monkeypatch.setitem(view_globals, "_augment_with_runtime", lambda item: item)
    client = oidc_app.test_client()
    sign_in(client)
    response = client.get("/api/servers")
    assert [s["id"] for s in response.get_json()] == ["a", "b"]
    assert "canary" not in response.text
    sign_in(client, "root")
    assert "credential-canary" in client.get("/api/servers").text


def test_owned_tokens_and_scope_intersection(oidc_app):
    service.set_mcp_enabled(True)
    client = oidc_app.test_client()
    sign_in(client)
    token = client.post("/api/integrations/mcp/tokens", json={"name": "own", "scope": "manage", "owner": {"sub": "root"}}).get_json()
    assert service.mcp_state()["tokens"][token["id"]]["owner"]["sub"] == "alice"
    headers = {"Authorization": "Bearer " + token["token"]}
    assert client.post("/api/servers/b/start", headers=headers).status_code == 403
    assert client.get("/api/servers/private", headers=headers).status_code == 404
    assert client.get("/api/integrations/mcp", headers=headers).status_code == 403
    assert client.get("/api/auth/oidc/login", headers=headers).status_code == 403
    sign_in(client, "bob")
    assert client.get("/api/integrations/mcp").get_json()["tokens"] == []
    assert client.delete("/api/integrations/mcp/tokens/" + token["id"]).status_code == 404
    sign_in(client, "root")
    assert client.delete("/api/integrations/mcp/tokens/" + token["id"]).status_code == 204
    assert client.get("/api/servers", headers=headers).status_code == 401
    legacy = service.create_token("legacy", "read")
    assert client.get("/api/servers", headers={"Authorization": "Bearer " + legacy["token"]}).status_code == 401


@pytest.fixture
def fake_provider(oidc_app, monkeypatch):
    key = RSAKey.generate_key(2048, auto_kid=True)
    metadata = {
        "issuer": ISSUER, "authorization_endpoint": ISSUER + "/authorize",
        "token_endpoint": ISSUER + "/token", "jwks_uri": ISSUER + "/keys",
    }
    class Response:
        status_code = 200
        def __init__(self, value):
            self.value = value
        def raise_for_status(self):
            pass
        def json(self):
            return self.value
    state = {"claims": {}, "fetch": [], "metadata": metadata, "jwks": {"keys": [key.as_dict()]}, "key": key}
    monkeypatch.setattr(oidc.requests, "get", lambda url, **kwargs: Response(
        state["jwks"] if url.endswith("/keys") else state["metadata"]
    ))
    def fetch(client, url, **kwargs):
        state["fetch"].append(kwargs)
        if "token" in state:
            return state["token"]
        nonce = state["nonce"]
        claims = {
            "iss": ISSUER, "sub": "alice", "aud": CLIENT_ID, "iat": int(time.time()),
            "exp": int(time.time()) + 600, "nonce": nonce, **state["claims"],
        }
        return {"id_token": jwt.encode({"alg": "RS256", "kid": key.kid}, claims, key), "access_token": "access-canary"}
    monkeypatch.setattr(oidc.OAuth2Session, "fetch_token", fetch)
    return state


def start_login(app, client, fake, path="/"):
    response = client.get("/api/auth/oidc/login", query_string={"redirect": path})
    assert response.status_code == 302
    args = parse_qs(urlsplit(response.location).query)
    assert args["code_challenge_method"] == ["S256"]
    assert args["redirect_uri"] == ["https://panel.example/api/auth/oidc/callback"]
    state = args["state"][0]
    fake["nonce"] = args["nonce"][0]
    return state


def test_signed_login_success_replay_and_local_logout(oidc_app, fake_provider):
    client = oidc_app.test_client()
    state = start_login(oidc_app, client, fake_provider, "/server/a/overview")
    response = client.get("/api/auth/oidc/callback", query_string={"state": state, "code": "authorization-code"})
    assert response.location == "/server/a/overview"
    assert fake_provider["fetch"][0]["code_verifier"]
    assert client.get("/api/auth/status").get_json()["authenticated"]
    with client.session_transaction() as session:
        assert not any(word in json.dumps(dict(session)) for word in ["canary", "authorization-code", "verifier", "id_token"])
    assert "oidc_error" in client.get("/api/auth/oidc/callback", query_string={"state": state, "code": "code"}).location
    assert client.post("/api/auth/logout").get_json() == {"authenticated": False}
    assert client.get("/api/servers").status_code == 401


@pytest.mark.parametrize("claims", [
    {"iss": "https://other.example"}, {"aud": "another-client"}, {"nonce": "wrong"},
    {"exp": 1}, {"sub": "not-configured"}, {"azp": "other"},
    {"aud": [CLIENT_ID, "other"]},
])
def test_invalid_or_unlisted_identity(oidc_app, fake_provider, claims):
    client = oidc_app.test_client()
    state = start_login(oidc_app, client, fake_provider)
    fake_provider["claims"] = claims
    response = client.get("/api/auth/oidc/callback", query_string={"state": state, "code": "secret-code"})
    assert response.location.startswith("/login?oidc_error=")
    assert client.get("/api/servers").status_code == 401


@pytest.mark.parametrize("path", [
    "https://evil.example", "//evil.example", "/\\evil", "/\n/evil",
    "/api/auth/oidc/login", "/login", "/%2fother.example", "/%5cevil",
    "/%0aevil", "/%61pi/auth/oidc/login",
])
def test_safe_returns(oidc_app, fake_provider, path):
    client = oidc_app.test_client()
    state = start_login(oidc_app, client, fake_provider, path)
    assert client.get("/api/auth/oidc/callback", query_string={"state": state, "code": "x"}).location == "/"


def test_browser_binding_expiry_and_cancellation(oidc_app, fake_provider):
    client = oidc_app.test_client()
    other = oidc_app.test_client()
    state = start_login(oidc_app, client, fake_provider)
    assert "oidc_error=invalid" in other.get("/api/auth/oidc/callback", query_string={"state": state, "code": "x"}).location
    state = start_login(oidc_app, client, fake_provider)
    oidc_app.extensions["oidc"].transactions[state]["created"] -= 601
    assert "oidc_error=invalid" in client.get("/api/auth/oidc/callback", query_string={"state": state, "code": "x"}).location
    state = start_login(oidc_app, client, fake_provider)
    assert "oidc_error=cancelled" in client.get("/api/auth/oidc/callback", query_string={"state": state, "error": "<untrusted>"}).location
    assert fake_provider["fetch"] == []


def test_outage_and_partial_configuration(oidc_app, monkeypatch, caplog):
    def fail(*args, **kwargs):
        raise oidc.requests.Timeout("client-secret-canary")
    monkeypatch.setattr(oidc.requests, "get", fail)
    client = oidc_app.test_client()
    assert "oidc_error=unavailable" in client.get("/api/auth/oidc/login").location
    assert "client-secret-canary" not in caplog.text
    monkeypatch.delenv("FABRICATOR_OIDC_CLIENT_SECRET")
    with pytest.raises(ValueError, match="OIDC requires"):
        oidc.configure()


@pytest.mark.parametrize("method,path,required,target", [
    (method, path, required, target)
    for (method, path), (required, target) in permissions.RULES.items()
    if target in {"path", None} and required
])
def test_individual_permission_gate(oidc_app, method, path, required, target):
    """Every explicit action passes through the gate, including custom grants."""
    import re
    sid = "a" if target == "path" else None
    user = permissions.Principal(
        "action", ISSUER, (), frozenset(required) if sid is None else frozenset(required) & permissions.GLOBAL,
        {"a": frozenset({"server.view", *required}) - permissions.GLOBAL} if sid else {}, {"a": ()} if sid else {},
    )
    oidc_app.extensions["oidc"].users["action"] = user
    route = next(rule for rule in oidc_app.url_map.iter_rules() if rule.rule == path and method in rule.methods)
    oidc_app.view_functions[route.endpoint] = lambda **kwargs: {"ok": True}
    url = re.sub(r"<(?:[^:>]+:)?[^>]+>", "1", path.replace("<server_id>", "a"))
    client = oidc_app.test_client()
    sign_in(client, "action")
    assert client.open(url, method=method, json={}).status_code == 200
    if sid:
        hidden = re.sub(r"<(?:[^:>]+:)?[^>]+>", "1", path.replace("<server_id>", "hidden"))
        assert client.open(hidden, method=method, json={}).status_code == 404
    if required != ("server.view",):
        limited = permissions.Principal("limited", ISSUER, ("admin",), frozenset(), {"a": frozenset({"server.view"})}, {"a": ("admin",)})
        oidc_app.extensions["oidc"].users["limited"] = limited
        sign_in(client, "limited")
        assert client.open(url, method=method, json={}).status_code == 403


def test_overridden_admin_has_no_privilege_bypass(oidc_app, monkeypatch):
    monkeypatch.setenv("FABRICATOR_AUTH_ROLES", json.dumps({
        "admin": ["server.view"], "superuser": sorted(permissions.CATALOG),
    }))
    oidc_app.extensions["oidc"].users = permissions.load_users(ISSUER, json.dumps({
        "root": {"roles": ["superuser"]}, "limited": {"roles": ["admin"]},
    }))
    client = oidc_app.test_client()
    sign_in(client, "limited")
    assert client.post("/api/servers/a/start").status_code == 403
    assert client.get("/api/integrations/mcp").status_code == 403
    assert client.post("/api/servers", json={}).status_code == 403


@pytest.mark.parametrize("field,value", [
    ("FABRICATOR_SESSION_COOKIE_SECURE", "0"),
    ("FABRICATOR_DISABLE_AUTH", "1"),
    ("FABRICATOR_OIDC_ISSUER", "http://identity.example"),
    ("FABRICATOR_OIDC_ISSUER", "https://identity.example:invalid"),
    ("FABRICATOR_OIDC_ISSUER", "https://identity.example /realm"),
    ("FABRICATOR_OIDC_ISSUER", "https://identity.example/?query=x"),
    ("FABRICATOR_OIDC_REDIRECT_URI", "https://panel.example/wrong"),
    ("FABRICATOR_OIDC_REDIRECT_URI", "https://panel.example/api/auth/oidc/callback?query=x"),
    ("FABRICATOR_OIDC_REDIRECT_URI", "https://user:password@panel.example/api/auth/oidc/callback"),
])
def test_invalid_deployment_configuration(oidc_app, monkeypatch, field, value):
    monkeypatch.setenv(field, value)
    with pytest.raises(ValueError):
        oidc.configure()


@pytest.mark.parametrize("metadata", [
    [], {}, {"issuer": "https://other.example"},
    {"issuer": ISSUER, "authorization_endpoint": "http://remote.example"},
])
def test_malformed_discovery_fails_closed(oidc_app, fake_provider, metadata):
    fake_provider["metadata"] = metadata
    client = oidc_app.test_client()
    assert client.get("/api/auth/oidc/login").location == "/login?oidc_error=unavailable"
    assert not oidc_app.extensions["oidc"].transactions


@pytest.mark.parametrize("jwks", [None, [], {}, {"keys": [None]}, {"keys": []}, {"keys": [{"kty": "RSA"}]}])
def test_malformed_jwks_fails_closed(oidc_app, fake_provider, jwks):
    client = oidc_app.test_client()
    state = start_login(oidc_app, client, fake_provider)
    fake_provider["jwks"] = jwks
    response = client.get("/api/auth/oidc/callback", query_string={"state": state, "code": "x"})
    assert response.location == "/login?oidc_error=invalid"
    assert not client.get("/api/auth/status").get_json()["authenticated"]


@pytest.mark.parametrize("token", [None, [], {}, {"access_token": "canary"}, {"id_token": "not-a-jwt"}, {"id_token": None}])
def test_malformed_token_response_fails_closed(oidc_app, fake_provider, token):
    client = oidc_app.test_client()
    state = start_login(oidc_app, client, fake_provider)
    fake_provider["token"] = token
    assert client.get("/api/auth/oidc/callback", query_string={"state": state, "code": "x"}).location == "/login?oidc_error=invalid"


def test_bad_signature_and_disallowed_algorithm(oidc_app, fake_provider):
    from joserfc.jwk import OctKey
    for key, algorithm in [(RSAKey.generate_key(2048), "RS256"), (OctKey.generate_key(256), "HS256")]:
        client = oidc_app.test_client()
        state = start_login(oidc_app, client, fake_provider)
        fake_provider["token"] = {"id_token": jwt.encode({"alg": algorithm}, {
            "iss": ISSUER, "sub": "root", "aud": CLIENT_ID, "iat": int(time.time()),
            "exp": int(time.time()) + 600, "nonce": fake_provider["nonce"],
        }, key)}
        assert client.get("/api/auth/oidc/callback", query_string={"state": state, "code": "x"}).location == "/login?oidc_error=invalid"


def test_duplicate_arguments_and_capacity(oidc_app, fake_provider, monkeypatch):
    client = oidc_app.test_client()
    state = start_login(oidc_app, client, fake_provider)
    assert client.get("/api/auth/oidc/callback", query_string=[("state", state), ("code", "a"), ("code", "b")]).location == "/login?oidc_error=invalid"
    assert not fake_provider["fetch"]
    monkeypatch.setattr(oidc, "_MAX_TRANSACTIONS", 1)
    assert client.get("/api/auth/oidc/login").location == "/login?oidc_error=busy"
    oidc_app.extensions["oidc"].transactions[state]["created"] -= 601
    assert client.get("/api/auth/oidc/login").status_code == 302
    assert len(oidc_app.extensions["oidc"].transactions) == 1


def test_metrics_projection_independent_of_settings_read():
    user = permissions.Principal("settings-only", ISSUER, (), frozenset(), {
        "a": frozenset({"server.view", "server.settings.read"}),
    }, {"a": ()})
    data = permissions.project_server({"id": "a", "installPath": "/server", "runtime": {"status": "running", "cpu": 99}}, user)
    assert data["installPath"] == "/server"
    assert data["runtime"] == {"status": "running"}


@pytest.mark.parametrize("path,permission,envelope", [
    ("/api/servers/a/settings", "server.settings.write", False),
    ("/api/servers/a/autostart", "server.autostart.write", True),
])
def test_mutation_responses_cannot_bypass_settings_read(oidc_app, path, permission, envelope):
    user = permissions.Principal("writer", ISSUER, (), frozenset(), {
        "a": frozenset({"server.view", permission}),
    }, {"a": ()})
    oidc_app.extensions["oidc"].users["writer"] = user
    rule, _ = oidc_app.url_map.bind("").match(path, method="PUT")
    record = {"id": "a", "name": "a", "rconPassword": "credential-canary", "installPath": "/private/canary"}
    oidc_app.view_functions[rule] = lambda **kwargs: {"server": record} if envelope else record
    client = oidc_app.test_client()
    sign_in(client, "writer")
    response = client.put(path, json={})
    assert response.status_code == 200
    assert "canary" not in response.text


def test_reorder_response_is_filtered_before_runtime_reads(oidc_app, monkeypatch):
    from dataclasses import replace
    view_globals = oidc_app.view_functions["server.reorder_servers"].__globals__
    storage = view_globals["storage"]
    records = [{"id": sid, "rconPassword": "credential-canary"} for sid in ("a", "hidden")]
    monkeypatch.setattr(storage, "reorder_servers", lambda _: records)
    observed = []
    def augment(record):
        observed.append(record["id"])
        return record
    monkeypatch.setitem(view_globals, "_augment_with_runtime", augment)
    user = oidc_app.extensions["oidc"].users["alice"]
    oidc_app.extensions["oidc"].users["alice"] = replace(user, global_permissions=user.global_permissions | {"servers.reorder"})
    client = oidc_app.test_client()
    sign_in(client)
    response = client.put("/api/servers/order", json={"ids": ["a"]})
    assert response.status_code == 200
    assert [r["id"] for r in response.get_json()] == ["a"]
    assert observed == ["a"]
    assert "canary" not in response.text


def test_body_job_and_upload_targets(oidc_app, monkeypatch):
    from backend.backups import progress
    from backend.modrinth import mrpack
    from types import SimpleNamespace
    client = oidc_app.test_client()
    sign_in(client)
    assert client.post("/api/modrinth/mod/example/install", json={"server_id": "b"}).status_code == 403
    assert client.post("/api/modrinth/mod/example/install", json={"server_id": "hidden"}).status_code == 404
    monkeypatch.setattr(progress, "get", lambda _: {"server_id": "hidden", "kind": "restore"})
    assert client.get("/api/backup-jobs/job").status_code == 404
    monkeypatch.setattr(progress, "get", lambda _: {"server_id": "a", "kind": "restore"})
    assert client.get("/api/backup-jobs/job").status_code == 403
    alice = oidc_app.extensions["oidc"].users["alice"]
    monkeypatch.setattr(mrpack, "get", lambda _: SimpleNamespace(owner=alice.owner))
    sign_in(client, "root")
    assert client.delete("/api/modrinth/modpack/upload/private").status_code == 404
    assert client.post("/api/modrinth/modpack/upload/private/install", json={"server_id": "a"}).status_code == 404


def test_token_owner_policy_transitions(oidc_app):
    service.set_mcp_enabled(True)
    client = oidc_app.test_client()
    sign_in(client)
    token = client.post("/api/integrations/mcp/tokens", json={"name": "own", "scope": "read"}).get_json()
    headers = {"Authorization": "Bearer " + token["token"]}
    assert client.get("/api/servers", headers=headers).status_code == 200
    del oidc_app.extensions["oidc"].users["alice"]
    assert client.get("/api/servers", headers=headers).status_code == 401
    oidc_app.extensions["oidc"] = None
    assert client.get("/api/servers", headers=headers).status_code == 401
    assert client.get("/api/servers").status_code == 401


def test_install_cannot_consume_another_users_staged_pack(oidc_app, monkeypatch):
    from backend.modrinth import mrpack
    from types import SimpleNamespace
    monkeypatch.setattr(mrpack, "get", lambda _: SimpleNamespace(owner={"issuer": ISSUER, "sub": "alice"}))
    client = oidc_app.test_client()
    sign_in(client, "root")
    response = client.post("/api/servers/a/install", json={"modpack": {"source": "upload", "upload_id": "private"}})
    assert response.status_code == 404


@pytest.mark.parametrize("status", [201, 302, 400, 500])
def test_token_exchange_requires_http_success(status):
    from types import SimpleNamespace
    with pytest.raises(ValueError):
        oidc._token_response(SimpleNamespace(status_code=status))
