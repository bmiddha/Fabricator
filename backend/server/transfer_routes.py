"""Capability-scoped preview/confirmation and owner-bound transfer jobs."""
import logging

from flask import Blueprint, jsonify, request

from backend.auth import permissions
from backend.backups import progress
from backend.server import transfers

transfers_bp = Blueprint("transfers", __name__, url_prefix="/api")
logger = logging.getLogger(__name__)


@transfers_bp.after_request
def private_response(response):
    response.headers["Cache-Control"] = "no-store"
    return response


@transfers_bp.route("/servers/<server_id>/worlds", methods=["GET"])
def list_worlds(server_id):
    try:
        return jsonify(worlds=transfers.list_worlds(server_id))
    except (ValueError, OSError) as exc:
        return jsonify(error=str(exc)), 400


def authorized(kind, source_id, data):
    user = permissions.principal()
    if user is None:
        return True
    required = ("server.fork", "files.read", "server.settings.read") if kind == "fork" else ("worlds.read",)
    if not user.can("server.view", source_id) or any(not user.can(p, source_id) for p in required):
        return False
    if kind == "fork":
        return user.can("servers.create")
    dest_id = data.get("destinationId")
    return isinstance(dest_id, str) and all(user.can(p, dest_id) for p in (
        "server.view", "worlds.import", "backups.create",
        *(("server.start",) if data.get("restart") is True else ()),
    ))


@transfers_bp.route("/servers/<server_id>/transfers/<kind>/preview", methods=["POST"])
@transfers_bp.route("/servers/<server_id>/transfers/<kind>", methods=["POST"])
def transfer(server_id, kind):
    if kind not in {"fork", "world-copy"}:
        return jsonify(error="Unknown transfer kind"), 404
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(error="Request body must be an object"), 400
    if not authorized(kind, server_id, data):
        return jsonify(error="Transfer permission denied"), 403
    try:
        preview = transfers.preview(kind, server_id, data)
        if request.path.endswith("/preview"):
            return jsonify(preview)
        if preview["previewToken"] != data.get("previewToken"):
            return jsonify(error="Preview is stale; preview again"), 409
        user = permissions.principal()
        owner = user.owner if user else None
        job_id = transfers.enqueue(kind, server_id, data, owner)
        return jsonify(jobId=job_id), 202
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    except OSError:
        logger.exception("Could not preview transfer")
        return jsonify(error="Could not read instance files; check storage permissions and panel logs"), 400


@transfers_bp.route("/transfer-jobs/<job_id>", methods=["GET"])
def job(job_id):
    value = progress.get(job_id)
    user = permissions.principal()
    if (not value or value.get("kind") not in {"fork", "world-copy"}
            or user and (value.get("owner") != user.owner
                         or not authorized(value["kind"], value["sourceId"], {
                             "destinationId": value.get("destinationId"),
                             "restart": value.get("restart", False),
                         }))):
        return jsonify(error="Transfer job not found"), 404
    payload = {k: v for k, v in value.items() if k != "owner"}
    if payload.get("error"):
        from backend.auth.redaction import redact, scrub_paths, _known_roots
        payload["error"] = scrub_paths(redact(payload["error"]), _known_roots())
    return jsonify(payload)
