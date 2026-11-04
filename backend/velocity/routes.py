"""Dedicated proxy configuration API; no forwarding secret is returned."""
from flask import Blueprint, jsonify, request

from backend.server import storage
from backend.auth import permissions
from backend.velocity import forwarding, service


velocity_bp = Blueprint("velocity", __name__, url_prefix="/api")


def _proxy(server_id):
    server = storage.get_server(server_id)
    if server is None:
        return None, (jsonify({"error": "Server not found"}), 404)
    if not service.is_velocity(server):
        return None, (jsonify({"error": "This server is not a Velocity proxy"}), 400)
    return server, None


@velocity_bp.errorhandler(service.ConfigError)
def config_error(exc):
    return jsonify({"error": str(exc)}), 409 if isinstance(exc, service.BusyError) else 400


@velocity_bp.errorhandler(OSError)
def io_error(exc):
    # Do not echo filesystem content or secret-bearing parser diagnostics.
    return jsonify({"error": "Could not read or write proxy configuration files"}), 500


@velocity_bp.route("/servers/<server_id>/velocity/config", methods=["GET", "PUT"])
def config(server_id):
    server, error = _proxy(server_id)
    if error:
        return error
    if request.method == "GET":
        return jsonify(service.get_config(server))
    data = request.get_json(silent=True)
    user = permissions.principal()
    backends = data.get("backends") if isinstance(data, dict) else None
    if user and isinstance(backends, list):
        for backend in backends:
            sid = backend.get("serverId") if isinstance(backend, dict) else None
            if not isinstance(sid, str):
                continue  # The configuration validator reports malformed entries.
            if not user.can("server.view", sid):
                return jsonify({"error": "Server not found"}), 404
            if not user.can("server.settings.read", sid):
                return jsonify({"error": "permission denied"}), 403
    return jsonify(service.put_config(server, data))


@velocity_bp.route("/servers/<server_id>/velocity/forwarding/preview", methods=["POST"])
def preview(server_id):
    server, error = _proxy(server_id)
    if error:
        return error
    data = request.get_json(silent=True)
    error = _forwarding_permissions(server_id, data, apply=False)
    if error is not None:
        return error
    return jsonify(forwarding.configure(server, data))


@velocity_bp.route("/servers/<server_id>/velocity/forwarding/apply", methods=["POST"])
def apply(server_id):
    server, error = _proxy(server_id)
    if error:
        return error
    data = request.get_json(silent=True)
    error = _forwarding_permissions(server_id, data, apply=True)
    if error is not None:
        return error
    return jsonify(forwarding.configure(server, data, apply=True))


def _forwarding_permissions(server_id, data, *, apply):
    user = permissions.principal()
    if user is None:
        return None
    ids = data.get("serverIds") if isinstance(data, dict) else None
    if not isinstance(ids, list) or not all(isinstance(sid, str) for sid in ids):
        return jsonify({"error": "serverIds must be a list of server IDs"}), 400
    for sid in [server_id, *ids]:
        if not user.can("server.view", sid):
            return jsonify({"error": "Server not found"}), 404
        if not user.can("server.settings.read", sid) or (
            apply and not user.can("server.settings.write", sid)
        ):
            return jsonify({"error": "permission denied"}), 403
    return None
