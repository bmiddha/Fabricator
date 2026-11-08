"""Owned backup objects. S3 credentials/endpoints are approved by the operator."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urlsplit
import uuid

from backend.backups import progress


class DestinationError(RuntimeError):
    """Safe, actionable transfer error suitable for API/progress responses."""


def _profiles():
    try:
        profiles = json.loads(os.environ.get("FABRICATOR_BACKUP_S3_PROFILES", "{}"))
    except ValueError:
        raise DestinationError("Invalid FABRICATOR_BACKUP_S3_PROFILES JSON") from None
    if not isinstance(profiles, dict):
        raise DestinationError("FABRICATOR_BACKUP_S3_PROFILES must be an object")
    return profiles


def normalise(value, server_id=None):
    if value is None:
        return {"type": "local"}
    if not isinstance(value, dict):
        raise ValueError("destination must be an object")
    kind = value.get("type", "local")
    if kind == "local":
        if set(value) - {"type"}:
            raise ValueError("Local destination accepts only type; use storagePath")
        return {"type": "local"}
    if kind != "s3":
        raise ValueError("destination.type must be local or s3")
    if set(value) - {"type", "profile", "bucket", "prefix", "region", "endpointUrl"}:
        raise ValueError("Unknown destination fields; credentials must stay on the backend")
    result = {"type": "s3"}
    for key in ("profile", "bucket", "prefix", "region", "endpointUrl"):
        raw = value.get(key, "")
        if not isinstance(raw, str):
            raise ValueError(f"destination.{key} must be a string")
        result[key] = raw.strip()
    result["profile"] = result["profile"] or "default"
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", result["bucket"]):
        raise ValueError("destination.bucket must be a valid S3 bucket name")
    result["prefix"] = result["prefix"].strip("/")
    if any(part in (".", "..") for part in result["prefix"].split("/")):
        raise ValueError("destination.prefix must not contain dot segments")
    if len(result["prefix"]) > 700 or any(ord(c) < 32 for c in result["prefix"]):
        raise ValueError("destination.prefix is too long or contains control characters")
    profile = _profile(result, server_id)
    endpoint = profile.get("endpointUrl", "")
    if result["endpointUrl"] and result["endpointUrl"] != endpoint:
        raise ValueError("Endpoint is not approved for this backend S3 profile")
    if endpoint:
        parsed = urlsplit(endpoint)
        if (parsed.scheme not in ("https", "http") or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("Backend S3 endpoint must be an HTTP(S) URL without credentials")
    result["endpointUrl"] = endpoint
    result["region"] = result["region"] or profile.get("region", "us-east-1")
    if not re.fullmatch(r"[a-zA-Z0-9-]{1,64}", result["region"]):
        raise ValueError("destination.region is invalid")
    return result


def _profile(destination, server_id):
    profile = _profiles().get(destination.get("profile", "default"))
    if not isinstance(profile, dict):
        raise DestinationError("S3 profile is not enabled; ask the operator to configure it")
    for key in ("awsProfile", "endpointUrl", "region"):
        if key in profile and not isinstance(profile[key], str):
            raise DestinationError("Backend S3 profile has invalid string settings")
    if profile.get("addressingStyle", "auto") not in ("auto", "path", "virtual"):
        raise DestinationError("Backend S3 addressingStyle must be auto, path or virtual")
    buckets = profile.get("buckets")
    if not isinstance(buckets, list) or destination.get("bucket") not in buckets:
        raise DestinationError("Bucket is not allowed by the backend S3 profile")
    if "serverIds" in profile:
        if not isinstance(profile["serverIds"], list):
            raise DestinationError("Backend S3 serverIds must be an array")
        if server_id is not None and server_id not in profile["serverIds"]:
            raise DestinationError("S3 profile is not allowed for this server")
    return profile


@contextmanager
def _client(destination, server_id):
    import boto3
    from botocore.config import Config
    from botocore.exceptions import BotoCoreError, ClientError
    from boto3.exceptions import S3UploadFailedError

    profile = _profile(destination, server_id)
    if destination.get("endpointUrl", "") != profile.get("endpointUrl", ""):
        raise DestinationError("Original S3 endpoint changed; restore the backend profile before accessing this snapshot")
    client = None
    try:
        session = boto3.Session(profile_name=profile.get("awsProfile") or None)
        client = session.client(
            "s3", region_name=destination.get("region") or "us-east-1",
            endpoint_url=destination.get("endpointUrl") or None,
            config=Config(
                connect_timeout=10, read_timeout=60,
                retries={"max_attempts": 3, "mode": "standard"},
                # SHA-256 read-back is mandatory; optional SDK checksum
                # extensions are not implemented consistently by S3 providers.
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
                s3={"addressing_style": profile.get("addressingStyle", "auto")},
            ),
        )
        yield client
    except (BotoCoreError, ClientError, S3UploadFailedError):
        # SDK messages can include signed URLs, credentials or provider bodies.
        raise DestinationError(
            "S3 operation failed. Check backend credentials, bucket permissions, "
            "endpoint connectivity and available storage; retry the operation."
        ) from None
    finally:
        if client is not None:
            client.close()


def is_remote(snapshot):
    return (snapshot.get("destination") or {}).get("type") == "s3"


def has_archive(snapshot):
    return bool(snapshot.get("filePath") or (
        is_remote(snapshot) and snapshot.get("objectKey")
    ))


def checksum(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _transfer(job_id, phase, total):
    transferred = 0
    if job_id:
        progress.update(job_id, phase=phase, transferred_bytes=0, total_bytes=total)

    def update(count):
        nonlocal transferred
        transferred += count
        if job_id:
            progress.update(job_id, transferred_bytes=transferred, total_bytes=total)
    return update


def _owned(snapshot):
    destination = snapshot.get("destination") or {}
    key = snapshot.get("objectKey")
    prefix = destination.get("prefix", "").strip("/")
    expected = "/".join(filter(None, (
        prefix, "fabricator", snapshot["serverId"], snapshot["id"], snapshot.get("fileName"),
    )))
    if not key or key != expected:
        raise DestinationError("Snapshot object ownership metadata is invalid; refusing S3 access")
    return destination, key


def publish(path, snapshot, cfg, job_id, *, recovery=None):
    """Upload, read back to verify SHA-256, then publish recovery metadata.

    Unique keys prevent overwrites. A crash before local indexing leaves an
    identifiable orphan, never a successful snapshot that triggers retention.
    """
    from boto3.s3.transfer import TransferConfig

    destination = normalise(cfg.get("destination"), snapshot["serverId"])
    snapshot.update(
        destination=destination, sha256=checksum(path),
        id=snapshot.get("id") or f"snp_{uuid.uuid4().hex}",
    )
    snapshot["objectKey"] = "/".join(filter(None, (
        destination["prefix"], "fabricator", snapshot["serverId"],
        snapshot["id"], snapshot["fileName"],
    )))
    _, key = _owned(snapshot)
    with _client(destination, snapshot["serverId"]) as client:
        client.upload_file(
            str(path), destination["bucket"], key,
            ExtraArgs={"Metadata": {
                "sha256": snapshot["sha256"], "fabricator-format": "2" if recovery else "1",
                "snapshot-id": snapshot["id"], "server-id": snapshot["serverId"],
                "config-id": snapshot.get("configId") or "",
                "created-at": snapshot["createdAt"],
            }},
            Config=TransferConfig(
                multipart_threshold=16 * 1024 * 1024,
                multipart_chunksize=16 * 1024 * 1024, use_threads=False,
            ),
            Callback=_transfer(job_id, "uploading", snapshot["sizeBytes"]),
        )
        head = client.head_object(Bucket=destination["bucket"], Key=key)
        if (head["ContentLength"] != snapshot["sizeBytes"]
                or head.get("Metadata", {}).get("sha256") != snapshot["sha256"]):
            raise DestinationError("S3 upload verification failed; retention was not run")
        _verify_stream(client, destination, key, snapshot, job_id)
        metadata = {
            "format": "fabricator-backup", "version": 2 if recovery else 1,
            "snapshot": {**snapshot, "filePath": None},
            "config": {"id": cfg.get("id"), "name": cfg.get("name")},
            "archiveFormat": "tar (plain or hybrid data.tar.gz/worlds.tar)",
        }
        if recovery:
            metadata["recovery"] = recovery
        if job_id:
            progress.update(job_id, phase="publishing_metadata")
        client.put_object(
            Bucket=destination["bucket"], Key=key + ".fabricator.json",
            Body=json.dumps(metadata).encode("utf-8"), ContentType="application/json",
        )
    snapshot["filePath"] = None
    return snapshot


def _verify_stream(client, destination, key, snapshot, job_id, target=None):
    update = _transfer(job_id, "downloading" if target else "verifying", snapshot["sizeBytes"])
    digest = hashlib.sha256()
    size = 0
    response = client.get_object(Bucket=destination["bucket"], Key=key)
    body = response["Body"]
    try:
        for chunk in body.iter_chunks(chunk_size=1024 * 1024):
            size += len(chunk)
            if size > snapshot["sizeBytes"]:
                raise DestinationError("S3 archive checksum mismatch; archive was not used")
            digest.update(chunk)
            if target:
                target.write(chunk)
            update(len(chunk))
    finally:
        body.close()
    if size != snapshot["sizeBytes"] or digest.hexdigest() != snapshot.get("sha256"):
        raise DestinationError("S3 archive checksum mismatch; archive was not used")


@contextmanager
def materialise(snapshot, job_id=None, *, directory=None, import_operation=False):
    """Yield a verified local archive; always clean remote temporary files."""
    if not is_remote(snapshot):
        path = Path(snapshot.get("filePath") or "")
        if not snapshot.get("filePath") or not path.is_file():
            raise FileNotFoundError("Archive file not found on disk")
        yield path
        return
    destination, key = _owned(snapshot)
    if import_operation and _profile(destination, None).get("allowImport") is not True:
        raise DestinationError("S3 profile must explicitly enable allowImport for instance recovery")
    with tempfile.TemporaryDirectory(prefix="fabricator-s3-download-", dir=directory) as temporary:
        path = Path(temporary) / "archive.tar"
        with _client(destination, None if import_operation else snapshot["serverId"]) as client, path.open("wb") as target:
            _verify_stream(client, destination, key, snapshot, job_id, target)
        yield path


def delete_archive(snapshot):
    if not is_remote(snapshot):
        if snapshot.get("filePath"):
            Path(snapshot["filePath"]).unlink(missing_ok=True)
        return
    destination, key = _owned(snapshot)
    with _client(destination, snapshot["serverId"]) as client:
        # Exact recorded keys only. Repeating either delete is safe.
        client.delete_object(Bucket=destination["bucket"], Key=key + ".fabricator.json")
        client.delete_object(Bucket=destination["bucket"], Key=key)


def location(snapshot):
    if is_remote(snapshot):
        return f"s3://{snapshot['destination']['bucket']}/{snapshot.get('objectKey', '')}"
    return snapshot.get("filePath")


def availability(snapshot, *, check_remote=False):
    if not has_archive(snapshot):
        return "none", None
    if not is_remote(snapshot):
        return ("available" if Path(snapshot["filePath"]).is_file() else "unavailable"), None
    if not check_remote:
        return "unchecked", None
    try:
        destination, key = _owned(snapshot)
        with _client(destination, snapshot["serverId"]) as client:
            head = client.head_object(Bucket=destination["bucket"], Key=key)
            if (head["ContentLength"] != snapshot["sizeBytes"]
                    or head.get("Metadata", {}).get("sha256") != snapshot.get("sha256")):
                raise DestinationError("S3 archive size or checksum metadata changed")
    except DestinationError as exc:
        return "unavailable", str(exc)
    return "available", None
