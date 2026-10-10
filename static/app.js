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
  const ws = { videoTask: null, videoEl: null, videoKey: null, linesTask: null, lineEls: [], segs: [], active: -1, follow: true, noteKey: null, logCount: -1 };

  const ORDER = ["upload", "downloading", "extracting", "slicing", "transcribing", "polishing"];
  const STATUS_TEXT = { pending: "排队中", running: "处理中", transcribed: "转写完成", done: "已完成", failed: "失败", canceled: "已取消" };
  const STAGE_TEXT = { queued: "等待开始", downloading: "下载视频", extracting: "提取音频", slicing: "语音切片", transcribing: "语音转写", transcribed: "等待生成文稿", polishing: "AI 生成文稿", done: "完成", failed: "失败" };
  const STYLE_TEXT = { general: "整理文稿", note: "结构化笔记", article: "公众号文章", clean: "仅清洗润色" };
  // 默认风格；页面把选择结果记在curStyle 里
  let curStyle = "general";
  // 已生成过文稿的风格集合（当前任务），用于提示与按钮文案
  let generatedStyles = new Set();
  // 每个任务上次「查看」的风格：点回历史项时回到你上次看的那份稿，
  // 而不是被 note_info.style（最后一次生成的）强行覆盖
  const viewStyle = new Map();

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
      // 裸流直传，不走 multipart：后者在服务端是纯 Python 解析，
      // 大文件要几分钟；裸流是内核拷贝速度。参数全走 query。
      const q = "?style=" + encodeURIComponent(style)
        + "&name=" + encodeURIComponent(file.name);
      const xhr = new XMLHttpRequest();
      xhr.open("POST", "/api/tasks/upload-raw" + q);
      xhr.setRequestHeader("Content-Type", "application/octet-stream");
      // 大文件的 progress 事件一秒能来几十上百次，每次都全量重绘
      // 会把上传本身拖到零点几 MB/s（实测踩过）。节流到 ~10Hz 足够流畅。
      let lastPaint = 0;
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable) {
          uploadPct = Math.round((e.loaded / e.total) * 100);
          const now = Date.now();
          if (now - lastPaint > 120) { lastPaint = now; render(); }
        }
      };
      xhr.onload = () => {
        try { resolve(JSON.parse(xhr.responseText)); }
        catch (e) { reject(new Error("上传失败 " + xhr.status)); }
      };
      xhr.onerror = () => reject(new Error("网络错误，上传失败"));
      xhr.send(file);
    });
  }

  // ---------- 渲染 ----------
  // 任务当前处在哪个阶段（换算成 ORDER 里的下标）。
  // queued 还没开始 -> -1（一行都不显示）；transcribed/done 这类终态要映射回对应行，
  // 否则 indexOf 拿不到下标会让已经做完的行反而被隐藏。
  function stageIdx(t) {
    if (!t) return -1;
    const s = t.stage;
    if (!s || s === "queued") return -1;
    if (s === "done") return ORDER.length - 1;
    if (s === "transcribed") return ORDER.indexOf("transcribing");
    if (s === "failed" || s === "canceled") {
      const i = ORDER.indexOf(t.failed_stage || s);
      return i < 0 ? -1 : i;
    }
    return ORDER.indexOf(s);
  }

  function rowPct(t, key) {
    // 没上传过就显示 0%，别凭空显示 100%
    if (key === "upload") return uploadPct != null ? uploadPct : ((t && t.isUpload) ? 100 : 0);
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
    const cur = stageIdx(t);
    // 只有任务正在执行（或正在上传）时才露出进度行；
    // 转写完成 / 已完成 / 失败 / 取消等非执行状态一律隐藏，不挂 100% 尾巴
    const running = !!t && (t.status === "pending" || t.status === "running" || uploadPct != null);
    $$(".prow").forEach((row) => {
      const key = row.dataset.stage;
      let show = false;
      if (running) {
        show = key === "upload"
          ? (uploadPct != null || !!(t && t.isUpload))
          : (cur >= 0 && ORDER.indexOf(key) <= cur);
      }
      row.classList.toggle("hidden", !show);
      if (!show) return;
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
    // 失败/取消后才给重试入口；整理阶段失败只补AI 整理，其余重跑全流程
    const rbtn = $("#btn-retry");
    if (rbtn) {
      const can = t.status === "failed" || t.status === "canceled";
      rbtn.classList.toggle("hidden", !can);
      rbtn.textContent = (t.failed_stage === "polishing" && t.transcript) ? "↻ 重试 AI 整理" : "↻ 重新转写";
    }
    // 日志常驻右栏。只在条数变化时重建 DOM，避免每 0.8 秒心跳都重排一次
    const logs = $("#ws-logs");
    const evs = (t && t.events) || [];
    if (evs.length !== ws.logCount) {
      ws.logCount = evs.length;
      logs.innerHTML = evs.length
        ? evs.slice(-120).map((e) => '<div class="' + e.level + '">' + esc(e.text) + "</div>").join("")
        : '<div class="empty">暂无日志</div>';
      logs.scrollTop = logs.scrollHeight;
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
    const vp = t && t.meta && t.meta.video_path;
    const key = currentId + "|" + (vp || "");
    if (ws.videoKey === key) return;
    ws.videoKey = key;
    ws.videoTask = currentId;
    ws.videoEl = null;
    if (!vp) {
      // 还没下载好时给个明确说法，别让预览区看起来像坏了
      const busy = !!(t && (t.status === "running" || t.status === "pending"));
      ws.videoKey = key + "|" + (busy ? "busy" : "none");
      host.innerHTML = '<div class="empty">' + (busy ? "正在准备视频…" : "暂无视频") + "</div>";
      return;
    }
    // 必须用 meta 里记录的真实文件名，不能写死 video.mp4：
    // 本地上传的视频落盘名是 source<原后缀>，写死会导致预览区一直空白
    const fname = vp.split(/[\\/]/).pop();
    const kind = (fname.match(/\.(mp3|m4a|aac|wav|flac|ogg|opus|wma)$/i)) ? "audio" : "video";
    host.innerHTML = '<' + kind + ' controls preload="metadata" playsinline src="/api/media/'
      + currentId + "/" + encodeURIComponent(fname) + '"></' + kind + '>';
    ws.videoEl = host.querySelector(kind);
    if (kind === "video" && ws.videoEl) {
      ws.videoEl.addEventListener("timeupdate", () => syncActive(ws.videoEl.currentTime));
      ws.videoEl.addEventListener("seeked", () => syncActive(ws.videoEl.currentTime, true));
    } else {
      // 纯音频没有画面，但也要能跟着播放高亮文字稿
      ws.videoEl = ws.videoEl || null;
      const a = host.querySelector("audio");
      if (a) {
        a.addEventListener("timeupdate", () => syncActive(a.currentTime));
        a.addEventListener("seeked", () => syncActive(a.currentTime, true));
        ws.videoEl = a;
      }
    }
  }

  function ensureLines(t) {
    const box = $("#lines");
    if (ws.linesTask === currentId) return;
    // 转写增量会反复重建，这里记住滚动位置与高亮，
    // 否则每来一次增量预览就被拉回顶部、正在看的那句也丢了高亮
    const keepScroll = ws.scrollTop || 0;
    const keepActive = ws.active;
    ws.linesTask = currentId;
    ws.segs = (t && t.segments) || [];
    const live = !!(t && t.status === "running" && t.stage === "transcribing");
    if (!ws.segs.length) {
      box.innerHTML = '<div class="empty">' + (live ? "正在识别，文字会陆续出现…" : "暂无转写结果") + "</div>";
      ws.lineEls = [];
      ws.active = -1;
      return;
    }
    box.innerHTML = ws.segs.map((s, i) =>
      '<div class="line" data-i="' + i + '"><time>' + fmtTime(s.start) + "</time><span>" + esc(s.text || "") + "</span></div>").join("")
      + (live ? '<div class="line pending-line">正在识别后续内容…</div>' : "");
    ws.lineEls = Array.prototype.slice.call(box.querySelectorAll(".line:not(.pending-line)"));
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
    // 恢复高亮与滚动位置
    if (keepActive >= 0 && ws.lineEls[keepActive]) {
      ws.active = -1;
      markActive(keepActive, false);
    }
    box.scrollTop = keepScroll;
  }

  function saveScroll() {
    const box = $("#lines");
    if (box) ws.scrollTop = box.scrollTop;
  }

  // 取某个风格下已生成的文稿，用于点历史记录后回显、以及切风格时切换内容
  function noteForStyle(t, style) {
    if (!t) return "";
    const v = (t.variants || {})[style];
    if (v && v.note) return v.note;
    if (style && (t.note_info || {}).style === style && t.note) return t.note;
    return "";
  }

  function ensureNote(t) {
    const box = $("#note-body");
    const text = noteForStyle(t, curStyle);
    const key = currentId + "|" + curStyle + "|" +
      (text ? text.length + "|" + text.slice(0, 40) : "none");
    if (ws.noteKey !== key) {
      ws.noteKey = key;
      box.innerHTML = text ? renderMarkdown(text)
        : '<div class="empty">转写完成后，在上方选一种风格点「生成文稿」</div>';
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
            t.failed_stage = ev.failed_stage || "";
          } else if (ev.type === "partial") {
            // 转写增量：边转写边显示，不用等全部分片跑完
            t.segments = ev.segments || [];
            t.transcript = ev.transcript || "";
            if (ev.spercent != null) t.spercent = ev.spercent;
            // 强制让文字区按新segments 重绘
            ws.linesTask = null;
          } else if (ev.type === "log") {
            t.events = t.events || [];
            if (!t.events.some((x) => x.t === ev.t && x.text === ev.text)) t.events.push(ev);
          } else if (ev.type === "meta") {
            // 下载一落盘就拿到了 video_path，预览区立刻可播，不用等全流程结束
            t.meta = ev.meta || {};
            ws.videoKey = null;
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
    ws.videoTask = null; ws.videoKey = null; ws.linesTask = null; ws.noteKey = null; ws.logCount = -1;
    ws.videoEl = null; ws.segs = []; ws.lineEls = []; ws.active = -1; ws.scrollTop = 0;
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
      // 沿用上次查看的风格（没有才用任务最后生成的那份），并同步按钮选中态
      if (t && t.note_info && t.note_info.style && STYLE_TEXT[t.note_info.style]) {
        curStyle = viewStyle.get(id) || t.note_info.style;
        syncStyleChips();
      }
      generatedStyles = new Set(
        (t && t.note_info && t.note_info.styles) ||
        Object.keys((t && t.variants) || {}).filter((k) => t.variants[k] && t.variants[k].note)
      );
      ws.videoTask = null; ws.videoKey = null; ws.linesTask = null; ws.noteKey = null;
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
        afterCreate(r.id, true);
      } catch (e) {
        toast(e.message, true);
      } finally {
        // 传完固定停在 100%，不要回落到 null（那会让进度行直接消失）
        uploadPct = 100;
        render();
      }
      return;
    }
    const url = $("#url").value.trim();
    if (!url) { toast("请先上传文件或粘贴链接", true); return; }
    try {
      uploadPct = null;                 // 链接任务没有上传这一步
      const r = await api("/api/tasks", { method: "POST", body: JSON.stringify({ url: url, style: curStyle }) });
      $("#url").value = "";
      afterCreate(r.id, false);
    } catch (e) {
      toast(e.message, true);
    }
  }

  function afterCreate(id, isUpload) {
    tasks.set(id, { id: id, url: "", platform: "", status: "pending", stage: "queued", percent: 0, spercent: 0, meta: {}, segments: [], events: [], created_at: Date.now() / 1000, isUpload: !!isUpload });
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

  async function retryTask() {
    if (!currentId) return;
    try {
      const r = await api("/api/tasks/" + currentId + "/retry", { method: "POST" });
      toast(r.message || "已重新开始");
      const t = tasks.get(currentId);
      if (t) { t.status = "pending"; t.stage = "queued"; t.error = ""; t.failed_stage = ""; }
      render();
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

  // 设置面板不展示的配置项。
  // 这些是部署期参数（接口地址、协议、Token、模型名）或与本项目无关的本地环境项，
  // 日常使用改不动也不需要改，暴露出来只会让设置面板变得冗长、容易误填。
  // 注意：只是前端不渲染，后端 DEFAULTS 与接口照常支持，
  // 需要改时仍可编辑 data/secrets.json 或用 V2N_* 环境变量。
  const HIDDEN_SETTINGS = [
    "asr_base_url", "asr_api_key", "asr_model",
    "llm_protocol", "llm_base_url", "llm_api_key", "llm_model",
    "cookie_browser", "cookie_file",
    "proxy", "ffmpeg_path",
  ];

  // 敏感项：后端只回「有没有配」，绝不回内容。
  // 输入框因此始终为空，placeholder 提示当前状态；
  // 留空表示不改（不是清空），要清空得点旁边的清除按钮。
  const SECRET_UI = {
    cookie_text: "cookie_douyin",
    cookie_text_bili: "cookie_bilibili",
    resolver_curl: "resolver_curl",
  };

  function openSettings() {
    const v = settingsCache.values || {}, help = settingsCache.help || {};
    const cfg = v._configured || {};
    const full = ["asr_base_url", "llm_base_url", "cookie_file", "cookie_text",
                  "cookie_text_bili", "resolver_curl", "proxy", "ffmpeg_path"];
    // 抖音与B站的 Cookie 必须分开填：域名不同，混在一起两边都会失效
    const areas = ["cookie_text", "cookie_text_bili", "resolver_curl"];
    const PH = {
      cookie_text: "【抖音】F12 → Network → 点任意请求 → Request Headers → 复制整段 Cookie"
        + "（形如 a=1; b=2）粘到这里，程序自动转 cookies.txt。也接受 Netscape 格式全文。"
        + "（出于安全考虑，已保存的 Cookie 不会回显；留空即保持不变）",
      cookie_text_bili: "【B站】在 B站登录后按同样方式复制整段 Cookie 粘到这里。"
        + "务必包含登录态字段（登录会话凭证），否则会返回 412。"
        + "（出于安全考虑，已保存的 Cookie 不会回显；留空即保持不变）",
      cookie_browser: "只填浏览器名，不要粘贴 Cookie 内容。chrome / edge / firefox / brave。"
        + "（容器/云端环境没有浏览器，优先用上面的 Cookie 文本框）",
      cookie_file: "本机已有的 Netscape cookies.txt 绝对路径，例如 D:\\cookies.txt（一般用不到）。",
      resolver_curl: "抖音解析接口的请求模板（可选，通常留空即可）。"
        + "在 dlpanda 页面上 F12 → Network → 随便点一次解析请求 → 右键「复制」→"
        + "「复制为 cURL」，把整段粘到这里。程序会自动："
        + "① 替换 _token / t0ken 为页面实时值 ② 只替换 url 字段为你要解析的链接 "
        + "③ 把 Cookie 注入浏览器会话。留空则使用内置模板。",
    };
    const rows = { cookie_text: 4, cookie_text_bili: 4, resolver_curl: 9 };
    const keys = Object.keys(help).filter((k) => HIDDEN_SETTINGS.indexOf(k) < 0);
    $("#settings-form").innerHTML = keys.map((k) => {
      const wide = full.includes(k) ? " full" : "";
      const isSecret = Object.prototype.hasOwnProperty.call(SECRET_UI, k);
      // 敏感项不回显真实值，val 恒为空
      const val = isSecret ? "" : (v[k] !== undefined ? v[k] : "");
      let ctl;
      if (areas.includes(k)) {
        const on = isSecret && !!cfg[SECRET_UI[k]];
        const state = on
          ? "✅ 已配置（内容不回显，留空保持不变）"
          : "未配置 —— 粘贴后点保存";
        ctl = '<textarea data-key="' + k + '" rows="' + (rows[k] || 4)
          + '" placeholder="' + esc(PH[k] || "") + '">' + esc(val) + "</textarea>"
          + '<div class="secret-row"><span class="secret-state">' + state + "</span>"
          + (on ? '<button type="button" class="btn small danger" data-clear="' + k
                 + '">清除</button>' : "") + "</div>"
          + (k === "resolver_curl"
            ? '<div class="secret-row"><button type="button" class="btn small"'
              + ' data-parse-curl="1">校验这段 curl</button>'
              + '<span class="secret-state" id="curl-check"></span></div>'
            : "");
      } else {
        ctl = '<input data-key="' + k + '" value="' + esc(val) + '" placeholder="'
          + esc(PH[k] || "") + '">';
      }
      return '<div class="field' + wide + '"><label>' + esc(help[k]) + "</label>" + ctl + "</div>";
    }).join("");
    $("#settings-modal").classList.remove("hidden");
  }

  async function clearSecret(key) {
    const labels = { cookie_text: "抖音", cookie_text_bili: "B站",
                     resolver_curl: "解析请求模板" };
    const label = labels[key] || key;
    const extra = key === "resolver_curl"
      ? "清除后抖音解析将回到内置模板。"
      : "清除后下载这两个平台的视频会因缺少登录态而失败。";
    if (!confirm("确定清除已保存的" + label + "？" + extra)) return;
    try {
      settingsCache = await api("/api/settings", {
        method: "POST", body: JSON.stringify({ values: {}, clear: [key] }),
      });
      openSettings();
      toast(label + " Cookie 已清除");
    } catch (e) { toast(e.message, true); }
  }

  async function checkCurl() {
    const box = $('#settings-form [data-key="resolver_curl"]');
    const out = $("#curl-check");
    if (!box || !out) return;
    const raw = box.value.trim();
    if (!raw) { out.textContent = "未填写，将使用内置模板"; return; }
    out.textContent = "校验中…";
    try {
      const r = await api("/api/settings/check-curl", {
        method: "POST", body: JSON.stringify({ values: { resolver_curl: raw } }),
      });
      out.textContent = "✓ " + (r.message || "格式可用");
    } catch (err) {
      out.textContent = "✗ " + err.message;
    }
  }

  async function saveSettings() {
    const values = {};
    $("#settings-form").querySelectorAll("input, textarea").forEach((inp) => {
      const k = inp.dataset.key;
      // 敏感项：只有真正填了新值才提交；留空 = 保持原值不变
      if (Object.prototype.hasOwnProperty.call(SECRET_UI, k)) {
        if (inp.value.trim()) values[k] = inp.value;
        return;
      }
      const orig = String((settingsCache.values || {})[k] !== undefined ? settingsCache.values[k] : "");
      if (inp.value === orig) return;
      values[k] = inp.value;
    });
    try {
      const r = await api("/api/settings", { method: "POST", body: JSON.stringify({ values: values }) });
      settingsCache = r;
      $("#settings-modal").classList.add("hidden");
      const f = (r && r.cookie_files) || {};
      const written = Object.keys(f).map((k) => (k === "bilibili" ? "B站" : "抖音") + " Cookie 已写入 " + f[k]);
      toast(written.length ? "设置已保存；" + written.join("；") : "设置已保存");
    } catch (e) {
      toast(e.message, true);
    }
  }

  // ---------- 事件 ----------
  document.addEventListener("click", async (e) => {
    const chk = e.target.closest("[data-parse-curl]");
    if (chk) { e.stopPropagation(); checkCurl(); return; }
    const clr = e.target.closest("[data-clear]");
    if (clr) { e.stopPropagation(); clearSecret(clr.dataset.clear); return; }
    const del = e.target.closest("[data-del]");
    if (del) { e.stopPropagation(); removeTask(del.dataset.del); return; }
    const hitem = e.target.closest(".hitem");
    if (hitem) { selectTask(hitem.dataset.id); return; }

    const chip = e.target.closest(".style-chip");
    if (chip) {
      curStyle = chip.dataset.style;
      viewStyle.set(currentId, curStyle);
      syncStyleChips();
      const t = tasks.get(currentId);
      ensureNote(t);              // 已生成过的风格直接回显，不重复消耗额度
      renderPolishBar(t);
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
    else if (e.target.id === "btn-retry") retryTask();
    else if (e.target.id === "btn-history") { $("#side-left").classList.toggle("folded"); syncSideBtns(); }
    else if (e.target.id === "btn-logs") { $("#side-right").classList.toggle("folded"); syncSideBtns(); }
    else if (e.target.id === "btn-settings") openSettings();
    else if (e.target.id === "btn-close-settings" || e.target.id === "btn-cancel-settings") $("#settings-modal").classList.add("hidden");
    else if (e.target.id === "btn-save-settings") saveSettings();
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
    if (e.key === "Escape") { $("#settings-modal").classList.add("hidden"); }
  });

  // ---------- 启动 ----------
  // 记住文字区滚动位置：转写增量会重建 DOM，重建后要还原回去
  $("#lines").addEventListener("scroll", saveScroll, { passive: true });

  // 顶栏按钮反映两侧栏的折叠状态（默认都展开）
  function syncSideBtns() {
    const pairs = [["#btn-history", "#side-left", "🕘 历史记录", "左侧历史记录"],
                   ["#btn-logs", "#side-right", "📋 处理日志", "右侧处理日志"]];
    pairs.forEach(([b, s, label, name]) => {
      const folded = $(s).classList.contains("folded");
      const btn = $(b);
      btn.classList.toggle("on", !folded);
      btn.textContent = label;
      btn.title = (folded ? "展开" : "折叠") + name;
    });
  }

  async function boot() {
    syncSideBtns();
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
