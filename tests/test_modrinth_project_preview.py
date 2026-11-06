"""Project preview reuses Modrinth's body, never a claimed README URL."""
import pytest


@pytest.mark.parametrize("route", ["project", "mod"])
def test_details_preserve_body_and_do_not_fetch_readme(client, monkeypatch, route):
    from backend.modrinth import routes
    payload = {
        "id": "known", "description": "Short", "body": "# Body\n\n<script>untrusted</script>",
        "source_url": "https://example.com/repo", "readme_url": "http://localhost/private",
    }
    calls = []
    def project(project_id):
        calls.append(project_id)
        return payload
    monkeypatch.setattr(routes.modrinth_client, "get_project", project)
    response = client.get(f"/api/modrinth/{route}/known")
    assert response.status_code == 200
    assert response.json == payload
    assert calls == ["known"]


@pytest.mark.parametrize("status", [404, 429, 502])
def test_preview_preserves_upstream_errors_for_retry(client, monkeypatch, status):
    from backend.modrinth import routes
    from backend.modrinth.client import ModrinthApiError
    def fail(_):
        raise ModrinthApiError("Unavailable", status_code=status, details={"retry_after": 3} if status == 429 else {})
    monkeypatch.setattr(routes.modrinth_client, "get_project", fail)
    response = client.get("/api/modrinth/project/known")
    assert response.status_code == status
    assert response.json["error"] == "Unavailable"
    if status == 429:
        assert response.json["retry_after"] == 3
        assert response.headers["Retry-After"] == "3"
