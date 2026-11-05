"""Security and input tests. No network access needed: gallery-dl is never run for real."""
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import app as appmod  # noqa: E402

HOST = f"127.0.0.1:{appmod.PORT}"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod, "DOWNLOAD_ROOT", tmp_path)
    appmod.jobs.clear()
    appmod.app.config["TESTING"] = True
    with appmod.app.test_client() as c:
        yield c
    appmod.jobs.clear()


def post(client, path, json=None, token=None, **headers):
    h = {"X-CSRF-Token": appmod.CSRF_TOKEN if token is None else token, **headers}
    return client.post(path, json=json, headers=h, base_url=f"http://{HOST}")


def test_index_has_token_and_headers(client):
    r = client.get("/", base_url=f"http://{HOST}")
    assert r.status_code == 200
    assert appmod.CSRF_TOKEN in r.get_data(as_text=True)
    assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Frame-Options"] == "DENY"


def test_rejects_foreign_host(client):
    assert client.get("/", base_url="http://evil.example:8765").status_code == 403


def test_post_requires_token(client):
    assert post(client, "/api/jobs", {"url": "https://x.com/a"}, token="").status_code == 403
    assert post(client, "/api/jobs", {"url": "https://x.com/a"}, token="wrong").status_code == 403


def test_post_rejects_foreign_origin(client):
    r = post(client, "/api/jobs", {"url": "https://x.com/a"}, Origin="https://evil.example")
    assert r.status_code == 403


@pytest.mark.parametrize("body", [
    {"url": "file:///etc/passwd"},
    {"url": "http://192.168.1.1/admin"},
    {"url": "http://localhost:8765/"},
    {"url": "https://x.com/a b"},
    {"url": "a" * 600},
    {"url": 123},
    {"url": "https://x.com/a", "browser": "; rm -rf /"},
    {"url": "https://x.com/a", "limit": -1},
    {"url": "https://x.com/a", "limit": 999999},
    {"url": "https://x.com/a", "limit": "ten"},
])
def test_rejects_bad_input(client, body):
    r = post(client, "/api/jobs", body)
    assert r.status_code == 400
    assert "error" in r.get_json()


def test_command_is_an_argument_list():
    job = appmod.Job("https://x.com/a;whoami", "firefox", 5, True)
    cmd = appmod.build_command(job)
    assert cmd[-2:] == ["--", "https://x.com/a;whoami"]
    assert ["--cookies-from-browser", "firefox"] == cmd[cmd.index("--cookies-from-browser"):][:2]
    assert "--range" in cmd and "1-5" in cmd


def test_folder_name_is_safe():
    name = appmod.folder_name("https://www.instagram.com/../../etc/passwd")
    assert re.fullmatch(r"[A-Za-z0-9._-]+", name)
    assert ".." not in name.split("_")[0]


def test_starts_one_job_at_a_time(client, monkeypatch):
    monkeypatch.setattr(appmod, "run_job", lambda job: None)  # never touches the network
    first = post(client, "/api/jobs", {"url": "https://www.reddit.com/user/x/"})
    assert first.status_code == 201
    second = post(client, "/api/jobs", {"url": "https://www.reddit.com/user/y/"})
    assert second.status_code == 409


def test_file_route_blocks_traversal_and_non_images(client, monkeypatch, tmp_path):
    monkeypatch.setattr(appmod, "run_job", lambda job: None)
    job_id = post(client, "/api/jobs", {"url": "https://www.reddit.com/user/x/"}).get_json()["id"]
    job = appmod.jobs[job_id]
    job.dir.mkdir(parents=True)
    (job.dir / "a.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (job.dir / "x.svg").write_text("<svg onload=alert(1)>")
    (tmp_path / "secret.png").write_bytes(b"no")
    base = f"http://{HOST}"
    ok = client.get(f"/jobs/{job_id}/file/a.png", base_url=base)
    assert ok.status_code == 200
    assert ok.headers["Content-Security-Policy"].startswith("sandbox")
    assert client.get(f"/jobs/{job_id}/file/x.svg", base_url=base).status_code == 404
    assert client.get(f"/jobs/{job_id}/file/..%2fsecret.png", base_url=base).status_code == 404
    assert client.get("/api/jobs/not-a-real-id", base_url=base).status_code == 404
