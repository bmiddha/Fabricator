"""Authenticated, server-scoped client pack editor and downloads."""
import uuid
import json
import math

from flask import Blueprint, current_app, jsonify, request, send_file

from backend.modrinth import client_pack, client_pack_shares as shares, mrpack
from backend.auth.permissions import principal
from backend.modrinth.ratelimit import RateLimiter, RateLimitExceeded
from backend.server import locks
from backend.modrinth.client import ModrinthApiError
from backend.modrinth.routes import modrinth_client, _modrinth_error_response, _registry
from backend.utils.routes import require_server, with_server_lock
from backend.utils.upload import UploadTooLargeError, stream_upload_to_temp

client_pack_bp = Blueprint("client_pack", __name__, url_prefix="/api/servers")
client_pack_share_bp = Blueprint("client_pack_share", __name__, url_prefix="/api/client-pack-shares")


@client_pack_share_bp.record_once
def _init_share_limiter(state):
    state.app.extensions["client_pack_share_limiter"] = RateLimiter(capacity=30, window_seconds=60, max_wait=0)


def _private_headers(response):
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
    return response


client_pack_bp.after_request(_private_headers)
client_pack_share_bp.after_request(_private_headers)


def _owner():
    user = principal()
    return user.owner if user else None


def _failure(exc, status=400):
    if isinstance(exc, ModrinthApiError):
        return _modrinth_error_response(exc)
    if isinstance(exc, OSError):
        current_app.logger.exception("Client pack storage operation failed")
        return jsonify(error="Could not read or persist client pack data; check server storage and retry."), 500
    return jsonify(error=str(exc)), status


@client_pack_bp.get("/<server_id>/client-pack")
@require_server
@with_server_lock
def read_client_pack(server_id, server):
    try:
        value = client_pack.plan(server, _registry().resolve_install_path(server), modrinth_client)
        return jsonify(client_pack.public_plan(value))
    except (ValueError, OSError, ModrinthApiError) as exc:
        return _failure(exc)


@client_pack_bp.put("/<server_id>/client-pack")
@require_server
@with_server_lock
def update_client_pack(server_id, server):
    try:
        return jsonify(config=client_pack.save_configuration(server, request.get_json(silent=True)))
    except (ValueError, OSError) as exc:
        return _failure(exc, 409 if "edited elsewhere" in str(exc) else 400)


@client_pack_bp.post("/<server_id>/client-pack/import")
@require_server
@with_server_lock
def import_client_metadata(server_id, server):
    """Raw .mrpack body; never calls an installer or downloads a jar."""
    path = mrpack.staging_dir() / f"mrpack-client-{uuid.uuid4().hex}.mrpack"
    try:
        limit = min(mrpack.max_upload_bytes(), 64 * 1024 * 1024)
        if request.content_length and request.content_length > limit:
            return jsonify(error="Client metadata upload exceeds 64 MiB"), 413
        stream_upload_to_temp(request.stream, path, max_bytes=limit)
        index = mrpack.read_index(path)
        mismatch = mrpack.compare_with_server(mrpack.describe(index), server)
        if mismatch:
            raise client_pack.PackError("Metadata targets a different game/loader: " + "; ".join(mismatch["reasons"]))
        install_path = _registry().resolve_install_path(server)
        # Reimport cannot know what the old installer skipped. Shared entries
        # absent from mods/ must be reviewed rather than inferred as installed.
        client_pack.capture_source(path, install_path)
        return jsonify(success=True)
    except UploadTooLargeError as exc:
        return jsonify(error=str(exc)), 413
    except (ValueError, OSError, mrpack.InvalidMrpackError) as exc:
        return _failure(exc)
    finally:
        path.unlink(missing_ok=True)


@client_pack_bp.get("/<server_id>/client-pack/exports")
@require_server
@with_server_lock
def list_client_exports(server_id, server):
    try:
        return jsonify(exports=client_pack.artifacts(server))
    except (ValueError, OSError) as exc:
        return _failure(exc)


@client_pack_bp.post("/<server_id>/client-pack/exports")
@require_server
@with_server_lock
def export_client_pack(server_id, server):
    try:
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise client_pack.PackError("Reviewed fingerprint is required")
        artifact = client_pack.build(
            server, _registry().resolve_install_path(server),
            modrinth_client, data.get("fingerprint"),
        )
        return jsonify(artifact), 201
    except (ValueError, OSError, ModrinthApiError) as exc:
        return _failure(exc, 409)


@client_pack_bp.get("/<server_id>/client-pack/exports/<artifact_id>/download")
@require_server
@with_server_lock
def download_client_pack(server_id, artifact_id, server):
    try:
        path, metadata = client_pack.artifact_path(server, artifact_id)
        # Open before releasing the lock; cleanup cannot change these bytes.
        handle = path.open("rb")
        response = send_file(
            handle, as_attachment=True,
            download_name=metadata["filename"],
            mimetype="application/x-modrinth-modpack+zip",
            max_age=0,
        )
        response.headers["Cache-Control"] = "private, no-store"
        response.call_on_close(handle.close)
        return response
    except FileNotFoundError as exc:
        return jsonify(error=str(exc)), 404
    except (ValueError, OSError) as exc:
        return _failure(exc)


@client_pack_bp.get("/<server_id>/client-pack/shares")
@require_server
@with_server_lock
def list_client_shares(server_id, server):
    try:
        return jsonify(shares=shares.listing(server, _owner()))
    except (ValueError, OSError) as exc:
        return _failure(exc)


@client_pack_bp.post("/<server_id>/client-pack/exports/<artifact_id>/shares")
@require_server
@with_server_lock
def create_client_share(server_id, artifact_id, server):
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(error="JSON expiry_seconds is required"), 400
    try:
        return jsonify(shares.create(server, artifact_id, data.get("expiry_seconds"), _owner())), 201
    except FileNotFoundError:
        return jsonify(error="Archive expired or not found; generate a new archive"), 404
    except (ValueError, OSError) as exc:
        return _failure(exc)


@client_pack_bp.delete("/<server_id>/client-pack/shares/<share_id>")
@require_server
@with_server_lock
def revoke_client_share(server_id, share_id, server):
    try:
        shares.revoke(server, share_id, _owner())
        return jsonify(success=True)
    except FileNotFoundError as exc:
        return jsonify(error=str(exc)), 404
    except (ValueError, OSError) as exc:
        return _failure(exc)


@client_pack_bp.delete("/<server_id>/client-pack/exports/<artifact_id>")
@require_server
@with_server_lock
def delete_client_export(server_id, artifact_id, server):
    data = request.get_json(silent=True)
    try:
        shares.remove_artifact(server, artifact_id, confirmed=isinstance(data, dict) and data.get("confirm_shared") is True)
        return jsonify(success=True)
    except FileNotFoundError:
        return jsonify(error="Archive expired or not found"), 404
    except (ValueError, OSError) as exc:
        return _failure(exc, 409)


@client_pack_share_bp.post("/download")
def download_shared_client_pack():
    """Token is POSTed from a URL fragment, never put in HTTP URLs or logs."""
    try:
        current_app.extensions["client_pack_share_limiter"].acquire()
    except RateLimitExceeded as exc:
        response = jsonify(error="Download limit reached; wait and retry")
        response.status_code = 429
        response.headers["Retry-After"] = str(max(1, math.ceil(exc.retry_after)))
        return response
    def unavailable():
        return jsonify(error="Share link unavailable or expired"), 404
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
        match = shares.resolve(token)
        if not match:
            return unavailable()
        server, _ = match
        lock = locks.try_acquire(server["id"])
        if lock is None:
            return jsonify(error="Download busy; wait and retry"), 503
        try:
            # Revocation/expiry/server deletion may have happened before lock.
            match = shares.resolve(token)
            if not match:
                return unavailable()
            path, metadata = client_pack.artifact_path(match[0], match[1]["artifact_id"])
            handle = path.open("rb")
            try:
                response = send_file(handle, as_attachment=True, download_name=metadata["filename"],
                                     mimetype="application/x-modrinth-modpack+zip", max_age=0)
            except BaseException:
                handle.close()
                raise
            response.call_on_close(handle.close)
            return response
        finally:
            lock.release()
    except FileNotFoundError:
        return unavailable()
    except (ValueError, OSError, KeyError, TypeError):
        current_app.logger.exception("Shared client pack storage operation failed")
        return jsonify(error="Shared download unavailable; retry later"), 503
