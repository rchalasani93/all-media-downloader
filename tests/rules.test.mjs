// node --test tests/
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const ctx = { URL, URLSearchParams };
ctx.globalThis = ctx;
vm.runInNewContext(readFileSync(new URL("../extension/rules.js", import.meta.url), "utf8"), ctx);
const R = ctx.AMDRules;

test("detects profiles per site", () => {
  assert.deepEqual({ ...R.detect("https://www.instagram.com/natgeo/") }, { site: "instagram", profile: "natgeo", mode: "api" });
  assert.equal(R.detect("https://www.instagram.com/p/abc123/").mode, "none");
  assert.equal(R.detect("https://www.reddit.com/user/spez/submitted/").api, "/user/spez/submitted.json");
  assert.equal(R.detect("https://old.reddit.com/r/EarthPorn/").api, "/r/EarthPorn/new.json");
  const x = R.detect("https://x.com/NASA/media");
  assert.equal(x.site, "x"); assert.equal(x.profile, "NASA"); assert.equal(x.hint, "");
  assert.match(R.detect("https://x.com/NASA").hint, /Media tab/);
  assert.equal(R.detect("https://x.com/home").mode, "none");
  assert.equal(R.detect("https://staff.tumblr.com/").profile, "staff");
  assert.equal(R.detect("https://www.pinterest.com/pinterest/boards/").site, "pinterest");
  assert.equal(R.detect("chrome://extensions").site, "unsupported");
  assert.equal(R.detect("https://www.flickr.com/photos/nasa").mode, "scroll");
});

test("upgrades X images to originals and skips avatars", () => {
  const up = R.upgrade("https://pbs.twimg.com/media/GabcXYZ?format=jpg&name=small");
  assert.equal(up.url, "https://pbs.twimg.com/media/GabcXYZ?format=jpg&name=orig");
  assert.equal(up.fallback, "https://pbs.twimg.com/media/GabcXYZ?format=jpg&name=large");
  assert.equal(R.upgrade("https://pbs.twimg.com/profile_images/1/a_normal.jpg"), null);
});

test("upgrades Pinterest images with a fallback", () => {
  const up = R.upgrade("https://i.pinimg.com/236x/aa/bb/cc/aabbcc.jpg");
  assert.equal(up.url, "https://i.pinimg.com/originals/aa/bb/cc/aabbcc.jpg");
  assert.equal(up.fallback, "https://i.pinimg.com/736x/aa/bb/cc/aabbcc.jpg");
});

test("rejects non-http URLs", () => {
  assert.equal(R.upgrade("javascript:alert(1)"), null);
  assert.equal(R.upgrade("data:image/png;base64,xx"), null);
});

test("file extensions and safe folder names", () => {
  assert.equal(R.extFromUrl("https://pbs.twimg.com/media/X?format=png&name=orig"), "png");
  assert.equal(R.extFromUrl("https://i.redd.it/abc.jpeg"), "jpg");
  assert.equal(R.extFromUrl("https://scontent.cdninstagram.com/v/t51/123_n.jpg?stp=x"), "jpg");
  assert.equal(R.extFromUrl("https://example.com/no-extension"), "jpg");
  assert.equal(R.safeName("../../etc/passwd"), "etc-passwd");
  assert.equal(R.safeName(""), "profile");
});

test("Instagram items: largest image, carousels, videos skipped", () => {
  const img = (w, url) => ({ width: w, height: w, url });
  const item = {
    media_type: 8,
    carousel_media: [
      { media_type: 1, image_versions2: { candidates: [img(320, "s"), img(1080, "big1")] } },
      { media_type: 2, video_versions: [{ url: "vid" }], image_versions2: { candidates: [img(1080, "poster")] } },
      { media_type: 1, image_versions2: { candidates: [img(1440, "big2"), img(640, "m")] } },
    ],
  };
  assert.deepEqual([...R.instagramItemUrls(item, true)], ["big1", "big2"]);
  assert.deepEqual([...R.instagramItemUrls(item, false)], ["big1", "vid", "big2"]);
});

test("Reddit posts: direct images, galleries in order, previews", () => {
  assert.deepEqual([...R.redditPostUrls({ data: { url: "https://i.redd.it/a.jpg" } }, true)], ["https://i.redd.it/a.jpg"]);
  const gallery = {
    data: {
      is_gallery: true,
      gallery_data: { items: [{ media_id: "b" }, { media_id: "a" }] },
      media_metadata: {
        a: { status: "valid", s: { u: "https://preview.redd.it/a.jpg?width=1&amp;s=x" } },
        b: { status: "valid", s: { u: "https://preview.redd.it/b.jpg?s=y" } },
      },
    },
  };
  assert.deepEqual([...R.redditPostUrls(gallery, true)], ["https://preview.redd.it/b.jpg?s=y", "https://preview.redd.it/a.jpg?width=1&s=x"]);
  assert.deepEqual([...R.redditPostUrls({ data: { url: "https://www.youtube.com/watch?v=1" } }, true)], []);
});

test("Instagram shortcode to media id", () => {
  assert.equal(R.shortcodeToId("B"), "1");
  assert.equal(R.shortcodeToId("BAA"), "4096");
  assert.equal(R.shortcodeToId("_"), "63");
  assert.equal(R.shortcodeToId("AAAAAAAAAAB"), "1");
  assert.equal(R.shortcodeToId("AAAAAAAAAABextra"), "1"); // suffix after 11 chars ignored
  assert.equal(R.shortcodeToId("bad/code"), null);
});

test("Instagram profile id from page HTML", () => {
  assert.equal(R.instagramIdFromHtml('..."id":"123456789","username":"some.user"...', "some.user"), "123456789");
  assert.equal(R.instagramIdFromHtml('{"username":"some.user","full_name":"x","pk":"987654321"}', "some.user"), "987654321");
  assert.equal(R.instagramIdFromHtml('<meta content="instagram://user?id=55555">', "x"), "55555");
  assert.equal(R.instagramIdFromHtml('"profilePage_42424242"', "x"), "42424242");
  assert.equal(R.instagramIdFromHtml('"id":"111","username":"other"', "some.user"), null);
});
