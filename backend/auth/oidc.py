"""Single-provider OIDC login. Tokens and login transactions stay server-side."""
from __future__ import annotations

import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import unquote, urlencode, urlsplit

import requests
from authlib.integrations.base_client.errors import OAuthError
from authlib.integrations.requests_client import OAuth2Session
from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import KeySet
from authlib.oauth2.rfc6749.errors import OAuth2Error
from authlib.oidc.core import CodeIDToken
from flask import Blueprint, current_app, redirect, request, session

from backend.auth.permissions import Principal, load_users

oidc_bp = Blueprint("oidc", __name__, url_prefix="/api/auth/oidc")
_PREFIX = "FABRICATOR_OIDC_"
_REQUIRED = ("ISSUER", "CLIENT_ID", "CLIENT_SECRET", "REDIRECT_URI", "USERS")
_ALGORITHMS = ("RS256", "RS384", "RS512", "ES256", "ES384", "ES512", "PS256", "PS384", "PS512")
_TTL = 600
_MAX_TRANSACTIONS = 256


def valid_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        port = parsed.port
        return (
            parsed.scheme in {"http", "https"} and bool(parsed.hostname)
            and (port is None or 0 < port <= 65535)
            and "\\" not in value and not any(ord(char) < 33 or ord(char) == 127 for char in value)
            and not parsed.username and not parsed.password and not parsed.fragment
            and (parsed.scheme == "https" or parsed.hostname in {"localhost", "127.0.0.1", "::1"})
        )
    except ValueError:
        return False


def safe_return(value):
    decoded = unquote(value) if isinstance(value, str) else value
    if (
        not isinstance(value, str) or not value.startswith("/") or value.startswith("//")
        or decoded.startswith("//") or "\\" in decoded
        or any(ord(char) < 33 or ord(char) == 127 for char in decoded)
        or decoded.startswith(("/api", "/login", "/setup"))
    ):
        return "/"
    return value


@dataclass
class OIDC:
    issuer: str
    client_id: str
    client_secret: str = field(repr=False)
    redirect_uri: str
    users: dict[str, Principal]
    name: str
    transactions: dict = field(default_factory=dict, repr=False)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def metadata(self):
        response = requests.get(
            self.issuer.rstrip("/") + "/.well-known/openid-configuration",
            timeout=10, allow_redirects=False,
        )
        response.raise_for_status()
        if response.status_code != 200:
            raise ValueError("discovery did not return success")
        data = response.json()
        if not isinstance(data, dict) or data.get("issuer") != self.issuer:
            raise ValueError("invalid discovery issuer")
        for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
            if not isinstance(data.get(key), str) or not valid_url(data[key]):
                raise ValueError("invalid discovery endpoint")
        return data

    def client(self):
        client = OAuth2Session(
            self.client_id, self.client_secret, scope="openid",
            redirect_uri=self.redirect_uri, code_challenge_method="S256",
        )
        client.register_compliance_hook("access_token_response", _token_response)
        return client

    def consume(self, state):
        with self.lock:
            entry = self.transactions.pop(state, None) if isinstance(state, str) else None
        if not entry or time.monotonic() - entry["created"] > _TTL:
            raise ValueError("invalid transaction")
        browser = session.get("oidc_browser")
        if not isinstance(browser, str) or not secrets.compare_digest(entry["browser"], browser):
            raise ValueError("invalid browser binding")
        return entry


def configure() -> OIDC | None:
    present = any(key.startswith(_PREFIX) or key == "FABRICATOR_AUTH_ROLES" for key in os.environ)
    if not present:
        return None
    if any(not os.environ.get(_PREFIX + key, "").strip() for key in _REQUIRED):
        raise ValueError("OIDC requires FABRICATOR_OIDC_ISSUER, CLIENT_ID, CLIENT_SECRET, REDIRECT_URI and USERS")
    from backend.auth.service import auth_disabled
    if auth_disabled():
        raise ValueError("OIDC cannot be combined with FABRICATOR_DISABLE_AUTH")
    issuer = os.environ[_PREFIX + "ISSUER"]
    callback = os.environ[_PREFIX + "REDIRECT_URI"]
    if not valid_url(issuer) or urlsplit(issuer).query:
        raise ValueError("FABRICATOR_OIDC_ISSUER must be an HTTPS issuer URL (HTTP allowed only on loopback)")
    if not valid_url(callback) or urlsplit(callback).path != "/api/auth/oidc/callback" or urlsplit(callback).query:
        raise ValueError("FABRICATOR_OIDC_REDIRECT_URI must be the absolute HTTPS /api/auth/oidc/callback URL")
    if urlsplit(callback).scheme == "https":
        from backend.auth.service import cookie_secure
        if not cookie_secure():
            raise ValueError("HTTPS OIDC requires FABRICATOR_SESSION_COOKIE_SECURE=1")
    return OIDC(
        issuer, os.environ[_PREFIX + "CLIENT_ID"], os.environ[_PREFIX + "CLIENT_SECRET"],
        callback, load_users(issuer, os.environ[_PREFIX + "USERS"]),
        os.environ.get(_PREFIX + "DISPLAY_NAME", "OpenID Connect"),
    )


def provider():
    return current_app.extensions.get("oidc")


def session_user():
    oidc = provider()
    if not oidc or session.get("auth_method") != "oidc" or session.get("issuer") != oidc.issuer:
        return None
    return oidc.users.get(session.get("sub"))


def _failure(code):
    current_app.logger.warning("OIDC login rejected: %s", code)
    response = redirect("/login?" + urlencode({"oidc_error": code}), 303)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


def _token_response(response):
    if response.status_code != 200:
        raise ValueError("token endpoint did not return success")
    return response


@oidc_bp.get("/login")
def login():
    oidc = provider()
    if oidc is None:
        return {"error": "OIDC is not configured"}, 404
    try:
        metadata = oidc.metadata()
        browser = session.get("oidc_browser")
        if not isinstance(browser, str):
            browser = secrets.token_urlsafe(32)
            session["oidc_browser"] = browser
        nonce, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
        with oidc.client() as client:
            url, state = client.create_authorization_url(
                metadata["authorization_endpoint"], nonce=nonce, code_verifier=verifier,
            )
        now = time.monotonic()
        with oidc.lock:
            oidc.transactions = {k: v for k, v in oidc.transactions.items() if now - v["created"] <= _TTL}
            if len(oidc.transactions) >= _MAX_TRANSACTIONS:
                return _failure("busy")
            oidc.transactions[state] = {
                "created": now, "browser": browser, "nonce": nonce, "verifier": verifier,
                "return": safe_return(request.args.get("redirect")), "metadata": metadata,
            }
        response = redirect(url, 302)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response
    except (requests.RequestException, OAuthError, OAuth2Error, ValueError, TypeError, KeyError):
        return _failure("unavailable")


@oidc_bp.get("/callback")
def callback():
    oidc = provider()
    if oidc is None:
        return {"error": "OIDC is not configured"}, 404
    try:
        if any(len(request.args.getlist(key)) != 1 for key in request.args):
            raise ValueError("duplicate callback argument")
        transaction = oidc.consume(request.args.get("state"))
        if request.args.get("error"):
            return _failure("cancelled")
        code = request.args.get("code")
        if not code:
            raise ValueError("missing code")
        metadata = transaction["metadata"]
        with oidc.client() as client:
            token = client.fetch_token(
                metadata["token_endpoint"], code=code, code_verifier=transaction["verifier"],
                timeout=10, allow_redirects=False,
            )
        response = requests.get(metadata["jwks_uri"], timeout=10, allow_redirects=False)
        response.raise_for_status()
        if response.status_code != 200:
            raise ValueError("JWKS did not return success")
        keys = response.json()
        if not isinstance(keys, dict) or not isinstance(keys.get("keys"), list) or any(
            not isinstance(key, dict) for key in keys["keys"]
        ):
            raise ValueError("invalid JWKS")
        if not isinstance(token, dict) or not isinstance(token.get("id_token"), str):
            raise ValueError("missing ID token")
        decoded = jwt.decode(token["id_token"], KeySet.import_key_set(keys), algorithms=_ALGORITHMS)
        claims = CodeIDToken(
            decoded.claims, decoded.header,
            options={
                "iss": {"essential": True, "value": oidc.issuer},
                "aud": {"essential": True, "value": oidc.client_id},
                "exp": {"essential": True}, "iat": {"essential": True},
                "sub": {"essential": True}, "nonce": {"essential": True},
            },
            params={"nonce": transaction["nonce"], "client_id": oidc.client_id, "access_token": token.get("access_token")},
        )
        claims.validate(leeway=30)
        audiences = claims.get("aud")
        if (
            claims.get("azp", oidc.client_id) != oidc.client_id
            or isinstance(audiences, list) and len(audiences) > 1 and claims.get("azp") != oidc.client_id
        ):
            raise ValueError("invalid authorized party")
        subject = claims.get("sub")
        if not isinstance(subject, str) or subject not in oidc.users:
            return _failure("denied")
        session.clear()
        session.update(authenticated=True, auth_method="oidc", issuer=oidc.issuer, sub=subject)
        session.permanent = True
        result = redirect(transaction["return"], 303)
        result.headers["Cache-Control"] = "no-store"
        result.headers["Referrer-Policy"] = "no-referrer"
        return result
    except (requests.RequestException, OAuthError, OAuth2Error, JoseError, ValueError, TypeError, KeyError):
        return _failure("invalid")
