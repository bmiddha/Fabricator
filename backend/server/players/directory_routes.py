"""Permission-scoped directory endpoints; no player-management operations."""
from flask import Blueprint, jsonify, request

from backend.server import storage
from backend.server.players.directory import aggregate
from backend.server.registry import get_server_process_registry

directory_bp = Blueprint("player_directory", __name__, url_prefix="/api/players")


def _snapshot():
    return aggregate(get_server_process_registry(), storage.get_all_servers())


@directory_bp.get("")
def list_players():
    relation = request.args.get("relation", "all")
    query = request.args.get("q", "").strip().casefold()
    server_id = request.args.get("server")
    try:
        offset = int(request.args.get("offset", 0))
        limit = int(request.args.get("limit", 50))
    except ValueError:
        return jsonify({"error": "Invalid pagination"}), 400
    if relation not in {"all", "joined", "whitelisted", "online"} or len(query) > 100 or offset < 0 or not 1 <= limit <= 100:
        return jsonify({"error": "Invalid directory filter or pagination"}), 400
    data = _snapshot()
    rows = [p for p in data["players"] if
            (not query or query in p["uuid"] or any(query in n.casefold() for n in p["names"]))
            and any((not server_id or s["id"] == server_id)
                    and (relation == "all" or s[relation] is True) for s in p["servers"])]
    return jsonify({**data, "players": rows[offset:offset + limit], "total": len(rows),
                    "offset": offset, "limit": limit})


@directory_bp.get("/<path:player_id>")
def get_player(player_id):
    data = _snapshot()
    player = next((p for p in data["players"] if p["id"] == player_id), None)
    if player is None:
        return jsonify({"error": "Player not found"}), 404
    return jsonify(player)
