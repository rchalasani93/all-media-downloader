// Runs inside the profile tab (injected when you click "Save photos").
// It finds photo URLs and streams them to the background worker, which downloads them.
// Requests to Instagram/Reddit are same-origin from this tab, so your own login is used.
(function () {
  "use strict";
  if (window.__amdCollector) return;
  window.__amdCollector = true;

  const R = globalThis.AMDRules;
  let stopped = false;
  const seen = new Set();

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const jitter = (min, max) => sleep(min + Math.random() * (max - min));
  const send = (msg) => chrome.runtime.sendMessage(msg).catch(() => {});

  function report(urls, info) {
    const fresh = [];
    for (const u of urls) {
      if (!u || seen.has(u.url)) continue;
      seen.add(u.url);
      fresh.push(u);
    }
    if (fresh.length || info) send({ type: "amd-found", items: fresh, info: info || "" });
    return fresh.length;
  }

  function cookie(name) {
    const m = document.cookie.match(new RegExp("(?:^|; )" + name + "=([^;]*)"));
    return m ? decodeURIComponent(m[1]) : "";
  }

  // ---------- Instagram ----------
  // Instagram's web endpoints change often, so this tries several ways in order and
  // records what happened, so a failure says which step broke.

  function igHeaders() {
    let claim = "0";
    try { claim = sessionStorage.getItem("www-claim-v2") || "0"; } catch { /* storage blocked */ }
    return {
      "x-ig-app-id": "936619743392459",
      "x-asbd-id": "359341",
      "x-ig-www-claim": claim,
      "x-csrftoken": cookie("csrftoken"),
      "x-requested-with": "XMLHttpRequest",
      "accept": "*/*",
    };
  }

  async function igGet(path) {
    const res = await fetch(path, { headers: igHeaders(), credentials: "include" });
    let body = null;
    if (res.ok) {
      try { body = await res.json(); } catch { body = null; }
    }
    return { status: res.status, ok: res.ok && body !== null, body };
  }

  async function igUserId(username, notes) {
    const r = await igGet(`/api/v1/users/web_profile_info/?username=${encodeURIComponent(username)}`);
    notes.push(`profile info ${r.status}`);
    if (r.status === 401) throw new Error("Instagram wants you logged in. Log in and try again.");
    if (r.ok && r.body.data && r.body.data.user) {
      const u = r.body.data.user;
      if (u.is_private && !u.followed_by_viewer) throw new Error("This account is private and you don't follow it.");
      return u.id;
    }
    // The profile page itself carries the id in its embedded JSON.
    const fromDom = R.instagramIdFromHtml(document.documentElement.innerHTML, username);
    if (fromDom) { notes.push("id from page"); return fromDom; }
    try {
      const page = await fetch(`/${encodeURIComponent(username)}/`, { credentials: "include" });
      const id = R.instagramIdFromHtml(await page.text(), username);
      notes.push(`page html ${page.status}${id ? " (id found)" : ""}`);
      return id;
    } catch {
      notes.push("page html failed");
      return null;
    }
  }

  async function igFeed(userId, opts, notes) {
    let maxId = "";
    let total = 0;
    let pages = 0;
    while (!stopped && total < opts.limit) {
      const q = new URLSearchParams({ count: "33" });
      if (maxId) q.set("max_id", maxId);
      const r = await igGet(`/api/v1/feed/user/${userId}/?${q}`);
      if (r.status === 429) { report([], "Rate limited. Pausing 60 seconds."); await sleep(60000); continue; }
      if (!r.ok) {
        notes.push(`feed ${r.status}`);
        return pages > 0 ? total : -1; // -1 = feed unusable, try the grid
      }
      pages++;
      const urls = [];
      for (const item of r.body.items || []) {
        for (const url of R.instagramItemUrls(item, opts.imagesOnly)) urls.push({ url, fallback: null });
      }
      total += report(urls.slice(0, opts.limit - total), `Reading the feed, page ${pages}`);
      if (!r.body.more_available || !r.body.next_max_id) break;
      maxId = r.body.next_max_id;
      await jitter(1500, 3000);
    }
    return total;
  }

  function gridPosts() {
    const out = [];
    for (const a of document.querySelectorAll('main a[href*="/p/"], main a[href*="/reel/"]')) {
      const m = a.getAttribute("href").match(/\/(p|reel)\/([A-Za-z0-9_-]+)/);
      if (!m) continue;
      const img = a.querySelector("img");
      out.push({ code: m[2], reel: m[1] === "reel", thumb: img ? biggestSrc(img) : null });
    }
    return out;
  }

  // Scroll the profile grid, then load each post's full-size media.
  async function igGrid(opts, notes) {
    const posts = new Map();
    const deadline = Date.now() + opts.minutes * 60000;
    let idle = 0;
    while (!stopped && posts.size < opts.limit && Date.now() < deadline && idle < 6) {
      const before = posts.size;
      for (const p of gridPosts()) if (!posts.has(p.code)) posts.set(p.code, p);
      report([], `Scrolling the profile: ${posts.size} posts found`);
      window.scrollBy(0, Math.round(window.innerHeight * 0.9));
      await jitter(1500, 2500);
      idle = posts.size === before ? idle + 1 : 0;
    }
    window.scrollTo(0, 0);

    let total = 0;
    let n = 0;
    let infoWorks = true;
    let misses = 0;
    for (const p of posts.values()) {
      if (stopped || total >= opts.limit) break;
      n++;
      if (p.reel && opts.imagesOnly) continue;
      let urls = [];
      if (infoWorks) {
        const id = R.shortcodeToId(p.code);
        const r = id ? await igGet(`/api/v1/media/${id}/info/`) : { status: 0, ok: false };
        if (r.status === 429) { report([], "Rate limited. Pausing 60 seconds."); await sleep(60000); }
        if (r.ok && r.body.items && r.body.items[0]) {
          urls = R.instagramItemUrls(r.body.items[0], opts.imagesOnly);
          misses = 0;
        } else if (++misses >= 3) {
          notes.push(`post info ${r.status}`);
          infoWorks = false; // three misses in a row: stop asking, use the grid images
        }
        await jitter(1200, 2200);
      }
      if (!urls.length && p.thumb) {
        const up = R.upgrade(p.thumb);
        if (up) urls = [up.url];
      }
      total += report(urls.slice(0, opts.limit - total).map((url) => ({ url, fallback: null })),
        `Loading posts: ${n} of ${posts.size}${infoWorks ? "" : " (grid-size images)"}`);
    }
    return total;
  }

  async function instagram(target, opts) {
    const notes = [];
    const userId = await igUserId(target.profile, notes);
    let got = -1;
    if (userId) got = await igFeed(userId, opts, notes);
    if (got < 0 && !stopped) {
      report([], "Feed unavailable, reading the profile grid instead");
      got = await igGrid(opts, notes);
    }
    if (got <= 0 && !stopped) {
      throw new Error(`No photos found. Steps tried: ${notes.join(", ")}. Make sure you're logged in and the profile grid shows posts.`);
    }
    if (notes.length) report([], `Done. Steps: ${notes.join(", ")}`);
  }

  async function reddit(target, opts) {
    let after = "";
    let total = 0;
    for (let pageNo = 0; !stopped && total < opts.limit && pageNo < 50; pageNo++) {
      const q = new URLSearchParams({ limit: "100", raw_json: "1" });
      if (after) q.set("after", after);
      const res = await fetch(`${target.api}?${q}`, { credentials: "include" });
      if (res.status === 429) { report([], "Rate limited. Pausing 60 seconds."); await sleep(60000); pageNo--; continue; }
      if (!res.ok) throw new Error(`Reddit answered ${res.status}.`);
      const listing = (await res.json()).data;
      const urls = [];
      for (const child of listing.children || []) {
        for (const url of R.redditPostUrls(child, opts.imagesOnly)) urls.push({ url, fallback: null });
      }
      total += report(urls.slice(0, opts.limit - total));
      if (!listing.after) break;
      after = listing.after;
      await jitter(1000, 2000);
    }
  }

  function biggestSrc(img) {
    if (img.srcset) {
      let best = null, bestW = 0;
      for (const part of img.srcset.split(",")) {
        const [u, w] = part.trim().split(/\s+/);
        const n = parseInt(w, 10) || 0;
        if (u && n >= bestW) { best = u; bestW = n; }
      }
      if (best) return new URL(best, location.href).href;
    }
    return img.currentSrc || img.src;
  }

  function collectVisible() {
    const out = [];
    for (const img of document.images) {
      const w = Math.max(img.naturalWidth, img.width), h = Math.max(img.naturalHeight, img.height);
      if (w < 150 || h < 150) continue; // avatars, icons, emoji
      const src = biggestSrc(img);
      if (!src || src.startsWith("data:") || /\.svg(\?|$)/i.test(src)) continue;
      const up = R.upgrade(src);
      if (up) out.push(up);
    }
    return out;
  }

  async function scroll(target, opts) {
    const deadline = Date.now() + opts.minutes * 60000;
    let idle = 0, total = 0, lastHeight = 0;
    while (!stopped && total < opts.limit && Date.now() < deadline && idle < 6) {
      const added = report(collectVisible().slice(0, opts.limit - total));
      total += added;
      window.scrollBy(0, Math.round(window.innerHeight * 0.9));
      await jitter(1200, 2000);
      const h = document.documentElement.scrollHeight;
      idle = added === 0 && h === lastHeight ? idle + 1 : 0;
      lastHeight = h;
    }
    if (Date.now() >= deadline) report([], "Reached the scroll time limit.");
  }

  chrome.runtime.onMessage.addListener((msg) => {
    if (msg.type === "amd-stop") stopped = true;
    if (msg.type !== "amd-start") return;
    stopped = false;
    seen.clear();
    const target = R.detect(location.href);
    const run = target.mode === "api" ? (target.site === "instagram" ? instagram : reddit) : scroll;
    run(target, msg.options)
      .then(() => send({ type: "amd-collect-done" }))
      .catch((e) => send({ type: "amd-collect-done", error: String(e.message || e) }));
  });
})();
