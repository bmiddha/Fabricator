"""Real incumbent shell/navigation with fixture-only Players and Chat processes."""
import io
import os
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest
from werkzeug.serving import make_server

from tests.test_transfers import fixture_servers


def test_players_and_chat_in_existing_shell(fixture_servers, client, tmp_path, monkeypatch):
    playwright = pytest.importorskip("playwright.sync_api")
    if not Path("frontend/dist/index.html").exists():
        pytest.skip("Build the frontend before browser acceptance")
    from backend.server.manager import ServerManager
    from backend.server import chat_storage
    from backend.server.players import files, observed
    _, storage, registry, _, root, *_ = fixture_servers
    uid = "12345678-1234-4234-8234-123456789abc"
    storage.update_server("source", {"loader": "fabric", "onlineMode": True})
    files.write_json_list(root, "whitelist.json", [{"uuid": uid, "name": "FixturePlayer"}])
    observed.record(root, uid, "FixturePlayer", joined=True)
    manager = ServerManager(str(root), loader="fabric", version="1.21.1")
    manager.server_id = "source"
    manager._process = SimpleNamespace(
        stdout=io.StringIO(f"[12:00:00] [User Authenticator #1/INFO]: UUID of player FixturePlayer is {uid}\n"
                          "[12:00:01] [Server thread/INFO]: FixturePlayer joined the game\n"
                          "[12:00:02] [Server thread/INFO]: <FixturePlayer> Hello from the fixture\n"),
        stderr=io.StringIO(), stdin=io.StringIO(), poll=lambda: None,
    )
    manager._start_log_streams()
    manager._stdout_thread.join(timeout=2)
    manager._stderr_thread.join(timeout=2)
    manager._ready_event.set()
    monkeypatch.setattr(registry, "get_manager", lambda sid: manager if sid == "source" else None)
    monkeypatch.setattr(registry, "get_status", lambda sid: {
        "status": "running" if sid == "source" else "stopped", "players": {"online": 1},
    })
    app = client.application
    app.config.update(FABRICATOR_AUTH_ENABLED=True, FABRICATOR_NEEDS_SETUP=False,
                      SECRET_KEY="fixture-browser-only-session-key")
    with client.session_transaction() as state:
        state["authenticated"] = True
    cookie = client.get_cookie(app.config["SESSION_COOKIE_NAME"])
    from flask import request
    operations = []
    @app.before_request
    def capture():
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            operations.append(request.path)
    server = make_server("127.0.0.1", 0, app, threaded=True)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    origin = f"http://127.0.0.1:{server.server_port}"
    output = Path(os.environ.get("FABRICATOR_SHIPMENT_ARTIFACTS", tmp_path))
    output.mkdir(parents=True, exist_ok=True)
    try:
        with playwright.sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(viewport={"width": 1440, "height": 960})
            context.add_cookies([{"name": cookie.key, "value": cookie.value, "url": origin}])
            context.route("https://mc-heads.net/**", lambda route: route.fulfill(status=404, body=""))
            page = context.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(origin + "/server/source/overview")
            page.locator(".online-heads").wait_for()
            assert page.locator(".stat-card").count() == 4
            sidebar = page.locator("#app-sidebar")
            paths = ["/players", *[f"/server/source/{suffix}" for suffix in (
                "overview", "console", "chat", "players", "map", "mods", "files",
                "backups", "schedules", "activity", "playit", "properties", "settings")]]
            for path in paths:
                assert sidebar.locator(f'a[href="{path}"]').count() == 1, path
            assert not sidebar.locator('a[href="/clients"]').count()
            page.screenshot(path=str(output / "players-overview-desktop.png"), full_page=True)
            sidebar.locator('a[href="/players"]').click()
            page.get_by_role("heading", name="Players", exact=True).wait_for()
            page.get_by_text("FixturePlayer", exact=True).first.wait_for()
            assert not sidebar.locator(".app-sidebar__no-server-chip").count()
            page.screenshot(path=str(output / "players-directory-desktop.png"), full_page=True)
            sidebar.locator(".app-sidebar__logo").click()
            page.locator(".online-heads").wait_for()
            page.goto(origin + "/server/source/chat")
            page.get_by_text("Hello from the fixture", exact=True).wait_for()
            page.locator(".server-chat__composer input").fill("Panel fixture reply")
            page.locator(".server-chat__composer button").click()
            page.get_by_text("Panel fixture reply", exact=True).wait_for()
            assert "minecraft:tellraw @a" in manager._process.stdin.getvalue()
            assert len(chat_storage.history("source")["items"]) == 2
            page.screenshot(path=str(output / "server-chat-desktop.png"), full_page=True)
            page.set_viewport_size({"width": 390, "height": 844})
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            page.get_by_role("button", name="Open navigation menu").click()
            sidebar.locator('a[href="/server/source/players"]').click()
            page.locator(".players-page").wait_for()
            assert page.get_by_role("button", name="Open navigation menu").get_attribute("aria-expanded") == "false"
            page.goto(origin + "/server/source/chat")
            page.get_by_text("Panel fixture reply", exact=True).wait_for()
            page.screenshot(path=str(output / "server-chat-mobile.png"), full_page=True)
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            assert not errors
            assert operations == ["/api/servers/source/chat"]
            context.close()
            browser.close()
    finally:
        server.shutdown()
        worker.join(timeout=5)
