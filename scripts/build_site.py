"""Build the static download page for one branch.

Usage: python scripts/build_site.py <checkout_dir> <out_dir> <env: prod|dev>

The page links to the hosted website for that environment (deploy/environments.json)
and offers the Chrome extension ZIP built from the same checkout, so /dev/ always
matches the develop branch.
"""
import html
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import json

EXT_DIR = "extension"
EXT_ZIP = "all-media-downloader-extension.zip"
PROD_URL = "https://rchalasani93.github.io/all-media-downloader/"


def git(src: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(src), *args], capture_output=True, text=True, check=True).stdout.strip()


def build(src: Path, out: Path, env: str) -> None:
    out.mkdir(parents=True, exist_ok=True)
    commit = git(src, "rev-parse", "--short", "HEAD")
    branch = "develop" if env == "dev" else "main"
    built = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    with zipfile.ZipFile(out / EXT_ZIP, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in sorted((src / EXT_DIR).rglob("*")):
            if p.is_file():
                info = zipfile.ZipInfo(f"all-media-downloader-extension/{p.relative_to(src / EXT_DIR).as_posix()}",
                                       date_time=(2026, 1, 1, 0, 0, 0))
                info.external_attr = 0o644 << 16
                zf.writestr(info, p.read_bytes(), zipfile.ZIP_DEFLATED)

    envs = json.loads((src / "deploy" / "environments.json").read_text(encoding="utf-8"))
    web_url = envs.get(env, {}).get("web_url", "")
    if web_url.startswith("https://"):
        web_button = f'<a class="button" href="{html.escape(web_url)}">Open the website</a>'
    else:
        web_button = '<span class="button disabled" aria-disabled="true">Website not deployed yet</span>'

    if env == "dev":
        banner = ('<p class="env">Development build from <code>develop</code>. '
                  'Things may break. <a href="../">Stable version</a></p>')
        other = ' · <a href="../">Stable version</a>'
        suffix = " (dev)"
    else:
        banner, suffix = "", ""
        other = ' · <a href="dev/">Development build</a>'

    # Search engines: production is indexed with a canonical URL and a sitemap; /dev/ is
    # noindex so the preview never competes with the stable page in Google.
    canonical = PROD_URL
    seo = [f'<link rel="canonical" href="{canonical}">']
    if env == "dev":
        seo.append('<meta name="robots" content="noindex">')
    else:
        seo += [
            '<meta name="robots" content="index, follow">',
            '<meta property="og:type" content="website">',
            '<meta property="og:site_name" content="All Media Downloader">',
            f'<meta property="og:url" content="{canonical}">',
            '<meta property="og:title" content="All Media Downloader">',
            '<meta property="og:description" content="Save every photo on a social media profile: '
            'Reddit, Tumblr, Pinterest and about 300 other sites, plus a Chrome extension for Instagram and X.">',
            f'<meta property="og:image" content="{canonical}screenshot.png">',
            '<meta name="twitter:card" content="summary_large_image">',
        ]
        (out / "sitemap.xml").write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
            f'  <url>\n    <loc>{canonical}</loc>\n  </url>\n</urlset>\n',
            encoding="utf-8")

    page = (src / "site" / "index.html").read_text(encoding="utf-8")
    for key, value in {
        "{{ENV_BANNER}}": banner,
        "{{OTHER_ENV}}": other,
        "{{TITLE_SUFFIX}}": suffix,
        "{{SEO_TAGS}}": "\n  ".join(seo),
        "{{EXT_ZIP}}": EXT_ZIP,
        "{{WEB_BUTTON}}": web_button,
        "{{COMMIT}}": html.escape(commit),
        "{{BRANCH}}": branch,
        "{{BUILT}}": built,
    }.items():
        page = page.replace(key, value)
    (out / "index.html").write_text(page, encoding="utf-8")
    (out / "site.css").write_bytes((src / "site" / "site.css").read_bytes())
    (out / "icon.svg").write_bytes((src / "web" / "static" / "icon.svg").read_bytes())
    for name in ("screenshot.png", "extension.png"):
        (out / name).write_bytes((src / "docs" / name).read_bytes())
    print(f"built {env} from {branch}@{commit} -> {out}")


def placeholder(out: Path) -> None:
    """Used for the root while main doesn't have the app yet."""
    out.mkdir(parents=True, exist_ok=True)
    (out / "index.html").write_text(
        '<!doctype html><meta charset="utf-8"><title>All Media Downloader</title>'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<body style="font:17px/1.5 Georgia,serif;background:#f3efe6;color:#1f1c17;max-width:640px;margin:60px auto;padding:0 20px">'
        '<h1>All Media Downloader</h1><p>No stable release yet. '
        '<a style="color:#a8361b" href="dev/">See the development build</a>.</p>',
        encoding="utf-8",
    )
    print(f"placeholder -> {out}")


if __name__ == "__main__":
    src, out, env = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
    if env not in ("prod", "dev"):
        sys.exit("env must be prod or dev")
    if (src / "site" / "index.html").is_file() and (src / EXT_DIR).is_dir():
        build(src, out, env)
    elif env == "prod":
        placeholder(out)
    else:
        sys.exit("develop has no site/ folder")
