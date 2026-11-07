"""Private world export management and cookie-free bearer archive redemption."""
import json
import math
import os
import stat
import threading
import tarfile

from flask import Blueprint, Response, current_app, jsonify, request

from backend.auth.permissions import principal
from backend.managed import is_managed
from backend.modrinth import client_pack_shares as shares
from backend.modrinth.client_pack_routes import _private_headers
from backend.modrinth.ratelimit import RateLimiter, RateLimitExceeded
from backend.server import locks, storage, world_exports as exports
from backend.utils.routes import require_server, with_server_lock

world_exports_bp = Blueprint("world_exports", __name__, url_prefix="/api/servers")
world_share_bp = Blueprint("world_share", __name__, url_prefix="/api/world-shares")


@world_share_bp.record_once
def _init(state):
    state.app.extensions["world_share_limiter"] = RateLimiter(capacity=30, window_seconds=60, max_wait=0)
    state.app.extensions["world_download_slots"] = threading.BoundedSemaphore(4)


world_exports_bp.after_request(_private_headers)
world_share_bp.after_request(_private_headers)


@world_exports_bp.before_request
def _managed_gate():
    if is_managed():
        return jsonify(error="World exports are unavailable in managed mode"), 409


def _owner():
    user = principal()
    return user.owner if user else None


def _failure(exc):
    if isinstance(exc, FileNotFoundError):
        return jsonify(error="World archive or share not found"), 404
    if isinstance(exc, OSError):
        current_app.logger.exception("World export storage operation failed")
        return jsonify(error="World export storage failed; check disk space and permissions and retry"), 500
    return jsonify(error=str(exc)), 409


def _download(server, artifact_id):
    if not storage.get_server(server["id"]):
        raise FileNotFoundError("Server deleted")
    path, metadata = exports.artifact_path(server, artifact_id)
    slots = current_app.extensions["world_download_slots"]
    if not slots.acquire(blocking=False):
        response = jsonify(error="World downloads busy; wait and retry")
        response.status_code = 503
        response.headers["Retry-After"] = "30"
        return response
    handle = None
    released = False

    def close():
        nonlocal released
        if not released:
            released = True
            try:
                if handle:
                    handle.close()
            finally:
                slots.release()

    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        handle = os.fdopen(fd, "rb")
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size != metadata["size"]:
            raise exports.PackError("World archive changed; check storage")

        def stream():
            try:
                yield from iter(lambda: handle.read(1024 * 1024), b"")
            finally:
                close()

        response = Response(stream(), mimetype="application/gzip")
        response.headers["Content-Length"] = str(metadata["size"])
        response.headers["Content-Disposition"] = f'attachment; filename="{metadata["filename"]}"'
        response.headers["X-Archive-SHA256"] = metadata["sha256"]
        response.call_on_close(close)
        return response
    except BaseException:
        close()
        raise


@world_exports_bp.get("/<server_id>/world-exports")
@require_server
@with_server_lock
def list_exports(server_id, server):
    try:
        return jsonify(exports=exports.artifacts(server))
    except (ValueError, OSError) as exc:
        return _failure(exc)


@world_exports_bp.post("/<server_id>/world-exports")
@require_server
@with_server_lock
def build_export(server_id, server):
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or not isinstance(data.get("world_id"), str):
        return jsonify(error="JSON world_id is required"), 400
    try:
        server = storage.get_server(server_id)
        if not server:
            return jsonify(error="Server not found"), 404
        return jsonify(exports.build(server, data["world_id"])), 201
    except (ValueError, OSError, tarfile.TarError) as exc:
        return _failure(exc)


@world_exports_bp.get("/<server_id>/world-exports/<artifact_id>/download")
@require_server
@with_server_lock
def download_export(server_id, artifact_id, server):
    try:
        return _download(server, artifact_id)
    except (ValueError, OSError) as exc:
        return _failure(exc)


@world_exports_bp.get("/<server_id>/world-export-shares")
@require_server
@with_server_lock
def list_shares(server_id, server):
    try:
        return jsonify(shares=shares.listing(server, _owner(), exports))
    except (ValueError, OSError) as exc:
        return _failure(exc)


@world_exports_bp.post("/<server_id>/world-exports/<artifact_id>/shares")
@require_server
@with_server_lock
def create_share(server_id, artifact_id, server):
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or data.get("acknowledge_privacy") is not True:
        return jsonify(error="Review world privacy and explicitly acknowledge before sharing"), 400
    try:
        return jsonify(shares.create(server, artifact_id, data.get("expiry_seconds"), _owner(), exports)), 201
    except (ValueError, OSError) as exc:
        return _failure(exc)


@world_exports_bp.delete("/<server_id>/world-export-shares/<share_id>")
@require_server
@with_server_lock
def revoke_share(server_id, share_id, server):
    try:
        shares.revoke(server, share_id, _owner(), exports)
        return jsonify(success=True)
    except (ValueError, OSError) as exc:
        return _failure(exc)


@world_exports_bp.delete("/<server_id>/world-exports/<artifact_id>")
@require_server
@with_server_lock
def delete_export(server_id, artifact_id, server):
    data = request.get_json(silent=True)
    try:
        shares.remove_artifact(server, artifact_id, store=exports,
                               confirmed=isinstance(data, dict) and data.get("confirm_shared") is True)
        return jsonify(success=True)
    except (ValueError, OSError) as exc:
        return _failure(exc)


@world_share_bp.post("/download")
def shared_download():
    try:
        current_app.extensions["world_share_limiter"].acquire()
    except RateLimitExceeded as exc:
        response = jsonify(error="Download limit reached; wait and retry")
        response.status_code = 429
        response.headers["Retry-After"] = str(max(1, math.ceil(exc.retry_after)))
        return response

    def unavailable():
        return jsonify(error="World share unavailable or expired"), 404

    if request.content_length and request.content_length > 512:
        return unavailable()
    try:
        raw = request.stream.read(513)
        if len(raw) > 512:
            return unavailable()
        data = json.loads(raw)
    except (ValueError, UnicodeError):
        return unavailable()
    token = data.get("token") if isinstance(data, dict) else None
    try:
        match = shares.resolve(token, exports)
        if not match:
            return unavailable()
        lock = locks.try_acquire(match[0]["id"])
        if lock is None:
            return jsonify(error="World download busy; wait and retry"), 503
        try:
            match = shares.resolve(token, exports)
            if not match:
                return unavailable()
            return _download(match[0], match[1]["artifact_id"])
        finally:
            lock.release()
    except FileNotFoundError:
        return unavailable()
    except (ValueError, OSError, KeyError, TypeError):
        current_app.logger.exception("Shared world archive storage operation failed")
        return jsonify(error="World download unavailable; check storage and retry later"), 503
