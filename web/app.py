"""All Media Downloader, hosted website.

Paste a profile link; the server downloads its photos with gallery-dl and hands
you a ZIP. Protected by a single access password (ACCESS_PASSWORD) because a
public scraping endpoint would be abused and get the server's IP banned.

Instagram and X block cloud servers and need a login, so those are handled by
the Chrome extension in /extension instead.
"""
import hmac
import ipaddress
import logging
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
import zipfile
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlparse

from flask import (Flask, abort, jsonify, redirect, render_template, request,
                   send_file, send_from_directory, session, url_for)
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.middleware.proxy_fix import ProxyFix

import fastsaver

# ---------- configuration (all from environment) ----------

APP_ENV = os.environ.get("APP_ENV", "production")  # production | local
LOCAL = APP_ENV == "local"
ACCESS_PASSWORD = os.environ.get("ACCESS_PASSWORD", "")
SECRET_KEY = os.environ.get("SECRET_KEY", "")
ENV_LABEL = os.environ.get("ENV_LABEL", "")  # e.g. "dev" shows a banner
DOWNLOAD_ROOT = Path(os.environ.get("DOWNLOAD_DIR", "/tmp/all-media-downloader")).resolve()
MAX_FILES = int(os.environ.get("MAX_FILES_PER_JOB", "500"))
MAX_BYTES = int(os.environ.get("MAX_MB_PER_JOB", "1500")) * 1024 * 1024
JOB_TTL = int(os.environ.get("JOB_TTL_MINUTES", "60")) * 60

if LOCAL:
    ACCESS_PASSWORD = ACCESS_PASSWORD or "local"
    SECRET_KEY = SECRET_KEY or secrets.token_hex(32)
if len(ACCESS_PASSWORD) < 8 and not LOCAL:
    sys.exit("ACCESS_PASSWORD must be set (at least 8 characters).")
if len(SECRET_KEY) < 32:
    sys.exit("SECRET_KEY must be set (at least 32 characters).")

ALLOWED_HOSTS = {h.strip().lower() for h in os.environ.get("ALLOWED_HOSTS", "").split(",") if h.strip()}
if os.environ.get("RENDER_EXTERNAL_HOSTNAME"):  # set automatically by Render
    ALLOWED_HOSTS.add(os.environ["RENDER_EXTERNAL_HOSTNAME"].lower())
if LOCAL:
    ALLOWED_HOSTS |= {"127.0.0.1:8765", "localhost:8765", "127.0.0.1", "localhost"}

IMAGE_EXT = ("jpg", "jpeg", "png", "webp", "gif", "avif", "heic")  # no svg: it can carry script
MAX_URL_LEN = 500
MAX_LINKS = 50  # post links per download
LOG_LINES = 300
# Sites that block servers. Explain instead of failing slowly.
EXTENSION_ONLY = {"instagram", "twitter", "facebook", "threads", "tiktok"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("amd")

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=0)  # Render terminates TLS
app.config.update(
    SECRET_KEY=SECRET_KEY,
    MAX_CONTENT_LENGTH=8 * 1024,
    SESSION_COOKIE_NAME="amd_session",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=not LOCAL,
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
)
limiter = Limiter(get_remote_address, app=app, default_limits=["600 per hour"], storage_uri="memory://")

jobs: dict[str, "Job"] = {}
jobs_lock = threading.Lock()


class Job:
    def __init__(self, owner: str, urls: list[str], limit: int, images_only: bool):
        self.id = uuid.uuid4().hex
        self.owner = owner
        self.urls = urls
        self.url = urls[0] if len(urls) == 1 else f"{len(urls)} links"
        self.limit = limit
        self.images_only = images_only
        self.dir = DOWNLOAD_ROOT / self.id
        self.label = folder_name(urls[0]) if len(urls) == 1 else f"posts_{time.strftime('%Y%m%d-%H%M')}"
        self.state = "running"  # running | done | failed | cancelled
        self.log: list[str] = []
        self.started = time.time()
        self.finished: float | None = None
        self.proc: subprocess.Popen | None = None

    def files(self) -> list[Path]:
        if not self.dir.is_dir():
            return []
        return sorted(
            (p for p in self.dir.iterdir() if p.is_file() and not p.name.endswith(".part")),
            key=lambda p: p.stat().st_mtime,
        )

    def public(self) -> dict:
        files = self.files()
        images = [p.name for p in files if p.suffix.lower().lstrip(".") in IMAGE_EXT]
        expires = (self.finished + JOB_TTL) if self.finished else None
        return {
            "id": self.id,
            "url": self.url,
            "state": self.state,
            "count": len(files),
            "bytes": sum(p.stat().st_size for p in files),
            "recent": images[-24:][::-1],
            "log": self.log[-60:],
            "elapsed": round((self.finished or time.time()) - self.started),
            "expires_in": max(0, round(expires - time.time())) if expires else None,
        }


def folder_name(url: str) -> str:
    p = urlparse(url)
    site = (p.hostname or "site").removeprefix("www.").split(".")[0]
    parts = [s for s in p.path.split("/") if s][:2]
    raw = "_".join([site, *parts]) or "profile"
    return re.sub(r"[^A-Za-z0-9._-]+", "-", raw)[:80].strip("-._") or "profile"


def is_public_host(host: str) -> bool:
    """Reject anything that resolves to a private, loopback or link-local address (SSRF)."""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            return False
    return True


def extractor_category(url: str) -> str | None:
    from gallery_dl import extractor
    ex = extractor.find(url)
    return getattr(ex, "category", None) if ex else None


def validate_url(raw: object) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("Paste a profile link.")
    url = raw.strip()
    if len(url) > MAX_URL_LEN:
        raise ValueError("That link is too long.")
    if any(c in url for c in "\r\n\t "):
        raise ValueError("The link contains spaces or line breaks.")
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname or "." not in p.hostname:
        raise ValueError("That doesn't look like a web link.")
    if p.username or p.password:
        raise ValueError("Links with a username or password in them aren't allowed.")
    if fastsaver.post_site(url):
        url = fastsaver.clean_url(url)
        if not fastsaver.enabled():
            raise ValueError("Single Instagram, X, TikTok and Facebook posts need FASTSAVER_API_KEY set on the server.")
    else:
        category = extractor_category(url)
        if category in (None, "directlink", "generic", "noop", "recursive"):
            raise ValueError("That site or link type isn't supported.")
        if category in EXTENSION_ONLY:
            raise ValueError("Whole profiles on this site need the Chrome extension. Single post links work here.")
    if not is_public_host(p.hostname):
        raise ValueError("That address isn't allowed.")
    return url


def validate_links(raw: object) -> list[str]:
    """One profile link, or up to MAX_LINKS post links (one per line)."""
    if isinstance(raw, list):
        lines = raw
    elif isinstance(raw, str):
        lines = raw.splitlines()
    else:
        raise ValueError("Paste a profile or post link.")
    lines = [l.strip() for l in lines if isinstance(l, str) and l.strip()]
    if not lines:
        raise ValueError("Paste a profile or post link.")
    if len(lines) > MAX_LINKS:
        raise ValueError(f"Up to {MAX_LINKS} links at a time.")
    urls, seen = [], set()
    for n, line in enumerate(lines, 1):
        try:
            url = validate_url(line)
        except ValueError as e:
            raise ValueError(f"Line {n}: {e}" if len(lines) > 1 else str(e)) from None
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def build_command(job: Job, url: str | None = None, remaining: int | None = None) -> list[str]:
    # Argument list, never a shell string. "--" ends options so the URL can't add flags.
    cmd = [
        sys.executable, "-m", "gallery_dl",
        "--directory", str(job.dir),
        "--sleep-request", "1.0-2.5",
        "--retries", "3",
        "--no-colors",
        "--range", f"1-{remaining if remaining is not None else job.limit}",
    ]
    if job.images_only:
        cmd += ["--filter", f"extension in {IMAGE_EXT!r}"]
    cmd += ["--", url or job.urls[0]]
    return cmd


def add_log(job: Job, line: str) -> None:
    job.log.append(line[:400])
    del job.log[:-LOG_LINES]


def dir_bytes(job: Job) -> int:
    return sum(p.stat().st_size for p in job.files())


def run_gallery_dl(job: Job, url: str, remaining: int) -> int:
    """Download one link with gallery-dl. Returns the process exit code."""
    total = dir_bytes(job)
    job.proc = subprocess.Popen(
        build_command(job, url, remaining), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace", bufsize=1,
    )
    assert job.proc.stdout
    for line in job.proc.stdout:
        line = line.rstrip()
        if not line:
            continue
        if line.startswith(str(job.dir)):
            p = Path(line)
            line = "saved  " + p.name
            total += p.stat().st_size if p.exists() else 0
            if total > MAX_BYTES:
                job.proc.terminate()
                add_log(job, f"Stopped at the {MAX_BYTES // 1024 // 1024} MB limit for one download.")
        elif line.startswith("# " + str(job.dir)):
            line = "exists " + Path(line[2:]).name
        add_log(job, line)
    return job.proc.wait()


def run_fastsaver(job: Job, url: str, index: int, remaining: int) -> int:
    """Download one post through FastSaver. Returns the number of files saved."""
    body = fastsaver.fetch(url)
    media, skipped = fastsaver.media_from(body, job.images_only)
    post_id = re.sub(r"[^A-Za-z0-9_-]+", "", str(body.get("id") or ""))[:40] or f"post{index}"
    site = re.sub(r"[^a-z0-9]+", "", str(body.get("source") or "post").split(".")[0].lower())[:20] or "post"
    saved = 0
    for n, (_kind, media_url) in enumerate(media[:remaining], 1):
        if job.state != "running":
            break
        if dir_bytes(job) > MAX_BYTES:
            add_log(job, f"Stopped at the {MAX_BYTES // 1024 // 1024} MB limit for one download.")
            break
        stem = job.dir / f"{index:03d}_{site}_{post_id}" if len(media) == 1 else job.dir / f"{index:03d}_{site}_{post_id}_{n:02d}"
        try:
            out = fastsaver.download(media_url, stem)
            add_log(job, "saved  " + out.name)
            saved += 1
        except fastsaver.FastSaverError as e:
            add_log(job, f"[post {index}] {e}")
            log.warning("job %s post %s media download failed: %s (host %s)", job.id[:8], index, e,
                        urlparse(media_url).hostname)
    if skipped:
        add_log(job, f"[post {index}] skipped {skipped} video(s) (Photos only is on)")
    if not media and not skipped:
        add_log(job, f"[post {index}] no media found in that post")
        log.warning("job %s post %s: FastSaver ok but no media (type=%r, keys=%s)", job.id[:8], index,
                    body.get("type"), sorted(body.keys())[:15])
    return saved


def run_job(job: Job) -> None:
    job.dir.mkdir(parents=True, exist_ok=True)
    failures = 0
    try:
        for index, url in enumerate(job.urls, 1):
            if job.state != "running":
                break
            remaining = job.limit - len(job.files())
            if remaining <= 0:
                add_log(job, f"Reached the {job.limit}-file limit.")
                break
            if len(job.urls) > 1:
                add_log(job, f"[{index}/{len(job.urls)}] {url}")
            if fastsaver.post_site(url):
                try:
                    if run_fastsaver(job, url, index, remaining) == 0:
                        failures += 1
                except fastsaver.FastSaverError as e:
                    failures += 1
                    add_log(job, f"[post {index}] {e}")
                    log.warning("job %s post %s failed: %s", job.id[:8], index, e)
                    if e.fatal:
                        add_log(job, "Stopped: the remaining post links would fail the same way.")
                        break
            else:
                if run_gallery_dl(job, url, remaining) != 0:
                    failures += 1
        if job.state == "running":
            job.state = "done" if job.files() else "failed"
            if failures and job.files():
                add_log(job, f"Finished. {failures} of {len(job.urls)} link(s) had problems; see above.")
    except Exception as exc:
        job.state = "failed"
        add_log(job, "Unexpected server error.")
        log.exception("job %s failed: %s", job.id, exc)
    finally:
        job.finished = time.time()
        if not job.files():
            shutil.rmtree(job.dir, ignore_errors=True)


def cleanup_loop() -> None:
    """Delete finished downloads after JOB_TTL so the disk never fills up."""
    while True:
        time.sleep(300)
        now = time.time()
        with jobs_lock:
            expired = [j for j in jobs.values() if j.finished and now - j.finished > JOB_TTL]
            for j in expired:
                jobs.pop(j.id, None)
        for j in expired:
            shutil.rmtree(j.dir, ignore_errors=True)
            (DOWNLOAD_ROOT / "_zips" / f"{j.id}.zip").unlink(missing_ok=True)


def owner_id() -> str:
    return session.get("uid", "")


def get_job(job_id: str) -> Job:
    if not re.fullmatch(r"[0-9a-f]{32}", job_id or ""):
        abort(404)
    job = jobs.get(job_id)
    if not job or not hmac.compare_digest(job.owner, owner_id()):  # no IDOR
        abort(404)
    return job


def csrf_token() -> str:
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


# ---------- request guards ----------

PUBLIC_ENDPOINTS = {"login", "static", "healthz"}


@app.before_request
def guard():
    if request.host.lower() not in ALLOWED_HOSTS and request.endpoint != "healthz":
        abort(400)
    if request.method == "POST":
        origin = request.headers.get("Origin")
        if origin and urlparse(origin).netloc.lower() != request.host.lower():
            abort(403)
        token = request.headers.get("X-CSRF-Token") or request.form.get("csrf", "")
        if not session.get("csrf") or not hmac.compare_digest(token, session["csrf"]):
            abort(403)
    if request.endpoint not in PUBLIC_ENDPOINTS and not session.get("uid"):
        if request.path.startswith("/api/") or request.path.startswith("/jobs/"):
            abort(401)
        return redirect(url_for("login"))


@app.after_request
def headers(resp):
    resp.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self'; script-src 'self'; style-src 'self'; "
        "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
    )
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "same-origin"  # no-referrer makes browsers send Origin: null on form posts
    resp.headers["Cross-Origin-Resource-Policy"] = "same-origin"
    resp.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    if not LOCAL:
        resp.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    if request.path.startswith("/api/") or request.endpoint == "login":
        resp.headers["Cache-Control"] = "no-store"
    return resp


@app.errorhandler(400)
@app.errorhandler(401)
@app.errorhandler(403)
@app.errorhandler(404)
@app.errorhandler(409)
@app.errorhandler(413)
@app.errorhandler(429)
def json_error(err):
    msg = getattr(err, "description", "Request failed")
    if err.code == 429:
        msg = "Too many requests. Wait a few minutes and try again."
    return jsonify(error=msg), err.code


@app.errorhandler(500)
def server_error(err):
    return jsonify(error="Something went wrong on the server."), 500  # no stack traces


# ---------- routes ----------

@app.get("/healthz")
def healthz():
    return "ok"


@app.route("/login", methods=["GET", "POST"])
@limiter.limit("5 per minute; 30 per hour", methods=["POST"])
def login():
    error = None
    if request.method == "POST":
        given = request.form.get("password", "")
        if hmac.compare_digest(given.encode(), ACCESS_PASSWORD.encode()):
            session.clear()
            session.permanent = True
            session["uid"] = secrets.token_hex(16)
            session["csrf"] = secrets.token_urlsafe(32)
            return redirect(url_for("index"))
        error = "That password isn't right."
        log.warning("failed login from %s", get_remote_address())
    return render_template("login.html", csrf=csrf_token(), error=error, env=ENV_LABEL), (401 if error else 200)


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.get("/")
def index():
    return render_template("index.html", csrf=csrf_token(), env=ENV_LABEL, ttl=JOB_TTL // 60,
                           max_files=MAX_FILES, max_links=MAX_LINKS, posts_enabled=fastsaver.enabled())


@app.post("/api/jobs")
@limiter.limit("20 per hour")
def start_job():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        abort(400, "Send JSON.")
    try:
        urls = validate_links(data.get("links", data.get("url")))
    except ValueError as e:
        abort(400, str(e))
    limit = data.get("limit")
    if limit in (None, "", 0):
        limit = MAX_FILES
    else:
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            abort(400, "The limit must be a number.")
        if not 1 <= limit <= MAX_FILES:
            abort(400, f"The limit must be between 1 and {MAX_FILES}.")
    images_only = bool(data.get("images_only", True))

    with jobs_lock:
        if any(j.state == "running" for j in jobs.values()):
            abort(409, "Another download is running. Try again when it finishes.")
        job = Job(owner_id(), urls, limit, images_only)
        jobs[job.id] = job
    threading.Thread(target=run_job, args=(job,), daemon=True).start()
    return jsonify(job.public()), 201


@app.get("/api/jobs")
def list_jobs():
    mine = [j for j in jobs.values() if j.owner == owner_id()]
    return jsonify([j.public() | {"log": []} for j in sorted(mine, key=lambda j: -j.started)])


@app.get("/api/jobs/<job_id>")
def job_status(job_id):
    return jsonify(get_job(job_id).public())


@app.post("/api/jobs/<job_id>/cancel")
def cancel_job(job_id):
    job = get_job(job_id)
    if job.state == "running":
        job.state = "cancelled"
        if job.proc and job.proc.poll() is None:
            job.proc.terminate()
        job.log.append("Stopped. Files saved so far are kept.")
    return jsonify(job.public())


@app.get("/jobs/<job_id>/file/<name>")
def job_file(job_id, name):
    job = get_job(job_id)
    if Path(name).suffix.lower().lstrip(".") not in IMAGE_EXT:
        abort(404)
    resp = send_from_directory(job.dir, name)  # safe_join: no ../ escapes
    resp.headers["Content-Security-Policy"] = "sandbox; default-src 'none'"
    resp.headers["Cache-Control"] = "private, max-age=3600"
    return resp


@app.get("/jobs/<job_id>/zip")
@limiter.limit("30 per hour")
def job_zip(job_id):
    job = get_job(job_id)
    files = job.files()
    if not files:
        abort(404, "Nothing downloaded yet.")
    zips = DOWNLOAD_ROOT / "_zips"
    zips.mkdir(parents=True, exist_ok=True)
    out = zips / f"{job.id}.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as zf:
        for p in files:
            zf.write(p, arcname=f"{job.label}/{p.name}")
    return send_file(out, as_attachment=True, download_name=f"{job.label}.zip")


DOWNLOAD_ROOT.mkdir(parents=True, exist_ok=True)
threading.Thread(target=cleanup_loop, daemon=True).start()

if __name__ == "__main__":
    print("\n  Local dev server: http://127.0.0.1:8765  (password: value of ACCESS_PASSWORD, or 'local')\n")
    app.run(host="127.0.0.1", port=8765, debug=False)
