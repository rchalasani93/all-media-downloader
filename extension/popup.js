"use strict";
const R = globalThis.AMDRules;
const $ = (id) => document.getElementById(id);
const SITE_NAMES = { instagram: "Instagram", reddit: "Reddit", x: "X", pinterest: "Pinterest", tumblr: "Tumblr" };

let tab = null;
let target = null;

function showError(text) {
  $("error").textContent = text;
  $("error").hidden = !text;
}

function render(job) {
  const running = job && (job.phase === "collecting" || job.phase === "downloading");
  const visible = job && (running || job.tabId === (tab && tab.id));
  $("form").hidden = Boolean(running || visible);
  $("progress").hidden = !visible;
  if (!visible) return;

  const label = {
    collecting: "Looking for photos and saving them",
    downloading: "Saving the last photos",
    done: "Finished",
    stopped: "Stopped",
    failed: "Stopped with a problem",
  }[job.phase] || job.phase;
  $("phase").textContent = label;
  $("phase").className = "phase " + job.phase;
  $("found").textContent = job.found;
  $("saved").textContent = job.saved;
  $("failed").textContent = job.failed;
  $("info").textContent = job.error || job.info || "";
  $("folder").textContent = job.folder;
  $("stop").hidden = !running;
  $("again").hidden = running;
}

async function init() {
  [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  target = R.detect(tab && tab.url ? tab.url : "");
  const name = SITE_NAMES[target.site] || target.site;
  if (target.site === "unsupported") {
    $("target").textContent = "Open a profile on Instagram, X, Reddit, Pinterest, Tumblr or another photo site.";
    $("go").disabled = true;
  } else if (target.mode === "none") {
    $("target").textContent = name;
    $("go").disabled = true;
  } else {
    $("target").textContent = `${name}: ${target.profile}`;
  }
  $("hint").textContent = target.hint || "";
  $("hint").hidden = !target.hint;
  $("minutes-row").hidden = target.mode !== "scroll";

  const { job } = await chrome.storage.session.get("job");
  render(job);
}

$("form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  showError("");
  const limitRaw = $("limit").value.trim();
  const limit = limitRaw ? parseInt(limitRaw, 10) : 100000;
  const minutes = parseInt($("minutes").value, 10) || 5;
  if (!(limit >= 1) || !(minutes >= 1 && minutes <= 60)) {
    showError("Check the numbers.");
    return;
  }
  $("go").disabled = true;
  const res = await chrome.runtime.sendMessage({
    type: "amd-begin",
    tabId: tab.id,
    options: { limit, minutes, imagesOnly: $("images-only").checked },
  });
  $("go").disabled = false;
  if (!res || !res.ok) showError((res && res.error) || "Couldn't start.");
});

$("stop").addEventListener("click", () => chrome.runtime.sendMessage({ type: "amd-stop" }));
$("open").addEventListener("click", () => chrome.downloads.showDefaultFolder());
$("again").addEventListener("click", async () => {
  await chrome.storage.session.set({ job: null });
  render(null);
});

chrome.storage.onChanged.addListener((changes, area) => {
  if (area === "session" && changes.job) render(changes.job.newValue);
});

init();
