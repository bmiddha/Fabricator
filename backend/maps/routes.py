"""Private BlueMap management, viewer capabilities and read-only resources."""
import mimetypes

from flask import Blueprint, Response, current_app, jsonify, request

from backend.maps import service
from backend.maps.sandbox import BOOTSTRAP
from backend.server import storage
from backend.utils.routes import require_server, with_server_lock

maps_bp = Blueprint("maps", __name__, url_prefix="/api/servers/<server_id>/map")


@maps_bp.after_request
def private_headers(response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
    return response


@maps_bp.errorhandler(service.MapError)
def invalid_map(error):
    return jsonify(error=str(error)), 409


@maps_bp.get("")
@require_server
def status(server_id, server):
    plan = service.preview(server)
    enabled = server.get("map", {}).get("enabled") is True
    result = {"enabled": enabled, "ready": False, "preview": plan,
              "configured": isinstance(server.get("map", {}).get("port"), int)}
    if enabled:
        try:
            config = service.manifest(server)
            if not config:
                raise service.MapError("Map configuration is missing.")
            if not plan["ready_to_configure"]:
                raise service.MapError("Map dependencies changed; review compatibility before viewing.")
            result.update(service.health(server, config), ready=True)
        except service.MapError as error:
            result["message"] = str(error)
    return jsonify(result)


@maps_bp.post("/setup")
@require_server
@with_server_lock
def setup(server_id, server):
    data = request.get_json(silent=True)
    service.configure(server, data.get("accept_resource_download") if isinstance(data, dict) else None)
    return jsonify(enabled=True, restart_required=True), 201


@maps_bp.post("/install-bluemap")
@require_server
@with_server_lock
def install_bluemap(server_id, server):
    service.install_bluemap(server)
    return jsonify(installed=True, restart_required=True), 201


@maps_bp.put("/enabled")
@require_server
@with_server_lock
def enabled(server_id, server):
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or type(data.get("enabled")) is not bool:
        return jsonify(error="enabled must be a boolean"), 400
    if data["enabled"] is False:
        storage.update_server(server_id, {"map": {**server.get("map", {}), "enabled": False}})
        return jsonify(enabled=False)
    config = service.manifest(server)
    if not config:
        raise service.MapError("Configure maps first.")
    if not service.preview(server)["ready_to_configure"]:
        raise service.MapError("Review compatible map dependencies before enabling access.")
    storage.update_server(server_id, {"map": {"enabled": data["enabled"], "port": config["port"]}})
    return jsonify(enabled=data["enabled"])


@maps_bp.post("/session")
@require_server
def session(server_id, server):
    if server.get("map", {}).get("enabled") is not True:
        raise service.MapError("Map access is disabled.")
    config = service.manifest(server)
    if not config:
        raise service.MapError("Configure maps first.")
    service.health(server, config)
    ticket = service.issue_ticket(server)
    return jsonify(url=f"/api/servers/{server_id}/map/view/{ticket}/",
                   expires_in=service.TICKET_SECONDS)


@maps_bp.get("/view/<ticket>/", defaults={"path": ""})
@maps_bp.get("/view/<ticket>/<path:path>")
def resource(server_id, ticket, path):
    # Also enforced by the auth gate, before resolving a server or transport.
    if not service.ticket_allowed(server_id, ticket):
        return jsonify(error="Map session expired; refresh the Map tab."), 401
    server = storage.get_server(server_id)
    if not server or server.get("map", {}).get("enabled") is not True:
        return jsonify(error="Map access is disabled."), 404
    path = service.safe_path(path)
    config = service.manifest(server)
    if not config:
        raise service.MapError("Map configuration is missing.")
    health = service.health(server, config)
    if path == "fabricator-sandbox.js":
        raw = BOOTSTRAP
    else:
        raw = service.upstream(config, path)
    if path == "index.html":
        if b"<head>" not in raw:
            raise service.MapError("BlueMap viewer entry point is not compatible.")
        raw = raw.replace(b"<head>", b'<head><script src="./fabricator-sandbox.js"></script>', 1)
    if path.endswith(".json"):
        try:
            raw = service.filter_json(path, raw, health["global_markers"])
        except (ValueError, TypeError, AttributeError):
            current_app.logger.warning("BlueMap returned malformed map JSON for %s", path)
            return jsonify(error="BlueMap returned invalid map data."), 502
    content_type = mimetypes.guess_type(path)[0] or "application/octet-stream"
    response = Response(raw, content_type=content_type)
    if path.endswith(".gz"):
        response.headers["Content-Encoding"] = "gzip"
    # An opaque iframe needs CORS for module scripts, fetch and WebGL textures.
    # Never enable credentials here or expose the panel's authenticated API.
    response.headers["Access-Control-Allow-Origin"] = "null"
    prefix = service.ticket_origin(ticket) + f"/api/servers/{server_id}/map/view/{ticket}/"
    response.headers["Content-Security-Policy"] = (
        "default-src 'none'; "
        # BlueMap 5.7's bundled i18n compiler uses Function(). Eval is permitted
        # only inside this opaque, cookie-free sandbox, never on panel pages.
        f"script-src 'unsafe-eval' {prefix}; style-src 'unsafe-inline' {prefix}; "
        f"img-src data: blob: {prefix}; font-src {prefix}; connect-src {prefix}; "
        "worker-src blob:; object-src 'none'; base-uri 'none'; form-action 'none'; "
        "frame-ancestors 'self'; sandbox allow-scripts"
    )
    return response
