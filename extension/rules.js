// Site detection and image URL rules. Shared by collector.js and the Node tests.
// Loaded as a plain script (no modules) so chrome.scripting can inject it.
(function (root) {
  "use strict";

  const IG_RESERVED = new Set(["p", "reel", "reels", "explore", "stories", "direct", "accounts", "tv", "about", "legal", "developer"]);
  const X_RESERVED = new Set(["home", "explore", "notifications", "messages", "i", "settings", "search", "compose", "login", "signup"]);

  function detect(href) {
    let u;
    try { u = new URL(href); } catch { return { site: "unsupported" }; }
    if (!/^https?:$/.test(u.protocol)) return { site: "unsupported" };
    const host = u.hostname.replace(/^www\./, "").toLowerCase();
    const parts = u.pathname.split("/").filter(Boolean);

    if (host === "instagram.com") {
      const postIdx = parts.findIndex((x) => ["p", "reel", "reels", "tv"].includes(x.toLowerCase()));
      if (postIdx >= 0 && parts[postIdx + 1] && postIdx <= 1) {
        const code = parts[postIdx + 1];
        return { site: "instagram", profile: "post-" + code, mode: "post", code };
      }
      if (parts[0] && !IG_RESERVED.has(parts[0].toLowerCase())) {
        return { site: "instagram", profile: parts[0], mode: "api" };
      }
      return { site: "instagram", mode: "none", hint: "Open a profile page, like instagram.com/natgeo" };
    }
    if (host === "reddit.com" || host === "old.reddit.com" || host === "new.reddit.com") {
      const kind = (parts[0] || "").toLowerCase();
      const cIdx = parts.findIndex((x) => x === "comments");
      if (cIdx >= 0 && /^[a-z0-9]+$/i.test(parts[cIdx + 1] || "")) {
        const id = parts[cIdx + 1];
        return { site: "reddit", profile: "post-" + id, mode: "post", api: `/comments/${id}.json` };
      }
      if ((kind === "user" || kind === "u") && parts[1]) {
        return { site: "reddit", profile: parts[1], mode: "api", api: `/user/${encodeURIComponent(parts[1])}/submitted.json` };
      }
      if (kind === "r" && parts[1]) {
        return { site: "reddit", profile: "r-" + parts[1], mode: "api", api: `/r/${encodeURIComponent(parts[1])}/new.json` };
      }
      return { site: "reddit", mode: "none", hint: "Open a user or subreddit page" };
    }
    if (host === "x.com" || host === "twitter.com" || host === "mobile.twitter.com") {
      if (parts[0] && parts[1] === "status" && /^\d+$/.test(parts[2] || "")) {
        return { site: "x", profile: `${parts[0]}-${parts[2]}`, mode: "post", statusId: parts[2] };
      }
      if (parts[0] && !X_RESERVED.has(parts[0].toLowerCase())) {
        const onMedia = parts[1] === "media";
        return {
          site: "x", profile: parts[0], mode: "scroll",
          hint: onMedia ? "" : "Tip: open the profile's Media tab first so only photos load",
        };
      }
      return { site: "x", mode: "none", hint: "Open a profile page, like x.com/NASA/media" };
    }
    if (host.endsWith("pinterest.com") || /^([a-z]{2}\.)?pinterest\.[a-z.]+$/.test(host)) {
      return { site: "pinterest", profile: parts.slice(0, 2).join("-") || "pinterest", mode: "scroll" };
    }
    if (host.endsWith("tumblr.com")) {
      const blog = host === "tumblr.com" ? (parts[0] || "tumblr") : host.split(".")[0];
      return { site: "tumblr", profile: blog, mode: "scroll" };
    }
    return { site: host || "site", profile: parts.slice(0, 2).join("-") || host, mode: "scroll" };
  }

  // Ask the CDN for the biggest version of an image. Returns { url, fallback }.
  function upgrade(raw) {
    let u;
    try { u = new URL(raw); } catch { return null; }
    if (!/^https?:$/.test(u.protocol)) return null;
    const host = u.hostname;

    if (host === "pbs.twimg.com") {
      if (!u.pathname.startsWith("/media/")) return null; // skip avatars, banners, emoji
      const fmt = u.searchParams.get("format") || (u.pathname.match(/\.(jpe?g|png|webp)$/i) || [])[1] || "jpg";
      const base = u.origin + u.pathname.replace(/\.(jpe?g|png|webp)$/i, "");
      return { url: `${base}?format=${fmt}&name=orig`, fallback: `${base}?format=${fmt}&name=large` };
    }
    if (host === "i.pinimg.com") {
      const m = u.pathname.match(/^\/(\d+x\d*|\d+x)\/(.+)$/);
      if (m) {
        return { url: `${u.origin}/originals/${m[2]}`, fallback: `${u.origin}/736x/${m[2]}` };
      }
      return { url: u.href, fallback: null };
    }
    if (host === "preview.redd.it" || host === "external-preview.redd.it") {
      // preview links need their signature; keep as is
      return { url: u.href.replace(/&amp;/g, "&"), fallback: null };
    }
    return { url: u.href, fallback: null };
  }

  function extFromUrl(raw) {
    try {
      const u = new URL(raw);
      const fmt = u.searchParams.get("format");
      if (fmt && /^(jpe?g|png|webp|gif|avif)$/i.test(fmt)) return fmt.toLowerCase().replace("jpeg", "jpg");
      const m = u.pathname.match(/\.(jpe?g|png|webp|gif|avif|mp4|webm|mov)$/i);
      if (m) return m[1].toLowerCase().replace("jpeg", "jpg");
    } catch { /* fall through */ }
    return "jpg";
  }

  function safeName(s) {
    return String(s || "profile").replace(/[^A-Za-z0-9._-]+/g, "-").replace(/^[-.]+|[-.]+$/g, "").slice(0, 80) || "profile";
  }

  // Instagram feed item -> list of media URLs (largest candidate first in IG's arrays).
  function instagramItemUrls(item, imagesOnly) {
    const out = [];
    const nodes = item.media_type === 8 && Array.isArray(item.carousel_media) ? item.carousel_media : [item];
    for (const n of nodes) {
      if (n.media_type === 2) {
        if (!imagesOnly && n.video_versions && n.video_versions[0]) out.push(n.video_versions[0].url);
        continue;
      }
      const c = n.image_versions2 && n.image_versions2.candidates;
      if (c && c.length) {
        const best = c.reduce((a, b) => ((b.width || 0) * (b.height || 0) > (a.width || 0) * (a.height || 0) ? b : a));
        out.push(best.url);
      }
    }
    return out;
  }

  // Reddit listing child -> list of image URLs.
  function redditPostUrls(post, imagesOnly) {
    const d = post && post.data ? post.data : post;
    if (!d) return [];
    if (d.is_gallery && d.media_metadata) {
      const order = d.gallery_data && d.gallery_data.items ? d.gallery_data.items.map((i) => i.media_id) : Object.keys(d.media_metadata);
      return order
        .map((id) => d.media_metadata[id])
        .filter((m) => m && m.status === "valid" && m.s)
        .map((m) => (m.s.u || m.s.gif || "").replace(/&amp;/g, "&"))
        .filter(Boolean);
    }
    const url = (d.url_overridden_by_dest || d.url || "").replace(/&amp;/g, "&");
    if (/^https:\/\/(i\.redd\.it|i\.imgur\.com)\/[^?#]+\.(jpe?g|png|gif|webp)$/i.test(url)) return [url];
    if (!imagesOnly && d.is_video && d.media && d.media.reddit_video && d.media.reddit_video.fallback_url) {
      return [d.media.reddit_video.fallback_url];
    }
    if (d.post_hint === "image" && d.preview && d.preview.images && d.preview.images[0]) {
      return [d.preview.images[0].source.url.replace(/&amp;/g, "&")];
    }
    return [];
  }

  // Instagram post shortcode (the part after /p/) -> numeric media id, as a string.
  const IG_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
  function shortcodeToId(code) {
    const clean = String(code || "").slice(0, 11); // longer codes carry a suffix that isn't part of the id
    if (!/^[A-Za-z0-9_-]+$/.test(clean)) return null;
    let id = 0n;
    for (const ch of clean) id = id * 64n + BigInt(IG_ALPHABET.indexOf(ch));
    return id.toString();
  }

  // Find the profile's numeric id in the HTML/JSON Instagram embeds in the page.
  function instagramIdFromHtml(htmlText, username) {
    const u = String(username).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    const patterns = [
      new RegExp(`"id":"(\\d{3,})","username":"${u}"`, "i"),
      new RegExp(`"username":"${u}"[^{}]{0,400}?"(?:pk|id|pk_id)":"?(\\d{3,})`, "i"),
      /"profilePage_(\d{3,})"/,
      /"profile_id":"?(\d{3,})/,
      /"target_id":"?(\d{3,})/,
      /instagram:\/\/user\?id=(\d{3,})/,
    ];
    for (const re of patterns) {
      const m = htmlText.match(re);
      if (m) return m[1];
    }
    return null;
  }

  root.AMDRules = {
    detect, upgrade, extFromUrl, safeName, instagramItemUrls, redditPostUrls, shortcodeToId, instagramIdFromHtml,
  };
})(typeof globalThis !== "undefined" ? globalThis : this);
