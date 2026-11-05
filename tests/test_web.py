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

real_run_job = appmod.run_job

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
    job = appmod.Job("owner", ["https://www.reddit.com/user/x;whoami/"], 5, True)
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


# ---- single posts (FastSaver) ----

import fastsaver  # noqa: E402


@pytest.mark.parametrize("url,site", [
    ("https://www.instagram.com/p/DRsmm9UjKfH/", "instagram"),
    ("https://www.instagram.com/reel/ABC123/", "instagram"),
    ("https://www.instagram.com/someone/p/ABC123/", "instagram"),
    ("https://x.com/NASA/status/1234567890", "x"),
    ("https://twitter.com/NASA/status/1234567890", "x"),
    ("https://www.tiktok.com/@user/video/7300000000000000000", "tiktok"),
    ("https://vm.tiktok.com/ZMabc123/", "tiktok"),
    ("https://www.facebook.com/reel/123456", "facebook"),
    ("https://fb.watch/abcDEF/", "facebook"),
    ("https://www.instagram.com/natgeo/", None),          # profile, not a post
    ("https://x.com/NASA", None),
    ("https://www.reddit.com/r/pics/comments/abc/x/", None),  # gallery-dl handles it
    ("https://evil.example/p/ABC/", None),
])
def test_post_site_classification(url, site):
    assert fastsaver.post_site(url) == site


def test_post_links_need_api_key(client, monkeypatch):
    monkeypatch.setattr(fastsaver, "API_KEY", "")
    r = start(client, {"links": ["https://www.instagram.com/p/ABC123/"]})
    assert r.status_code == 400
    assert "FASTSAVER_API_KEY" in r.get_json()["error"]


def test_multiple_post_links_accepted(client, monkeypatch):
    monkeypatch.setattr(fastsaver, "API_KEY", "fs_sk_test")
    r = start(client, {"links": "https://www.instagram.com/p/ABC123/\nhttps://x.com/NASA/status/1\n\nhttps://www.instagram.com/p/ABC123/"})
    assert r.status_code == 201
    job = appmod.jobs[r.get_json()["id"]]
    assert job.urls == ["https://www.instagram.com/p/ABC123/", "https://x.com/NASA/status/1"]  # duplicates dropped
    assert r.get_json()["url"] == "2 links"


def test_link_limits_and_line_numbers(client, monkeypatch):
    monkeypatch.setattr(fastsaver, "API_KEY", "fs_sk_test")
    many = "\n".join(f"https://www.instagram.com/p/A{i}/" for i in range(51))
    assert "Up to 50" in start(client, {"links": many}).get_json()["error"]
    r = start(client, {"links": "https://www.instagram.com/p/A1/\nfile:///etc/passwd"})
    assert r.status_code == 400 and r.get_json()["error"].startswith("Line 2:")


def test_run_job_with_fastsaver_album(tmp_path, monkeypatch):
    monkeypatch.setattr(fastsaver, "API_KEY", "fs_sk_test")
    album = {"ok": True, "id": "ABC123", "source": "instagram.com", "type": "album", "items": [
        {"type": "image", "download_url": "https://cdn.example/a.jpg"},
        {"type": "video", "download_url": "https://cdn.example/b.mp4"},
        {"type": "image", "download_url": "https://cdn.example/c.jpg"},
    ]}
    monkeypatch.setattr(fastsaver, "fetch", lambda url: album)
    saved = []

    def fake_download(url, stem):
        out = stem.with_suffix(".jpg")
        out.write_bytes(b"\xff\xd8 fake")
        saved.append(url)
        return out
    monkeypatch.setattr(fastsaver, "download", fake_download)
    job = appmod.Job("o", ["https://www.instagram.com/p/ABC123/"], 50, True)
    appmod.jobs[job.id] = job
    real_run_job(job)  # the real one, not the fixture's stub
    assert saved == ["https://cdn.example/a.jpg", "https://cdn.example/c.jpg"]
    assert job.state == "done"
    assert sorted(p.name for p in job.files()) == ["001_instagram_ABC123_01.jpg", "001_instagram_ABC123_02.jpg"]
    assert any("skipped 1 video" in l for l in job.log)


def test_fastsaver_errors_are_readable(monkeypatch):
    monkeypatch.setattr(fastsaver, "API_KEY", "fs_sk_secret")
    monkeypatch.setattr(fastsaver, "_pace", lambda: None)
    import io
    import urllib.error

    def raise_http(code, body):
        def _open(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, code, "x", {}, io.BytesIO(body))
        return _open
    monkeypatch.setattr(fastsaver.urllib.request, "urlopen", raise_http(400, b'{"ok": false, "detail": "Insufficient credits."}'))
    with pytest.raises(fastsaver.FastSaverError) as e:
        fastsaver.fetch("https://www.instagram.com/p/X/")
    assert "Insufficient credits" in str(e.value) and "fs_sk_secret" not in str(e.value)
    monkeypatch.setattr(fastsaver.urllib.request, "urlopen", raise_http(401, b'{"ok": false}'))
    with pytest.raises(fastsaver.FastSaverError, match="API key"):
        fastsaver.fetch("https://www.instagram.com/p/X/")


def test_media_downloads_refuse_private_and_plain_http(tmp_path):
    with pytest.raises(fastsaver.FastSaverError):
        fastsaver.download("http://cdn.example/a.jpg", tmp_path / "a")
    with pytest.raises(fastsaver.FastSaverError):
        fastsaver.download("https://127.0.0.1/a.jpg", tmp_path / "a")
    with pytest.raises(fastsaver.FastSaverError):
        fastsaver.download("https://169.254.169.254/latest/meta-data", tmp_path / "a")
    # a redirect to a private address is refused too
    with pytest.raises(fastsaver.FastSaverError):
        fastsaver._SafeRedirects().redirect_request(None, None, 302, "Found", {}, "https://10.0.0.1/x.jpg")


def test_media_from_handles_single_and_album():
    assert fastsaver.media_from({"type": "video", "download_url": "https://a/v.mp4"}, True) == ([], 1)
    assert fastsaver.media_from({"type": "image", "download_url": "https://a/i.jpg"}, True) == ([("image", "https://a/i.jpg")], 0)
    assert fastsaver.media_from({"type": "album", "items": [{"url": "https://a/1.jpg", "type": "image"}, "junk"]}, False) == ([("image", "https://a/1.jpg")], 0)


def test_bad_key_stops_the_whole_job(monkeypatch):
    monkeypatch.setattr(fastsaver, "API_KEY", "fs_sk_test")
    calls = []

    def fail(url):
        calls.append(url)
        raise fastsaver.FastSaverError("FastSaver didn't accept the API key.", fatal=True)
    monkeypatch.setattr(fastsaver, "fetch", fail)
    job = appmod.Job("o", ["https://www.instagram.com/p/A/", "https://www.instagram.com/p/B/"], 50, True)
    real_run_job(job)
    assert len(calls) == 1 and job.state == "failed"
