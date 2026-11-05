"""Contact Sheet: a local web UI for downloading the photos on a social media profile.

Runs only on your own machine (127.0.0.1). The actual fetching is done by
gallery-dl (https://github.com/mikf/gallery-dl), which supports Instagram,
X/Twitter, Reddit, Tumblr, Pinterest and many more sites.
"""
import hmac
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import uuid
import zipfile
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, abort, jsonify, render_template, request, send_file, send_from_directory

BASE = Path(__file__).resolve().parent
DOWNLOAD_ROOT = Path(os.environ.get("CONTACT_SHEET_DIR", BASE / "downloads")).resolve()
PORT = int(os.environ.get("CONTACT_SHEET_PORT", "8765"))
HOST = "127.0.0.1"  # never bind to 0.0.0.0: this tool can use your browser logins

ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
BROWSERS = {"", "chrome", "firefox", "edge", "brave", "safari", "chromium", "opera", "vivaldi"}
IMAGE_EXT = ("jpg", "jpeg", "png", "webp", "gif", "avif", "heic")  # no svg: it can carry script
MAX_URL_LEN = 500
MAX_LIMIT = 5000
LOG_LINES = 300

app = Flask(__name__)
app.config.update(MAX_CONTENT_LENGTH=4 * 1024, JSON_SORT_KEYS=False)

CSRF_TOKEN = secrets.token_urlsafe(32)  # new every start; only the page we serve knows it
jobs: dict[str, "Job"] = {}
jobs_lock = threading.Lock()


class Job:
    def __init__(self, url: str, browser: str, limit: int | None, images_only: bool):
        self.id = uuid.uuid4().hex
        self.url = url
        self.browser = browser
        self.limit = limit
        self.images_only = images_only
        self.dir = DOWNLOAD_ROOT / f"{folder_name(url)}_{time.strftime('%Y%m%d-%H%M%S')}"
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
        return {
            "id": self.id,
            "url": self.url,
            "state": self.state,
            "count": len(files),
            "bytes": sum(p.stat().st_size for p in files),
            "folder": str(self.dir),
            "recent": images[-24:][::-1],
            "log": self.log[-60:],
            "elapsed": round((self.finished or time.time()) - self.started),
        }


def folder_name(url: str) -> str:
    p = urlparse(url)
    site = (p.hostname or "site").removeprefix("www.").split(".")[0]
    parts = [s for s in p.path.split("/") if s][:2]
    raw = "_".join([site, *parts]) or "profile"
    return re.sub(r"[^A-Za-z0-9._-]+", "-", raw)[:80].strip("-._") or "profile"


def validate_url(raw: object) -> str:
    if not isinstance(raw, str):
        raise ValueError("Paste a profile link.")
    url = raw.strip()
    if not url:
        raise ValueError("Paste a profile link.")
    if len(url) > MAX_URL_LEN:
        raise ValueError("That link is too long.")
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname or "." not in p.hostname:
        raise ValueError("That doesn't look like a web link.")
    host = p.hostname.lower()
    if host in ("localhost",) or host.startswith(("127.", "10.", "192.168.", "169.254.")) or host.endswith(".local"):
        raise ValueError("Local network addresses aren't allowed.")
    if any(c in url for c in "\r\n\t "):
        raise ValueError("The link contains spaces or line breaks.")
    return url


def build_command(job: Job) -> list[str]:
    # Argument list, never a shell string: the URL can't inject commands.
    cmd = [
        sys.executable, "-m", "gallery_dl",
        "--directory", str(job.dir),
        "--sleep-request", "1.0-2.5",  # be gentle; fast scraping gets accounts flagged
        "--retries", "3",
        "--no-colors",
    ]
    if job.browser:
        cmd += ["--cookies-from-browser", job.browser]
    if job.limit:
        cmd += ["--range", f"1-{job.limit}"]
    if job.images_only:
        cmd += ["--filter", f"extension in {IMAGE_EXT!r}"]
    cmd += ["--", job.url]
    return cmd


def run_job(job: Job) -> None:
    job.dir.mkdir(parents=True, exist_ok=True)
    try:
        job.proc = subprocess.Popen(
            build_command(job),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        assert job.proc.stdout
        for line in job.proc.stdout:
            line = line.rstrip()
            if not line:
                continue
            # gallery-dl prints full paths of saved files; show just the file name.
            if line.startswith(str(job.dir)):
                line = "saved  " + Path(line).name
            elif line.startswith("# " + str(job.dir)):
                line = "exists " + Path(line[2:]).name
            job.log.append(line[:400])
            del job.log[:-LOG_LINES]
        code = job.proc.wait()
        if job.state == "running":
            job.state = "done" if code == 0 or job.files() else "failed"
            if code != 0 and job.files():
                job.log.append(f"Finished with some errors (exit code {code}). Files saved so far are kept.")
    except FileNotFoundError:
        job.state = "failed"
        job.log.append("gallery-dl isn't installed. Run: pip install -r requirements.txt")
    except Exception as exc:  # keep the server alive, report the problem
        job.state = "failed"
        job.log.append(f"Unexpected error: {exc.__class__.__name__}")
    finally:
        job.finished = time.time()
        if not job.files():
            shutil.rmtree(job.dir, ignore_errors=True)


def get_job(job_id: str) -> Job:
    if not re.fullmatch(r"[0-9a-f]{32}", job_id or ""):
        abort(404)
    job = jobs.get(job_id)
    if not job:
        abort(404)
    return job


# ---------- request guards ----------

@app.before_request
def guard():
    # Blocks DNS-rebinding: another site pointing its domain at 127.0.0.1.
    if request.host not in ALLOWED_HOSTS:
        abort(403)
    if request.method == "POST":
        origin = request.headers.get("Origin")
        if origin and urlparse(origin).netloc not in ALLOWED_HOSTS:
            abort(403)
        token = request.headers.get("X-CSRF-Token", "")
        if not hmac.compare_digest(token, CSRF_TOKEN):
            abort(403)


@app.after_request
def headers(resp):
    resp.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self'; script-src 'self'; style-src 'self'; "
        "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
    )
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Cross-Origin-Resource-Policy"] = "same-origin"
    if request.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


@app.errorhandler(400)
@app.errorhandler(403)
@app.errorhandler(404)
@app.errorhandler(409)
@app.errorhandler(413)
def json_error(err):
    return jsonify(error=getattr(err, "description", "Request failed")), err.code


# ---------- routes ----------

@app.get("/")
def index():
    return render_template("index.html", csrf=CSRF_TOKEN, folder=str(DOWNLOAD_ROOT))


@app.post("/api/jobs")
def start_job():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        abort(400, "Send JSON.")
    try:
        url = validate_url(data.get("url"))
    except ValueError as e:
        abort(400, str(e))
    browser = data.get("browser", "")
    if browser not in BROWSERS:
        abort(400, "Unknown browser.")
    limit = data.get("limit")
    if limit in (None, "", 0):
        limit = None
    else:
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            abort(400, "The limit must be a number.")
        if not 1 <= limit <= MAX_LIMIT:
            abort(400, f"The limit must be between 1 and {MAX_LIMIT}.")
    images_only = bool(data.get("images_only", True))

    with jobs_lock:
        # One download at a time: keeps you under the sites' rate limits.
        if any(j.state == "running" for j in jobs.values()):
            abort(409, "A download is already running. Wait for it or stop it first.")
        job = Job(url, browser, limit, images_only)
        jobs[job.id] = job
    threading.Thread(target=run_job, args=(job,), daemon=True).start()
    return jsonify(job.public()), 201


@app.get("/api/jobs")
def list_jobs():
    return jsonify([j.public() | {"log": []} for j in sorted(jobs.values(), key=lambda j: -j.started)])


@app.get("/api/jobs/<job_id>")
def job_status(job_id):
    return jsonify(get_job(job_id).public())


@app.post("/api/jobs/<job_id>/cancel")
def cancel_job(job_id):
    job = get_job(job_id)
    if job.state == "running" and job.proc:
        job.state = "cancelled"
        job.proc.terminate()
        job.log.append("Stopped. Files saved so far are kept.")
    return jsonify(job.public())


@app.post("/api/jobs/<job_id>/open")
def open_folder(job_id):
    job = get_job(job_id)
    if not job.dir.is_dir():
        abort(404, "The folder is empty or was removed.")
    path = str(job.dir)
    if sys.platform.startswith("win"):
        os.startfile(path)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])
    return jsonify(ok=True)


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
def job_zip(job_id):
    job = get_job(job_id)
    files = job.files()
    if not files:
        abort(404, "Nothing downloaded yet.")
    zips = DOWNLOAD_ROOT / "_zips"
    zips.mkdir(parents=True, exist_ok=True)
    out = zips / f"{job.dir.name}.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as zf:  # photos are already compressed
        for p in files:
            zf.write(p, arcname=p.name)
    return send_file(out, as_attachment=True, download_name=out.name)


if __name__ == "__main__":
    DOWNLOAD_ROOT.mkdir(parents=True, exist_ok=True)
    print(f"\n  Contact Sheet is running at http://127.0.0.1:{PORT}")
    print(f"  Photos are saved in {DOWNLOAD_ROOT}\n  Press Ctrl+C to stop.\n")
    app.run(host=HOST, port=PORT, debug=False, threaded=True)
