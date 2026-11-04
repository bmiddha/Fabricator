from copy import deepcopy

import pytest

from backend.dns import models, service, storage
from backend.dns.client import ProviderError


@pytest.fixture
def provider(monkeypatch, tmp_servers_root):
    class Fake:
        zones = {}
        fail = False

        def __init__(self, url):
            self.url = url
            self.live = self.zones.setdefault(url, {})

        def negotiate(self, domain):
            return {"include": ["example.com"]}

        def adjust(self, records):
            return deepcopy(records)

        def records(self):
            return deepcopy(list(self.live.values()))

        def apply(self, changes):
            if self.fail:
                raise ProviderError("Provider unavailable")
            for record in changes["Delete"] + changes["UpdateOld"]:
                self.live.pop(service.key(record), None)
            for record in changes["Create"] + changes["UpdateNew"]:
                self.live[service.key(record)] = deepcopy(record)

    monkeypatch.setattr(service, "Webhook", Fake)
    monkeypatch.setattr(service.servers, "get_all_servers", lambda: [])
    with storage.transaction() as data:
        data["config"] = models.config({"enabled": True, "url": "http://dns:8888", "zone": "example.com"})
    return Fake


def address(host="game.example.com", ip="192.0.2.1"):
    return {"dnsName": host, "recordType": "A", "recordTTL": 300, "targets": [ip]}


@pytest.mark.parametrize("value", ["notexample.com", "example.com.attacker.test", "game..example.com"])
def test_domain_boundary(value):
    with pytest.raises(ValueError):
        models.record(address(value), "example.com")


@pytest.mark.parametrize("kind,target", [
    ("A", "::1"), ("AAAA", "192.0.2.1"), ("SRV", "0 0 65536 example.com"),
    ("SRV", "0 -1 25565 example.com"), ("MX", "mail.example.com"),
    ("CAA", '256 issue "example.com"'), ("NS", "bad/name"),
])
def test_invalid_values(kind, target):
    with pytest.raises(ValueError):
        models.record({"dnsName": "x.example.com", "recordType": kind, "targets": [target]}, "example.com")


def test_cname_conflict_and_srv_alias():
    alias = {"dnsName": "x.example.com", "recordType": "CNAME", "targets": ["target.example.net"]}
    with pytest.raises(ValueError, match="coexist"):
        models.records([alias, address("x.example.com")], "example.com")
    with pytest.raises(ValueError, match="SRV targets"):
        models.records([alias, {"dnsName": "_minecraft._tcp.x.example.com", "recordType": "SRV", "targets": ["0 0 25565 x.example.com"]}], "example.com")


def test_sync_idempotent_and_cleanup(provider):
    with storage.transaction() as data:
        data["records"] = [address()]
    assert service.sync()["state"] == "applied"
    live = provider("http://dns:8888").live
    assert len(live) == 2
    assert service.preview()["groups"][0]["changes"] == {"Create": [], "Delete": [], "UpdateOld": [], "UpdateNew": []}
    live[("unrelated.example.com", "A")] = address("unrelated.example.com")
    with storage.transaction() as data:
        data["records"] = []
    assert service.sync()["state"] == "applied"
    assert list(live) == [("unrelated.example.com", "A")]
    assert storage.load()["applied"] == []


def test_refuse_adopting_unmanaged_record(provider):
    provider("http://dns:8888").live[service.key(address())] = address()
    with storage.transaction() as data:
        data["records"] = [address()]
    assert service.sync()["state"] == "failed"
    assert "unmanaged" in storage.load()["status"]["error"]


def test_refuse_overwriting_other_owner(provider):
    with storage.transaction() as data:
        data["records"] = [address()]
    marker = service.marker(address(), "different-installation")
    provider("http://dns:8888").live[service.key(marker)] = marker
    assert "Ownership conflict" in service.sync()["error"]


def test_durable_failed_cleanup_and_recovery(provider):
    with storage.transaction() as data:
        data["records"] = [address()]
    service.sync()
    with storage.transaction() as data:
        data["records"] = []
    provider.fail = True
    assert service.sync()["state"] == "failed"
    assert storage.load()["applied"][0]["records"]
    provider.fail = False
    assert service.sync()["state"] == "applied"
    assert not provider("http://dns:8888").live


def test_recover_lost_response(provider, monkeypatch):
    with storage.transaction() as data:
        data["records"] = [address()]
    original = provider.apply

    def apply_then_lose(self, changes):
        original(self, changes)
        raise ProviderError("Response lost")

    monkeypatch.setattr(provider, "apply", apply_then_lose)
    assert service.sync()["state"] == "failed"
    monkeypatch.setattr(provider, "apply", original)
    assert service.sync()["state"] == "applied"
    assert len(provider("http://dns:8888").live) == 2


def test_provider_migration_cleans_old(provider):
    with storage.transaction() as data:
        data["records"] = [address()]
    service.sync()
    with storage.transaction() as data:
        data["config"]["url"] = "http://new-dns:8888"
    assert service.sync()["state"] == "applied"
    assert not provider("http://dns:8888").live
    assert len(provider("http://new-dns:8888").live) == 2


def test_bindings_refresh_port_and_deleted_server(provider, monkeypatch):
    current = [{"id": "test", "port": 25566, "status": "stopped"}]
    monkeypatch.setattr(service.servers, "get_all_servers", lambda: current)
    with storage.transaction() as data:
        data["bindings"]["test"] = models.binding({
            "enabled": True, "hostname": "game.example.com",
            "addresses": ["192.0.2.1", "2001:db8::1"],
        }, "example.com")
    assert service.sync()["state"] == "applied"
    live = provider("http://dns:8888").live
    assert live[("_minecraft._tcp.game.example.com", "SRV")]["targets"] == ["0 0 25566 game.example.com."]
    assert len(live) == 6
    current[0]["port"] = 25567
    service.sync()
    assert "25567" in live[("_minecraft._tcp.game.example.com", "SRV")]["targets"][0]
    current.clear()
    service.sync()
    assert not live


def test_status_persists_without_provider_success(client, provider):
    assert client.put("/api/dns/records", json=[address()]).status_code == 200
    provider.fail = True
    response = client.post("/api/dns/sync")
    assert response.status_code == 502
    assert response.json["state"] == "failed"
    assert client.get("/api/dns").json["records"] == [address()]


def test_config_invalid_url_and_unknown_fields(client):
    for url in ["file:///tmp/dns", "https://user:secret@example.com", "https://example.com?token=secret"]:
        response = client.put("/api/dns/config", json={"url": url, "zone": "example.com"})
        assert response.status_code == 400
    assert client.put("/api/dns/config", json=[]).status_code == 400


def test_config_domain_change_requires_empty_bindings(client, provider):
    client.put("/api/dns/records", json=[address()])
    response = client.put("/api/dns/config", json={"url": "http://dns", "zone": "other.test"})
    assert response.status_code == 400


def test_owner_is_stable_and_corrupt_state_is_not_overwritten(client):
    first = client.get("/api/dns").json["owner"]
    assert client.get("/api/dns").json["owner"] == first
    storage.path().write_text("{broken")
    with pytest.raises(ValueError):
        storage.load()
    assert storage.path().read_text() == "{broken"


def test_external_edits_not_deleted(provider):
    with storage.transaction() as data:
        data["records"] = [address()]
    service.sync()
    provider("http://dns:8888").live[service.key(address())] = address(ip="192.0.2.99")
    with storage.transaction() as data:
        data["records"] = []
    assert "changed outside" in service.sync()["error"]
    assert provider("http://dns:8888").live[service.key(address())]["targets"] == ["192.0.2.99"]


def test_existing_cname_conflict_before_writes(provider):
    alias = {"dnsName": "game.example.com", "recordType": "CNAME", "recordTTL": 300, "targets": ["other.example.net."]}
    provider("http://dns:8888").live[service.key(alias)] = alias
    with storage.transaction() as data:
        data["records"] = [address()]
    assert "CNAME/data conflict" in service.sync()["error"]
    assert len(provider("http://dns:8888").live) == 1


def test_dns_admin_requires_session(auth_client):
    assert auth_client.get("/api/dns").status_code == 401
    assert auth_client.post("/api/dns/sync").status_code == 401


def test_managed_dns_rejected(client, monkeypatch):
    monkeypatch.setenv("FABRICATOR_MANAGED", "1")
    assert client.get("/api/dns").status_code == 403


def test_oversize_and_invalid_json_bodies(client, provider):
    response = client.put("/api/dns/records", data=b" " * (2 * 1024 * 1024 + 1), content_type="application/json")
    assert response.status_code == 413
    assert client.put("/api/dns/records", data="{broken", content_type="application/json").status_code == 400
    assert client.put("/api/dns/config", data="not json").status_code == 400


def test_default_dns_does_not_create_state_or_touch_provider(tmp_servers_root, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Disabled DNS must not connect")
    monkeypatch.setattr(service, "Webhook", forbidden)
    assert service.sync()["state"] == "disabled"
    assert not storage.path().exists()


def test_unconfirmed_provider_write_not_reported_as_success(provider, monkeypatch):
    monkeypatch.setattr(provider, "apply", lambda self, changes: None)
    with storage.transaction() as data:
        data["records"] = [address()]
    assert service.sync()["state"] == "failed"
    assert "did not converge" in storage.load()["status"]["error"]
    assert storage.load()["applied"][0]["pending"] == [address()]


def test_scheduler_is_bounded_and_can_wake_early(monkeypatch):
    from types import SimpleNamespace
    from backend.backups import scheduler as module
    calls = {}
    fake = SimpleNamespace(
        running=True,
        add_job=lambda **kw: calls.update(kw),
        get_job=lambda job: job == "fabricator-dns",
        modify_job=lambda job, **kw: calls.update(wakeup=job, **kw),
    )
    monkeypatch.setattr(module, "scheduler", fake)
    monkeypatch.delenv("FABRICATOR_DISABLE_SCHEDULER", raising=False)
    service.init_dns()
    assert calls["seconds"] == 60
    assert calls["max_instances"] == 1
    assert calls["coalesce"] is True
    service.request_sync()
    assert calls["wakeup"] == "fabricator-dns"
    calls.clear()
    monkeypatch.setenv("FABRICATOR_DISABLE_SCHEDULER", "1")
    service.init_dns()
    assert calls == {}
