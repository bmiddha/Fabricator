"""Session-gated operator DNS configuration."""
from __future__ import annotations

import json
from flask import Blueprint, jsonify, request
from werkzeug.exceptions import RequestEntityTooLarge

from backend.managed import is_managed
from backend.server import storage as server_storage
from . import models, service, storage
from .client import ProviderError

dns_bp = Blueprint("dns", __name__, url_prefix="/api")


def _body():
    if not request.is_json:
        raise ValueError("JSON request body required")
    raw = request.stream.read(2 * 1024 * 1024 + 1)
    if len(raw) > 2 * 1024 * 1024:
        raise RequestEntityTooLarge()
    try:
        return json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise ValueError("Invalid JSON request body") from exc


@dns_bp.errorhandler(RequestEntityTooLarge)
def oversized(exc):
    return jsonify({"error": "DNS request exceeds 2 MiB"}), 413


@dns_bp.before_request
def managed_gate():
    if is_managed():
        return jsonify({"error": "DNS administration is unavailable in managed mode"}), 403


@dns_bp.errorhandler(ValueError)
def invalid(exc):
    return jsonify({"error": str(exc)}), 400


@dns_bp.errorhandler(ProviderError)
def provider_error(exc):
    return jsonify({"error": str(exc)}), 502


@dns_bp.route("/dns", methods=["GET"])
def get_dns():
    with storage.transaction() as data:
        return jsonify(data)


@dns_bp.route("/dns/config", methods=["PUT"])
def configure():
    cfg = models.config(_body())
    with storage.transaction() as data:
        if data["config"] and data["config"]["domain"] != cfg["domain"] and (data["records"] or any(b.get("enabled") for b in data["bindings"].values())):
            raise ValueError("Remove existing DNS bindings and manual records before changing the managed domain")
        data["config"] = cfg
        data["status"] = {"state": "pending", "error": None}
    service.request_sync()
    return jsonify(cfg)


@dns_bp.route("/dns/records", methods=["PUT"])
def set_records():
    with storage.transaction() as data:
        if not data["config"]:
            raise ValueError("Configure a DNS provider first")
        data["records"] = models.records(_body(), data["config"]["domain"])
        service.desired(data)
        data["status"] = {"state": "pending", "error": None}
    service.request_sync()
    return jsonify(data["records"])


@dns_bp.route("/dns/plan", methods=["POST"])
def plan():
    return jsonify(service.preview())


@dns_bp.route("/dns/sync", methods=["POST"])
def synchronize():
    status = service.sync()
    return jsonify(status), 502 if status["state"] == "failed" else 200


@dns_bp.route("/servers/<server_id>/dns", methods=["GET", "PUT"])
def server_dns(server_id):
    if not server_storage.get_server(server_id):
        return jsonify({"error": "Server not found"}), 404
    if request.method == "GET":
        with storage.lock:
            return jsonify(storage.load()["bindings"].get(server_id, {"enabled": False}))
    with storage.transaction() as data:
        if not data["config"]:
            raise ValueError("Configure a DNS provider in global settings first")
        binding = models.binding(_body(), data["config"]["domain"])
        data["bindings"][server_id] = binding
        service.desired(data)
        data["status"] = {"state": "pending", "error": None}
    service.request_sync()
    return jsonify(binding)
