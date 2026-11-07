"""Flask application factory and shared extensions."""
import os
import sys
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

from backend.core.config import get_config
from backend.auth import init_auth
from backend.server.routes import server_bp
from backend.modrinth.routes import modrinth_bp
from backend.modrinth.client_pack_routes import client_pack_bp, client_pack_share_bp
from backend.modrinth.update_routes import updates_bp
from backend.system.routes import system_bp
from backend.backups.routes import backups_bp
from backend.server.players.routes import players_bp
from backend.playit.routes import playit_bp
from backend.integrations.routes import integrations_bp
from backend.schedules.routes import schedules_bp
from backend.dns.routes import dns_bp
from backend.velocity.routes import velocity_bp
from backend.server.transfer_routes import transfers_bp
from backend.server.world_export_routes import world_exports_bp, world_share_bp
from backend.maps.routes import maps_bp
from backend.diagnostics.routes import diagnostics_bp
from backend.events.routes import events_bp
from backend.backups.scheduler import init_scheduler

def get_base_path() -> str:
    """Return base path that works in dev and when frozen with PyInstaller."""
    if getattr(sys, 'frozen', False):
        return sys._MEIPASS  # type: ignore[attr-defined]
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load_dotenv() -> None:
    """Load ``.env`` from project root (and optionally cwd)."""
    from dotenv import load_dotenv
    base = Path(get_base_path())
    load_dotenv(base / ".env")
    load_dotenv()


def create_app() -> Flask:
    """Create and configure the Flask application."""
    _load_dotenv()
    base_path = get_base_path()
    static_dir = Path(base_path) / "frontend" / "dist"
    static_folder_path = str(static_dir)

    app = Flask(__name__, static_folder=None)

    config = get_config()
    app.config.from_object(config)

    # Built-in authentication: fail-closed validation + the request gate. Runs
    # before any startup side effects so a misconfigured (enabled-but-unconfigured)
    # install refuses to start cleanly instead of half-initialising.
    init_auth(app)

    CORS(app, origins=config.CORS_ORIGINS)

    app.register_blueprint(server_bp)
    app.register_blueprint(modrinth_bp)
    app.register_blueprint(client_pack_bp)
    app.register_blueprint(client_pack_share_bp)
    app.register_blueprint(system_bp)
    app.register_blueprint(backups_bp)
    app.register_blueprint(players_bp)
    app.register_blueprint(playit_bp)
    app.register_blueprint(integrations_bp)
    app.register_blueprint(schedules_bp)
    app.register_blueprint(dns_bp)
    app.register_blueprint(maps_bp)
    app.register_blueprint(diagnostics_bp)
    app.register_blueprint(velocity_bp, url_prefix="/api")
    app.register_blueprint(transfers_bp)
    app.register_blueprint(updates_bp)
    app.register_blueprint(events_bp)
    from backend.events.audit import capture_actor, record_response
    from backend.events.service import init_events
    app.before_request(capture_actor)
    app.after_request(record_response)
    app.register_blueprint(world_exports_bp)
    app.register_blueprint(world_share_bp)

    # One-time startup cleanup: flip stale 'installing'/'starting' statuses
    # left over from a previous run. Tolerates a missing/malformed index.
    from backend.server.routes import cleanup_stale_statuses
    cleanup_stale_statuses()
    from backend.server.transfers import recover as recover_transfers
    recover_transfers()
    from backend.modrinth.updates import recover as recover_updates
    recover_updates()
    from backend.server.world_exports import recover as recover_world_exports
    recover_world_exports()
    init_events(app)

    # Same idea for abandoned world uploads (up to 10 GiB each). They are
    # stranded by exactly the unclean shutdown this boot follows, so sweeping
    # here catches the case the on-next-upload sweep cannot: an interrupted
    # import on an install that never imports another world. Best-effort —
    # housekeeping must never stop the app from starting.
    try:
        from backend.backups.world_import import sweep_stale_uploads
        sweep_stale_uploads()
    except Exception:  # pragma: no cover - defensive
        app.logger.warning("Could not sweep abandoned world uploads", exc_info=True)

    # Boot the backup scheduler AFTER stale-status cleanup so any
    # config we re-register doesn't try to fire against a server that
    # was just transitioned out of 'installing'. Idempotent across
    # double-init (Werkzeug debug-reload, repeated create_app() in
    # tests) and gated by FABRICATOR_DISABLE_SCHEDULER=1.
    init_scheduler(app)
    from backend.schedules.service import register_all_jobs as register_schedule_jobs
    register_schedule_jobs()
    from backend.dns.service import init_dns
    init_dns()
    from backend.diagnostics.service import init as init_diagnostics
    init_diagnostics(app)

    @app.route(
        "/api/<path:_>",
        methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
    )
    def api_not_found(_):
        """Return JSON (not the SPA's HTML) for unknown /api/... paths."""
        return jsonify({"error": "endpoint not found", "path": request.path}), 404

    @app.route("/", defaults={"path": ""})
    @app.route("/<path:path>")
    def serve_frontend(path: str):
        """Serve Vue build files when available, otherwise warn the user."""

        index_file = Path(static_folder_path) / "index.html"
        requested = Path(static_folder_path) / path

        if path and requested.exists() and requested.is_file():
            return send_from_directory(static_folder_path, path)

        if index_file.exists():
            if path in {"client-pack-share", "world-share"}:
                response = send_from_directory(static_folder_path, "index.html")
                response.headers["Cache-Control"] = "no-store"
                response.headers["Referrer-Policy"] = "no-referrer"
                response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
                response.headers["Content-Security-Policy"] = (
                    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                    "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
                    "base-uri 'none'; frame-ancestors 'none'; form-action 'none'"
                )
                return response
            return send_from_directory(static_folder_path, "index.html")

        return (
            jsonify(
                {
                    "error": "Frontend not built",
                    "detail": "Run `npm run build` in the frontend folder.",
                }
            ),
            503,
        )

    return app
