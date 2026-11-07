"""Operator-only, owner-bound staged pack updates; MCP tokens cannot approve."""
import logging

from flask import Blueprint, jsonify, request

from backend.auth import permissions
from backend.modrinth import mrpack, updates
from backend.server import transfers

updates_bp = Blueprint("modpack_updates", __name__, url_prefix="/api/servers/<server_id>/modpack-updates")
logger = logging.getLogger(__name__)


@updates_bp.after_request
def private(response):
    response.headers["Cache-Control"] = "no-store"
    return response


def _owner():
    user = permissions.principal()
    return user.owner if user else None


def _require(server_id, *grants):
    if updates.is_managed():
        raise ValueError("Staged modpack updates are disabled in managed mode")
    user = permissions.principal()
    if user and any(not user.can(grant, server_id) for grant in ("server.view", *grants)):
        raise PermissionError("Staged update permission denied")


def _public(plan):
    fields = {"id", "serverId", "createdAt", "status", "warnings", "summary", "changes",
              "preservedFiles", "loaderChanges", "dependencyReview", "clientImpact",
              "reviewToken", "stagingId", "validatedAt", "promotedAt", "safetySnapshotId",
              "error", "failedAction", "productionChanged", "recoveryRequired"}
    value = {key: item for key, item in plan.items() if key in fields}
    if value.get("error"):
        from backend.auth.redaction import redact, scrub_paths, _known_roots
        value["error"] = scrub_paths(redact(value["error"]), _known_roots())
    return value


def _plan(server_id, plan_id):
    plan = updates.read(server_id, plan_id)
    if plan.get("owner") != _owner():
        raise PermissionError("Update plan not found")
    return plan


@updates_bp.route("", methods=["GET", "POST"])
def collection(server_id):
    _require(server_id, "modpacks.install", "files.read", "server.settings.read")
    if request.method == "GET":
        return jsonify(plans=[_public(plan) for plan in updates.list_plans(server_id, _owner())])
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ValueError("Request body must be an object")
    if data.get("source") == "upload":
        pack = mrpack.get(data.get("upload_id"))
        if not pack or pack.owner != _owner():
            raise PermissionError("Uploaded pack not found")
    plan = updates.create(server_id, data, _owner())
    updates.enqueue("prepare", server_id, plan["id"])
    return jsonify(_public(plan)), 202


@updates_bp.route("/<plan_id>", methods=["GET", "DELETE"])
def detail(server_id, plan_id):
    _require(server_id, "modpacks.install", "files.read", "server.settings.read")
    with transfers.locked([server_id]):
        plan = _plan(server_id, plan_id)
        if request.method == "DELETE":
            if plan["status"] in updates.BUSY:
                raise ValueError("Wait for the active update operation before deleting its plan")
            server = updates.storage.get_server(server_id)
            if server and (transfers._literal_root(server) / ".fabricator-update").exists():
                raise ValueError("Recover the interrupted production update before deleting review data")
            directory = updates._path(server_id, plan_id)
            transfers._remove(directory)
            client_metadata = updates.client_pack._source_path(directory / "incoming")
            client_metadata.unlink(missing_ok=True)
            return jsonify(deleted=True, stagingRetained=bool(plan.get("stagingId")))
        return jsonify(_public(plan))


@updates_bp.route("/<plan_id>/<action>", methods=["POST"])
def action(server_id, plan_id, action):
    if action not in {"stage", "validate", "promote"}:
        return jsonify(error="Unknown update action"), 404
    _require(server_id, "modpacks.install", "files.read", "server.settings.read")
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ValueError("Request body must be an object")
    with transfers.locked([server_id]):
        plan = _plan(server_id, plan_id)
        if plan["status"] in updates.BUSY:
            raise ValueError("An update operation is already in progress")
        if data.get("reviewToken") != plan.get("reviewToken") or not plan.get("reviewToken"):
            raise updates.StalePlan("Review token changed; refresh the plan")
        expected = {"stage": "review", "validate": "staged", "promote": "validated"}[action]
        if plan["status"] != expected:
            raise ValueError(f"This action requires an update in {expected} state")
        updates._check(plan)
        if action == "stage":
            _require(server_id, "server.fork", "servers.create")
            if data.get("acknowledgeRisks") is not True:
                raise ValueError("Acknowledge staging copy and configuration risks")
        else:
            staging_id = plan.get("stagingId")
            if not staging_id:
                raise ValueError("Create a staging instance first")
            _require(staging_id, "server.settings.read", "files.read", "server.start", "server.stop")
        if action == "validate" and data.get("executePackCode") is not True:
            raise ValueError("Explicitly opt in to executing trusted pack code on staging")
        if action == "promote":
            _require(server_id, "modpacks.promote", "backups.create")
            server = updates.storage.get_server(server_id)
            if data.get("confirmName") != server["name"]:
                raise ValueError("Type the production instance name to approve promotion")
        plan.update(status={"stage": "staging", "validate": "validating", "promote": "promoting"}[action])
        updates._write(updates._path(server_id, plan_id), plan)
        updates.enqueue(action, server_id, plan_id, data)
        return jsonify(_public(plan)), 202


@updates_bp.errorhandler(ValueError)
@updates_bp.errorhandler(PermissionError)
@updates_bp.errorhandler(OSError)
def invalid(exc):
    if isinstance(exc, PermissionError):
        return jsonify(error=str(exc)), 403
    if isinstance(exc, OSError):
        logger.exception("Could not access staged modpack update")
        return jsonify(error="Could not access update storage; check panel logs and storage permissions"), 400
    return jsonify(error=str(exc)), 409 if isinstance(exc, updates.StalePlan) else 400
