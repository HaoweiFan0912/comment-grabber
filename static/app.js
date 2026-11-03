const $ = (id) => document.getElementById(id);
const form = $("form");

const FIELDS = [
  "topic", "title_must_include", "comment_must_include", "output_dir", "max_videos", "max_comments", "order",
  "language", "region", "published_after", "published_before", "include_replies",
];
const MORE_FILTERS = ["language", "region", "published_after", "published_before", "include_replies"];
const STORE_KEY = "comment-grabber:form";
const OUTCOMES = {
  done: { title: "Done", tone: "" },
  stopped: { title: "Stopped", tone: "warn" },
  empty: { title: "No videos found", tone: "warn" },
  failed: { title: "Something went wrong", tone: "error" },
};

let config = { daily_quota: 10000, can_pick_folder: false, can_reveal: false };
let running = false;
let logCount = 0;
let pollTimer = null;

const fmt = (n) => n.toLocaleString("en-US");

async function api(path, body) {
  const init = body === undefined
    ? {}
    : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  const res = await fetch(path, init);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`);
  return data;
}

// ---- Form ----

function readForm() {
  const data = {};
  for (const name of FIELDS) {
    const el = form.elements[name];
    if (el.type === "checkbox") data[name] = el.checked;
    else if (el.type === "number") data[name] = Number(el.value);
    else data[name] = el.value.trim();
  }
  return data;
}

function fillForm(data) {
  for (const name of FIELDS) {
    if (!(name in data)) continue;
    const el = form.elements[name];
    if (el.type === "checkbox") el.checked = Boolean(data[name]);
    else el.value = data[name];
  }
}

function saveForm() {
  try { localStorage.setItem(STORE_KEY, JSON.stringify(readForm())); } catch {}
}

function restoreForm() {
  try {
    const saved = JSON.parse(localStorage.getItem(STORE_KEY) || "null");
    if (saved) fillForm(saved);
  } catch {}
  // Keep remembered filters visible, so they're never applied without you noticing
  const data = readForm();
  if (MORE_FILTERS.some((name) => data[name])) $("more").open = true;
}

function updateHints() {
  const filtering = form.comment_must_include.value.trim() !== "";
  $("maxCommentsLabel").textContent = filtering ? "Comments to check per video" : "Comments per video";
  $("maxCommentsHint").textContent = filtering ? "Only matching ones are saved; replies not counted" : "Not counting replies";

  const videos = Math.max(0, Math.floor(Number(form.max_videos.value)) || 0);
  const comments = Math.max(0, Math.floor(Number(form.max_comments.value)) || 0);
  // Search costs 100 per page of 50 results (a title filter may page through all ~10 pages); comments cost 1 per 100
  const searchPages = form.title_must_include.value.trim() ? 10 : Math.ceil(videos / 50);
  const units = searchPages * 100 + videos * Math.ceil(comments / 100);
  const replies = form.include_replies.checked ? " plus replies" : "";
  const el = $("estimate");
  el.textContent = `Uses up to about ${fmt(units)} quota units${replies} (daily limit ${fmt(config.daily_quota)})`;
  el.classList.toggle("over", units > config.daily_quota);
}

function showError(message) {
  $("formError").textContent = message;
  $("formError").hidden = false;
}

function clearError() {
  $("formError").hidden = true;
}

// ---- Progress ----

function setRunning(next) {
  if (next === running) return;
  running = next;
  $("fields").disabled = next;
  $("startBtn").hidden = next;
  $("stopBtn").hidden = !next;
  $("stopBtn").disabled = false;
  $("stopBtn").textContent = "Stop";
}

function appendLogs(logs) {
  const list = $("logList");
  const atBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 24;
  for (const entry of logs) {
    const li = document.createElement("li");
    li.className = entry.level;
    const time = document.createElement("time");
    time.textContent = entry.time;
    li.append(time, entry.message);
    list.append(li);
  }
  if (logs.length && atBottom) list.scrollTop = list.scrollHeight;
}

function render(s) {
  const p = s.progress;
  if (!p) return;
  $("progressCard").hidden = false;
  appendLogs(s.logs);
  logCount = s.log_count;

  const searching = s.running && p.videos_total === 0;
  const bar = $("bar");
  bar.classList.toggle("indeterminate", searching);
  bar.classList.toggle("done", !s.running && s.result?.status === "done");
  $("barFill").style.width = searching ? "" : `${p.videos_total ? (p.videos_done / p.videos_total) * 100 : 0}%`;
  $("progressText").textContent = p.videos_total ? `${p.videos_done} / ${p.videos_total} videos` : "";
  $("currentVideo").textContent = s.running && p.current_video ? `Now fetching: ${p.current_video}` : "";

  const filtering = s.filtering_comments;
  $("statCommentsLabel").textContent = filtering ? "Matching comments" : "Comments";
  $("statRepliesLabel").textContent = filtering ? "Matching replies" : "Replies";
  $("statScanned").hidden = !filtering;
  $("statScanned").textContent = `of ${fmt(p.comments_scanned)} checked`;
  $("statComments").textContent = fmt(p.comments);
  $("statReplies").textContent = fmt(p.replies);
  $("statSkipped").textContent = fmt(p.videos_skipped);
  $("statQuota").textContent = fmt(p.quota_used);

  if (s.running) {
    $("phase").textContent = searching ? "Searching for videos…" : "Fetching comments…";
    $("result").hidden = true;
  } else if (s.result) {
    showResult(s.result);
  }
}

function showResult(r) {
  const outcome = OUTCOMES[r.status] || OUTCOMES.failed;
  $("phase").textContent = outcome.title;
  $("result").className = `result ${outcome.tone}`;
  $("result").hidden = false;
  $("resultMsg").textContent = r.message;

  const files = [["Excel", r.excel_path], ["CSV", r.output_path]].filter(([, path]) => path);
  $("resultFiles").replaceChildren(...files.map(([label, path]) => {
    const li = document.createElement("li");
    const tag = document.createElement("span");
    tag.className = "hint";
    tag.textContent = label;
    const code = document.createElement("code");
    code.textContent = path;
    li.append(tag, code);
    return li;
  }));
  $("revealBtn").hidden = !config.can_reveal || files.length === 0;
}

async function poll() {
  clearTimeout(pollTimer);
  let s;
  try {
    s = await api(`/api/status?since=${logCount}`);
  } catch {
    setRunning(false);
    showError("Lost connection to the app — it may have been closed. Start it again and reload this page.");
    return;
  }
  render(s);
  setRunning(s.running);
  if (s.running) pollTimer = setTimeout(poll, 800);
}

// ---- Events ----

form.addEventListener("input", () => { saveForm(); updateHints(); });
form.addEventListener("change", () => { saveForm(); updateHints(); });

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  clearError();
  const data = readForm();
  if (!data.topic) { showError("Please enter a topic"); form.topic.focus(); return; }
  if (!data.output_dir) { showError("Please choose where to save the files"); form.output_dir.focus(); return; }

  $("startBtn").disabled = true;
  try {
    await api("/api/start", data);
  } catch (e) {
    showError(e.message);
    return;
  } finally {
    $("startBtn").disabled = false;
  }
  logCount = 0;
  $("logList").replaceChildren();
  $("result").hidden = true;
  setRunning(true);
  poll();
});

$("stopBtn").addEventListener("click", async () => {
  const btn = $("stopBtn");
  btn.disabled = true;
  btn.textContent = "Stopping…";
  try {
    await api("/api/stop", {});
  } catch (e) {
    showError(e.message);
    btn.disabled = false;
    btn.textContent = "Stop";
  }
});

$("pickBtn").addEventListener("click", async () => {
  const btn = $("pickBtn");
  btn.disabled = true;
  btn.textContent = "Choose in the dialog…";
  try {
    const r = await api("/api/pick-folder", { current: form.output_dir.value });
    if (r.path) {
      form.output_dir.value = r.path;
      saveForm();
    }
  } catch (e) {
    showError(e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = "Choose…";
  }
});

$("revealBtn").addEventListener("click", () => {
  api("/api/reveal", {}).catch((e) => showError(e.message));
});

async function init() {
  restoreForm();
  try {
    config = await api("/api/config");
  } catch {
    showError("Can't reach the app — make sure it's running.");
    return;
  }
  if (!form.output_dir.value) form.output_dir.value = config.default_dir;
  form.max_videos.max = config.max_videos;
  form.max_comments.max = config.max_comments;
  $("pickBtn").hidden = !config.can_pick_folder;

  const badge = $("keyBadge");
  badge.textContent = config.key_ready ? "API key set" : "No API key";
  badge.className = `badge ${config.key_ready ? "ok" : "bad"}`;
  $("keyWarning").hidden = config.key_ready;
  updateHints();

  // If a job is still running (or just finished) when the page is reloaded, show it again
  const s = await api("/api/status?since=0").catch(() => null);
  if (s?.progress) {
    render(s);
    setRunning(s.running);
    if (s.running) poll();
  }
}

init();
