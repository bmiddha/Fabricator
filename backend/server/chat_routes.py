"""Authenticated, durable server-scoped chat with no console capability."""
from flask import Blueprint, current_app, g, jsonify, request, session

from backend.auth import permissions
from backend.events import store as audit
from backend.server import chat, chat_storage
from backend.server.registry import get_server_process_registry
from backend.utils.routes import require_server, with_server_lock

chat_bp = Blueprint("chat", __name__, url_prefix="/api")


def _identity():
    user = g.get("principal")
    if user:
        return (
            (user.issuer, user.subject),
            chat.display_name(session.get("oidc_display_name") or user.subject),
        )
    if current_app.config.get("FABRICATOR_AUTH_ENABLED") and session.get("authenticated") is True:
        return ("shared-password", "panel"), "Shared admin"
    return None, None


@chat_bp.after_request
def private_chat(response):
    response.headers["Cache-Control"] = "no-store"
    return response


@chat_bp.get("/servers/<server_id>/chat")
@require_server
def read_chat(server_id, server):
    def integer(key, default=None, maximum=2**63 - 1):
        value = request.args.get(key)
        if value is None:
            return default
        if not value.isascii() or not value.isdigit() or not 1 <= int(value) <= maximum:
            raise ValueError(f"{key} must be a positive integer up to {maximum}")
        return int(value)
    try:
        before, after = integer("before"), integer("after")
        limit = integer("limit", 100, chat_storage.MAX_PAGE)
        if before is not None and after is not None:
            raise ValueError("Use before or after, not both")
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    try:
        history = chat_storage.history(server_id, before=before, after=after, limit=limit)
    except chat_storage.ChatStorageError:
        current_app.logger.error("Chat history read failed")
        return jsonify({"error": "Chat history storage is unavailable; no console fallback is used"}), 503
    manager = get_server_process_registry().get_manager(server_id)
    loader = manager.loader if manager else server.get("loader")
    version = manager.version if manager else server.get("version")
    supported = chat.supported(loader, version)
    _, name = _identity()
    ready = bool(manager and manager.is_running and manager.startup_ready and not manager.is_stopping)
    return jsonify({
        **history,
        "supported": supported,
        "supportNote": (
            "Standard <player> chat only. Custom/plugin formats and system messages are not parsed."
            if supported else "Chat is unsupported for this loader/version. Use a supported Java release."
        ),
        "running": bool(manager and manager.is_running),
        "offlineMode": server.get("onlineMode") is not True,
        "ready": ready,
        "sender": name,
        "canSend": bool(name and supported and ready and permissions.can("chat.send", server_id)),
        "captureErrors": manager.chat.capture_errors if manager else 0,
        "retentionSeconds": chat_storage.RETENTION_SECONDS,
        "maxItems": chat_storage.MAX_PER_SERVER,
        "maxTotalItems": chat_storage.MAX_TOTAL,
        "maxMessageLength": chat.MAX_MESSAGE,
    })


@chat_bp.post("/servers/<server_id>/chat")
@require_server
@with_server_lock
def send_chat(server_id, server):
    actor, name = _identity()
    if actor is None:
        return jsonify({"error": "Sign in to send attributed chat; authentication is disabled"}), 403
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or set(body) != {"message"}:
        return jsonify({"error": "Provide only a message; sender is the authenticated panel identity"}), 400
    try:
        message = chat.validate_message(body["message"])
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    manager = get_server_process_registry().get_manager(server_id)
    loader = manager.loader if manager else server.get("loader")
    version = manager.version if manager else server.get("version")
    if not chat.supported(loader, version):
        return jsonify({"error": "Chat is unsupported for this loader/version"}), 422
    if not manager or not manager.is_running or not manager.startup_ready or manager.is_stopping:
        audit.emit("chat.send", server_id=server_id, outcome="failed")
        return jsonify({"error": "Server is stopped or not ready; chat never starts a server"}), 409
    retry = manager.chat.reserve(actor)
    if retry:
        return jsonify({"error": "Chat rate limit reached; try again later", "retryAfter": retry}), 429, {"Retry-After": str(retry)}
    try:
        pending = chat_storage.append(server_id, {
            "kind": "fabricator", "sender": name, "message": message, "status": "pending",
        }, who=audit.actor())
    except chat_storage.ChatStorageError:
        current_app.logger.error("Chat send refused because its history could not be persisted")
        audit.emit("chat.send", server_id=server_id, outcome="failed")
        return jsonify({"error": "Chat history could not be saved; message was not sent", "submitted": False}), 503
    result = manager.send_command(chat.command_for(name, message), require_ready=True)
    try:
        item = chat_storage.finish_send(server_id, pending["id"], "submitted" if result.get("success") else "failed")
    except chat_storage.ChatStorageError:
        current_app.logger.error("Chat submission outcome could not be persisted")
        audit.emit("chat.send", server_id=server_id, outcome="failed")
        return jsonify({
            "error": "Chat submission outcome could not be saved; delivery is uncertain. Do not retry blindly.",
            "submitted": bool(result.get("success")),
        }), 503
    if not result.get("success"):
        audit.emit("chat.send", server_id=server_id, outcome="failed")
        return jsonify({"error": "Server chat could not be submitted; check server readiness and connection"}), 503
    audit.emit("chat.send", server_id=server_id, outcome="accepted")
    return jsonify({"item": item, "status": "submitted"}), 202
