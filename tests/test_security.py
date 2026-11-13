"""The local server must not be usable by websites you visit."""
import importlib
import os
import shutil
import sys

import pytest
from fastapi.testclient import TestClient

from conftest import JDS, ROOT, BANK_FILE

EXT = "chrome-extension://" + "a" * 32
OTHER_EXT = "chrome-extension://" + "b" * 32


@pytest.fixture
def client(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    shutil.copy(BANK_FILE, data / "experiences.yaml")
    monkeypatch.setenv("BANK_PATH", str(data / "experiences.yaml"))
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path / "output"))
    monkeypatch.setenv("PORT", "8765")
    sys.modules.pop("app", None)
    app = importlib.import_module("app")
    c = TestClient(app.app, base_url="http://127.0.0.1:8765")
    c.token = app.TOKEN
    c.mod = app
    return c


def h(c, **extra):
    return {"X-RT-Token": c.token, **extra}


def test_token_file_is_private_and_random(client):
    assert len(client.token) >= 32
    assert client.mod.TOKEN_FILE.read_text().strip() == client.token
    assert ".token" in (ROOT / ".gitignore").read_text()


def test_health_is_open_but_has_no_secrets(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.text
    assert client.token not in body and "@" not in body


@pytest.mark.parametrize("host", ["evil.com", "evil.com:8765", "127.0.0.1.nip.io:8765", "attacker.localhost.evil:8765"])
def test_dns_rebinding_blocked(client, host):
    assert client.get("/api/health", headers={"Host": host}).status_code == 421


def test_cross_site_origin_blocked(client):
    r = client.post("/api/jobs", json={"jd": JDS["backend"]}, headers=h(client, Origin="https://evil.com"))
    assert r.status_code == 403
    r = client.get("/api/bank", headers={"Origin": "https://evil.com"})
    assert r.status_code == 403


def test_cross_site_navigation_blocked(client):
    # <img src> / <iframe> / form posts from another site carry Sec-Fetch-Site: cross-site
    for path in ("/api/bank", "/api/pdf/x", "/api/history"):
        assert client.get(path, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    r = client.post("/api/jobs", data="jd=x", headers={"Sec-Fetch-Site": "cross-site",
                                                        "Content-Type": "application/x-www-form-urlencoded"})
    assert r.status_code == 403


def test_token_required(client):
    assert client.get("/api/bank").status_code == 401
    assert client.get("/api/history").status_code == 401
    assert client.post("/api/jobs", json={"jd": JDS["backend"]}).status_code == 401
    assert client.put("/api/bank", json={"yaml": "x"}, headers={"X-RT-Token": "wrong"}).status_code == 401
    assert client.get("/api/bank", headers=h(client)).status_code == 200


def test_cors_only_for_extensions(client):
    r = client.options("/api/jobs", headers={"Origin": "https://evil.com", "Access-Control-Request-Method": "POST"})
    assert "access-control-allow-origin" not in {k.lower() for k in r.headers}
    r = client.options("/api/jobs", headers={"Origin": EXT, "Access-Control-Request-Method": "POST",
                                             "Access-Control-Request-Headers": "x-rt-token,content-type"})
    assert r.headers.get("access-control-allow-origin") == EXT


def test_pairing_trust_on_first_use(client):
    assert client.post("/api/pair").status_code == 403                      # web page / no origin
    assert client.post("/api/pair", headers={"Origin": "https://evil.com"}).status_code == 403
    r = client.post("/api/pair", headers={"Origin": EXT})
    assert r.status_code == 200 and r.json()["token"] == client.token
    assert client.post("/api/pair", headers={"Origin": OTHER_EXT}).status_code == 403
    # an unpaired extension can't use the API even with a stolen token
    assert client.get("/api/history", headers=h(client, Origin=OTHER_EXT)).status_code == 403
    assert client.get("/api/history", headers=h(client, Origin=EXT)).status_code == 200
    client.post("/api/pair/reset", headers=h(client))
    assert client.post("/api/pair", headers={"Origin": OTHER_EXT}).status_code == 200


def test_ui_gets_token_inlined(client):
    r = client.get("/")
    assert r.status_code == 200 and client.token in r.text and "__RT_TOKEN__" not in r.text


def test_path_traversal(client):
    for rid in ("..", "..%2F..%2Fdata", "%2e%2e", "x/../../data"):
        r = client.get(f"/api/run/{rid}", headers=h(client))
        assert r.status_code == 404


def test_bank_validation_errors_returned(client):
    r = client.put("/api/bank", json={"yaml": "profile: {first_name: A}\nexperiences: []\n"}, headers=h(client))
    assert r.status_code == 422
    errs = r.json()["detail"]["errors"]
    assert any("last_name" in e["path"] for e in errs)


def test_bank_version_conflict(client):
    v = client.get("/api/bank.json", headers=h(client)).json()
    r = client.put("/api/bank.json", json={"bank": v["bank"], "version": "stale"}, headers=h(client))
    assert r.status_code == 409
    r = client.put("/api/bank.json", json={"bank": v["bank"], "version": v["version"]}, headers=h(client))
    assert r.status_code == 200


def test_oversized_input_rejected(client):
    r = client.post("/api/jobs", json={"jd": "x" * 70_000}, headers=h(client))
    assert r.status_code == 422


def test_parse_resume_rejects_non_pdf(client):
    import base64
    r = client.post("/api/parse-resume", json={"pdf_base64": base64.b64encode(b"<html>").decode()}, headers=h(client))
    assert r.status_code == 400
