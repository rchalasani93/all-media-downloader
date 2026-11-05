"""Security and input tests for the hosted website. gallery-dl is never run for real."""
import os
import re
import sys
from pathlib import Path

import pytest

os.environ.update(APP_ENV="local", ACCESS_PASSWORD="correct-horse-battery", SECRET_KEY="t" * 48,
                  DOWNLOAD_DIR=str(Path(__file__).parent / ".tmp-downloads"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "web"))
import app as appmod  # noqa: E402

BASE = "http://127.0.0.1:8765"
PASSWORD = "correct-horse-battery"


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod, "DOWNLOAD_ROOT", tmp_path)
    monkeypatch.setattr(appmod, "run_job", lambda job: None)  # never touches the network
    monkeypatch.setattr(appmod, "is_public_host", lambda host: True)  # no DNS in tests
    appmod.limiter.enabled = False
    appmod.jobs.clear()
    yield
    appmod.jobs.clear()


def new_client():
    c = appmod.app.test_client()
    c.get("/login", base_url=BASE)
    return c


def csrf(c):
    with c.session_transaction(base_url=BASE) as s:
        return s.get("csrf", "")


def login(c, password=PASSWORD):
    return c.post("/login", data={"password": password, "csrf": csrf(c)}, base_url=BASE)


def start(c, body):
    return c.post("/api/jobs", json=body, headers={"X-CSRF-Token": csrf(c)}, base_url=BASE)


@pytest.fixture()
def client():
    c = new_client()
    assert login(c).status_code == 302
    return c


# ---- authentication ----

def test_pages_require_login():
    c = appmod.app.test_client()
    assert c.get("/", base_url=BASE).status_code == 302
    assert c.get("/api/jobs", base_url=BASE).status_code == 401


def test_wrong_password_rejected():
    c = new_client()
    r = login(c, "nope")
    assert r.status_code == 401
    assert c.get("/api/jobs", base_url=BASE).status_code == 401


def test_login_needs_csrf():
    c = new_client()
    r = c.post("/login", data={"password": PASSWORD}, base_url=BASE)
    assert r.status_code == 403


def test_login_is_rate_limited():
    appmod.limiter.enabled = True
    appmod.limiter.reset()
    c = new_client()
    codes = [login(c, "wrong").status_code for _ in range(7)]
    assert 429 in codes
    appmod.limiter.reset()


def test_logout(client):
    client.post("/logout", data={"csrf": csrf(client)}, base_url=BASE)
    assert client.get("/api/jobs", base_url=BASE).status_code == 401


# ---- request guards ----

def test_headers(client):
    r = client.get("/", base_url=BASE)
    assert r.status_code == 200
    assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Frame-Options"] == "DENY"


def test_rejects_unknown_host(client):
    assert client.get("/", base_url="http://evil.example").status_code == 400


def test_post_requires_csrf(client):
    r = client.post("/api/jobs", json={"url": "https://www.reddit.com/user/x/"}, base_url=BASE)
    assert r.status_code == 403


def test_post_rejects_foreign_origin(client):
    r = client.post("/api/jobs", json={"url": "https://www.reddit.com/user/x/"},
                    headers={"X-CSRF-Token": csrf(client), "Origin": "https://evil.example"}, base_url=BASE)
    assert r.status_code == 403


# ---- input validation ----

@pytest.mark.parametrize("body", [
    {"url": "file:///etc/passwd"},
    {"url": "https://example.com/"},                       # no extractor
    {"url": "https://example.com/photo.jpg"},              # directlink: SSRF vector
    {"url": "https://user:pw@www.reddit.com/user/x/"},
    {"url": "https://www.reddit.com/user/x/ y"},
    {"url": "a" * 600},
    {"url": 123},
    {"url": "https://www.reddit.com/user/x/", "limit": -1},
    {"url": "https://www.reddit.com/user/x/", "limit": 100000},
    {"url": "https://www.reddit.com/user/x/", "limit": "ten"},
])
def test_rejects_bad_input(client, body):
    r = start(client, body)
    assert r.status_code == 400, r.get_json()
    assert "error" in r.get_json()


@pytest.mark.parametrize("url", ["https://www.instagram.com/natgeo/", "https://x.com/NASA"])
def test_points_blocked_sites_to_extension(client, url):
    r = start(client, {"url": url})
    assert r.status_code == 400
    assert "extension" in r.get_json()["error"]


def test_ssrf_guard_blocks_private_addresses(monkeypatch):
    monkeypatch.undo()  # use the real resolver for literals
    assert appmod.is_public_host("127.0.0.1") is False
    assert appmod.is_public_host("10.0.0.5") is False
    assert appmod.is_public_host("169.254.169.254") is False  # cloud metadata endpoint
    assert appmod.is_public_host("::1") is False


def test_command_is_an_argument_list():
    job = appmod.Job("owner", "https://www.reddit.com/user/x;whoami/", 5, True)
    cmd = appmod.build_command(job)
    assert cmd[-2:] == ["--", "https://www.reddit.com/user/x;whoami/"]
    assert "1-5" in cmd
    assert "--cookies-from-browser" not in cmd


# ---- jobs ----

def test_start_job_and_one_at_a_time(client):
    first = start(client, {"url": "https://www.reddit.com/user/x/submitted/"})
    assert first.status_code == 201
    assert first.get_json()["state"] == "running"
    second = start(client, {"url": "https://www.reddit.com/user/y/"})
    assert second.status_code == 409


def test_other_sessions_cannot_see_my_jobs(client):
    job_id = start(client, {"url": "https://www.reddit.com/user/x/"}).get_json()["id"]
    other = new_client()
    assert login(other).status_code == 302
    assert other.get(f"/api/jobs/{job_id}", base_url=BASE).status_code == 404
    assert other.get(f"/jobs/{job_id}/zip", base_url=BASE).status_code == 404
    assert other.get("/api/jobs", base_url=BASE).get_json() == []
    assert client.get(f"/api/jobs/{job_id}", base_url=BASE).status_code == 200


def test_file_route_blocks_traversal_and_non_images(client, tmp_path):
    job_id = start(client, {"url": "https://www.reddit.com/user/x/"}).get_json()["id"]
    job = appmod.jobs[job_id]
    job.dir.mkdir(parents=True)
    (job.dir / "a.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (job.dir / "x.svg").write_text("<svg onload=alert(1)>")
    (tmp_path / "secret.png").write_bytes(b"no")
    ok = client.get(f"/jobs/{job_id}/file/a.png", base_url=BASE)
    assert ok.status_code == 200
    assert ok.headers["Content-Security-Policy"].startswith("sandbox")
    assert client.get(f"/jobs/{job_id}/file/x.svg", base_url=BASE).status_code == 404
    assert client.get(f"/jobs/{job_id}/file/..%2fsecret.png", base_url=BASE).status_code == 404
    zip_resp = client.get(f"/jobs/{job_id}/zip", base_url=BASE)
    assert zip_resp.status_code == 200
    assert re.search(r'filename=reddit_user_x\.zip', zip_resp.headers["Content-Disposition"])


def test_health_is_public():
    assert appmod.app.test_client().get("/healthz", base_url=BASE).data == b"ok"


def test_login_form_post_with_same_origin_header_works():
    c = new_client()
    r = c.post("/login", data={"password": PASSWORD, "csrf": csrf(c)},
               headers={"Origin": BASE}, base_url=BASE)
    assert r.status_code == 302
    assert appmod.app.test_client().get("/", base_url=BASE).headers  # sanity


def test_referrer_policy_keeps_origin_on_forms():
    r = appmod.app.test_client().get("/login", base_url=BASE)
    assert r.headers["Referrer-Policy"] == "same-origin"
