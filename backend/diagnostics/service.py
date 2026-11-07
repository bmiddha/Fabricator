"""Local sampling and conservative Paper spark orchestration."""
import logging
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
import uuid

from backend.diagnostics import storage
from backend.utils import platform

logger = logging.getLogger(__name__)
SAMPLE_SECONDS = 30
_thread = None
_init_lock = threading.Lock()
_captures_lock = threading.RLock()
_active = {}
_worker_error = None
_REPORT_RE = re.compile(r"https://spark\.lucko\.me/[A-Za-z0-9]{5,100}")


def get_server_process_registry():
    from backend.server.registry import get_server_process_registry as get_registry
    return get_registry()


class DiagnosticError(ValueError):
    pass


def capability(server):
    # Only released Paper versions with documented bundled spark support.
    version = re.fullmatch(r"1\.(\d+)(?:\.\d+)?", str(server.get("version", "")))
    bundled = server.get("loader") == "paper" and bool(version and int(version[1]) >= 21)
    return {
        "supported": bundled,
        "reason": "paper-bundled" if bundled else "unverified-runtime",
        "docs": "https://docs.papermc.io/paper/profiling/",
    }


def parse_jstat(output):
    lines = output.strip().splitlines()
    if len(lines) != 2:
        return {}
    headers, readings = (line.split() for line in lines)
    if len(headers) != len(readings) or len(set(headers)) != len(headers):
        return {}
    values = dict(zip(headers, readings))

    def number(key):
        try:
            value = float(values[key])
            return value if math.isfinite(value) and value >= 0 else None
        except (KeyError, ValueError):
            return None

    result = {}
    heap = [number(key) for key in ("S0U", "S1U", "EU", "OU")]
    if all(value is not None for value in heap):
        result["heapUsedBytes"] = round(sum(heap) * 1024)
    gc_time = number("GCT")
    if gc_time is not None:
        result["gcTimeMs"] = gc_time * 1000
    counters = ("YGC", "FGC", "CGC") if "CGC" in values else ("YGC", "FGC")
    counts = [number(key) for key in counters]
    if all(value is not None and value.is_integer() for value in counts):
        result["gcCollections"] = int(sum(counts))
    return result


def jvm_sample(manager):
    java = manager._java_executable()
    resolved = java if os.path.isabs(java) else shutil.which(java)
    if not resolved:
        return {}, "jstat-unavailable"
    jstat = Path(os.path.realpath(resolved)).with_name("jstat.exe" if platform.is_windows() else "jstat")
    if not jstat.is_file():
        return {}, "jstat-unavailable"
    process = manager._process
    if not process or not manager.is_running:
        return {}, "stopped"
    try:
        result = subprocess.run(
            [str(jstat), "-gc", str(process.pid)], capture_output=True, text=True,
            timeout=2, check=True, env={**os.environ, "LC_ALL": "C"},
            **platform.subprocess_no_window_kwargs(),
        )
    except (OSError, subprocess.SubprocessError):
        logger.warning("JVM diagnostics probe unavailable for process %s", process.pid)
        return {}, "jstat-probe-failed"
    if manager._process is not process or not manager.is_running:
        return {}, "process-changed"
    values = parse_jstat(result.stdout)
    return values, None if values else "jstat-format-unsupported"


def sample(server, registry=None, now=None):
    registry = registry or get_server_process_registry()
    manager = registry.get_manager(server["id"])
    now = time.time() if now is None else now
    result = {"ts": int(now), "running": bool(manager and manager.is_running)}
    if result["running"]:
        process = manager._process
        status = manager.status()
        for key in ("tps", "mspt", "cpu"):
            value = status.get(key)
            if isinstance(value, (int, float)) and math.isfinite(value):
                result[key] = value
        rss = status.get("ram", {}).get("rssBytes")
        if rss is not None:
            result["rssBytes"] = rss
        result["run"] = manager.diagnostics_run_id
        if server.get("diagnostics", {}).get("jvmSampling") is True:
            values, error = jvm_sample(manager)
            result.update(values)
            if error:
                result["jvmUnavailable"] = error
        else:
            result["jvmUnavailable"] = "sampling-disabled"
        if manager._process is not process or not manager.is_running:
            result = {"ts": int(now), "running": manager.is_running,
                      "jvmUnavailable": "process-changed"}
    # Shared filesystem capacity; avoid world scans during lag.
    try:
        path = registry.resolve_install_path(server)
        disk = shutil.disk_usage(path)
        result.update(volumeUsedBytes=disk.used, volumeTotalBytes=disk.total)
    except (OSError, ValueError):
        result["diskUnavailable"] = "volume-unavailable"
    return result


def sample_all():
    from backend.server import storage as servers
    records = servers.load_servers()
    storage.cleanup({server["id"] for server in records})
    failures = 0
    for server in records:
        try:
            storage.append(server["id"], sample(server))
        except Exception:
            logger.exception("Could not retain diagnostics for server %s", server["id"])
            failures += 1
    if failures:
        raise RuntimeError(f"Could not retain diagnostics for {failures} server(s)")


def _sampler():
    global _worker_error
    while True:
        try:
            sample_all()
            _worker_error = None
        except Exception:
            _worker_error = "Performance sampling failed; check Fabricator logs."
            logger.exception("Performance sampling failed")
        time.sleep(SAMPLE_SECONDS)


def init(app):
    from backend.server import storage as servers
    global _thread
    if os.environ.get("FABRICATOR_DISABLE_DIAGNOSTICS") == "1" or app.testing:
        return
    with _init_lock:
        if _thread and _thread.is_alive():
            return
        for server in servers.load_servers():
            capture_history(server["id"])
        _thread = threading.Thread(target=_sampler, daemon=True, name="performance-history")
        _thread.start()


def overview(server, seconds):
    manager = get_server_process_registry().get_manager(server["id"])
    ready = bool(manager and manager.is_running and manager._ready_event.is_set()
                 and manager.loader == server.get("loader") and manager.version == server.get("version"))
    return {
        "samples": storage.history(server["id"], seconds),
        "captures": capture_history(server["id"]),
        "capability": capability(server),
        "captureReady": ready,
        "jvmSampling": server.get("diagnostics", {}).get("jvmSampling") is True,
        "intervalSeconds": SAMPLE_SECONDS, "retentionSeconds": storage.RETENTION_SECONDS,
        "samplingEnabled": os.environ.get("FABRICATOR_DISABLE_DIAGNOSTICS") != "1",
        "samplingError": _worker_error,
    }


def capture_history(server_id):
    with _captures_lock:
        captures = storage.captures(server_id)
        for capture in captures:
            if capture["status"] in {"requested", "running"} and _active.get(server_id) != capture["id"]:
                capture.update(status="interrupted", message="Tracking ended. spark may still finish; check the server console.")
                storage.save_capture(server_id, capture)
        return captures


def console_body(manager, line):
    from backend.server.tps import strip_formatting
    clean = strip_formatting(manager._ANSI_RE.sub("", line))
    prefix = manager._LOG_LINE_RE.match(clean)
    if not prefix:
        return None
    body = clean[prefix.end():]
    if body.startswith(("<", "* ", "[Not Secure]")) or "System chat:" in body:
        return None
    return body


def apply_capture_line(capture, body):
    """Only fixed status text and a strict official viewer URL leave the console."""
    lowered = body.lower()
    spark_message = lowered.startswith(("[spark]", "[\u26a1]", "profiler ", "the profiler ", "the active profiler "))
    unknown_command = capture["status"] == "requested" and lowered.startswith(("unknown command", "unknown or incomplete command"))
    if unknown_command or (spark_message and any(text in lowered for text in (
        "unknown command", "unknown or incomplete command", "already running",
        "profiler is not running", "profiler isn't running", "profiler is not active",
        "failed to", "not enabled", "cancelled",
    ))):
        capture.update(status="failed", message="spark rejected the capture. Check the server console.")
    elif spark_message and "profiler is now running!" in lowered:
        capture.update(status="running", message="spark acknowledged the capture.")
    elif spark_message and "error occurred whilst uploading" in lowered:
        capture.update(status="failed", message="Upload failed. spark may have saved a local file; check the server console.")
    if capture["status"] == "running":
        match = _REPORT_RE.fullmatch(body.strip())
        if match:
            capture.update(status="ready", reportUrl=match[0],
                           message="spark report received.")


def delete_state(server_id):
    with _captures_lock:
        storage.delete(server_id)
        _active.pop(server_id, None)


def _persist_capture(server_id, capture):
    # Prevent a finishing worker from resurrecting deleted references.
    with _captures_lock:
        if _active.get(server_id) == capture["id"]:
            storage.save_capture(server_id, capture)


def _watch_capture(server_id, manager, process, capture, cursor):
    try:
        deadline = time.monotonic() + capture["durationSeconds"] + 90
        confirmed_by = time.monotonic() + 15
        while time.monotonic() < deadline:
            with _captures_lock:
                if _active.get(server_id) != capture["id"]:
                    return
            if manager._process is not process or not manager.is_running:
                capture.update(status="interrupted", message="Server process stopped or changed during capture.")
                break
            cursor, lines = manager.console_since(cursor)
            previous = dict(capture)
            for line in lines:
                body = console_body(manager, line)
                if body is not None:
                    apply_capture_line(capture, body)
                if capture["status"] in {"ready", "failed"}:
                    break
            if capture["status"] in {"ready", "failed"}:
                break
            if capture["status"] == "requested" and time.monotonic() > confirmed_by:
                # Keep the lease through the timeout to prevent duplicate captures.
                capture["message"] = "Waiting for spark acknowledgement. Check the server console."
            if capture != previous:
                _persist_capture(server_id, capture)
            time.sleep(1)
        else:
            capture.update(status="failed", message="No report received before the deadline. Check the server console before retrying.")
        _persist_capture(server_id, capture)
    except Exception:
        logger.exception("Could not track spark capture for %s", server_id)
        capture.update(status="failed", message="Tracking failed. Check Fabricator logs and the server console.")
        try:
            _persist_capture(server_id, capture)
        except Exception:
            logger.exception("Could not persist capture failure for %s", server_id)
    finally:
        with _captures_lock:
            if _active.get(server_id) == capture["id"]:
                _active.pop(server_id, None)


def start_capture(server, data):
    if not isinstance(data, dict) or data.get("acceptUpload") is not True:
        raise DiagnosticError("Consent to upload operational diagnostics to spark is required.")
    duration = data.get("durationSeconds", 60)
    if type(duration) is not int or duration not in (60, 120, 300):
        raise DiagnosticError("Capture duration must be 60, 120 or 300 seconds.")
    if not capability(server)["supported"]:
        raise DiagnosticError("Guided captures require a verified Paper 1.21+ release.")
    manager = get_server_process_registry().get_manager(server["id"])
    if not manager or not manager.is_running or not manager._ready_event.is_set():
        raise DiagnosticError("Start the server and wait until it is ready before profiling.")
    if manager.loader != server.get("loader") or manager.version != server.get("version"):
        raise DiagnosticError("Configured and running runtimes differ. Check the server console.")
    with _captures_lock:
        if server["id"] in _active:
            raise DiagnosticError("A guided capture is already in progress.")
        capture = {
            "id": uuid.uuid4().hex, "created": time.time(),
            "durationSeconds": duration, "status": "requested",
            "message": "Command sent; waiting for spark acknowledgement.",
        }
        cursor, _ = manager.console_since()
        process = manager._process
        storage.save_capture(server["id"], capture)
        result = manager.send_command(f"spark profiler start --timeout {duration}")
        if not result.get("success"):
            capture.update(status="failed", message="Could not send spark command to the server.")
            storage.save_capture(server["id"], capture)
            raise DiagnosticError(capture["message"])
        _active[server["id"]] = capture["id"]
        worker = threading.Thread(target=_watch_capture,
                                  args=(server["id"], manager, process, capture, cursor),
                                  daemon=True, name="spark-capture")
        try:
            worker.start()
        except RuntimeError:
            logger.exception("Could not start spark capture tracking for %s", server["id"])
            capture.update(status="failed", message="Capture tracking could not start. spark may still finish; check the console before retrying.")
            try:
                _persist_capture(server["id"], capture)
            finally:
                _active.pop(server["id"], None)
            raise DiagnosticError(capture["message"]) from None
        return dict(capture)
