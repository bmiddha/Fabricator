"""Server-scoped diagnostics; no host metrics, raw reports or arbitrary commands."""
from flask import Blueprint, jsonify, request

from backend.diagnostics import service
from backend.server import storage
from backend.utils.routes import require_server, with_server_lock

diagnostics_bp = Blueprint("diagnostics", __name__, url_prefix="/api/servers/<server_id>/diagnostics")


@diagnostics_bp.after_request
def private(response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@diagnostics_bp.errorhandler(service.DiagnosticError)
def invalid(error):
    return jsonify(error=str(error)), 409


@diagnostics_bp.get("")
@require_server
def history(server_id, server):
    window = request.args.get("window", "1h")
    seconds = {"1h": 3600, "6h": 21600, "24h": 86400}.get(window)
    if seconds is None:
        return jsonify(error="window must be 1h, 6h or 24h"), 400
    return jsonify(service.overview(server, seconds))


@diagnostics_bp.put("/sampling")
@require_server
@with_server_lock
def sampling(server_id, server):
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or type(data.get("jvmSampling")) is not bool:
        return jsonify(error="jvmSampling must be a boolean"), 400
    storage.update_server(server_id, {"diagnostics": {"jvmSampling": data["jvmSampling"]}})
    return jsonify(jvmSampling=data["jvmSampling"])


@diagnostics_bp.post("/captures")
@require_server
@with_server_lock
def capture(server_id, server):
    return jsonify(service.start_capture(server, request.get_json(silent=True))), 202
