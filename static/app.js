/**
 * Hermes — client controller.
 */

(() => {
  "use strict";

  const LOSSLESS_SCRUB_FORMATS = new Set(["PNG", "JPEG", "PDF"]);
  const CONVERTIBLE_SOURCE_FORMATS = new Set([
    "PNG", "JPEG", "HEIC", "WEBP", "TIFF", "BMP", "AVIF",
  ]);
  const CONVERT_TARGETS = ["JPEG", "PNG", "WEBP"];
  const ACCEPTED_EXTENSIONS = [
    "png", "jpg", "jpeg", "pdf",
    "heic", "heif", "webp", "tif", "tiff", "bmp", "avif",
  ];
  const RISK_RANK = { high: 0, medium: 1, low: 2 };

  const state = {
    files: new Map(),
    totalBytesSaved: 0,
  };

  const $ = (id) => document.getElementById(id);

  /* ---------- Utilities ---------- */

  function esc(s) {
    if (s === null || s === undefined) return "";
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function fmtBytes(n) {
    if (!Number.isFinite(n) || n <= 0) return "0 B";
    const units = ["B", "KB", "MB", "GB"];
    let i = 0;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
    return `${n.toFixed(n >= 100 || i === 0 ? 0 : 1)} ${units[i]}`;
  }

  function extOf(name) {
    const i = name.lastIndexOf(".");
    return i === -1 ? "" : name.slice(i + 1).toLowerCase();
  }

  function renameForFormat(name, targetFormat) {
    const ext = { JPEG: ".jpg", PNG: ".png", WEBP: ".webp" }[targetFormat] || ".bin";
    const i = name.lastIndexOf(".");
    const base = i === -1 ? name : name.slice(0, i);
    return `${base}${ext}`;
  }

  /**
   * Simplify a raw metadata key for display.
   *   "Make"                 → "Make"
   *   "GPSLatitude"          → "Latitude"
   *   "GPS_GPSLatitude"      → "Latitude"
   *   "ExifSub_41986"        → "41986"
   *   "Tag_270"              → "270"
   *   "DateTimeOriginal"     → "DateTime Original"
   */
  function cleanKey(key) {
    if (!key) return "";
    let s = String(key);
    s = s.replace(/^ExifSub_/, "");
    s = s.replace(/^Tag_/, "");
    // Category column already says "GPS / Location", drop the prefix.
    // Handles both "GPSLatitude" and the doubled "GPS_GPSLatitude" forms.
    s = s.replace(/^GPS[_\s]*GPS/i, "");
    s = s.replace(/^GPS[_\s]+/i, "");
    s = s.replace(/^GPS(?=[A-Z])/, "");
    s = s.replace(/([a-z])([A-Z])/g, "$1 $2");
    s = s.replace(/_/g, " ");
    s = s.replace(/\s+/g, " ").trim();
    return s;
  }

  function riskLabel(risk) {
    return { high: "high risk", medium: "medium risk", low: "low risk" }[risk] || "unknown";
  }

  function toast(msg, kind = "success", ms = 3500) {
    const c = $("toastContainer");
    if (!c) return;
    const el = document.createElement("div");
    el.className = `toast toast-${kind}`;
    el.textContent = msg;
    c.appendChild(el);
    setTimeout(() => {
      el.style.opacity = "0";
      el.style.transform = "translateY(6px)";
      setTimeout(() => el.remove(), 250);
    }, ms);
  }

  function triggerDownload(blob, filename) {
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  /* ---------- Theme ---------- */

  function initTheme() {
    const btn = $("themeToggleBtn");
    if (!btn) return;
    btn.addEventListener("click", () => {
      const cur = document.documentElement.getAttribute("data-theme");
      const systemDark = window.matchMedia("(prefers-color-scheme: dark)").matches;
      const isDark = cur ? cur === "dark" : systemDark;
      const next = isDark ? "light" : "dark";
      document.documentElement.setAttribute("data-theme", next);
      localStorage.setItem("color-scheme", next);
      const meta = document.querySelector('meta[name="color-scheme"]');
      if (meta) meta.content = next;
    });
  }

  /* ---------- Tabs ---------- */

  function initTabs() {
    const tDrop = $("tabDropzone");
    const tLocal = $("tabLocalPath");
    const sDrop = $("sectionDropzone");
    const sLocal = $("sectionLocalPath");
    if (!tDrop || !tLocal) return;

    const go = (isDrop) => {
      tDrop.classList.toggle("active", isDrop);
      tLocal.classList.toggle("active", !isDrop);
      tDrop.setAttribute("aria-selected", isDrop ? "true" : "false");
      tLocal.setAttribute("aria-selected", isDrop ? "false" : "true");
      if (sDrop) sDrop.classList.toggle("active", isDrop);
      if (sLocal) sLocal.classList.toggle("active", !isDrop);
    };

    tDrop.addEventListener("click", () => go(true));
    tLocal.addEventListener("click", () => go(false));
  }

  /* ---------- File pickers ---------- */

  async function pickFiles() {
    if (typeof window.showOpenFilePicker === "function") {
      try {
        const handles = await window.showOpenFilePicker({
          multiple: true,
          excludeAcceptAllOption: false,
          types: [{
            description: "Photos & documents",
            accept: {
              "image/jpeg": [".jpg", ".jpeg"],
              "image/png": [".png"],
              "image/heic": [".heic"],
              "image/heif": [".heif"],
              "image/webp": [".webp"],
              "image/tiff": [".tif", ".tiff"],
              "image/bmp": [".bmp"],
              "image/avif": [".avif"],
              "application/pdf": [".pdf"],
            },
          }],
        });
        for (const h of handles) {
          const f = await h.getFile();
          addFile(f, h);
        }
        return;
      } catch (err) {
        if (err.name === "AbortError") return;
        console.warn("Picker error, falling back to input:", err);
      }
    }
    const fallback = $("fallbackFileInput");
    if (fallback) fallback.click();
  }

  async function pickFolder() {
    if (typeof window.showDirectoryPicker === "function") {
      try {
        const dir = await window.showDirectoryPicker({ mode: "readwrite" });
        await scanDirHandle(dir);
      } catch (err) {
        if (err.name !== "AbortError") console.warn("Dir picker error:", err);
      }
    } else {
      const fallback = $("fallbackFolderInput");
      if (fallback) fallback.click();
    }
  }

  async function scanDirHandle(dirHandle) {
    for await (const entry of dirHandle.values()) {
      if (entry.kind === "file") {
        if (ACCEPTED_EXTENSIONS.includes(extOf(entry.name))) {
          const f = await entry.getFile();
          addFile(f, entry);
        }
      } else if (entry.kind === "directory") {
        await scanDirHandle(entry);
      }
    }
  }

  /* ---------- Queue ---------- */

  function addFile(file, handle = null) {
    if (!ACCEPTED_EXTENSIONS.includes(extOf(file.name))) {
      toast(`Skipped unsupported file: ${file.name}`, "error");
      return;
    }

    const id = "f_" + Math.random().toString(36).slice(2, 10) + Date.now();
    const item = {
      id, file, handle,
      inspection: null,
      thumbnail: null,
      status: "inspecting",
      scrubBlob: null,
      convertBlob: null,
      convertName: null,
      savedBytes: 0,
      destroyedCount: 0,
      error: null,
    };

    state.files.set(id, item);
    renderQueue();
    inspectItem(item);
  }

  async function inspectItem(item) {
    try {
      const form = new FormData();
      form.append("file", item.file, item.file.name);
      const res = await fetch("/api/inspect", { method: "POST", body: form });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      item.inspection = data.inspection || {};
      item.thumbnail = data.thumbnail || null;
      item.status = "inspected";
    } catch (e) {
      item.status = "error";
      item.error = String(e);
    }
    updateRow(item);
    updateToolbar();
  }

  async function scrubItem(id) {
    const item = state.files.get(id);
    if (!item) return;

    item.status = "scrubbing";
    updateRow(item);

    try {
      const form = new FormData();
      form.append("file", item.file, item.file.name);
      const res = await fetch("/api/scrub", { method: "POST", body: form });

      if (!res.ok) {
        let msg = `HTTP ${res.status}`;
        try { const j = await res.json(); if (j.error) msg = j.error; }
        catch (_) { const t = await res.text().catch(() => ""); if (t) msg = t; }
        throw new Error(msg);
      }

      const blob = await res.blob();
      item.scrubBlob = blob;
      item.savedBytes = item.file.size - blob.size;

      let overwritten = false;
      if (item.handle && typeof item.handle.createWritable === "function") {
        try {
          const w = await item.handle.createWritable();
          await w.write(blob);
          await w.close();
          overwritten = true;
        } catch (err) {
          console.warn("In-place write failed:", err);
        }
      }

      item.status = overwritten ? "overwritten" : "scrubbed";
      state.totalBytesSaved += Math.max(0, item.savedBytes);

      toast(
        overwritten
          ? `Overwritten in place: ${item.file.name}`
          : `Scrubbed: ${item.file.name}`,
        "success"
      );
    } catch (e) {
      item.status = "error";
      item.error = String(e);
      toast(`Scrub failed: ${item.file.name}`, "error");
    }

    updateRow(item);
    updateToolbar();
  }

  async function convertItem(id, targetFormat) {
    const item = state.files.get(id);
    if (!item) return;

    item.status = "converting";
    updateRow(item);

    try {
      const form = new FormData();
      form.append("file", item.file, item.file.name);
      form.append("target_format", targetFormat);
      const res = await fetch("/api/convert", { method: "POST", body: form });

      if (!res.ok) {
        let msg = `HTTP ${res.status}`;
        try { const j = await res.json(); if (j.error) msg = j.error; }
        catch (_) { const t = await res.text().catch(() => ""); if (t) msg = t; }
        throw new Error(msg);
      }

      const blob = await res.blob();
      const cd = res.headers.get("Content-Disposition") || "";
      const m = /filename="([^"]+)"/.exec(cd);
      const outName = m ? m[1] : renameForFormat(item.file.name, targetFormat);

      const destroyedCount = parseInt(res.headers.get("X-Destroyed-Count") || "0", 10);

      item.convertBlob = blob;
      item.convertName = outName;
      item.savedBytes = item.file.size - blob.size;
      item.destroyedCount = destroyedCount;
      item.status = "converted";

      state.totalBytesSaved += Math.max(0, item.savedBytes);

      toast(`Converted to ${targetFormat}: ${item.file.name}`, "success");
    } catch (e) {
      item.status = "error";
      item.error = String(e);
      toast(`Convert failed: ${item.file.name}`, "error");
    }

    updateRow(item);
    updateToolbar();
  }

  /* ---------- Rendering ---------- */

  function renderQueue() {
    const c = $("fileListContainer");
    if (!c) return;

    if (state.files.size === 0) {
      c.innerHTML = "";
      updateToolbar();
      return;
    }

    const alive = new Set([...state.files.keys()]);
    c.querySelectorAll(".file-row").forEach((el) => {
      if (!alive.has(el.dataset.id)) el.remove();
    });

    for (const item of state.files.values()) {
      if (!c.querySelector(`.file-row[data-id="${item.id}"]`)) {
        const row = document.createElement("div");
        row.className = "file-row";
        row.dataset.id = item.id;
        c.appendChild(row);
        updateRow(item);
      }
    }
    updateToolbar();
  }

  function updateRow(item) {
    const row = document.querySelector(`.file-row[data-id="${item.id}"]`);
    if (!row) return;

    const rep = item.inspection || {};
    const ftype = rep.file_type || "?";
    const entries = rep.entries || [];
    const count = rep.metadata_count || 0;

    const isScrubbable = LOSSLESS_SCRUB_FORMATS.has(ftype);
    const isConvertible = CONVERTIBLE_SOURCE_FORMATS.has(ftype);
    const isDone =
      item.status === "scrubbed" ||
      item.status === "overwritten" ||
      item.status === "converted";

    const high = entries.filter((e) => e.risk === "high").length;
    const med = entries.filter((e) => e.risk === "medium").length;
    const low = entries.filter((e) => e.risk === "low").length;
    const maxRisk = high > 0 ? "high" : med > 0 ? "medium" : low > 0 ? "low" : "neutral";

    let thumb;
    if (item.thumbnail) {
      thumb = `<img src="${item.thumbnail}" alt="">`;
    } else if (ftype === "PDF") {
      thumb = `<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>`;
    } else {
      thumb = `<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><polyline points="21 15 16 10 5 21"/></svg>`;
    }

    const metaParts = [`<span class="size">${fmtBytes(item.file.size)}</span>`];
    if (rep.details?.dimensions) metaParts.push(`<span class="dims">${esc(rep.details.dimensions)}</span>`);
    if (rep.details?.pages) {
      const p = rep.details.pages;
      metaParts.push(`<span class="dims">${p} page${p === 1 ? "" : "s"}</span>`);
    }

    let statusLine = "";
    if (item.status === "inspecting") {
      statusLine = `<span class="status-dot neutral"></span><span class="status-text">Reading…</span>`;
    } else if (rep.error) {
      statusLine = `<span class="status-text" style="color:var(--text-3)">No inspection</span>`;
    } else if (count === 0) {
      statusLine = `<span class="status-dot low"></span><span class="status-text">Clean</span>`;
    } else {
      statusLine = `<span class="status-dot ${maxRisk}"></span><span class="status-text">${count} tag${count === 1 ? "" : "s"}</span>`;
    }
    metaParts.push(statusLine);

    let actions = "";
    if (item.status === "scrubbing" || item.status === "converting") {
      actions = `<button class="btn btn-secondary btn-sm" disabled>Working…</button>`;
    } else if (isDone) {
      const label =
        item.status === "overwritten" ? "Overwritten in place"
        : item.status === "converted" ? "Converted"
        : "Scrubbed";
      actions = `
        <span class="done-label">${label}</span>
        <button class="btn btn-outline btn-sm" data-action="download" data-id="${item.id}">Download</button>
        <button class="btn btn-subtle btn-sm" data-action="remove" data-id="${item.id}" title="Remove">✕</button>
      `;
    } else {
      const scrubBtn = isScrubbable
        ? `<button class="btn btn-primary btn-sm" data-action="scrub" data-id="${item.id}">Scrub</button>`
        : "";
      const convertGroup = isConvertible
        ? `<div class="convert-group">
             <select class="convert-select" data-id="${item.id}">
               ${CONVERT_TARGETS.map((t) => `<option value="${t}">${t}</option>`).join("")}
             </select>
             <button class="btn btn-outline btn-sm" data-action="convert" data-id="${item.id}">Convert</button>
           </div>`
        : "";
      actions = `
        ${scrubBtn}
        ${convertGroup}
        <button class="btn btn-subtle btn-sm" data-action="remove" data-id="${item.id}" title="Remove">✕</button>
      `;
    }

    let detail = "";

    if (isDone && count > 0) {
      const pills = entries
        .slice(0, 12)
        .map((e) => `<span class="purged-pill">${esc(cleanKey(e.key))}</span>`)
        .join("");
      const more = entries.length > 12
        ? `<span class="purged-pill more">+${entries.length - 12}</span>`
        : "";
      const destroyNote = item.status === "converted" && item.destroyedCount > 0
        ? `<div class="purged-note">Re-encoded. ${item.destroyedCount} container-level item${item.destroyedCount === 1 ? "" : "s"} destroyed.</div>`
        : "";
      detail = `
        <div class="purged">
          <div class="purged-title">Removed ${entries.length} metadata tag${entries.length === 1 ? "" : "s"}</div>
          ${destroyNote}
          <div class="purged-list">${pills}${more}</div>
        </div>`;
    } else if (count > 0) {
      detail = `
        <div class="inspector">
          <button class="inspector-toggle" aria-expanded="false" data-toggle="1">
            <span>Full metadata inspector (${count} tag${count === 1 ? "" : "s"})</span>
            <span class="chevron">▼</span>
          </button>
          <div class="inspector-body" hidden>
            <input type="text" class="inspector-filter" placeholder="Filter tags">
            ${renderInspectorTable(entries)}
          </div>
        </div>`;
    } else if (rep.error) {
      detail = `<div class="local-err">${esc(rep.error)}</div>`;
    }

    row.innerHTML = `
      <div class="file-main">
        <div class="thumb">${thumb}</div>
        <div class="file-info">
          <div class="file-name-line">
            <span class="file-name" title="${esc(item.file.name)}">${esc(item.file.name)}</span>
            <span class="file-type">${esc(ftype)}</span>
          </div>
          <div class="file-meta">${metaParts.join("")}</div>
        </div>
        <div class="file-actions">${actions}</div>
      </div>
      ${detail}
    `;

    const toggle = row.querySelector(".inspector-toggle");
    if (toggle) {
      toggle.addEventListener("click", (e) => {
        e.stopPropagation();
        const body = row.querySelector(".inspector-body");
        const open = toggle.getAttribute("aria-expanded") === "true";
        toggle.setAttribute("aria-expanded", open ? "false" : "true");
        if (body) body.hidden = open;
      });
    }

    const filter = row.querySelector(".inspector-filter");
    if (filter) {
      filter.addEventListener("input", (e) => {
        const q = e.target.value.toLowerCase().trim();
        row.querySelectorAll(".meta-row").forEach((r) => {
          const t = r.dataset.filter || "";
          r.classList.toggle("hidden", q && !t.includes(q));
        });
      });
    }
  }

  /**
   * Render the metadata inspector as rows sorted by risk (high → medium → low).
   * Within each risk band, entries are grouped by category with a visible
   * category header between groups.
   */
  function renderInspectorTable(entries) {
    const sorted = [...entries].sort((a, b) => {
      const ra = RISK_RANK[a.risk] ?? 9;
      const rb = RISK_RANK[b.risk] ?? 9;
      if (ra !== rb) return ra - rb;
      const ca = String(a.category || "").localeCompare(String(b.category || ""));
      if (ca !== 0) return ca;
      return String(a.key || "").localeCompare(String(b.key || ""));
    });

    let html = `<div class="meta-groups">`;
    let lastCat = null;

    for (const e of sorted) {
      const cat = e.category || "Other";
      if (cat !== lastCat) {
        html += `<div class="meta-group-header">${esc(cat)}</div>`;
        lastCat = cat;
      }

      const filterText = `${e.category} ${e.key} ${e.value}`.toLowerCase();
      const hasCoords =
        e.lat !== null && e.lat !== undefined &&
        e.lon !== null && e.lon !== undefined;

      const valueHtml = hasCoords
        ? `<div class="meta-value gps">
             <code class="gps-coords">${e.lat.toFixed(5)}, ${e.lon.toFixed(5)}</code>
             <div class="gps-actions">
               <a href="https://www.openstreetmap.org/?mlat=${e.lat}&mlon=${e.lon}#map=16/${e.lat}/${e.lon}"
                  target="_blank" rel="noopener noreferrer">View on map</a>
               <button type="button" class="gps-copy"
                       data-lat="${e.lat}" data-lon="${e.lon}">Copy</button>
             </div>
           </div>`
        : `<div class="meta-value">${esc(e.value)}</div>`;

      html += `
        <div class="meta-row" data-filter="${esc(filterText)}">
          <div class="meta-key">${esc(cleanKey(e.key))}</div>
          ${valueHtml}
          <span class="risk-square ${esc(e.risk)}" title="${esc(riskLabel(e.risk))}"></span>
        </div>`;
    }

    html += `</div>`;
    return html;
  }

  /* ---------- Toolbar ---------- */

  function updateToolbar() {
    const t = $("batchToolbar");
    if (!t) return;

    if (state.files.size === 0) {
      t.classList.add("hidden");
      return;
    }
    t.classList.remove("hidden");

    let totalTags = 0;
    for (const item of state.files.values()) {
      totalTags += (item.inspection && item.inspection.metadata_count) || 0;
    }

    const fEl = $("statFileCount");
    const tEl = $("statMetadataCount");
    const bEl = $("statBytesSaved");
    if (fEl) fEl.textContent = String(state.files.size);
    if (tEl) tEl.textContent = String(totalTags);
    if (bEl) bEl.textContent = fmtBytes(state.totalBytesSaved);
  }

  /* ---------- Delegated actions ---------- */

  function initCardActions() {
    const c = $("fileListContainer");
    if (!c) return;

    c.addEventListener("click", async (e) => {
      const copy = e.target.closest(".gps-copy");
      if (copy) {
        const lat = copy.dataset.lat;
        const lon = copy.dataset.lon;
        try {
          await navigator.clipboard.writeText(`${lat}, ${lon}`);
          const orig = copy.textContent;
          copy.textContent = "Copied";
          setTimeout(() => (copy.textContent = orig), 1500);
        } catch (_) {}
        return;
      }

      const btn = e.target.closest("[data-action]");
      if (!btn) return;

      const id = btn.dataset.id;
      const action = btn.dataset.action;
      const item = state.files.get(id);

      if (action === "remove") {
        state.files.delete(id);
        document.querySelector(`.file-row[data-id="${id}"]`)?.remove();
        updateToolbar();
        return;
      }
      if (action === "scrub") { scrubItem(id); return; }
      if (action === "convert") {
        const sel = c.querySelector(`.convert-select[data-id="${id}"]`);
        convertItem(id, sel ? sel.value : "JPEG");
        return;
      }
      if (action === "download") {
        if (!item) return;
        const blob = item.convertBlob || item.scrubBlob;
        if (!blob) return;
        const name = item.convertBlob
          ? (item.convertName || renameForFormat(item.file.name, "JPEG"))
          : item.file.name;
        triggerDownload(blob, name);
      }
    });
  }

  /* ---------- Batch ---------- */

  async function scrubAll() {
    for (const item of [...state.files.values()]) {
      const ftype = item.inspection && item.inspection.file_type;
      if (!LOSSLESS_SCRUB_FORMATS.has(ftype)) continue;
      if (item.status === "scrubbed" || item.status === "overwritten" || item.status === "converted") continue;
      await scrubItem(item.id);
    }
    toast("Batch scrub complete", "success");
  }

  async function downloadAllZip() {
    if (typeof JSZip === "undefined") {
      toast("JSZip not loaded", "error");
      return;
    }
    const zip = new JSZip();
    let n = 0;
    for (const item of state.files.values()) {
      const blob = item.convertBlob || item.scrubBlob;
      if (!blob) continue;
      const name = item.convertBlob
        ? (item.convertName || renameForFormat(item.file.name, "JPEG"))
        : item.file.name;
      zip.file(name, blob);
      n++;
    }
    if (n === 0) { toast("Nothing to download", "info"); return; }
    const blob = await zip.generateAsync({ type: "blob" });
    triggerDownload(blob, "hermes-clean.zip");
  }

  function clearAll() {
    state.files.clear();
    state.totalBytesSaved = 0;
    const c = $("fileListContainer");
    if (c) c.innerHTML = "";
    updateToolbar();
  }

  /* ---------- Drop zone ---------- */

  function initDropzone() {
    const dz = $("dropZone");
    if (!dz) return;

    $("selectFilesBtn")?.addEventListener("click", (e) => {
      e.stopPropagation();
      pickFiles();
    });
    $("selectFolderBtn")?.addEventListener("click", (e) => {
      e.stopPropagation();
      pickFolder();
    });

    const fallbackFile = $("fallbackFileInput");
    const fallbackFolder = $("fallbackFolderInput");
    if (fallbackFile) {
      fallbackFile.addEventListener("change", (e) => {
        Array.from(e.target.files || []).forEach((f) => addFile(f));
        e.target.value = "";
      });
    }
    if (fallbackFolder) {
      fallbackFolder.addEventListener("change", (e) => {
        Array.from(e.target.files || []).forEach((f) => addFile(f));
        e.target.value = "";
      });
    }

    ["dragenter", "dragover"].forEach((ev) =>
      dz.addEventListener(ev, (e) => {
        e.preventDefault(); e.stopPropagation();
        dz.classList.add("dragover");
      })
    );
    ["dragleave", "drop"].forEach((ev) =>
      dz.addEventListener(ev, (e) => {
        e.preventDefault(); e.stopPropagation();
        dz.classList.remove("dragover");
      })
    );

    dz.addEventListener("drop", async (e) => {
      const items = e.dataTransfer.items;
      if (items && items.length && items[0].getAsFileSystemHandle) {
        for (let i = 0; i < items.length; i++) {
          const it = items[i];
          try {
            const h = await it.getAsFileSystemHandle();
            if (h.kind === "file") {
              const f = await h.getFile();
              addFile(f, h);
            } else if (h.kind === "directory") {
              await scanDirHandle(h);
            }
          } catch (err) {
            const f = it.getAsFile();
            if (f) addFile(f);
          }
        }
      } else {
        Array.from(e.dataTransfer.files || []).forEach((f) => addFile(f));
      }
    });

    dz.addEventListener("click", (e) => {
      if (e.target.closest("button")) return;
      pickFiles();
    });
    dz.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        pickFiles();
      }
    });
  }

  /* ---------- Local path ---------- */

  function initLocalPath() {
    const form = $("localPathForm");
    if (!form) return;

    document.querySelectorAll(".preset-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        const input = $("localPathInput");
        if (input) input.value = btn.dataset.path || "";
      });
    });

    const inspectBtn = $("localInspectBtn");
    if (inspectBtn) {
      inspectBtn.addEventListener("click", async () => {
        const path = $("localPathInput")?.value?.trim();
        if (!path) return;
        inspectBtn.disabled = true;
        const box = $("localResultBox");
        if (box) {
          box.classList.remove("hidden");
          box.innerHTML = `<div class="local-msg">Reading metadata from <code>${esc(path)}</code>…</div>`;
        }
        try {
          const res = await fetch("/api/inspect-local-path", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              path,
              recursive: $("localRecursiveCheck")?.checked ?? true,
            }),
          });
          const data = await res.json();
          if (!res.ok || data.error) throw new Error(data.error || `HTTP ${res.status}`);
          renderLocalInspect(data);
        } catch (e) {
          if (box) box.innerHTML = `<div class="local-err">${esc(String(e))}</div>`;
        } finally {
          inspectBtn.disabled = false;
        }
      });
    }

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const path = $("localPathInput")?.value?.trim();
      if (!path) return;

      const btn = $("localScrubSubmitBtn");
      if (!btn) return;
      btn.disabled = true;

      const box = $("localResultBox");
      if (box) {
        box.classList.remove("hidden");
        box.innerHTML = `<div class="local-msg">Scrubbing <code>${esc(path)}</code>…</div>`;
      }

      try {
        const res = await fetch("/api/scrub-local-path", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            path,
            recursive: $("localRecursiveCheck")?.checked ?? true,
            backup: $("localBackupCheck")?.checked ?? false,
          }),
        });
        const data = await res.json();
        if (!res.ok || data.error) throw new Error(data.error || `HTTP ${res.status}`);
        renderLocalScrub(data);
        toast("Local scrub complete", "success");
      } catch (e) {
        if (box) box.innerHTML = `<div class="local-err">${esc(String(e))}</div>`;
        toast("Local scrub failed", "error");
      } finally {
        btn.disabled = false;
      }
    });
  }

  function renderEntriesTable(entries) {
    if (!entries || entries.length === 0) {
      return `<div class="local-msg" style="margin-top:8px">No metadata tags found.</div>`;
    }
    return renderInspectorTable(entries);
  }

  function renderLocalInspect(data) {
    const box = $("localResultBox");
    if (!box) return;
    const r = data.result || {};

    if (data.mode === "file") {
      box.innerHTML = `
        <div class="local-summary ok">
          <div class="title">${esc(r.filename || "")} <span class="file-type">${esc(r.file_type || "?")}</span></div>
          <div class="path">${esc(r.path || "")}</div>
          <div>${r.metadata_count || 0} metadata tag${r.metadata_count === 1 ? "" : "s"} found.</div>
        </div>
        ${renderEntriesTable(r.entries)}`;
    } else {
      const files = r.files || [];
      box.innerHTML = `
        <div class="local-summary ok">
          <div class="title">Folder</div>
          <div class="path">${esc(r.directory || "")}</div>
          <div>${r.total_files || 0} files, ${r.total_tags || 0} total tags.</div>
        </div>
        <div class="file-list-local">
          ${files.map((f) => `
            <details class="local-details">
              <summary>${esc(f.filename || "")} — ${f.metadata_count || 0} tag${f.metadata_count === 1 ? "" : "s"}</summary>
              ${renderEntriesTable(f.entries)}
            </details>
          `).join("")}
        </div>`;
    }
  }

  function renderLocalScrub(data) {
    const box = $("localResultBox");
    if (!box) return;
    const r = data.result || {};

    if (data.mode === "file") {
      const stripped = r.stripped_items || [];
      box.innerHTML = `
        <div class="local-summary ${r.success ? "ok" : "err"}">
          <div class="title">${r.success ? "Scrubbed" : "Failed"}: ${esc(r.filename || "")}</div>
          ${r.error ? `<div class="local-err">${esc(r.error)}</div>` : ""}
          ${r.bytes_saved ? `<div>Saved ${fmtBytes(r.bytes_saved)}</div>` : ""}
          ${stripped.length ? `
            <details class="local-details">
              <summary>${stripped.length} items removed</summary>
              <ul>${stripped.map((s) => `<li>${esc(s)}</li>`).join("")}</ul>
            </details>` : ""}
        </div>`;
    } else {
      const files = r.files || [];
      box.innerHTML = `
        <div class="local-summary ok">
          <div class="title">${r.total_scrubbed || 0} of ${r.total_found || 0} files scrubbed</div>
          <div>Saved ${fmtBytes(r.total_bytes_saved || 0)} total</div>
        </div>
        <details class="local-details">
          <summary>Per-file detail</summary>
          <ul>
            ${files.map((f) => `
              <li class="${f.success ? "ok" : "err"}">
                ${esc(f.filename || "")} — ${f.success ? fmtBytes(f.bytes_saved || 0) : esc(f.error || "failed")}
              </li>`).join("")}
          </ul>
        </details>`;
    }
  }

  /* ---------- Boot ---------- */

  function init() {
    initTheme();
    initTabs();
    initDropzone();
    initLocalPath();
    initCardActions();

    $("scrubAllBtn")?.addEventListener("click", scrubAll);
    $("downloadAllZipBtn")?.addEventListener("click", downloadAllZip);
    $("clearAllBtn")?.addEventListener("click", clearAll);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();