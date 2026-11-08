"""Installation-scoped S3 recovery; never accessible to MCP bearer tokens."""
from flask import Blueprint, jsonify, request

from backend.auth import permissions
from backend.backups import destinations, progress, recovery

recovery_bp = Blueprint("s3_recovery", __name__, url_prefix="/api/s3-recovery")


@recovery_bp.after_request
def private_response(response):
    response.headers["Cache-Control"] = "no-store"
    return response


@recovery_bp.before_request
def authorize():
    user = permissions.principal()
    if user and not all(user.can(p) for p in ("servers.import", "servers.create")):
        return jsonify(error="Instance import and creation permissions are required"), 403


def owner():
    user = permissions.principal()
    return user.owner if user else None


@recovery_bp.route("/discover", methods=["POST"])
def discover():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(error="Request body must be an object"), 400
    try:
        return jsonify(recovery.discover(data.get("destination"), data.get("cursor")))
    except (ValueError, destinations.DestinationError) as exc:
        return jsonify(error=str(exc)), 400


@recovery_bp.route("/previews", methods=["POST"])
@recovery_bp.route("/imports", methods=["POST"])
def enqueue():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(error="Request body must be an object"), 400
    kind = "s3-preview" if request.path.endswith("/previews") else "s3-import"
    try:
        return jsonify(jobId=recovery.enqueue(kind, data, owner())), 202
    except ValueError as exc:
        return jsonify(error=str(exc)), 400


@recovery_bp.route("/previews/<token>", methods=["GET", "DELETE"])
def preview(token):
    try:
        if request.method == "DELETE":
            recovery.discard(token, owner())
            return jsonify(ok=True)
        return jsonify(recovery.preview_status(token, owner()))
    except ValueError as exc:
        return jsonify(error=str(exc)), 404


@recovery_bp.route("/jobs/<job_id>", methods=["GET"])
def job(job_id):
    value = progress.get(job_id)
    if not value or value.get("kind") not in {"s3-preview", "s3-import"} or value.get("owner") != owner():
        return jsonify(error="Recovery job not found; check the saved preview or instance list after a restart"), 404
    payload = {k: v for k, v in value.items() if k != "owner"}
    if payload.get("error"):
        from backend.auth.redaction import redact, scrub_paths, _known_roots
        payload["error"] = scrub_paths(redact(payload["error"]), _known_roots())
    return jsonify(payload)
