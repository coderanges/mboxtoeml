"use strict";
/* State model: source -> inspection -> destination -> conversion -> results -> viewer -> modification.
   SSE is primary progress; polling is fallback only (never both). */
const $ = (id) => document.getElementById(id);

const S = {
  source: { mode: null, uploadId: "", filename: "", path: "" }, // mode: 'upload' | 'path'
  inspection: { status: "idle", info: null, error: "" }, // idle|inspecting|ready|failed|uploading
  destination: { dir: "", collision: "rename" },
  conversion: { jobId: null, status: "idle", detail: null, cancelling: false, t0: 0, timer: null },
  stream: { es: null, poll: null },
  results: { filter: "all", query: "", dateFrom: "", dateTo: "", offset: 0, limit: 25 },
  viewer: { filename: null, eml: null, tab: "text" },
};
let uploadXhr = null;

function showError(msg) {
  const el = $("error");
  if (!msg) { el.hidden = true; el.textContent = ""; return; }
  el.hidden = false;
  el.textContent = msg;
}
async function api(path, opts) {
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`);
  return data;
}
function fmtBytes(n) {
  if (n == null || n === "") return "—";
  const u = ["B", "KB", "MB", "GB"];
  let i = 0; let v = Number(n);
  if (!Number.isFinite(v)) return "—";
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return `${v.toFixed(v >= 10 || i === 0 ? 0 : 1)} ${u[i]}`;
}
function fmtDur(s) {
  s = Math.max(0, Math.round(s));
  if (s < 60) return `${s}s`;
  return `${Math.floor(s / 60)}m ${s % 60}s`;
}
function setStep(name) {
  for (const [id, key] of [["stepSource", "source"], ["stepSetup", "setup"], ["stepConvert", "convert"], ["stepResults", "results"]]) {
    const el = $(id);
    el.classList.toggle("active", key === name);
    if (key === name) el.setAttribute("aria-current", "step");
    else el.removeAttribute("aria-current");
  }
}
function setPill(el, text, tone) {
  el.textContent = text;
  if (tone) el.dataset.tone = tone;
  else el.removeAttribute("data-tone");
}

// ---------- health ----------
fetch("/api/health")
  .then(() => { $("health").textContent = "Server: connected"; })
  .catch(() => { $("health").textContent = "Server: unreachable"; });

// ---------- source: Pick File (primary), drag-and-drop (convenience) ----------
let inspectTimer = null;
let inspectSeq = 0;

function sourceRef() {
  // What inspect/convert should send for the current source, or null.
  if (S.source.mode === "upload" && S.source.uploadId) {
    return { upload_id: S.source.uploadId };
  }
  const path = $("mboxPath").value.trim();
  if (path) return { mbox_path: path };
  return null;
}

$("pickBtn").addEventListener("click", () => $("fileInput").click());
$("fileInput").addEventListener("change", () => {
  const f = $("fileInput").files && $("fileInput").files[0];
  $("fileInput").value = "";
  if (f) uploadFile(f);
});

function uploadFile(file) {
  if (uploadXhr) { showError("An upload is already in progress."); return; }
  if (!/\.mbox$/i.test(file.name || "")) {
    showError(`"${file.name}" is not an .mbox file.`);
    return;
  }
  showError("");
  S.source = { mode: "upload", uploadId: "", filename: file.name, path: "" };
  S.inspection = { status: "uploading", info: null, error: "" };
  renderInspection();
  renderReadiness();
  $("uploadWrap").hidden = false;
  $("uploadCancel").disabled = false;
  $("pickBtn").disabled = true;

  const xhr = new XMLHttpRequest();
  uploadXhr = xhr;
  xhr.open("POST", "/api/uploads");
  xhr.setRequestHeader("Content-Type", "application/octet-stream");
  xhr.setRequestHeader("X-Filename", file.name);
  xhr.upload.addEventListener("progress", (e) => {
    if (!e.lengthComputable) return;
    const pct = Math.round((e.loaded / e.total) * 100);
    $("uploadBar").style.width = `${pct}%`;
    $("uploadProgress").setAttribute("aria-valuenow", String(pct));
    $("uploadMeta").textContent = `Uploading ${file.name} — ${fmtBytes(e.loaded)} of ${fmtBytes(e.total)} (${pct}%)`;
  });
  xhr.addEventListener("load", () => {
    uploadXhr = null;
    $("pickBtn").disabled = false;
    let data = {};
    try { data = JSON.parse(xhr.responseText || "{}"); } catch { /* handled below */ }
    if (xhr.status >= 200 && xhr.status < 300 && data.upload_id) {
      S.source.uploadId = data.upload_id;
      S.source.filename = data.filename || file.name;
      $("uploadMeta").textContent = `Uploaded ${S.source.filename} (${fmtBytes(data.size_bytes)}). Inspecting…`;
      inspectNow();
    } else {
      S.source = { mode: null, uploadId: "", filename: "", path: "" };
      S.inspection = { status: "failed", info: null, error: data.error || `Upload failed (${xhr.status}).` };
      $("uploadWrap").hidden = true;
      renderInspection();
      renderReadiness();
    }
  });
  xhr.addEventListener("error", () => {
    uploadXhr = null;
    $("pickBtn").disabled = false;
    S.source = { mode: null, uploadId: "", filename: "", path: "" };
    S.inspection = { status: "failed", info: null, error: "Upload failed: connection error." };
    $("uploadWrap").hidden = true;
    renderInspection();
    renderReadiness();
  });
  xhr.addEventListener("abort", () => {
    uploadXhr = null;
    $("pickBtn").disabled = false;
    S.source = { mode: null, uploadId: "", filename: "", path: "" };
    S.inspection = { status: "idle", info: null, error: "" };
    $("uploadWrap").hidden = true;
    renderInspection();
    renderReadiness();
  });
  xhr.send(file);
}
$("uploadCancel").addEventListener("click", () => {
  if (uploadXhr) uploadXhr.abort();
});

$("mboxPath").addEventListener("input", () => {
  // Manual server path is a fallback; typing here replaces any upload.
  if (uploadXhr) uploadXhr.abort();
  $("uploadWrap").hidden = true;
  const path = $("mboxPath").value.trim();
  S.source = path
    ? { mode: "path", uploadId: "", filename: "", path }
    : { mode: null, uploadId: "", filename: "", path: "" };
  scheduleInspect();
});
function scheduleInspect() {
  clearTimeout(inspectTimer);
  if (!sourceRef()) {
    S.inspection = { status: "idle", info: null, error: "" };
    renderInspection();
    renderReadiness();
    return;
  }
  S.inspection.status = "inspecting";
  S.inspection.error = "";
  renderInspection();
  renderReadiness();
  inspectTimer = setTimeout(() => inspectNow(), 600);
}
async function inspectNow() {
  const seq = ++inspectSeq;
  const ref = sourceRef();
  if (!ref) return;
  if (S.inspection.status !== "uploading") {
    S.inspection = { status: "inspecting", info: null, error: "" };
    renderInspection();
  }
  try {
    const info = await api("/api/inspect", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...ref, limit: 8 }),
    });
    if (seq !== inspectSeq) return; // stale response
    S.inspection = { status: "ready", info, error: "" };
    if (info.suggested_output && !$("outputDir").value.trim()) {
      $("outputDir").value = info.suggested_output;
      S.destination.dir = info.suggested_output;
    }
    $("uploadWrap").hidden = true;
  } catch (e) {
    if (seq !== inspectSeq) return;
    S.inspection = { status: "failed", info: null, error: e.message };
    $("uploadWrap").hidden = true;
  }
  renderInspection();
  renderReadiness();
}
function renderInspection() {
  const st = S.inspection.status;
  const pill = $("inspectState");
  const wrap = $("sourceInfo");
  if (st === "idle") {
    setPill(pill, "Idle — no mailbox selected.", null);
    wrap.hidden = true; $("previewWrap").hidden = true; $("retryInspect").hidden = true;
  } else if (st === "inspecting") {
    setPill(pill, "Inspecting mailbox…", "busy");
    wrap.hidden = true; $("retryInspect").hidden = true;
  } else if (st === "uploading") {
    setPill(pill, `Uploading ${S.source.filename || "mailbox"}…`, "busy");
    wrap.hidden = true; $("retryInspect").hidden = true;
  } else if (st === "ready") {
    const info = S.inspection.info;
    setPill(pill, `Ready — ${info.total} messages · ${fmtBytes(info.size_bytes)}`, "ok");
    wrap.hidden = false; $("retryInspect").hidden = true;
    $("srcName").textContent = info.source_name || S.source.filename || "mailbox";
    $("srcCount").textContent = String(info.total);
    $("srcSize").textContent = fmtBytes(info.size_bytes);
    $("previewWrap").hidden = false;
    const tb = $("previewBody"); tb.textContent = "";
    for (const m of info.messages) {
      const tr = document.createElement("tr");
      for (const c of [String(m.index), m.subject || "(no subject)", m.from || "", m.date || ""]) {
        const td = document.createElement("td"); td.textContent = c; tr.appendChild(td);
      }
      tb.appendChild(tr);
    }
    setStep("setup");
  } else {
    setPill(pill, `Could not inspect this mailbox. ${S.inspection.error}`, "err");
    wrap.hidden = true; $("previewWrap").hidden = true; $("retryInspect").hidden = false;
  }
}
$("retryInspect").addEventListener("click", () => {
  if (S.source.mode === "upload" && !S.source.uploadId) return;
  inspectNow();
});

// ---------- drag and drop (same upload pipeline as Pick File) ----------
const dz = $("dropzone");
["dragenter", "dragover"].forEach((ev) => dz.addEventListener(ev, (e) => {
  e.preventDefault(); dz.classList.add("over");
}));
["dragleave", "drop"].forEach((ev) => dz.addEventListener(ev, (e) => {
  e.preventDefault(); dz.classList.remove("over");
}));
dz.addEventListener("drop", (e) => {
  const f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
  if (!f) return;
  const note = $("dropFile");
  note.hidden = false;
  note.textContent = `Dropped "${f.name}".`;
  uploadFile(f);
});
dz.addEventListener("keydown", (e) => {
  if (e.key === "Enter" || e.key === " ") { e.preventDefault(); $("pickBtn").click(); }
});
dz.addEventListener("click", () => $("pickBtn").click());

// ---------- output folder picker (same file-picker interface as MBOX) ----------
// The browser's directory picker reveals only the chosen folder's name, so
// it is created/reused by name under the server output root and the
// effective path fills the output field. Cancelling sends nothing.
// Primary: showDirectoryPicker() is folder-only by construction (it cannot
// pick files). Fallback: directory-mode <input>, guarded so a file pick is
// rejected with a clear message instead of being silently misused.
async function chooseOutputByName(name) {
  showError("");
  try {
    const r = await api("/api/output-folder", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    });
    $("outputDir").value = r.path;
    S.destination.dir = r.path;
    renderReadiness();
    $("outputDir").focus();
  } catch (e) {
    showError(e.message);
  }
}
$("pickOutBtn").addEventListener("click", async () => {
  if (window.showDirectoryPicker) {
    try {
      const handle = await window.showDirectoryPicker({ id: "mbox2eml-output" });
      if (handle && handle.name) await chooseOutputByName(handle.name);
    } catch (e) {
      if (e && e.name === "AbortError") return; // user cancelled: keep previous value
      showError(`Folder picker failed (${(e && e.message) || e}); type the path instead.`);
    }
    return;
  }
  $("dirInput").click();
});
$("dirInput").addEventListener("change", async () => {
  const files = $("dirInput").files;
  $("dirInput").value = "";
  if (!files || !files.length) {
    showError("That folder appears to be empty — pick a folder containing at least one file, or type the path instead.");
    return;
  }
  const rel = files[0].webkitRelativePath || "";
  if (!rel) {
    showError("Please choose a folder, not a file.");
    return;
  }
  await chooseOutputByName(rel.split("/")[0]);
});
// ---------- destination + collision ----------
$("outputDir").addEventListener("input", () => {
  S.destination.dir = $("outputDir").value.trim();
  renderReadiness();
});
document.querySelectorAll('input[name="collision"]').forEach((r) => {
  r.addEventListener("change", () => {
    S.destination.collision = document.querySelector('input[name="collision"]:checked').value;
    renderReadiness();
  });
});
$("defaultOutBtn").addEventListener("click", async () => {
  // Path sources derive the default from the mailbox location; uploads use
  // the server-provided default since the browser file has no server path.
  const pathMode = S.source.mode === "path" || $("mboxPath").value.trim();
  if (pathMode) {
    const m = $("mboxPath").value.trim();
    if (!m) { showError("Enter an MBOX path first to derive a default."); return; }
    const idx = Math.max(m.lastIndexOf("/"), m.lastIndexOf("\\"));
    const dir = idx >= 0 ? m.slice(0, idx) : ".";
    $("outputDir").value = `${dir}/output`;
    S.destination.dir = $("outputDir").value.trim();
    renderReadiness();
    return;
  }
  try {
    const d = await api("/api/defaults");
    $("outputDir").value = d.output_dir;
    S.destination.dir = d.output_dir;
  } catch (e) { showError(e.message); }
  renderReadiness();
});

function readiness() {
  const insp = S.inspection.status === "ready" ? S.inspection.info : null;
  const out = $("outputDir").value.trim();
  if (!insp) return { ok: false, text: "Pick an MBOX file to continue." };
  if (!out) return { ok: false, text: "Choose an output directory to continue." };
  const coll = document.querySelector('input[name="collision"]:checked').value;
  const label = { rename: "Rename", overwrite: "Overwrite", skip: "Skip" }[coll] || coll;
  const name = insp.source_name || S.source.filename || "mailbox";
  return {
    ok: true,
    text: `${name} · ${insp.total} messages · ${fmtBytes(insp.size_bytes)} → ${out} · ${label}`,
  };
}
function renderReadiness() {
  const r = readiness();
  $("readinessText").textContent = r.ok ? `Ready: ${r.text}` : r.text;
  $("startBtn").disabled = !r.ok || S.conversion.status === "running";
  if (r.ok) setStep("setup");
}

// ---------- conversion ----------
$("startBtn").addEventListener("click", async () => {
  showError("");
  const r = readiness();
  if (!r.ok) { showError(r.text); return; }
  try {
    const ref = sourceRef();
    if (!ref) { showError("Pick an MBOX file first."); return; }
    const { job_id } = await api("/api/convert", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        ...ref,
        output_dir: $("outputDir").value.trim(),
        collision: document.querySelector('input[name="collision"]:checked').value,
      }),
    });
    S.conversion = { jobId: job_id, status: "running", detail: null, cancelling: false, t0: Date.now(), timer: null };
    S.results.offset = 0;
    S.viewer = { filename: null, eml: null, tab: "text" };
    renderViewer();
    setStep("convert");
    subscribe(job_id);
    S.conversion.timer = setInterval(tickElapsed, 500);
  } catch (e) { showError(e.message); }
});
$("cancelBtn").addEventListener("click", async () => {
  const c = S.conversion;
  if (!c.jobId || c.status !== "running" || c.cancelling) return;
  c.cancelling = true;
  renderConversion();
  try {
    await api(`/api/jobs/${c.jobId}/cancel`, { method: "POST" });
    setPill($("convState"), "Cancelling…", "busy");
  } catch (e) { c.cancelling = false; showError(e.message); renderConversion(); }
});
function tickElapsed() {
  if (S.conversion.status === "running" && S.conversion.detail) {
    const el = (Date.now() - S.conversion.t0) / 1000;
    $("stElapsed").textContent = fmtDur(el);
    $("stEta").textContent = etaText(S.conversion.detail, el);
  }
}
function etaText(st, elapsed) {
  if (st.processed < 5 || elapsed < 2 || st.total <= 0) return "calculating…";
  const rate = st.processed / Math.max(elapsed, 0.1);
  if (rate <= 0) return "calculating…";
  return `about ${fmtDur((st.total - st.processed) / rate)}`;
}
function subscribe(jobId) {
  stopStreams();
  let sseOk = false;
  try {
    const es = new EventSource(`/api/jobs/${jobId}/events`);
    S.stream.es = es;
    es.onmessage = (ev) => {
      try {
        const st = JSON.parse(ev.data);
        sseOk = true;
        onStatus(st);
      } catch { /* ignore */ }
    };
    es.onerror = () => {
      es.close();
      if (S.stream.es === es) S.stream.es = null;
      startPolling(jobId);
    };
    setTimeout(() => { if (!sseOk && !S.stream.poll) startPolling(jobId); }, 2500);
  } catch { startPolling(jobId); }
}
function startPolling(jobId) {
  if (S.stream.poll) return;
  const tick = async () => {
    try {
      const st = await api(`/api/jobs/${jobId}`);
      onStatus(st);
      if (!st.done) await loadResults(false);
    } catch { /* keep polling */ }
  };
  tick();
  S.stream.poll = setInterval(tick, 1200);
}
function stopStreams() {
  if (S.stream.es) { S.stream.es.close(); S.stream.es = null; }
  if (S.stream.poll) { clearInterval(S.stream.poll); S.stream.poll = null; }
}
function onStatus(st) {
  S.conversion.detail = st;
  S.conversion.status = st.status;
  renderConversion();
  if (st.done) {
    stopStreams();
    if (S.conversion.timer) { clearInterval(S.conversion.timer); S.conversion.timer = null; }
    loadResults(true);
    setStep("results");
  } else {
    loadResults(false);
  }
}
function renderConversion() {
  const c = S.conversion;
  const st = c.detail;
  const bar = $("bar");
  if (!st) {
    setPill($("convState"), "Idle.", null);
    $("cancelBtn").disabled = true;
    $("progressWrap").setAttribute("aria-valuenow", "0");
    renderReadiness();
    return;
  }
  const pct = st.percentage ?? 0;
  bar.style.width = `${pct}%`;
  $("progressWrap").setAttribute("aria-valuenow", String(Math.round(pct)));
  $("stProcessed").textContent = `${st.processed} / ${st.total}`;
  $("stOk").textContent = String(st.succeeded);
  $("stFail").textContent = String(st.failed);
  $("stSkip").textContent = String(st.skipped);
  const el = c.t0 ? (Date.now() - c.t0) / 1000 : st.elapsed_secs;
  $("stElapsed").textContent = fmtDur(el);
  $("stEta").textContent = st.done ? "—" : etaText(st, el);
  $("curFile").textContent = st.current_filename ? `Current: ${st.current_filename}` : "";
  $("cancelBtn").disabled = st.done || c.cancelling;

  const box = $("summary");
  if (st.status === "done") {
    setPill($("convState"), "Completed.", "ok");
    $("startBtn").disabled = false;
    box.hidden = false;
    box.dataset.tone = st.failed ? "warn" : "ok";
    box.textContent = "";
    const h = document.createElement("strong");
    h.textContent = "Conversion complete";
    const p = document.createElement("p");
    p.textContent = `${st.total} messages processed — ${st.succeeded} created, ${st.skipped} skipped, ${st.failed} failed. Output: ${st.output_dir}`;
    box.append(h, p);
    if (st.failed) {
      const hint = document.createElement("p");
      hint.className = "muted small";
      hint.textContent = "Failures are listed below — switch the filter to Failed to inspect them.";
      box.appendChild(hint);
    }
    renderReadiness();
  } else if (st.status === "cancelled") {
    setPill($("convState"), "Cancelled.", "err");
    $("startBtn").disabled = false;
    box.hidden = false;
    box.dataset.tone = "warn";
    box.textContent = "";
    const h = document.createElement("strong");
    h.textContent = "Conversion cancelled";
    const p = document.createElement("p");
    const created = st.succeeded;
    p.textContent = `${st.processed} of ${st.total} messages processed. ${created} files created; ${st.total - st.processed} were not processed. Partial output remains in ${st.output_dir}.`;
    box.append(h, p);
    renderReadiness();
  } else if (st.status === "error") {
    setPill($("convState"), "Failed.", "err");
    $("startBtn").disabled = false;
    box.hidden = false;
    box.dataset.tone = "err";
    box.textContent = "";
    const h = document.createElement("strong");
    h.textContent = "Conversion failed";
    const p = document.createElement("p");
    p.textContent = st.error || "The conversion could not complete.";
    box.append(h, p);
    showError(st.error);
    renderReadiness();
  } else {
    setPill($("convState"), c.cancelling ? "Cancelling…" : "Converting mailbox…", "busy");
    $("startBtn").disabled = true;
    box.hidden = true;
  }
}

// ---------- results ----------
for (const r of document.querySelectorAll('input[name="filter"]')) {
  r.addEventListener("change", () => {
    S.results.filter = document.querySelector('input[name="filter"]:checked').value;
    S.results.offset = 0;
    loadResults(true);
  });
}
let searchTimer = null;
$("search").addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    S.results.query = $("search").value.trim();
    S.results.offset = 0;
    loadResults(true);
  }, 300);
});
function onDateChange() {
  S.results.dateFrom = $("dateFrom").value;
  S.results.dateTo = $("dateTo").value;
  S.results.offset = 0;
  loadResults(true);
}
$("dateFrom").addEventListener("change", onDateChange);
$("dateTo").addEventListener("change", onDateChange);
$("clearFilters").addEventListener("click", () => {
  $("search").value = "";
  $("dateFrom").value = "";
  $("dateTo").value = "";
  document.querySelector('input[name="filter"][value="all"]').checked = true;
  S.results.query = "";
  S.results.dateFrom = "";
  S.results.dateTo = "";
  S.results.filter = "all";
  S.results.offset = 0;
  showError("");
  loadResults(true);
});
$("prevPage").addEventListener("click", () => {
  S.results.offset = Math.max(0, S.results.offset - S.results.limit);
  loadResults(true);
});
$("nextPage").addEventListener("click", () => {
  S.results.offset += S.results.limit;
  loadResults(true);
});
const STATUS_LABEL = {
  ok: ["Successful", "ok"], error: ["Failed", "err"], skipped: ["Skipped", "skip"],
};
async function loadResults(announce) {
  const c = S.conversion;
  if (!c.jobId) {
    $("resultsMeta").textContent = "No conversion results yet.";
    return;
  }
  const params = new URLSearchParams({
    offset: String(S.results.offset), limit: String(S.results.limit),
    status: S.results.filter, q: S.results.query,
    date_from: S.results.dateFrom, date_to: S.results.dateTo,
  });
  try {
    const r = await api(`/api/jobs/${c.jobId}/results?${params}`);
    const total = r.results_total;
    $("resultsMeta").textContent = total
      ? `${total} message${total === 1 ? "" : "s"}${S.results.query ? ` matching "${S.results.query}"` : ""}`
      : (S.results.query ? `No messages match "${S.results.query}".` : "No results on this page.");
    $("pageMeta").textContent = total ? `${r.offset + 1}–${r.offset + r.items.length} of ${total}` : "";
    $("prevPage").disabled = S.results.offset <= 0;
    $("nextPage").disabled = r.offset + r.items.length >= total;
    const ul = $("resultsList");
    ul.textContent = "";
    for (const it of r.items) {
      const [label, tone] = STATUS_LABEL[it.status] || [it.status, "skip"];
      const li = document.createElement("li");
      const btn = document.createElement("button");
      btn.type = "button";
      if (it.filename) btn.dataset.file = it.filename;
      if (it.filename === S.viewer.filename) btn.classList.add("selected");
      btn.setAttribute("aria-label", `${label}: ${it.subject || "(no subject)"}`);
      const badge = document.createElement("span");
      badge.className = "badge"; badge.dataset.tone = tone; badge.textContent = label;
      const text = document.createElement("span");
      const subj = document.createElement("span");
      subj.className = "result-subject";
      subj.textContent = it.subject || "(no subject)";
      const meta = document.createElement("span");
      meta.className = "result-meta";
      meta.textContent = it.status === "error"
        ? (it.error || "Conversion failed")
        : [it.from, it.filename].filter(Boolean).join(" · ");
      text.append(subj, document.createElement("br"), meta);
      btn.append(badge, text);
      if (it.filename) {
        const file = it.filename;
        btn.addEventListener("click", () => selectEml(file));
      } else {
        btn.disabled = true;
      }
      li.appendChild(btn);
      ul.appendChild(li);
    }
    if (announce && total === 0 && S.results.query) showError("");
  } catch (e) { showError(e.message); }
}

// ---------- viewer ----------
for (const r of document.querySelectorAll('input[name="view"]')) {
  r.addEventListener("change", () => {
    S.viewer.tab = document.querySelector('input[name="view"]:checked').value;
    renderViewerBody();
  });
}
async function selectEml(filename) {
  const c = S.conversion;
  if (!filename || !c.jobId) return;
  S.viewer.filename = filename;
  $("viewerMeta").textContent = `Loading ${filename}…`;
  try {
    const eml = await api(`/api/eml?job_id=${encodeURIComponent(c.jobId)}&filename=${encodeURIComponent(filename)}`);
    S.viewer.tab = eml.body_text ? "text" : "html";
    S.viewer.eml = eml;
    syncViewToggle();
    renderViewer();
    document.querySelectorAll("#resultsList button").forEach((b) => {
      b.classList.toggle("selected", b.dataset.file === filename);
    });
    await loadResults(false);
  } catch (e) { $("viewerMeta").textContent = ""; showError(e.message); }
}
function syncViewToggle() {
  const eml = S.viewer.eml;
  const both = Boolean(eml && eml.body_text && (eml.body_html_sanitized || eml.body_html));
  $("viewToggle").hidden = !both;
  const radio = document.querySelector(`input[name="view"][value="${S.viewer.tab}"]`);
  if (radio) radio.checked = true;
}
function renderViewer() {
  const { filename, eml } = S.viewer;
  if (!eml) {
    $("viewerMeta").textContent = filename ? `Loading ${filename}…` : "Select a result to read it here.";
    $("viewerBody").hidden = true;
    return;
  }
  $("viewerMeta").textContent = "";
  $("viewerBody").hidden = false;
  $("vFile").textContent = eml.filename || filename;
  $("vSubject").textContent = eml.subject || "(no subject)";
  $("vFrom").textContent = eml.from || "—";
  $("vTo").textContent = (eml.to || []).join(", ") || "—";
  $("vCc").textContent = (eml.cc || []).join(", ") || "—";
  $("vReply").textContent = eml.reply_to || "—";
  $("vDate").textContent = eml.date || "—";
  renderViewerBody();
  const atts = eml.attachments || [];
  const ul = $("vAtt");
  ul.textContent = "";
  if (!atts.length) {
    const li = document.createElement("li");
    li.className = "muted"; li.textContent = "None";
    ul.appendChild(li);
  }
  for (const a of atts) {
    const li = document.createElement("li");
    const name = document.createElement("strong");
    name.textContent = `📎 ${a.filename}`;
    const meta = document.createElement("span");
    meta.className = "muted small";
    meta.textContent = ` ${a.content_type} · ${fmtBytes(a.size_bytes)}`;
    li.append(name, document.createElement("br"), meta);
    ul.appendChild(li);
  }
  const dl = $("dlLink");
  dl.href = `/api/eml/download?job_id=${encodeURIComponent(S.conversion.jobId)}&filename=${encodeURIComponent(filename)}`;
  dl.hidden = false;
  if (!$("modValue").value) $("modValue").value = (eml.to || [])[0] || "";
}
function renderViewerBody() {
  const eml = S.viewer.eml;
  if (!eml) return;
  syncViewToggle();
  const html = eml.body_html_sanitized || eml.body_html || "";
  const showHtml = S.viewer.tab === "html" && html;
  $("vBody").hidden = Boolean(showHtml);
  $("htmlWrap").hidden = !showHtml;
  if (showHtml) {
    $("vHtml").srcdoc = html;
    const n = eml.remote_blocked || 0;
    $("blockedNote").textContent = n
      ? `${n} remote resource${n === 1 ? "" : "s"} blocked for privacy.`
      : "Remote content blocked for privacy.";
  } else {
    $("vBody").textContent = eml.body_text || "(no text body)";
    $("vHtml").removeAttribute("srcdoc");
  }
}

// ---------- modification (View vs Modify stay separate) ----------
$("modBtn").addEventListener("click", async () => {
  showError("");
  $("modMeta").textContent = "";
  const c = S.conversion;
  if (!c.jobId || !S.viewer.filename) { showError("Select a converted message first."); return; }
  const header = $("modHeader").value.trim();
  const value = $("modValue").value;
  const output_filename = $("modOut").value.trim();
  if (!header) { showError("Header name is required."); return; }
  $("modBtn").disabled = true;
  try {
    const r = await api("/api/eml/modify", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ job_id: c.jobId, filename: S.viewer.filename, header, value, output_filename }),
    });
    $("modMeta").textContent = `Updated ${r.header} in ${r.filename}. The preview below shows the modified message.`;
    await loadResults(false);
    S.viewer.filename = r.filename;
    await selectEml(r.filename);
  } catch (e) { showError(e.message); }
  finally { $("modBtn").disabled = false; }
});

// init
S.destination.collision = document.querySelector('input[name="collision"]:checked').value;
renderInspection();
renderReadiness();
renderConversion();
