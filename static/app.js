(function () {
  "use strict";

  const $ = (s) => document.querySelector(s);
  const $$ = (s) => Array.prototype.slice.call(document.querySelectorAll(s));
  const tasks = new Map();
  const following = new Set();
  let currentId = null;
  let pickedFile = null;
  let uploadPct = null;
  let settingsCache = { values: {}, help: {} };
  const ws = { videoTask: null, videoEl: null, linesTask: null, lineEls: [], segs: [], active: -1, follow: true, noteKey: null };

  const ORDER = ["upload", "downloading", "extracting", "slicing", "transcribing", "polishing"];
  const STATUS_TEXT = { pending: "排队中", running: "处理中", transcribed: "转写完成", done: "已完成", failed: "失败", canceled: "已取消" };
  const STAGE_TEXT = { queued: "等待开始", downloading: "下载视频", extracting: "提取音频", slicing: "语音切片", transcribing: "语音转写", transcribed: "等待生成文稿", polishing: "AI 生成文稿", done: "完成", failed: "失败" };
  const STYLE_TEXT = { general: "整理文稿", note: "结构化笔记", article: "公众号文章", clean: "仅清洗润色" };
  // 默认风格；页面把选择结果记在curStyle 里
  let curStyle = "general";
  // 已生成过文稿的风格集合（当前任务），用于提示与按钮文案
  let generatedStyles = new Set();

  function esc(s) { return String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;"); }

  function inline(s) {
    s = esc(s);
    s = s.replace(/`([^`]+)`/g, "<code>$1</code>");
    s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    s = s.replace(/(^|[^*])\*([^*]+)\*/g, "$1<em>$2</em>");
    return s;
  }

  function renderMarkdown(md) {
    const lines = String(md || "").split("\n");
    const out = [];
    let i = 0;
    const isBreak = (l) => /^(#{1,6}\s|```|\s*(-|\*)\s|\s*\d+\.\s|\s*>)/.test(l);
    while (i < lines.length) {
      const line = lines[i];
      if (/^```/.test(line)) {
        const buf = [];
        i++;
        while (i < lines.length && !/^```/.test(lines[i])) { buf.push(esc(lines[i])); i++; }
        i++;
        out.push("<pre><code>" + buf.join("\n") + "</code></pre>");
        continue;
      }
      const h = line.match(/^(#{1,6})\s+(.*)$/);
      if (h) { out.push("<h" + h[1].length + ">" + inline(h[2]) + "</h" + h[1].length + ">"); i++; continue; }
      if (/^\s*(-|\*)\s+/.test(line)) {
        const items = [];
        while (i < lines.length && /^\s*(-|\*)\s+/.test(lines[i])) { items.push("<li>" + inline(lines[i].replace(/^\s*(-|\*)\s+/, "")) + "</li>"); i++; }
        out.push("<ul>" + items.join("") + "</ul>"); continue;
      }
      if (/^\s*\d+\.\s+/.test(line)) {
        const items = [];
        while (i < lines.length && /^\s*\d+\.\s+/.test(lines[i])) { items.push("<li>" + inline(lines[i].replace(/^\s*\d+\.\s+/, "")) + "</li>"); i++; }
        out.push("<ol>" + items.join("") + "</ol>"); continue;
      }
      if (/^\s*>\s?/.test(line)) {
        const buf = [];
        while (i < lines.length && /^\s*>\s?/.test(lines[i])) { buf.push(inline(lines[i].replace(/^\s*>\s?/, ""))); i++; }
        out.push("<blockquote>" + buf.join("<br>") + "</blockquote>"); continue;
      }
      if (/^(-{3,}|\*{3,})$/.test(line.trim())) { out.push("<hr>"); i++; continue; }
      if (!line.trim()) { i++; continue; }
      const buf = [];
      while (i < lines.length && lines[i].trim() && !isBreak(lines[i])) { buf.push(inline(lines[i])); i++; }
      out.push("<p>" + buf.join("<br>") + "</p>");
    }
    return out.join("");
  }

  function fmtTime(sec) {
    sec = Math.max(0, Math.floor(sec || 0));
    const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
    const pad = (n) => String(n).padStart(2, "0");
    return h > 0 ? h + ":" + pad(m) + ":" + pad(s) : pad(m) + ":" + pad(s);
  }

  let toastTimer = null;
  function toast(msg, isErr) {
    const el = $("#toast");
    el.textContent = msg;
    el.className = "toast" + (isErr ? " err" : "");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.add("hidden"), 3200);
  }

  async function api(path, opts) {
    const r = await fetch(path, Object.assign({ headers: { "Content-Type": "application/json" } }, opts));
    if (!r.ok) {
      let msg = "请求失败 " + r.status;
      try { const d = await r.json(); msg = d.detail || msg; } catch (e) {}
      throw new Error(msg);
    }
    return r.json();
  }

  function uploadFile(file, style) {
    return new Promise((resolve, reject) => {
      const fd = new FormData();
      fd.append("file", file);
      fd.append("style", style);
      const xhr = new XMLHttpRequest();
      xhr.open("POST", "/api/tasks/upload");
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable) {
          uploadPct = Math.round((e.loaded / e.total) * 100);
          render();
        }
      };
      xhr.onload = () => {
        try { resolve(JSON.parse(xhr.responseText)); }
        catch (e) { reject(new Error("上传失败 " + xhr.status)); }
      };
      xhr.onerror = () => reject(new Error("网络错误，上传失败"));
      xhr.send(fd);
    });
  }

  // ---------- 渲染 ----------
  function rowPct(t, key) {
    if (key === "upload") return uploadPct == null ? (t ? 100 : 0) : uploadPct;
    if (!t) return 0;
    if (t.status === "done") return 100;
    if (t.stage === "transcribed") return key === "transcribing" ? 100 : (key === "polishing" ? 0 : (ORDER.indexOf(key) < ORDER.indexOf("transcribing") ? 100 : 0));
    const idx = ORDER.indexOf(key), cur = ORDER.indexOf(t.stage);
    if (cur < 0) return 0;
    if (idx < cur) return 100;
    if (idx > cur) return 0;
    return t.spercent || 0;
  }

  function render() {
    renderHistory();
    renderWorkspace();
  }

  function renderHistory() {
    const list = Array.from(tasks.values()).sort((a, b) => b.created_at - a.created_at);
    const box = $("#history-list");
    if (!list.length) { box.innerHTML = '<div class="empty">暂无记录</div>'; return; }
    box.innerHTML = list.map((t) => {
      const when = new Date((t.created_at || 0) * 1000);
      const time = isNaN(when.getTime()) ? "" : when.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
      return '<div class="hitem' + (t.id === currentId ? " on" : "") + '" data-id="' + t.id + '">' +
        '<div class="hmain"><b>' + esc((t.meta && t.meta.title) || t.url || "(无标题)") + '</b>' +
        '<span>' + esc(time) + " · " + esc((t.meta && t.meta.uploader) || t.platform || "") + '</span></div>' +
        '<span class="hstatus ' + esc(t.status) + '">' + esc(STATUS_TEXT[t.status] || t.status) + '</span>' +
        '<button class="icon hdel" data-del="' + t.id + '" title="删除">×</button>' +
        "</div>";
    }).join("");
  }

  function renderWorkspace() {
    const t = tasks.get(currentId);
    $$(".prow").forEach((row) => {
      const key = row.dataset.stage;
      const pct = rowPct(t, key);
      row.querySelector(".pbar i").style.width = pct + "%";
      row.querySelector(".ppct").textContent = Math.round(pct) + "%";
    });
    const title = $("#ws-title"), st = $("#ws-status"), msg = $("#ws-msg");
    if (!t) {
      title.textContent = "尚未开始任务";
      st.textContent = "";
      st.className = "status";
      msg.textContent = "";
      $("#ws-error").classList.add("hidden");
      return;
    }
    title.textContent = (t.meta && t.meta.title) || t.url || "(无标题)";
    st.className = "status " + t.status;
    st.textContent = (STATUS_TEXT[t.status] || t.status) + (t.status === "running" ? " · " + (STAGE_TEXT[t.stage] || t.stage) : "");
    msg.textContent = t.message || "";
    const err = $("#ws-error");
    if (t.error) { err.textContent = t.error; err.classList.remove("hidden"); } else err.classList.add("hidden");
    const logs = $("#ws-logs");
    if (!logs.classList.contains("hidden")) {
      logs.innerHTML = (t.events || []).slice(-80).map((e) => '<div class="' + e.level + '">' + esc(e.text) + "</div>").join("");
    }
    ensureVideo(t);
    ensureLines(t);
    ensureNote(t);
    renderPolishBar(t);
  }

  // 「生成文稿」按钮的可用状态与文案，以及成稿元信息
  function renderPolishBar(t) {
    const btn = $("#btn-polish"), meta = $("#note-meta");
    const hasText = !!(t && t.transcript && t.transcript.length);
    const canRun = hasText && (t.status === "transcribed" || t.status === "done");
    btn.disabled = !canRun;
    if (t && t.status === "running" && t.stage === "polishing") {
      btn.textContent = "⏳ 正在生成…";
    } else if (generatedStyles.has(curStyle)) {
      btn.textContent = "↻ 重新生成「" + (STYLE_TEXT[curStyle] || curStyle) + "」";
    } else {
      btn.textContent = "✨ 生成「" + (STYLE_TEXT[curStyle] || curStyle) + "」";
    }
    const info = (t && t.note_info) || {};
    if (info.style) {
      meta.textContent = (STYLE_TEXT[info.style] || info.style) + " · " +
        (info.chars || 0) + " 字 · " + (info.at || "");
    } else {
      meta.textContent = "";
    }
  }

  function ensureVideo(t) {
    const host = $("#video-host");
    const has = !!(t && t.meta && t.meta.video_path);
    if (ws.videoTask !== currentId) {
      ws.videoTask = currentId;
      ws.videoEl = null;
      host.innerHTML = has
        ? '<video controls preload="metadata" playsinline src="/api/media/' + currentId + '/video.mp4"></video>'
        : '<div class="empty">暂无视频</div>';
      if (has) {
        ws.videoEl = host.querySelector("video");
        ws.videoEl.addEventListener("timeupdate", () => syncActive(ws.videoEl.currentTime));
        ws.videoEl.addEventListener("seeked", () => syncActive(ws.videoEl.currentTime, true));
      }
    }
  }

  function ensureLines(t) {
    const box = $("#lines");
    if (ws.linesTask !== currentId) {
      ws.linesTask = currentId;
      ws.active = -1;
      ws.segs = (t && t.segments) || [];
      box.innerHTML = ws.segs.length
        ? ws.segs.map((s, i) =>
            '<div class="line" data-i="' + i + '"><time>' + fmtTime(s.start) + "</time><span>" + esc(s.text || "") + "</span></div>").join("")
        : '<div class="empty">暂无转写结果</div>';
      ws.lineEls = Array.prototype.slice.call(box.querySelectorAll(".line"));
      ws.lineEls.forEach((line) => {
        line.addEventListener("click", () => {
          const i = parseInt(line.dataset.i, 10);
          const s = ws.segs[i];
          if (!s) return;
          if (ws.videoEl) {
            ws.videoEl.currentTime = Math.max(0, (s.start || 0) - 0.3);
            ws.videoEl.play().catch(() => {});
          }
          markActive(i, false);
        });
      });
    }
  }

  function ensureNote(t) {
    const box = $("#note-body");
    const key = currentId + "|" + ((t && t.note) ? t.note.length + "|" + t.note.slice(0, 40) : "none");
    if (ws.noteKey !== key) {
      ws.noteKey = key;
      box.innerHTML = (t && t.note) ? renderMarkdown(t.note) : '<div class="empty">转写完成后点击上方按钮生成</div>';
    }
  }

  function findIdx(segs, t) {
    let lo = 0, hi = segs.length - 1, ans = -1;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      if ((segs[mid].start || 0) <= t) { ans = mid; lo = mid + 1; } else hi = mid - 1;
    }
    return ans;
  }

  function markActive(i, scroll) {
    if (i === ws.active) return;
    if (ws.lineEls[ws.active]) ws.lineEls[ws.active].classList.remove("active");
    ws.active = i;
    const line = ws.lineEls[i];
    if (!line) return;
    line.classList.add("active");
    if (scroll && ws.follow) line.scrollIntoView({ block: "center", behavior: "smooth" });
  }

  function syncActive(t, force) {
    if (!ws.segs.length) return;
    markActive(findIdx(ws.segs, t), true);
    if (force && ws.lineEls[ws.active]) ws.lineEls[ws.active].scrollIntoView({ block: "center" });
  }

  // ---------- SSE ----------
  async function follow(tid) {
    if (following.has(tid)) return;
    following.add(tid);
    try {
      const r = await fetch("/api/tasks/" + tid + "/events");
      const reader = r.body.getReader();
      const dec = new TextDecoder();
      let buf = "";
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        const parts = buf.split("\n\n");
        buf = parts.pop();
        for (const p of parts) {
          if (!p.startsWith("data: ")) continue;
          let ev;
          try { ev = JSON.parse(p.slice(6)); } catch (e) { continue; }
          const t = tasks.get(tid);
          if (!t) continue;
          if (ev.type === "state") {
            t.status = ev.status; t.stage = ev.stage; t.percent = ev.percent;
            t.spercent = ev.spercent; t.message = ev.message; t.error = ev.error;
          } else if (ev.type === "log") {
            t.events = t.events || [];
            if (!t.events.some((x) => x.t === ev.t && x.text === ev.text)) t.events.push(ev);
          } else if (ev.type === "eof") {
            await refreshOne(tid);
            render();
            return;
          }
          render();
        }
      }
    } catch (e) {
      /* 忽略网络中断 */
    } finally {
      following.delete(tid);
    }
  }

  async function refreshOne(tid) {
    try {
      const d = await api("/api/tasks/" + tid);
      tasks.set(tid, Object.assign({}, tasks.get(tid), d));
    } catch (e) {}
  }

  function selectTask(id) {
    currentId = id;
    ws.videoTask = null; ws.linesTask = null; ws.noteKey = null;
    ws.videoEl = null; ws.segs = []; ws.lineEls = []; ws.active = -1;
    $("#video-host").innerHTML = '<div class="empty">加载中…</div>';
    $("#lines").innerHTML = '<div class="empty">加载中…</div>';
    render();
    if (id) loadDetail(id);
  }

  // 拉取任务完整详情（列表接口不含note / segments），用于点历史记录后回显全部内容
  async function loadDetail(id) {
    try {
      const d = await api("/api/tasks/" + id);
      if (currentId !== id) return;          // 期间切走了，丢弃这次结果
      tasks.set(id, Object.assign({}, tasks.get(id), d));
      const t = tasks.get(id);
      // 沿用任务自身记录的风格，切换按钮的选中态
      if (t && t.note_info && t.note_info.style && STYLE_TEXT[t.note_info.style]) {
        curStyle = t.note_info.style;
        syncStyleChips();
      }
      generatedStyles = new Set(
        (t && t.note_info && t.note_info.styles) ||
        Object.keys((t && t.variants) || {}).filter((k) => t.variants[k] && t.variants[k].note)
      );
      ws.videoTask = null; ws.linesTask = null; ws.noteKey = null;
      render();
    } catch (e) {
      toast("加载该记录失败：" + e.message, true);
    }
  }

  function syncStyleChips() {
    $$(".style-chip").forEach((b) => b.classList.toggle("on", b.dataset.style === curStyle));
  }

  // ---------- 动作 ----------
  async function start() {
    if (pickedFile) {
      uploadPct = 0;
      try {
        const r = await uploadFile(pickedFile, curStyle);
        pickedFile = null;
        $("#file").value = "";
        $("#drop-text").textContent = "将文件拖放到此处";
        afterCreate(r.id);
      } catch (e) {
        toast(e.message, true);
      } finally {
        uploadPct = null;
        render();
      }
      return;
    }
    const url = $("#url").value.trim();
    if (!url) { toast("请先上传文件或粘贴链接", true); return; }
    try {
      const r = await api("/api/tasks", { method: "POST", body: JSON.stringify({ url: url, style: curStyle }) });
      $("#url").value = "";
      afterCreate(r.id);
    } catch (e) {
      toast(e.message, true);
    }
  }

  function afterCreate(id) {
    tasks.set(id, { id: id, url: "", platform: "", status: "pending", stage: "queued", percent: 0, spercent: 0, meta: {}, segments: [], events: [], created_at: Date.now() / 1000 });
    selectTask(id);
    follow(id);
  }

  async function polish() {
    if (!currentId) return;
    try {
      const r = await api("/api/tasks/" + currentId + "/polish", {
        method: "POST",
        body: JSON.stringify({ style: curStyle })
      });
      if (r && r.idempotent) toast(r.message || "已复用该风格的成稿");
      follow(currentId);
    } catch (e) {
      toast(e.message, true);
    }
  }

  async function removeTask(id) {
    try { await api("/api/tasks/" + id, { method: "DELETE" }); } catch (e) {}
    tasks.delete(id);
    if (currentId === id) {
      const rest = Array.from(tasks.values()).sort((a, b) => b.created_at - a.created_at);
      selectTask(rest.length ? rest[0].id : null);
    } else {
      render();
    }
  }

  function openSettings() {
    const v = settingsCache.values || {}, help = settingsCache.help || {};
    const full = ["asr_base_url", "llm_base_url", "cookie_file", "cookie_text", "proxy", "ffmpeg_path"];
    // 针对 Cookie 相关的框给出「怎么填」的具体提示，避免填错位置
    const PH = {
      cookie_text: "方式1（推荐）：浏览器 F12 → Network → 点任意请求 → Request Headers → " +
        "复制整段 Cookie（形如 a=1; b=2）粘到这里，程序自动转 cookies.txt。也接受 Netscape 格式全文。",
      cookie_browser: "方式2：只填浏览器名，不要粘贴 Cookie 内容。chrome / edge / firefox / brave。",
      cookie_file: "方式3：本机已有的 Netscape cookies.txt 绝对路径，例如 D:\\cookies.txt（一般用不到）。",
    };
    $("#settings-form").innerHTML = Object.keys(help).map((k) => {
      const wide = full.includes(k) ? " full" : "";
      const val = v[k] !== undefined ? v[k] : "";
      const ctl = k === "cookie_text"
        ? '<textarea data-key="' + k + '" rows="5" placeholder="' + esc(PH.cookie_text) + '">' + esc(val) + "</textarea>"
        : '<input data-key="' + k + '" value="' + esc(val) + '" placeholder="' + esc(PH[k] || "") + '">';
      return '<div class="field' + wide + '"><label>' + esc(help[k]) + "</label>" + ctl + "</div>";
    }).join("");
    $("#settings-modal").classList.remove("hidden");
  }

  async function saveSettings() {
    const values = {};
    $("#settings-form").querySelectorAll("input, textarea").forEach((inp) => {
      const k = inp.dataset.key;
      const orig = String((settingsCache.values || {})[k] !== undefined ? settingsCache.values[k] : "");
      if (inp.value === orig) return;
      values[k] = inp.value;
    });
    try {
      const r = await api("/api/settings", { method: "POST", body: JSON.stringify({ values: values }) });
      settingsCache = r;
      $("#settings-modal").classList.add("hidden");
      toast(r && r.cookie_file ? "设置已保存，Cookie 已写入 " + r.cookie_file : "设置已保存");
    } catch (e) {
      toast(e.message, true);
    }
  }

  // ---------- 事件 ----------
  document.addEventListener("click", async (e) => {
    const del = e.target.closest("[data-del]");
    if (del) { e.stopPropagation(); removeTask(del.dataset.del); return; }
    const hitem = e.target.closest(".hitem");
    if (hitem) { selectTask(hitem.dataset.id); $("#history-panel").classList.add("hidden"); return; }

    const chip = e.target.closest(".style-chip");
    if (chip) {
      curStyle = chip.dataset.style;
      syncStyleChips();
      renderPolishBar(tasks.get(currentId));
      return;
    }

    const act = e.target.closest("[data-act]");
    if (act) {
      const a = act.dataset.act;
      if (!currentId) { toast("请先选择或创建任务", true); return; }
      if (a === "copy") {
        const t = tasks.get(currentId);
        if (!t || !t.note) { toast("还没有可复制的文稿", true); return; }
        navigator.clipboard.writeText(t.note).then(() => toast("已复制"), () => toast("复制失败", true));
      }
      return;
    }
    if (e.target.id === "btn-start") start();
    else if (e.target.id === "btn-polish") polish();
    else if (e.target.id === "btn-history") $("#history-panel").classList.toggle("hidden");
    else if (e.target.id === "btn-history-close") $("#history-panel").classList.add("hidden");
    else if (e.target.id === "btn-settings") openSettings();
    else if (e.target.id === "btn-close-settings" || e.target.id === "btn-cancel-settings") $("#settings-modal").classList.add("hidden");
    else if (e.target.id === "btn-save-settings") saveSettings();
    else if (e.target.id === "btn-logs") $("#ws-logs").classList.toggle("hidden");
    else if (e.target.id === "pick") $("#file").click();
  });

  const drop = $("#drop");
  if (drop) {
    drop.addEventListener("click", () => $("#file").click());
    drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("over"); });
    drop.addEventListener("dragleave", () => drop.classList.remove("over"));
    drop.addEventListener("drop", (e) => {
      e.preventDefault();
      drop.classList.remove("over");
      const f = e.dataTransfer.files && e.dataTransfer.files[0];
      if (f) { pickedFile = f; $("#drop-text").textContent = "已选择：" + f.name; }
    });
  }
  const fileInput = $("#file");
  if (fileInput) fileInput.addEventListener("change", () => {
    const f = fileInput.files && fileInput.files[0];
    if (f) { pickedFile = f; $("#drop-text").textContent = "已选择：" + f.name; }
  });
  $("#url").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) start();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { $("#history-panel").classList.add("hidden"); $("#settings-modal").classList.add("hidden"); }
  });

  // ---------- 启动 ----------
  async function boot() {
    try {
      const list = await api("/api/tasks");
      list.forEach((t) => tasks.set(t.id, t));
      const sorted = list.slice().sort((a, b) => b.created_at - a.created_at);
      if (sorted.length) {
        currentId = sorted[0].id;
        sorted.forEach((t) => { if (t.status === "running" || t.status === "pending") follow(t.id); });
      }
      render();
      // 默认选中最近一条，并拉详情回显视频/ 转写 / 文稿
      if (currentId) loadDetail(currentId);
    } catch (e) {}
    try {
      const h = await api("/api/health");
      const bits = [];
      bits.push(h.ffmpeg ? "ffmpeg 就绪" : '<span class="bad">缺少 ffmpeg</span>');
      bits.push(h.yt_dlp ? "yt-dlp 就绪" : '<span class="bad">缺少 yt-dlp</span>');
      $("#health").innerHTML = bits.join(" · ");
    } catch (e) {}
    try { settingsCache = await api("/api/settings"); } catch (e) {}
  }

  boot();
})();
