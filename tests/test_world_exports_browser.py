"""Optional real-browser native streaming acceptance using only fixture worlds."""
import hashlib
import os
from pathlib import Path
import threading

import pytest
from werkzeug.serving import make_server

from tests.test_transfers import fixture_servers
from tests.test_world_exports import export, share


def test_native_cookie_free_stream_and_review_ui(fixture_servers, client, tmp_path):
    playwright = pytest.importorskip("playwright.sync_api")
    if not Path("frontend/dist/index.html").exists():
        pytest.skip("Build the frontend before browser acceptance")
    _, _, _, _, root, _, _, _ = fixture_servers
    with (root / "island" / "region" / "large.mca").open("wb") as handle:
        for _ in range(18):
            handle.write(os.urandom(1024 * 1024))
    app = client.application
    captured = []
    from flask import request
    @app.before_request
    def capture():
        if request.path == "/api/world-shares/download":
            captured.append({"cookie": request.headers.get("Cookie"),
                             "authorization": request.headers.get("Authorization")})
    metadata = export(client)
    link = share(client, metadata)
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_port}"
    output = Path(os.environ.get("FABRICATOR_BROWSER_ARTIFACTS", tmp_path))
    output.mkdir(parents=True, exist_ok=True)
    try:
        with playwright.sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            admin = browser.new_context(viewport={"width": 1280, "height": 960})
            dashboard = admin.new_page()
            dashboard.goto(origin + "/server/source/backups")
            dashboard.get_by_role("button", name="Open world downloads", exact=True).click()
            dashboard.get_by_role("button", name="Review & share", exact=True).click()
            assert dashboard.get_by_role("button", name="Create anonymous download link").is_disabled()
            dashboard.get_by_text("I reviewed this archive", exact=False).check()
            assert dashboard.get_by_role("button", name="Create anonymous download link").is_enabled()
            dashboard.get_by_role("button", name="Create anonymous download link").scroll_into_view_if_needed()
            dashboard.screenshot(path=str(output / "world-downloads-desktop.png"), full_page=True, animations="disabled")
            dashboard.set_viewport_size({"width": 390, "height": 844})
            dashboard.get_by_role("button", name="Create anonymous download link").scroll_into_view_if_needed()
            dashboard.screenshot(path=str(output / "world-downloads-mobile.png"), full_page=True, animations="disabled")
            assert dashboard.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            admin.close()
            app.config.update(FABRICATOR_AUTH_ENABLED=True, FABRICATOR_NEEDS_SETUP=False)
            recipient = browser.new_context(accept_downloads=True, viewport={"width": 1280, "height": 900})
            # Deliberately present ambient cookies; redemption must omit them.
            recipient.add_cookies([{"name": "ambient-canary", "value": "never-send", "url": origin}])
            page = recipient.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(origin + link["url"])
            page.get_by_role("button", name="Download world archive").wait_for()
            assert page.url == origin + "/world-share"
            page.screenshot(path=str(output / "world-share-desktop.png"), full_page=True, animations="disabled")
            with page.expect_download(timeout=30000) as download:
                page.get_by_role("button", name="Download world archive").click()
            downloaded = download.value
            assert downloaded.failure() is None
            path = Path(downloaded.path())
            with path.open("rb") as handle:
                digest = hashlib.file_digest(handle, "sha256").hexdigest()
            assert digest == metadata["sha256"]
            assert path.stat().st_size == metadata["size"] > 16 * 1024 * 1024
            assert captured == [{"cookie": None, "authorization": None}]
            page.get_by_text("Download started.", exact=False).wait_for()
            assert not errors
            from backend.server import world_exports as exports
            from backend.modrinth import client_pack_shares as shares
            shares.revoke({"id": "source"}, link["id"], None, exports)
            page.get_by_role("button", name="Download world archive").click()
            page.get_by_role("alert").filter(has_text="unavailable, expired or revoked").wait_for()
            page.set_viewport_size({"width": 390, "height": 844})
            page.screenshot(path=str(output / "world-share-mobile.png"), full_page=True, animations="disabled")
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            recipient.close()
            browser.close()
    finally:
        server.shutdown()
        thread.join(timeout=5)
