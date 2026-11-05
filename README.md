# All Media Downloader

![Contact Sheet, the app's local web page](docs/screenshot.png)

**Contact Sheet** is a small web page that runs on your own computer. Paste a social media profile link and it saves every photo on that profile to a folder, then gives you a ZIP. It uses [gallery-dl](https://github.com/mikf/gallery-dl) for the downloading, so it works with Instagram, X/Twitter, Reddit, Tumblr, Pinterest and [about 300 other sites](https://github.com/mikf/gallery-dl/blob/master/docs/supportedsites.md).

## Start it

You need Python 3.10 or newer.

- **Windows:** double-click `start.bat`
- **macOS / Linux:** run `./start.sh`

The first run sets up a private Python environment in `.venv` and installs everything. After that, the page opens at http://127.0.0.1:8765. Press Ctrl+C in the terminal window to stop it.

Manual start: `pip install -r requirements.txt` and then `python app.py`.

## Using it

1. Paste a profile link, such as `https://www.instagram.com/natgeo/`, `https://x.com/NASA`, `https://www.reddit.com/user/someone/submitted/` or `https://someblog.tumblr.com`.
2. **Use my login from:** Instagram and X show almost nothing to logged-out visitors. Log in to the site in Chrome or Firefox, pick that browser here, and the app reads its cookies locally. On Windows, close Chrome first if reading its cookies fails. Firefox is the most reliable choice.
3. **Stop after:** set a number to test with a few files first.
4. Photos appear in the contact sheet as they arrive. When it finishes, use **Download ZIP** or **Open folder**.

Files are saved in `downloads/<site>_<profile>_<date>/`. To save them somewhere else, set `CONTACT_SHEET_DIR` before starting. Set `CONTACT_SHEET_PORT` to use a port other than 8765.

## When something fails

| Message | What to do |
|---|---|
| Site wants a login | Pick your browser under "Use my login from". Check that you're logged in there. |
| Rate limiting / 429 | Wait 10 to 30 minutes. Instagram is the strictest. Use "Stop after" for big profiles. |
| Unsupported URL | Use the main profile link, not a single post or a shortened link. |
| Something broke after a site changed | Update gallery-dl with `.venv/bin/pip install -U gallery-dl` (on Windows: `.venv\Scripts\pip install -U gallery-dl`). Sites change often and gallery-dl ships fixes quickly. |

## For your own accounts

If you only want a backup of your own photos, each platform's official export gets you everything, including originals and captions:
- Instagram: Accounts Center → Your information and permissions → Download your information
- X: Settings → Your account → Download an archive of your data
- Reddit: Settings → Privacy → Request your data

## Security notes

- The app only listens on 127.0.0.1, so other devices on your network can't reach it. Don't change that: the app can use your browser logins.
- Every request that changes something needs a token that's created fresh each time the app starts. The app also checks the Host and Origin headers. This stops other websites you have open from starting downloads through it, including through DNS-rebinding tricks.
- Links are checked on the server: only http(s) links, no local network addresses. gallery-dl is called with an argument list, never through a shell.
- Downloaded images are served only from the job folder (protected against path traversal), only as image types (SVG is excluded), and with a sandbox CSP.
- Debug mode is off. No accounts, no passwords and no API keys are stored.
- Downloads run one at a time with a 1 to 2.5 second gap between requests, so your account doesn't get flagged.

Please respect the people whose photos you save, and each site's terms of use.

## Branches and environments

| Branch | Published to | What's there |
|---|---|---|
| `develop` | `https://rchalasani93.github.io/all-media-downloader/dev/` | Download page plus a ZIP built from `develop` |
| `main` | `https://rchalasani93.github.io/all-media-downloader/` | Download page plus a ZIP built from `main` |

GitHub Pages only serves static files, so the published site is the download page. The app itself always runs on your computer.

Every push runs the tests and a dependency audit. The site is then rebuilt from both branches (`.github/workflows/pages.yml`).

## Development

```
pip install -r requirements.txt -r requirements-dev.txt
pytest
python app.py
```
