"use strict";

const BATCH_SIZE = 10;     // files per upload request
const CONCURRENCY = 3;     // parallel upload requests
const PAGE_SIZE = 100;

const $ = (id) => document.getElementById(id);
const state = { status: "", kind: "", q: "", offset: 0, threshold: 30, uploading: false, busy: false };

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const pct = (v) => (v === null || v === undefined ? "—" : `${Number(v).toFixed(1)}%`);
const label = { queued: "Queued", processing: "Checking", accepted: "Accepted", rejected: "Rejected",
  needs_review: "Needs review", error: "Error", indexed: "Indexed" };

async function api(path, opts) {
  const res = await fetch(path, opts);
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
  return res.json();
}

// ---- upload -----------------------------------------------------------
function chunk(arr, n) { const out = []; for (let i = 0; i < arr.length; i += n) out.push(arr.slice(i, i + n)); return out; }

async function uploadFiles(fileList) {
  const files = [...fileList].filter((f) => f.name.toLowerCase().endsWith(".pdf") || f.type === "application/pdf");
  const skipped = fileList.length - files.length;
  if (!files.length) { showErrors([`No PDF files selected${skipped ? ` (${skipped} non-PDF skipped)` : ""}.`]); return; }
  const kind = document.querySelector("input[name=kind]:checked").value;
  const batches = chunk(files, BATCH_SIZE);
  const errors = skipped ? [`${skipped} non-PDF file(s) skipped.`] : [];
  let done = 0, accepted = 0;

  state.uploading = true;
  $("progress").hidden = false;
  showErrors([]);
  const update = () => {
    $("progress-fill").style.width = `${(100 * done) / files.length}%`;
    $("progress-text").textContent = done < files.length
      ? `Uploading ${done} / ${files.length} files…`
      : `Uploaded ${accepted} of ${files.length} files. Checks run in the background — results appear below.`;
  };
  update();

  let next = 0;
  async function workerLoop() {
    while (next < batches.length) {
      const batch = batches[next++];
      const fd = new FormData();
      fd.append("kind", kind);
      batch.forEach((f) => fd.append("files", f, f.name));
      try {
        const { results } = await api("/api/upload", { method: "POST", body: fd });
        results.forEach((r) => (r.ok ? accepted++ : errors.push(`${r.filename}: ${r.error}`)));
      } catch (e) {
        batch.forEach((f) => errors.push(`${f.name}: upload failed (${e.message.slice(0, 120)})`));
      }
      done += batch.length;
      update();
      refresh();
    }
  }
  await Promise.all(Array.from({ length: Math.min(CONCURRENCY, batches.length) }, workerLoop));
  state.uploading = false;
  showErrors(errors);
  refresh();
}

function showErrors(list) {
  $("upload-errors").innerHTML = list.slice(0, 50).map((e) => `<li>${esc(e)}</li>`).join("")
    + (list.length > 50 ? `<li>…and ${list.length - 50} more</li>` : "");
}

const drop = $("drop");
$("file-input").addEventListener("change", (e) => { uploadFiles(e.target.files); e.target.value = ""; });
["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); }));
["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
drop.addEventListener("drop", (e) => uploadFiles(e.dataTransfer.files));

// ---- table ------------------------------------------------------------
function statusCell(d) {
  return `<span class="pill ${d.status}">${label[d.status] || d.status}</span>`
    + (d.decision_source === "manual" ? '<span class="manual">manual</span>' : "")
    + (d.stage && d.status === "processing" ? `<span class="stage">${esc(d.stage)}…</span>` : "");
}
function scoreCell(v) {
  const cls = v !== null && v !== undefined && v > state.threshold ? "over-limit" : "";
  return `<td class="num ${cls}">${pct(v)}</td>`;
}

async function loadRows() {
  const params = new URLSearchParams({ limit: PAGE_SIZE, offset: state.offset });
  if (state.status) params.set("status", state.status);
  if (state.kind) params.set("kind", state.kind);
  if (state.q) params.set("q", state.q);
  const { items, total } = await api(`/api/documents?${params}`);
  $("rows").innerHTML = items.map((d) => `
    <tr data-id="${d.id}">
      <td class="file" title="${esc(d.url || d.filename)}">${esc(d.filename)}</td>
      <td>${statusCell(d)}</td>
      ${scoreCell(d.plagiarism_pct)}${scoreCell(d.ai_pct)}
      <td class="num">${d.pages ?? "—"}</td>
      <td>${new Date(d.created_at).toLocaleString()}</td>
    </tr>`).join("");
  $("empty").hidden = items.length > 0;
  const end = Math.min(state.offset + items.length, total);
  $("page-info").textContent = total ? `${state.offset + 1}–${end} of ${total}` : "";
  $("prev").disabled = state.offset === 0;
  $("next").disabled = end >= total;
}

async function loadStats() {
  const s = await api("/api/stats");
  const b = s.by_status;
  $("s-total").textContent = s.total;
  $("s-working").textContent = (b.queued || 0) + (b.processing || 0);
  $("s-accepted").textContent = b.accepted || 0;
  $("s-rejected").textContent = b.rejected || 0;
  $("s-review").textContent = (b.needs_review || 0) + (b.error || 0);
  $("s-refs").textContent = s.references;
  $("s-web").textContent = s.web_pages;
  return (b.queued || 0) + (b.processing || 0);
}

async function refresh() {
  if (state.busy) return;
  state.busy = true;
  try {
    const working = await loadStats();
    await loadRows();
    return working;
  } finally { state.busy = false; }
}

$("tabs").addEventListener("click", (e) => {
  const btn = e.target.closest("button"); if (!btn) return;
  document.querySelectorAll("#tabs button").forEach((b) => b.classList.toggle("active", b === btn));
  state.status = btn.dataset.status || ""; state.kind = btn.dataset.kind || ""; state.offset = 0;
  refresh();
});
let searchTimer;
$("search").addEventListener("input", (e) => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => { state.q = e.target.value.trim(); state.offset = 0; refresh(); }, 250);
});
$("prev").addEventListener("click", () => { state.offset = Math.max(0, state.offset - PAGE_SIZE); refresh(); });
$("next").addEventListener("click", () => { state.offset += PAGE_SIZE; refresh(); });
$("rows").addEventListener("click", (e) => { const tr = e.target.closest("tr[data-id]"); if (tr) openDetail(tr.dataset.id); });

// ---- detail dialog ----------------------------------------------------
function meter(v) {
  const w = Math.min(100, v || 0);
  return `<div class="meter"><div class="f ${w > state.threshold ? "hi" : ""}" style="width:${w}%"></div><div class="t" style="left:${state.threshold}%"></div></div>`;
}

async function openDetail(id) {
  const d = await api(`/api/documents/${id}`);
  $("d-title").textContent = d.filename;
  const det = d.details || {};
  const sources = det.plagiarism?.sources || [];
  const segs = det.ai?.flagged_segments || [];
  const isSub = d.kind === "submission";
  const isWeb = d.kind === "web";
  const web = det.web;
  const srcLabel = (s) => s.kind === "web" ? "web page" : s.kind === "reference" ? "reference source" : "earlier submission";
  const srcLink = (s) => s.kind === "web" && s.url
    ? `<a href="${esc(s.url)}" target="_blank" rel="noopener noreferrer">${esc(s.filename)}</a>`
    : `<a href="#" data-open="${s.doc_id}">${esc(s.filename)}</a>`;
  const done = !["queued", "processing"].includes(d.status);

  $("d-body").innerHTML = `
    <div>${statusCell(d)} <span class="muted">· ${d.pages ?? "?"} pages · ${d.words ?? "?"} words · ${(d.size_bytes / 1048576).toFixed(2)} MB</span></div>
    ${isSub ? `<div class="scores">
      <div class="score"><div class="muted">Plagiarism similarity</div><div class="v">${pct(d.plagiarism_pct)}</div>${meter(d.plagiarism_pct)}</div>
      <div class="score"><div class="muted">AI-generated likelihood (${esc(det.ai?.provider || "—")})</div><div class="v">${pct(d.ai_pct)}</div>${meter(d.ai_pct)}</div>
    </div>` : isWeb ? `<p class="muted">Web page found by internet search — submissions are compared against it.
      <a href="${esc(d.url)}" target="_blank" rel="noopener noreferrer">${esc(d.url)}</a></p>`
      : `<p class="muted">Reference source — other uploads are compared against it; it is never accepted or rejected itself.</p>`}
    ${d.reasons?.length ? `<h3>Why</h3><ul class="reasons">${d.reasons.map((r) => `<li>${esc(r)}</li>`).join("")}</ul>` : ""}
    ${d.error ? `<h3>Error</h3><p>${esc(d.error)}</p>` : ""}
    ${d.note ? `<h3>Reviewer note</h3><p>${esc(d.note)}</p>` : ""}
    ${det.exact_duplicate_of ? `<h3>Exact duplicate</h3><p>Byte-identical to <strong>${esc(det.exact_duplicate_of.filename)}</strong> (#${det.exact_duplicate_of.id}).</p>` : ""}
    ${isSub && done ? `<h3>Matching sources</h3>${sources.length ? `<ul class="list">${sources.map((s) => `
        <li><strong>${pct(s.pct)}</strong> overlap with ${srcLink(s)}
        <span class="muted">(${srcLabel(s)})</span></li>`).join("")}</ul>`
      : `<p class="muted">No overlapping documents or web pages found.</p>`}` : ""}
    ${web ? `<h3>Internet search</h3><p class="muted">Searched ${web.queries} exact phrases via ${esc(web.provider)}`
        + `${web.queries_failed ? ` — <strong>${web.queries_failed} failed</strong>` : ""}; `
        + `${web.pages.length} candidate page(s) checked.</p>`
        + (web.pages.length ? `<ul class="list">${web.pages.map((p) => `<li><a href="${esc(p.url)}" target="_blank" rel="noopener noreferrer">${esc(p.title || p.url)}</a>
          <span class="muted">· came up in ${p.query_hits} of ${web.queries} searches · ${esc(p.status)}</span></li>`).join("")}</ul>` : "")
        + (web.errors.length ? `<p class="muted">${web.errors.map(esc).join("<br>")}</p>` : "")
      : isSub && done ? `<h3>Internet search</h3><p class="muted">Not run (internet search is turned off).</p>` : ""}
    ${segs.length ? `<h3>Passages flagged as AI-like</h3><ul class="list">${segs.map((s) => `
        <li><strong>${Math.round(s.probability * 100)}%</strong> <span class="muted">burstiness ${s.burstiness} · ${s.marker_hits} stock phrases</span>
        <div class="excerpt">${esc(s.excerpt)}</div></li>`).join("")}</ul>` : ""}
    <div class="actions">
      ${isSub && done ? `<textarea id="d-note" placeholder="Reviewer note (optional)">${esc(d.note || "")}</textarea>
        <button class="btn ok" data-act="accepted">Override: accept</button>
        <button class="btn bad" data-act="rejected">Override: reject</button>` : ""}
      ${done && !isWeb ? `<button class="btn" data-act="recheck" title="Compare against every other document, including later uploads">Re-check</button>` : ""}
      ${isWeb ? `<a class="btn" href="${esc(d.url)}" target="_blank" rel="noopener noreferrer">Open page</a>`
        : `<a class="btn" href="/api/documents/${d.id}/file" target="_blank" rel="noopener">Open PDF</a>`}
      <button class="btn bad" data-act="delete">Delete</button>
    </div>`;

  $("d-body").onclick = async (e) => {
    const link = e.target.closest("[data-open]");
    if (link) { e.preventDefault(); openDetail(link.dataset.open); return; }
    const act = e.target.closest("[data-act]")?.dataset.act; if (!act) return;
    try {
      if (act === "accepted" || act === "rejected") {
        await api(`/api/documents/${d.id}/override`, { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ status: act, note: $("d-note")?.value || "" }) });
      } else if (act === "recheck") {
        await api(`/api/documents/${d.id}/recheck`, { method: "POST" });
      } else if (act === "delete") {
        if (!confirm(`Delete ${d.filename}? This also removes it from the plagiarism index.`)) return;
        await api(`/api/documents/${d.id}`, { method: "DELETE" });
        $("detail").close(); refresh(); return;
      }
      refresh(); openDetail(d.id);
    } catch (err) { alert(err.message); }
  };
  if (!$("detail").open) $("detail").showModal();
}

// ---- boot -------------------------------------------------------------
(async function boot() {
  const cfg = await api("/api/config");
  state.threshold = cfg.reject_threshold;
  $("rule").innerHTML = `Auto-reject above <strong>${cfg.reject_threshold}%</strong> plagiarism or AI`;
  const names = { brave: "Brave", serper: "Google via Serper", searxng: "SearXNG" };
  $("web-badge").textContent = cfg.web_search === "off" ? "Internet search off" : `Internet search: ${names[cfg.web_search] || cfg.web_search}`;
  $("web-badge").classList.toggle("off", cfg.web_search === "off");
  $("drop-hint").textContent = `Up to ${cfg.max_file_mb} MB per file. Hundreds of files at once are fine — they upload in batches and are checked by ${cfg.workers} parallel workers.`;
  let working = await refresh();
  // Poll fast while work is in flight, slowly otherwise.
  (async function poll() {
    try { const w = await refresh(); if (w !== undefined) working = w; } catch (_) { /* transient */ }
    setTimeout(poll, working || state.uploading ? 2000 : 10000);
  })();
})();
