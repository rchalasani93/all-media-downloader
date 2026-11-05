// Background worker: receives photo URLs from collector.js and downloads them
// into Downloads/AllMediaDownloader/<site>_<profile>/ with a gentle pace.
importScripts("rules.js");
const R = globalThis.AMDRules;

const CONCURRENCY = 3;
const GAP_MS = 350;

let job = null;          // one job at a time
let keepAlive = null;
const active = new Map(); // downloadId -> item

function blankJob(tabId, target, options) {
  return {
    tabId,
    site: target.site,
    profile: target.profile,
    folder: `AllMediaDownloader/${R.safeName(target.site)}_${R.safeName(target.profile)}`,
    options,
    phase: "collecting", // collecting | downloading | done | stopped | failed
    found: 0,
    saved: 0,
    failed: 0,
    info: "",
    error: "",
    queue: [],
    next: 1,
    started: Date.now(),
  };
}

function publish() {
  if (!job) return chrome.storage.session.set({ job: null });
  const { queue, ...visible } = job;
  return chrome.storage.session.set({ job: { ...visible, queued: queue.length + active.size } });
}

function startKeepAlive() {
  // Calling an extension API every 20s stops Chrome from suspending the worker mid-job.
  if (!keepAlive) keepAlive = setInterval(() => chrome.runtime.getPlatformInfo(), 20000);
}
function stopKeepAlive() {
  clearInterval(keepAlive);
  keepAlive = null;
}

function finishIfIdle() {
  if (!job) return;
  if (job.phase === "downloading" && job.queue.length === 0 && active.size === 0) {
    job.phase = job.error ? "failed" : "done";
    stopKeepAlive();
  }
  publish();
}

async function pump() {
  while (job && job.queue.length && active.size < CONCURRENCY && job.phase !== "stopped") {
    const item = job.queue.shift();
    const n = String(job.next++).padStart(4, "0");
    const filename = `${job.folder}/${n}.${R.extFromUrl(item.url)}`;
    try {
      const id = await chrome.downloads.download({ url: item.url, filename, conflictAction: "uniquify", saveAs: false });
      active.set(id, { ...item, filename });
    } catch (e) {
      job.failed++;
    }
    await new Promise((r) => setTimeout(r, GAP_MS));
  }
  finishIfIdle();
}

chrome.downloads.onChanged.addListener(async (delta) => {
  const item = active.get(delta.id);
  if (!item || !delta.state || !job) return;
  if (delta.state.current === "complete") {
    active.delete(delta.id);
    job.saved++;
    chrome.downloads.erase({ id: delta.id }); // keep Chrome's download bar clean; the file stays
  } else if (delta.state.current === "interrupted") {
    active.delete(delta.id);
    if (item.fallback) {
      job.queue.unshift({ url: item.fallback, fallback: null });
    } else {
      job.failed++;
    }
  }
  pump();
});

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.type === "amd-begin") {
    // From the popup: inject the collector into the tab and start it.
    (async () => {
      if (job && (job.phase === "collecting" || job.phase === "downloading")) {
        sendResponse({ ok: false, error: "A download is already running." });
        return;
      }
      const tab = await chrome.tabs.get(msg.tabId);
      const target = R.detect(tab.url);
      if (target.mode === "none" || target.site === "unsupported") {
        sendResponse({ ok: false, error: target.hint || "This page isn't supported." });
        return;
      }
      job = blankJob(msg.tabId, target, msg.options);
      active.clear();
      startKeepAlive();
      await publish();
      try {
        await chrome.scripting.executeScript({ target: { tabId: msg.tabId }, files: ["rules.js", "collector.js"] });
        await chrome.tabs.sendMessage(msg.tabId, { type: "amd-start", options: msg.options });
        sendResponse({ ok: true });
      } catch (e) {
        job.phase = "failed";
        job.error = "Couldn't run on this page. Reload the tab and try again.";
        stopKeepAlive();
        publish();
        sendResponse({ ok: false, error: job.error });
      }
    })();
    return true; // async response
  }

  if (msg.type === "amd-stop") {
    if (job) {
      job.phase = "stopped";
      job.queue = [];
      chrome.tabs.sendMessage(job.tabId, { type: "amd-stop" }).catch(() => {});
      stopKeepAlive();
      publish();
    }
    return;
  }

  // Messages below come from the collector in the job's tab only.
  if (!job || !sender.tab || sender.tab.id !== job.tabId || job.phase === "stopped") return;

  if (msg.type === "amd-found") {
    const items = (msg.items || []).filter((i) => i && /^https?:\/\//.test(i.url));
    job.found += items.length;
    job.queue.push(...items);
    if (msg.info) job.info = msg.info;
    publish();
    pump();
  } else if (msg.type === "amd-collect-done") {
    if (msg.error) job.error = msg.error;
    job.phase = "downloading";
    finishIfIdle();
    pump();
  }
});

chrome.tabs.onRemoved.addListener((tabId) => {
  if (job && job.tabId === tabId && job.phase === "collecting") {
    job.phase = "downloading";
    job.info = "The tab was closed, so it stopped looking for more photos.";
    finishIfIdle();
  }
});
