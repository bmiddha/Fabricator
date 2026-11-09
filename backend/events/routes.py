"""Capability-scoped activity and instance-wide notification management."""
from flask import Blueprint, jsonify, request

from backend.events import service, store
from backend.auth.permissions import can
from backend.utils.routes import require_server

events_bp = Blueprint("events", __name__, url_prefix="/api")


@events_bp.after_request
def private_response(response):
    response.headers["Cache-Control"] = "no-store"
    return response


@events_bp.before_request
def bounded_body():
    if request.content_length is not None and request.content_length > 16_384:
        return jsonify({"error": "Notification configuration body exceeds 16 KiB"}), 413


def _history(server_id=None, instance=False, alerts=False):
    try:
        before = int(request.args["before"]) if "before" in request.args else None
        if before is not None and before <= 0:
            raise ValueError
    except ValueError:
        return jsonify({"error": "before must be a positive event ID"}), 400
    return jsonify(store.history(server_id, before=before, instance=instance, alerts_only=alerts))


@events_bp.get("/activity")
def instance_activity():
    return _history(instance=True, alerts=request.args.get("alerts") == "1")


@events_bp.get("/servers/<server_id>/activity")
@require_server
def server_activity(server_id, server):
    return _history(server_id)


@events_bp.get("/servers/<server_id>/tasks")
@require_server
def server_tasks(server_id, server):
    result = store.tasks(server_id)
    def visible(task):
        permission = "backups.restore" if task["action"] == "worlds.restore" else task["action"]
        scopes = {sid for sid in (task["server_id"], task["related_server"]) if sid}
        if any(not can("server.view", sid) for sid in scopes):
            return False
        if all(can("activity.read", sid) for sid in scopes):
            return True
        if permission == "worlds.copy":
            return can("worlds.read", task["server_id"]) and all(
                can(p, task["related_server"]) for p in ("worlds.import", "backups.create")
            )
        if permission == "server.fork":
            return can("servers.create") and all(
                can(p, task["server_id"]) for p in ("server.fork", "files.read", "server.settings.read")
            )
        return all(can(permission, sid)
                   or (permission == "server.install" and can("server.upgrade", sid))
                   for sid in scopes)
    result["items"] = [
        {**task, "server_name": server.get("name") or server_id}
        for task in result["items"] if visible(task)
    ]
    return jsonify(result)


@events_bp.get("/servers/<server_id>/alerts")
@require_server
def server_alerts(server_id, server):
    return _history(server_id, alerts=True)


@events_bp.get("/notifications")
def notifications():
    return jsonify({"destinations": service.destinations(), "deliveries": service.deliveries(),
                    "alerts": store.history(instance=True, alerts_only=True)["items"]})


@events_bp.post("/notifications/destinations")
def create_destination():
    try:
        destination_id = service.save_destination(request.get_json(silent=True))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"id": destination_id}), 201


@events_bp.put("/notifications/destinations/<destination_id>")
def update_destination(destination_id):
    try:
        service.save_destination(request.get_json(silent=True), destination_id)
    except LookupError:
        return jsonify({"error": "Destination not found"}), 404
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"success": True})


@events_bp.delete("/notifications/destinations/<destination_id>")
def delete_destination(destination_id):
    if not service.delete_destination(destination_id):
        return jsonify({"error": "Destination not found"}), 404
    return jsonify({"success": True})


@events_bp.post("/notifications/destinations/<destination_id>/test")
def test_destination(destination_id):
    try:
        event_id = service.test_destination(destination_id)
    except LookupError:
        return jsonify({"error": "Destination not found"}), 404
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"event_id": event_id, "status": "queued"}), 202
