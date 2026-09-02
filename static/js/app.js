/* AK YouTube Downloader — front-end */
(function () {
  "use strict";

  const $ = (s, r) => (r || document).querySelector(s);
  const esc = (s) => (s || "").replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

  /* ------------------------------------------------ toast */
  let toastTimer;
  function toast(msg) {
    const t = $("#toast");
    if (!t) return;
    t.textContent = msg;
    t.classList.add("on");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => t.classList.remove("on"), 3600);
  }

  /* ------------------------------------------------ mobile nav */
  const navToggle = $("#navToggle");
  const siteNav = $("#siteNav");
  if (navToggle && siteNav) {
    navToggle.addEventListener("click", () => {
      const open = siteNav.classList.toggle("open");
      navToggle.setAttribute("aria-expanded", open ? "true" : "false");
      navToggle.setAttribute("aria-label", open ? "Close menu" : "Open menu");
    });
    siteNav.addEventListener("click", (e) => {
      if (e.target.tagName === "A") {
        siteNav.classList.remove("open");
        navToggle.setAttribute("aria-expanded", "false");
      }
    });
  }

  /* ------------------------------------------------ helpers */
  async function api(path, body) {
    const res = await fetch(path, body !== undefined
      ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }
      : {});
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || "Something went wrong. Please try again.");
    return data;
  }
  const fmtSize = (b) => !b ? "" : b < 1048576 ? (b / 1024).toFixed(0) + " KB"
    : b < 1073741824 ? (b / 1048576).toFixed(1) + " MB" : (b / 1073741824).toFixed(2) + " GB";
  const fmtTime = (s) => {
    s = Math.round(s || 0);
    const h = (s / 3600) | 0, m = ((s % 3600) / 60) | 0, x = s % 60;
    return (h ? h + ":" + String(m).padStart(2, "0") : m) + ":" + String(x).padStart(2, "0");
  };
  const fmtViews = (n) => !n ? "" : n >= 1e6 ? (n / 1e6).toFixed(1) + "M views"
    : n >= 1e3 ? Math.round(n / 1e3) + "K views" : n + " views";

  /* ------------------------------------------------ contact form */
  const contactForm = $("#contactForm");
  if (contactForm) {
    contactForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      const note = $("#cNote"), btn = $("#cSend");
      btn.disabled = true; btn.textContent = "Sending…";
      try {
        await api("/api/contact", {
          name: $("#cName").value, email: $("#cEmail").value,
          subject: $("#cSubject").value, message: $("#cMessage").value,
        });
        note.hidden = false; note.className = "form-note ok";
        note.textContent = "Thanks — your message has been received. We usually reply within two working days.";
        contactForm.reset();
      } catch (err) {
        note.hidden = false; note.className = "form-note bad"; note.textContent = err.message;
      } finally { btn.disabled = false; btn.textContent = "Send message"; }
    });
  }

  /* ------------------------------------------------ downloader tool */
  const tool = $("#tool");
  if (!tool) return;

  const els = {
    form: $("#toolForm"), url: $("#url"), get: $("#getBtn"),
    error: $("#toolError"), card: $("#toolCard"),
    thumb: $("#thumb"), title: $("#vTitle"), meta: $("#vMeta"),
    options: $("#toolOptions"), kind: $("#kind"), quality: $("#quality"),
    qualityLabel: $("#qualityLabel"), start: $("#startBtn"), startLabel: $("#startLabel"),
    state: $("#toolState"), stateText: $("#stateText"), stateSub: $("#stateSub"),
    track: $("#track"), bar: $("#bar"), cancel: $("#cancelBtn"),
    done: $("#toolDone"), doneName: $("#doneName"), doneSub: $("#doneSub"),
    saveAgain: $("#saveAgain"),
  };

  let info = null;
  let activeJobId = null;
  let saved = new Set();

  // remember the visitor's last format choice (per browser, never sent anywhere)
  let preferred = tool.dataset.mode || "video";
  try {
    const stored = localStorage.getItem("ak-format");
    if (tool.dataset.mode !== "audio" && (stored === "video" || stored === "audio")) preferred = stored;
  } catch (e) { /* storage blocked — ignore */ }
  els.kind.value = preferred;

  function showError(msg) {
    els.error.hidden = false;
    els.error.textContent = msg;
  }
  function clearError() { els.error.hidden = true; els.error.textContent = ""; }

  function fillQuality() {
    const audio = els.kind.value === "audio";
    els.qualityLabel.textContent = audio ? "Audio quality" : "Video quality";
    els.quality.innerHTML = "";
    if (audio) {
      [["320", "320 kbps — best"], ["256", "256 kbps"], ["192", "192 kbps — recommended"],
       ["128", "128 kbps — smallest"]].forEach(([v, l]) => els.quality.add(new Option(l, v)));
      els.quality.value = "192";
    } else {
      const labels = { 2160: "2160p — 4K", 1440: "1440p — 2K", 1080: "1080p — Full HD",
                       720: "720p — HD", 480: "480p", 360: "360p", 240: "240p", 144: "144p" };
      const hs = (info && info.heights && info.heights.length) ? info.heights : [1080, 720, 480, 360];
      els.quality.add(new Option("Best available", "best"));
      hs.slice(0, 7).forEach((h) => els.quality.add(new Option(labels[h] || h + "p", String(h))));
      els.quality.value = hs.indexOf(1080) !== -1 ? "1080" : "best";
    }
    els.startLabel.textContent = audio ? "Download MP3" : "Download MP4";
  }

  els.kind.addEventListener("change", () => {
    try { localStorage.setItem("ak-format", els.kind.value); } catch (e) {}
    fillQuality();
  });

  /* --------------------------- step 1: fetch details */
  els.form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const url = els.url.value.trim();
    if (!url) return;
    clearError();
    els.get.disabled = true;
    els.get.textContent = "Checking…";
    els.card.hidden = true;
    els.state.hidden = true;
    els.done.hidden = true;
    try {
      info = await api("/api/info", { url });
      renderInfo(info);
    } catch (err) {
      showError(err.message);
    } finally {
      els.get.disabled = false;
      els.get.textContent = "Get video";
    }
  });

  function renderInfo(v) {
    const thumbBox = els.thumb.parentElement;
    if (v.thumbnail) {
      els.thumb.src = v.thumbnail;
      thumbBox.style.display = "";
    } else {
      els.thumb.removeAttribute("src");
      thumbBox.style.display = "none";
    }
    els.thumb.onerror = () => { thumbBox.style.display = "none"; };
    els.thumb.alt = v.title ? "Thumbnail for " + v.title : "";
    els.title.textContent = v.title;
    els.meta.textContent = [v.uploader, v.duration ? fmtTime(v.duration) : "", fmtViews(v.view_count)]
      .filter(Boolean).join("  ·  ");
    fillQuality();
    els.options.hidden = false;
    els.card.hidden = false;
    els.card.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  /* --------------------------- step 2: start the download */
  els.start.addEventListener("click", async () => {
    if (!info) return;
    const audio = els.kind.value === "audio";
    clearError();
    els.done.hidden = true;
    els.state.hidden = false;
    els.track.classList.add("indet");
    els.bar.style.width = "0%";
    els.stateText.textContent = "Starting…";
    els.stateSub.textContent = "Contacting YouTube";
    els.start.disabled = true;
    try {
      const res = await api("/api/download", {
        url: info.url,
        mode: audio ? "audio" : "video",
        quality: audio ? "best" : els.quality.value,
        audio_format: "mp3",
        audio_quality: audio ? els.quality.value : "192",
      });
      activeJobId = res.job.id;
      if (res.duplicate) toast("This one is already being prepared.");
    } catch (err) {
      els.state.hidden = true;
      els.start.disabled = false;
      showError(err.message);
    }
  });

  els.cancel.addEventListener("click", async () => {
    if (!activeJobId) return;
    try { await api("/api/jobs/" + activeJobId + "/cancel", {}); } catch (e) {}
  });

  /* --------------------------- step 3: progress + automatic save */
  function onJob(job) {
    if (job.id !== activeJobId) return;

    if (job.status === "queued") {
      els.stateText.textContent = "Waiting in queue…";
      els.stateSub.textContent = "Another download is finishing first";
      els.track.classList.add("indet");
    } else if (job.status === "downloading") {
      const pct = job.progress || 0;
      els.stateText.textContent = "Downloading… " + pct.toFixed(0) + "%";
      els.track.classList.toggle("indet", !job.total);
      els.bar.style.width = pct + "%";
      const bits = [];
      if (job.total) bits.push(fmtSize(job.downloaded) + " of " + fmtSize(job.total));
      if (job.speed) bits.push((job.speed / 1048576).toFixed(1) + " MB/s");
      if (job.eta) bits.push(fmtTime(job.eta) + " left");
      els.stateSub.textContent = bits.join("  ·  ");
    } else if (job.status === "processing") {
      els.stateText.textContent = job.stage || "Preparing your file…";
      els.stateSub.textContent = "Almost there — this part can take a moment on large files";
      els.track.classList.add("indet");
    } else if (job.status === "done") {
      els.state.hidden = true;
      els.start.disabled = false;
      els.done.hidden = false;
      els.doneName.textContent = job.filename || "Your file is ready";
      els.doneSub.textContent = (job.mode === "audio" ? "MP3" : "MP4")
        + (job.filesize ? "  ·  " + fmtSize(job.filesize) : "") + "  ·  saving to your device…";
      els.saveAgain.href = "/api/file/" + job.id;
      els.saveAgain.setAttribute("download", job.filename || "");
      if (!saved.has(job.id)) {
        saved.add(job.id);
        saveToDevice(job);
        setTimeout(() => {
          els.doneSub.textContent = (job.mode === "audio" ? "MP3" : "MP4")
            + (job.filesize ? "  ·  " + fmtSize(job.filesize) : "")
            + "  ·  saved. If your browser blocked it, press Save again.";
        }, 1400);
      }
      toast("Download ready — saving to your device");
    } else if (job.status === "error") {
      els.state.hidden = true;
      els.start.disabled = false;
      showError(job.error || "The download failed. Please try again.");
    } else if (job.status === "cancelled") {
      els.state.hidden = true;
      els.start.disabled = false;
      toast("Download cancelled");
    }
  }

  function saveToDevice(job) {
    const a = document.createElement("a");
    a.href = "/api/file/" + job.id;
    a.download = job.filename || "";
    a.rel = "noopener";
    a.style.display = "none";
    document.body.appendChild(a);
    a.click();
    setTimeout(() => a.remove(), 1000);
  }

  /* --------------------------- live updates */
  let attempts = 0;
  function connect() {
    let ws;
    try {
      ws = new WebSocket((location.protocol === "https:" ? "wss" : "ws") + "://" + location.host + "/ws");
    } catch (e) { return pollFallback(); }
    ws.onopen = () => { attempts = 0; };
    ws.onmessage = (ev) => {
      const msg = JSON.parse(ev.data);
      if (msg.type === "snapshot") msg.jobs.forEach(onJob);
      if (msg.type === "job") onJob(msg.job);
    };
    ws.onclose = () => {
      attempts++;
      if (attempts > 6) return pollFallback();
      setTimeout(connect, Math.min(5000, 600 * attempts));
    };
    ws.onerror = () => ws.close();
  }

  // If websockets are blocked (some corporate proxies), fall back to polling.
  let polling = false;
  function pollFallback() {
    if (polling) return;
    polling = true;
    setInterval(async () => {
      if (!activeJobId) return;
      try {
        const data = await api("/api/jobs");
        const job = data.jobs.find((j) => j.id === activeJobId);
        if (job) onJob(job);
      } catch (e) {}
    }, 1500);
  }

  connect();
})();
