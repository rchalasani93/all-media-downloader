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

  async function instagram(target, opts) {
    const headers = { "x-ig-app-id": "936619743392459", "x-requested-with": "XMLHttpRequest", "x-csrftoken": cookie("csrftoken") };
    const info = await fetch(`/api/v1/users/web_profile_info/?username=${encodeURIComponent(target.profile)}`, { headers, credentials: "include" });
    if (info.status === 401 || info.status === 403) throw new Error("Instagram wants you logged in. Log in and try again.");
    if (info.status === 429) throw new Error("Instagram is rate limiting. Wait 15 minutes.");
    if (!info.ok) throw new Error(`Couldn't load the profile (${info.status}).`);
    const user = (await info.json()).data.user;
    if (user.is_private && !user.followed_by_viewer) throw new Error("This account is private and you don't follow it.");
    report([], `${user.edge_owner_to_timeline_media ? user.edge_owner_to_timeline_media.count : "?"} posts on profile`);

    let maxId = "";
    let total = 0;
    while (!stopped && total < opts.limit) {
      const q = new URLSearchParams({ count: "33" });
      if (maxId) q.set("max_id", maxId);
      const res = await fetch(`/api/v1/feed/user/${user.id}/?${q}`, { headers, credentials: "include" });
      if (res.status === 429) { report([], "Rate limited. Pausing 60 seconds."); await sleep(60000); continue; }
      if (!res.ok) throw new Error(`Instagram stopped answering (${res.status}). Saved what was found.`);
      const page = await res.json();
      const urls = [];
      for (const item of page.items || []) {
        for (const url of R.instagramItemUrls(item, opts.imagesOnly)) urls.push({ url, fallback: null });
      }
      total += report(urls.slice(0, opts.limit - total));
      if (!page.more_available || !page.next_max_id) break;
      maxId = page.next_max_id;
      await jitter(1500, 3000);
    }
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
