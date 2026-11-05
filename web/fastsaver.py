"""FastSaver API client for single posts on sites that block servers
(Instagram, X, TikTok, Facebook). Docs: https://api.fastsaver.io/docs/fetch

The API key comes from the FASTSAVER_API_KEY environment variable only and is
never logged or sent to the browser.
"""
import ipaddress
import json
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API_BASE = "https://api.fastsaver.io/v1"
API_KEY = os.environ.get("FASTSAVER_API_KEY", "").strip().strip("\"'").strip()
RPM = max(1, int(os.environ.get("FASTSAVER_RPM", "10")))  # free plan: 10 requests/minute
MAX_FILE_BYTES = int(os.environ.get("MAX_MB_PER_FILE", "300")) * 1024 * 1024
TIMEOUT = 60
USER_AGENT = "all-media-downloader/1.0"

_pace_lock = threading.Lock()
_last_call = 0.0


class FastSaverError(Exception):
    """A problem worth showing to the user (no secrets in the message)."""

    def __init__(self, message: str, fatal: bool = False):
        super().__init__(message)
        self.fatal = fatal  # True when retrying other links is pointless (bad key, no credits)


def key_shape() -> str:
    """Describe the configured key without revealing it."""
    k = API_KEY
    parts = [f"{len(k)} characters", "starts with fs_sk_" if k.startswith("fs_sk_") else "does NOT start with fs_sk_"]
    if any(c.isspace() for c in k):
        parts.append("contains spaces")
    return ", ".join(parts)


def enabled() -> bool:
    return bool(API_KEY)


# ---------- which links go to FastSaver ----------

_POST_PATTERNS = [
    ("instagram", re.compile(r"^(www\.)?instagram\.com$"), re.compile(r"^/(?:[^/]+/)?(?:p|reel|reels|tv)/[A-Za-z0-9_-]+|^/stories/[^/]+/\d+")),
    ("x", re.compile(r"^(www\.|mobile\.)?(x|twitter)\.com$"), re.compile(r"^/[A-Za-z0-9_]{1,15}/status/\d+")),
    ("tiktok", re.compile(r"^(www\.|m\.)?tiktok\.com$"), re.compile(r"^/@[^/]+/(?:video|photo)/\d+")),
    ("tiktok", re.compile(r"^(vm|vt)\.tiktok\.com$"), re.compile(r"^/[A-Za-z0-9]+")),
    ("facebook", re.compile(r"^(www\.|m\.|web\.)?(facebook|fb)\.com$"),
     re.compile(r"^/(?:[^/]+/(?:posts|videos|photos)/|reel/|watch|share/|photo|permalink\.php|story\.php)")),
    ("facebook", re.compile(r"^fb\.watch$"), re.compile(r"^/[A-Za-z0-9_-]+")),
]


_TRACKING = re.compile(r"^(utm_\w+|igsh|igshid|mdxt|srtk|img_index|fbclid|si|s|t|ref|ref_src|ref_url|is_from_webapp|sender_device|_r|_t|mibextid|rdid|share_url|sfnsn)$", re.I)


def clean_url(url: str) -> str:
    """Drop share/tracking parameters (Instagram's copy-link adds several) and the fragment."""
    p = urllib.parse.urlparse(url)
    keep = [(k, v) for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=True) if not _TRACKING.match(k)]
    return urllib.parse.urlunparse(p._replace(query=urllib.parse.urlencode(keep), fragment=""))


def post_site(url: str) -> str | None:
    """Return the site name if this is a single-post link FastSaver should handle."""
    p = urllib.parse.urlparse(url)
    host = (p.hostname or "").lower()
    for site, host_re, path_re in _POST_PATTERNS:
        if host_re.match(host) and path_re.match(p.path or "/"):
            return site
    return None


# ---------- SSRF-safe downloading ----------

def host_is_public(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError):
        return False
    return bool(infos) and all(ipaddress.ip_address(i[4][0]).is_global for i in infos)


def _check_url(url: str) -> None:
    p = urllib.parse.urlparse(url)
    if p.scheme != "https" or not p.hostname or p.username or p.password:
        raise FastSaverError("The media link wasn't a plain https link, so it was skipped.")
    if not host_is_public(p.hostname):
        raise FastSaverError("The media link pointed to a private address, so it was skipped.")


class _SafeRedirects(urllib.request.HTTPRedirectHandler):
    max_redirections = 5

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _check_url(newurl)  # every hop must stay on the public internet
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_SafeRedirects)

_EXT_BY_TYPE = {
    "image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif",
    "image/avif": "avif", "image/heic": "heic", "video/mp4": "mp4", "video/webm": "webm",
    "video/quicktime": "mov",
}


def sniff_ext(head: bytes) -> str | None:
    """Recognise common media formats from their first bytes."""
    if head.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in (b"avif", b"avis"):
            return "avif"
        if brand in (b"heic", b"heix", b"mif1"):
            return "heic"
        return "mp4"
    if head.startswith(b"\x1aE\xdf\xa3"):
        return "webm"
    return None


def download(url: str, dest_stem: Path) -> Path:
    """Download one media file to dest_stem.<ext>. Returns the saved path."""
    _check_url(url)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "image/*,video/*,*/*;q=0.5"})
    try:
        with _opener.open(req, timeout=TIMEOUT) as resp:
            ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            length = int(resp.headers.get("Content-Length") or 0)
            if length > MAX_FILE_BYTES:
                raise FastSaverError("Skipped a file over the size limit.")
            first = resp.read(256 * 1024)
            # Trust the file's own bytes first; fall back to the declared type.
            ext = sniff_ext(first[:16]) or _EXT_BY_TYPE.get(ctype)
            if not ext:
                raise FastSaverError(f"Skipped a file that isn't an image or video (type '{ctype or 'unknown'}').")
            out = dest_stem.with_suffix("." + ext)
            part = out.with_suffix(out.suffix + ".part")
            total = len(first)
            with open(part, "wb") as fh:
                fh.write(first)
                while chunk := resp.read(256 * 1024):
                    total += len(chunk)
                    if total > MAX_FILE_BYTES:
                        fh.close()
                        part.unlink(missing_ok=True)
                        raise FastSaverError("Skipped a file over the size limit.")
                    fh.write(chunk)
            part.rename(out)
            return out
    except urllib.error.HTTPError as e:
        raise FastSaverError(f"The media server answered {e.code}.") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise FastSaverError("Couldn't reach the media server.") from None


# ---------- the API call ----------

def _pace() -> None:
    """Stay under the plan's requests-per-minute limit, across all jobs."""
    global _last_call
    gap = 60.0 / RPM + 0.2
    with _pace_lock:
        wait = _last_call + gap - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_call = time.monotonic()


def fetch(post_url: str) -> dict:
    """Call /v1/fetch. Returns the JSON body when ok is true, else raises FastSaverError."""
    if not API_KEY:
        raise FastSaverError("Single-post downloads aren't set up: FASTSAVER_API_KEY is missing on the server.", fatal=True)
    query = urllib.parse.urlencode({"url": post_url})
    for attempt in range(4):
        _pace()
        req = urllib.request.Request(f"{API_BASE}/fetch?{query}",
                                     headers={"X-Api-Key": API_KEY, "User-Agent": USER_AGENT, "Accept": "application/json"})
        status, body, retry_after = 200, None, None
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                body = json.loads(resp.read(2 * 1024 * 1024) or b"null")
        except urllib.error.HTTPError as e:
            status = e.code
            retry_after = e.headers.get("Retry-After") if e.headers else None
            try:
                body = json.loads(e.read(64 * 1024) or b"null")
            except ValueError:
                body = None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            if attempt < 3:
                time.sleep(3)
                continue
            raise FastSaverError("Couldn't reach FastSaver.") from None

        if status in (429, 503) or (status >= 500 and not isinstance(body, dict)):
            try:
                delay = min(30.0, float(retry_after)) if retry_after else 10.0
            except ValueError:
                delay = 10.0
            time.sleep(delay)
            continue
        if status == 401:
            raise FastSaverError(f"FastSaver didn't accept the API key ({key_shape()}). "
                                 "Copy the key again from your FastSaver dashboard into FASTSAVER_API_KEY on Render.", fatal=True)
        if isinstance(body, dict) and body.get("ok") is True:
            return body
        detail = body.get("detail") if isinstance(body, dict) else None
        if isinstance(detail, str) and detail:
            raise FastSaverError(f"FastSaver: {detail[:200]}", fatal="credit" in detail.lower())
        raise FastSaverError(f"FastSaver couldn't read that post ({status}).")
    raise FastSaverError("FastSaver is busy. Try again in a minute.")


def media_from(body: dict, images_only: bool) -> tuple[list[tuple[str, str]], int]:
    """(list of (kind, url)), number of videos skipped. Albums put their media in `items`."""
    entries = body.get("items") if body.get("type") == "album" else [body]
    out, skipped = [], 0
    for e in entries or []:
        if not isinstance(e, dict):
            continue
        url = e.get("download_url") or e.get("url")
        kind = str(e.get("type") or "").lower()
        if not isinstance(url, str) or not url:
            continue
        if kind == "video" and images_only:
            skipped += 1
            continue
        out.append((kind or "media", url))
    return out, skipped
