"use strict";
// All server data is written with textContent / attributes only. No innerHTML.

const csrf = document.querySelector('meta[name="csrf-token"]').content;
const $ = (id) => document.getElementById(id);

const form = $("job-form");
const startBtn = $("start");
const formError = $("form-error");
const current = $("current");

let activeId = null;
let pollTimer = null;

async function api(path, options = {}) {
  const res = await fetch(path, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      "X-CSRF-Token": csrf,
      ...(options.headers || {}),
    },
  });
  let body = null;
  try { body = await res.json(); } catch { /* empty body */ }
  if (res.status === 401) { window.location.href = "/login"; throw new Error("Signed out"); }
  if (!res.ok) throw new Error((body && body.error) || `Request failed (${res.status})`);
  return body;
}

function formatBytes(n) {
  if (n < 1024) return `${n} B`;
  if (n < 1024 ** 2) return `${(n / 1024).toFixed(0)} KB`;
  if (n < 1024 ** 3) return `${(n / 1024 ** 2).toFixed(1)} MB`;
  return `${(n / 1024 ** 3).toFixed(2)} GB`;
}

function formatTime(s) {
  if (s < 60) return `${s}s`;
  return `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s`;
}

const STATUS = {
  running: "Downloading",
  done: "Finished",
  failed: "Couldn't download",
  cancelled: "Stopped",
};

function render(job) {
  current.hidden = false;
  const status = $("status-text");
  status.textContent = "";
  status.className = "status " + job.state;
  if (job.state === "running") {
    const dot = document.createElement("span");
    dot.className = "pulse";
    status.append(dot);
  }
  let label = STATUS[job.state] || job.state;
  if (job.state === "failed") {
    const hint = lastError(job.log);
    if (hint) label += `: ${hint}`;
  }
  status.append(label);

  $("status-url").textContent = job.url;
  $("stat-count").textContent = job.count;
  $("stat-size").textContent = formatBytes(job.bytes);
  $("stat-time").textContent = formatTime(job.elapsed);
  $("expiry").textContent = job.expires_in == null
    ? ""
    : `Kept on the server for ${Math.ceil(job.expires_in / 60)} more minutes.`;

  const running = job.state === "running";
  $("cancel").hidden = !running;
  $("zip").hidden = running || job.count === 0;
  $("zip").href = `/jobs/${job.id}/zip`;

  const frames = $("frames");
  const wanted = job.recent.join("\n");
  if (frames.dataset.names !== wanted) {
    frames.dataset.names = wanted;
    frames.replaceChildren(...job.recent.map((name, i) => {
      const li = document.createElement("li");
      const img = document.createElement("img");
      img.loading = "lazy";
      img.alt = name;
      img.src = `/jobs/${job.id}/file/${encodeURIComponent(name)}`;
      const num = document.createElement("span");
      num.textContent = String(job.count - i).padStart(2, "0");
      li.append(img, num);
      return li;
    }));
  }
  $("frames-empty").hidden = job.recent.length > 0;
  $("frames-empty").textContent = running
    ? "Photos appear here as they come in."
    : "No photos were saved.";

  const log = $("log");
  const atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 4;
  log.textContent = job.log.join("\n") || "Waiting for the first response from the site.";
  if (atBottom) log.scrollTop = log.scrollHeight;

  startBtn.disabled = running;
}

// Turn gallery-dl's error lines into one short, readable reason.
function lastError(lines) {
  const err = [...lines].reverse().find((l) => /error|warning/i.test(l));
  if (!err) return "";
  const msg = err.replace(/^\[[^\]]*\]\[[^\]]*\]\s*/, "");
  if (/login|log in|authenticat|401|403/i.test(msg)) return "the site wants a login or blocks servers. Try the Chrome extension.";
  if (/unsupported url/i.test(msg)) return "this site or link type isn't supported.";
  if (/429|rate/i.test(msg)) return "the site is rate limiting. Wait a few minutes and try again.";
  return msg.slice(0, 160);
}

async function poll() {
  if (!activeId) return;
  try {
    const job = await api(`/api/jobs/${activeId}`);
    render(job);
    if (job.state === "running") {
      pollTimer = setTimeout(poll, 1500);
    } else {
      loadHistory();
    }
  } catch (e) {
    pollTimer = setTimeout(poll, 4000);
  }
}

form.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  formError.hidden = true;
  const url = $("url").value.trim();
  if (!url) {
    formError.textContent = "Paste a profile link first.";
    formError.hidden = false;
    return;
  }
  startBtn.disabled = true;
  startBtn.textContent = "Starting";
  try {
    const job = await api("/api/jobs", {
      method: "POST",
      body: JSON.stringify({
        url,
        limit: $("limit").value || null,
        images_only: $("images-only").checked,
      }),
    });
    activeId = job.id;
    $("frames").dataset.names = "";
    clearTimeout(pollTimer);
    render(job);
    poll();
  } catch (e) {
    formError.textContent = e.message;
    formError.hidden = false;
    startBtn.disabled = false;
  } finally {
    startBtn.textContent = "Download photos";
  }
});

$("cancel").addEventListener("click", async () => {
  if (!activeId) return;
  try { render(await api(`/api/jobs/${activeId}/cancel`, { method: "POST" })); } catch { /* next poll catches up */ }
});

async function loadHistory() {
  try {
    const all = await api("/api/jobs");
    const past = all.filter((j) => j.id !== activeId);
    $("history-wrap").hidden = past.length === 0;
    $("history").replaceChildren(...past.map((j) => {
      const tr = document.createElement("tr");
      const cells = [j.url, String(j.count), STATUS[j.state] || j.state];
      for (const text of cells) {
        const td = document.createElement("td");
        td.textContent = text;
        tr.append(td);
      }
      const td = document.createElement("td");
      if (j.count > 0 && j.state !== "running") {
        const a = document.createElement("a");
        a.href = `/jobs/${j.id}/zip`;
        a.textContent = "ZIP";
        td.append(a);
      }
      tr.append(td);
      return tr;
    }));
  } catch { /* history is optional */ }
}

// Resume the view if a download was running when the page was reloaded.
(async () => {
  try {
    const all = await api("/api/jobs");
    const running = all.find((j) => j.state === "running");
    if (running) { activeId = running.id; poll(); }
    else if (all.length) { activeId = all[0].id; render(await api(`/api/jobs/${activeId}`)); }
  } catch { /* first run */ }
  loadHistory();
})();
