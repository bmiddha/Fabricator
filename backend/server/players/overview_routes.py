"""Bounded current-player snapshot for the server overview."""
from flask import Blueprint, jsonify

from backend.server.players.live import preview_online
from backend.server.registry import get_server_process_registry
from backend.utils.routes import require_server

player_preview_bp = Blueprint("player_preview", __name__, url_prefix="/api/servers")


@player_preview_bp.get("/<server_id>/players/preview")
@require_server
def get_preview(server_id, server):
    return jsonify(preview_online(get_server_process_registry(), server))
