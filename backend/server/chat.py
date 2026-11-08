"""Narrow player chat parsing and bounded send rates; no console backfill."""
from __future__ import annotations

from collections import deque
import json
import math
import re
import threading
import time
import unicodedata
import logging

MAX_MESSAGE = 256
MAX_NAME = 48
USER_RATE = 5
SERVER_RATE = 30
RATE_SECONDS = 60
MAX_ACTORS = 2048
LOADERS = frozenset({
    "vanilla", "fabric", "quilt", "forge", "neoforge",
    "paper", "purpur", "pufferfish", "folia", "spigot", "bukkit", "craftbukkit",
})
_VERSION = re.compile(r"^(1|2[6-9])\.(\d+)(?:\.(\d+))?$")
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_PREFIX = (
    r"^(?:\[\d{2}:\d{2}:\d{2} INFO\]"
    r"|\[\d{2}:\d{2}:\d{2}\] \[(?:Server thread|Async Chat Thread - #\d+)/INFO\]"
    r"(?: \[(?:minecraft/MinecraftServer|net\.minecraft\.server\.MinecraftServer)\])?"
    r"): "
)
_PLAYER_CHAT = re.compile(
    _PREFIX + r"(?:\[Not Secure\] )?<([A-Za-z0-9_. ]{1,32})> (.+)$"
)
logger = logging.getLogger(__name__)


def supported(loader, version):
    match = _VERSION.fullmatch(str(version or ""))
    if str(loader or "").lower() not in LOADERS or not match:
        return False
    major, minor, patch = (int(value or 0) for value in match.groups())
    # The fixed minecraft: namespace requires the Brigadier command registry.
    return major >= 26 or (major, minor, patch) >= (1, 13, 0)


def validate_message(value):
    if (
        not isinstance(value, str) or not value.strip() or len(value) > MAX_MESSAGE
        or any(unicodedata.category(c).startswith("C") or c in "\u2028\u2029\u00a7" for c in value)
    ):
        raise ValueError("Message must be 1-256 characters, without controls, newlines or formatting codes")
    return value.strip()


def display_name(value):
    """Keep claim text literal, single-line and unable to forge our label."""
    clean = "".join(
        c for c in str(value)
        if not unicodedata.category(c).startswith("C") and c not in "\u2028\u2029\u00a7"
    ).replace("[", "(").replace("]", ")").strip()
    return clean[:MAX_NAME] or "OIDC user"


def command_for(name, message):
    # Only the text field is user-controlled. No selector, click event, NBT,
    # translation or command fragment can be supplied by the caller.
    component = {"text": f"[Fabricator \u00b7 {display_name(name)}] {validate_message(message)}"}
    return "minecraft:tellraw @a " + json.dumps(component, ensure_ascii=False, separators=(",", ":"))


def parse_player_chat(line):
    if len(line) > 2048:
        return None
    clean = _ANSI.sub("", line.rstrip("\r\n"))
    match = _PLAYER_CHAT.fullmatch(clean)
    if not match:
        return None
    try:
        message = validate_message(match.group(2))
    except ValueError:
        return None
    return {"kind": "player", "sender": match.group(1), "message": message}


class ChatState:
    def __init__(self):
        self._lock = threading.Lock()
        self._server_sends = deque()
        self._actors = {}
        self.capture_errors = 0

    def clear(self):
        # Rates survive server restarts; current-run capture diagnostics reset.
        with self._lock:
            self.capture_errors = 0

    def capture(self, line, loader, version, server_id, source_id):
        if server_id and supported(loader, version):
            item = parse_player_chat(line)
            if item:
                from backend.server import chat_storage
                try:
                    chat_storage.append(server_id, item, source_id=source_id)
                except chat_storage.ChatStorageError:
                    with self._lock:
                        self.capture_errors += 1
                        if self.capture_errors == 1:
                            logger.error("Player chat persistence failed; current-run history is incomplete")

    def reserve(self, actor, now=None):
        """Return a Retry-After delay, or zero for a reserved send attempt."""
        now = time.monotonic() if now is None else now
        with self._lock:
            for key, sends in list(self._actors.items()):
                while sends and now - sends[0] >= RATE_SECONDS:
                    sends.popleft()
                if not sends:
                    del self._actors[key]
            while self._server_sends and now - self._server_sends[0] >= RATE_SECONDS:
                self._server_sends.popleft()
            sends = self._actors.get(actor, deque())
            waits = []
            if len(sends) >= USER_RATE:
                waits.append(sends[0] + RATE_SECONDS - now)
            if len(self._server_sends) >= SERVER_RATE:
                waits.append(self._server_sends[0] + RATE_SECONDS - now)
            if actor not in self._actors and len(self._actors) >= MAX_ACTORS:
                waits.append(RATE_SECONDS)
            if waits:
                return max(1, math.ceil(max(waits)))
            sends.append(now)
            self._actors[actor] = sends
            self._server_sends.append(now)
            return 0
