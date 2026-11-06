"""Authenticated, server-scoped client pack editor and downloads."""
import uuid

from flask import Blueprint, current_app, jsonify, request, send_file

from backend.modrinth import client_pack, mrpack
from backend.modrinth.client import ModrinthApiError
from backend.modrinth.routes import modrinth_client, _modrinth_error_response, _registry
from backend.utils.routes import require_server, with_server_lock
from backend.utils.upload import UploadTooLargeError, stream_upload_to_temp

client_pack_bp = Blueprint("client_pack", __name__, url_prefix="/api/servers")


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
