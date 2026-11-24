(function () {
  "use strict";

  const $ = (s) => document.querySelector(s);
  const $$ = (s) => Array.prototype.slice.call(document.querySelectorAll(s));
  const tasks = new Map();
  const following = new Set();
  // 每个任务一个 AbortController：切换任务时主动 abort 旧连接的 fetch，
  // 否则旧的 SSE 会一直 hanging，断开后又被 scheduleReconnect 无限重连（#1 泄漏）。
  const controllers = new Map();
  // 轮询兜底（应对预览代理缓冲 SSE 流导致 partial 到不了浏览器）：
  // 每个被 follow 的任务额外挂一个定时拉详情的计时器，SSE 静默超时就补拉一次。
  const pollers = new Map();
  const lastSseAt = new Map();
  const pollingInFlight = new Set();
  const POLL_INTERVAL_MS = 1500;   // 兜底轮询周期
  // SSE 超过这么久没数据才启用轮询。故意压短：预览代理(monkeycode-ai.online)
  // 会缓冲 SSE 流，partial 到不了浏览器，页面冻在 0%；窗口太长用户会以为还卡。
  // 直连环境 SSE 持续推数据(节流 0.4s)，2.5s 内必有 lastSseAt 刷新，轮询不会误触发。
  const SSE_SILENCE_MS = 2500;

  function clearPoller(tid) {
    const iv = pollers.get(tid);
    if (iv) { clearInterval(iv); pollers.delete(tid); }
  }

  function stopFollow(tid) {
    const c = controllers.get(tid);
    if (c) c.abort();
    clearPoller(tid);
    lastSseAt.delete(tid);
  }

  // 轮询兜底：代理缓冲 SSE 时，partial 被卡在网关、页面冻在 0%；
  // 周期拉一次 /api/tasks/<tid>（即刷新走的非流式详情接口，代理不缓冲）回填进度，
  // 保证「转多少显多少」在代理环境下也成立。SSE 一旦有数据就暂停轮询，不浪费请求。
  async function _pollTick(tid) {
    const t = tasks.get(tid);
    if (!t) { clearPoller(tid); return; }
    // 终态由 eof / loadDetail 收口，不必再轮询
    if (t.status === "done" || t.status === "failed"
        || t.status === "canceled" || t.status === "transcribed") {
      clearPoller(tid); return;
    }
    // SSE 最近 4 秒内还有数据，信任它，跳过本轮
    if (following.has(tid)
        && (Date.now() - (lastSseAt.get(tid) || 0)) < SSE_SILENCE_MS) return;
    if (pollingInFlight.has(tid)) return;   // 上次还没回来，避免请求堆叠
    pollingInFlight.add(tid);
    try {
      await refreshOne(tid);                // 原地合并 + 本地增量优先
      const cur = tasks.get(tid);
      // 不再强制清 linesSig：ensureLines 的签名（句数/尾句/直播态）本身
      // 就能识别内容变化；每轮都清会把文本稿整表重建，滚动位置被反复
      // 恢复、正在播放的跟随滚动也被打断，看起来像页面在刷新。
      if (cur) render();
    } catch (e) { /* 网络抖动忽略，下个周期再试 */ }
    finally { pollingInFlight.delete(tid); }
  }

  function startPolling(tid) {
    clearPoller(tid);
    lastSseAt.set(tid, Date.now());         // 先给 SSE 一个窗口，别一上来就拉
    const iv = setInterval(() => _pollTick(tid), POLL_INTERVAL_MS);
    pollers.set(tid, iv);
  }
  let currentId = null;
  let pickedFiles = [];
  let uploadPct = null;
  // 任务搜索：query 非空时历史列表改用服务端过滤结果（historyItems）
  let historyQuery = "";
  let historyItems = [];
  let historySearchTimer = null;
  let settingsCache = { values: {}, help: {} };
  // 记录设置接口的加载错误；为空表示加载正常
  let settingsLoadErr = "";
  const ws = { videoTask: null, videoEl: null, videoKey: null, linesTask: null, linesSig: null, lineEls: [], segs: [], active: -1, follow: true, noteKey: null, logCount: -1, lineCount: 0, lineTail: "", renderedTask: null };

  const ORDER = ["upload", "downloading", "extracting", "slicing", "transcribing", "polishing"];
  const STATUS_TEXT = { pending: "排队中", running: "处理中", transcribed: "转写完成", done: "已完成", failed: "失败", canceled: "已取消" };
  const STAGE_TEXT = { queued: "等待开始", downloading: "下载视频", extracting: "提取音频", slicing: "语音切片", transcribing: "语音转写", transcribed: "等待生成文稿", polishing: "AI 生成文稿", done: "完成", failed: "失败" };
  const STYLE_TEXT = { cleaned: "清洗文稿", general: "整理文稿", digest: "精简文稿", article: "公众号文章", star: "面试话术", note: "结构化笔记", clean: "仅清洗润色" };
  // 默认风格；页面把选择结果记在curStyle 里
  let curStyle = "general";
  // 已生成过文稿的风格集合（当前任务），用于提示与按钮文案
  let generatedStyles = new Set();
  // 每个任务上次「查看」的风格：点回历史项时回到你上次看的那份稿，
  // 而不是被 note_info.style（最后一次生成的）强行覆盖
  const viewStyle = new Map();

  function esc(s) { return String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;"); }

  // 字数统计：中文按「不计空格」计，和 Word/WPS 的字符数口径一致。
  // 直接用 length() 会被换行与缩进灌水——转写稿是逐句拼出来的，空白占比很高。
  function charCount(s) {
    return String(s == null ? "" : s).replace(/\s+/g, "").length;
  }

  // 千分位分隔，五位以上的字数一眼可读
  function fmtNum(n) {
    return String(n).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  }

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
  // ms：停留时长。默认 3.2 秒对「一键说明白一件事」够用，
  // 但带操作指引的提示（比如待同步数量）一闪而过等于没说，所以要能拉长。
  function toast(msg, isErr, ms) {
    const el = $("#toast");
    el.textContent = msg;
    el.className = "toast" + (isErr ? " err" : "");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.add("hidden"), ms || 3200);
  }

  // ---- 复制到剪贴板 ------------------------------------------------------
  // 坑：navigator.clipboard 只在「安全上下文」里存在（HTTPS 或 localhost）。
  // 本站是 HTTP 部署（http://ip:31773），浏览器里 navigator.clipboard 是
  // undefined，直接 .writeText() 会抛 TypeError；调用点在 async 点击监听里，
  // 错误变成未处理的 rejection——不弹任何提示，表现为「复制按钮没反应」。
  // 所以：能用 async clipboard 就用，不能用/失败了退回 execCommand 方案。
  function _copyFallback(text) {
    try {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.setAttribute("readonly", "");          // 防 iOS 弹键盘
      ta.style.position = "fixed";
      ta.style.top = "0";
      ta.style.left = "0";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      ta.setSelectionRange(0, ta.value.length); // iOS Safari 必须显式设置选区
      const ok = document.execCommand("copy");
      document.body.removeChild(ta);
      return ok;
    } catch (e) {
      return false;
    }
  }

  // 统一返回 Promise<boolean>：true=复制成功。绝不抛错、绝不静默。
  function copyText(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(text)
        .then(() => true, () => _copyFallback(text));
    }
    return Promise.resolve(_copyFallback(text));
  }

  // ---- 访问控制 ----------------------------------------------------------
  // 服务端设置 V2N_ACCESS_TOKEN 后，未携带令牌的 /api/* 一律返回 401。
  // 登录成功会写入 httpOnly cookie，同源请求（包括 <video src="/api/media/..">
  // 这类无法自定义 header 的浏览器直连请求）由浏览器自动携带，
  // 所以只需要在第一次 401 时引导用户输入一次即可。
  let authToken = "";
  let authResolve = null;
  // 鉴权/门禁状态：role=admin|user|none；开放配置模式（未设管理员令牌）下 admin_configured=false
  let authState = { role: "none", admin_configured: false,
                    gate_active: false, free_remaining: 0, pro_used: 0, pro_limit: 0 };

  async function refreshAuthState() {
    try {
      const s = await fetch("/api/auth/state").then((r) => r.json());
      authState = Object.assign(authState, s);
    } catch (e) {}
    updateAuthUI();
  }

  function updateAuthUI() {
    // 设置按钮：管理员可见；开放配置模式（未设管理员令牌）也可见；普通 token 隐藏（后端同样拒绝）
    const showSettings = authState.role === "admin" || !authState.admin_configured;
    const btn = $("#btn-settings");
    if (btn) btn.classList.toggle("hidden", !showSettings);
    // 登录/登出入口：未登录 -> "🔑 登录"（点击主动弹出令牌框）；
    // 已登录 -> 显示角色，点击退出。没有这个入口时，未登录用户只能等
    // 某次请求被 401/402 打回来才看到输入框，很容易以为"没有地方输密钥"。
    const authBtn = $("#btn-auth");
    if (authBtn) {
      if (authState.role === "none") {
        authBtn.textContent = "🔑 登录";
        authBtn.classList.remove("hidden");
        authBtn.onclick = () => askToken();
      } else {
        authBtn.textContent = authState.role === "admin" ? "🔓 管理员" : "👤 已登录";
        authBtn.classList.remove("hidden");
        authBtn.onclick = async () => {
          if (!confirm("退出当前令牌？（退出后免费次数仍照常计算）")) return;
          try {
            await fetch("/api/auth/logout", { method: "POST" });
          } catch (e) {}
          authToken = "";
          await refreshAuthState();
          toast("已退出登录");
        };
      }
    }
    // 门禁横幅：只对匿名访客显示；已登录（管理员/普通令牌）不受免费额度限制
    const banner = $("#gate-banner");
    if (banner) {
      if (authState.role === "none" && authState.gate_active) {
        banner.textContent = "免费次数已用完，继续使用请输入访问令牌。";
        banner.classList.remove("hidden");
      } else if (authState.role === "none" && authState.pro_limit && authState.free_remaining > 0) {
        banner.textContent = "免费额度剩余 " + authState.free_remaining
          + " 次（成功转写），用完需令牌";
        banner.classList.remove("hidden");
      } else {
        banner.classList.add("hidden");
      }
    }
  }

  function askToken() {
    return new Promise((resolve) => {
      if (authResolve) { authResolve(false); authResolve = null; }
      authResolve = resolve;
      $("#auth-err").textContent = "";
      $("#auth-token-input").value = "";
      $("#auth-modal").classList.remove("hidden");
      setTimeout(() => { try { $("#auth-token-input").focus(); } catch (e) {} }, 50);
    });
  }

  function settleAuth(v) {
    $("#auth-modal").classList.add("hidden");
    const rs = authResolve;
    authResolve = null;
    if (rs) rs(v);
  }

  async function submitToken() {
    const tok = ($("#auth-token-input").value || "").trim();
    if (!tok) { $("#auth-err").textContent = "请输入令牌"; return; }
    try {
      const r = await fetch("/api/auth/login", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token: tok }),
      });
      if (r.ok) { authToken = tok; settleAuth(true); refreshAuthState(); return; }
      $("#auth-err").textContent = "令牌不正确，请重试";
    } catch (e) {
      $("#auth-err").textContent = "网络错误，请重试";
    }
  }

  async function api(path, opts) {
    const call = async () => {
      // opts.headers 优先（上传等场景自带 Content-Type 或不带）
      const headers = Object.assign({ "Content-Type": "application/json" },
                                    (opts && opts.headers) || {});
      if (authToken) headers["X-Access-Token"] = authToken;
      return fetch(path, Object.assign({}, opts, { headers }));
    };
    let r = await call();
    // 401 = 需要令牌（含管理员）；402 = 免费次数用完需令牌。
    // 引导输入一次，成功后重试同一个请求，用户无感知。
    if (r.status === 401 || r.status === 402) {
      const okd = await askToken();
      if (okd) r = await call();
    }
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
        // 先判状态码：413/401/500 的响应体不是任务 JSON，旧实现只试
        // JSON.parse，服务端给出的具体原因（如超过上传上限）会被吞掉。
        if (xhr.status < 200 || xhr.status >= 300) {
          let msg = "上传失败 " + xhr.status;
          try {
            const d = JSON.parse(xhr.responseText);
            if (d && d.detail) msg = String(d.detail);
          } catch (e) {}
          reject(new Error(msg));
          return;
        }
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
    // 搜索中：列表数据源是服务端按 q 过滤的结果（historyItems），
    // 否则用本地全量任务。两者都是同一份摘要结构。
    const src = historyQuery ? historyItems : Array.from(tasks.values());
    const list = src.slice().sort((a, b) => b.created_at - a.created_at);
    const box = $("#history-list");
    // 整表 innerHTML 重建会把滚动位置清零：长列表里每来一次进度推送
    // 就跳回顶部，没法往下翻。先存 scrollTop，重建后恢复。
    const keepTop = box.scrollTop;
    if (!list.length) {
      box.innerHTML = '<div class="empty">' + (historyQuery ? "没有匹配的任务" : "暂无记录") + "</div>";
      box.scrollTop = keepTop;
      return;
    }
    // 日期分组（今天 / 昨天 / 更早）：长列表里先定位到天，再找条目；
    // 组标题在列表滚动时吸顶。平台做成小标签，比混在文本里好认。
    const nowD = new Date();
    const dayStart = new Date(nowD.getFullYear(), nowD.getMonth(), nowD.getDate()).getTime() / 1000;
    const yStart = dayStart - 86400;
    const bucketOf = (ts) => (ts >= dayStart ? "今天" : ts >= yStart ? "昨天" : "更早");
    const PLAT = { douyin: "抖音", bilibili: "B站", local: "本地" };
    let lastBucket = null;
    box.innerHTML = list.map((t) => {
      const when = new Date((t.created_at || 0) * 1000);
      const time = isNaN(when.getTime()) ? "" : when.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
      let head = "";
      const b = bucketOf(t.created_at || 0);
      if (b !== lastBucket) { lastBucket = b; head = '<div class="hgroup">' + b + "</div>"; }
      const parts = [];
      if (time) parts.push(esc(time));
      const plat = PLAT[t.platform];
      if (plat) parts.push('<i class="htag">' + plat + "</i>");
      if (t.meta && t.meta.duration) parts.push(fmtTime(Number(t.meta.duration) || 0));
      if (t.meta && t.meta.uploader) parts.push(esc(t.meta.uploader));
      // 已生成文稿的标记：不用点开就知道哪条有稿、有几篇
      const nStyles = (t.note_info && t.note_info.styles) || [];
      if (nStyles.length) parts.push("📄 " + nStyles.length + " 篇文稿");
      else if (t.note_info && t.note_info.style) parts.push("📄 已生成");
      const running = t.status === "running" || t.status === "pending";
      const pct = Math.max(0, Math.min(100, Math.round(t.percent || t.spercent || 0)));
      return head +
        '<div class="hitem' + (t.id === currentId ? " on" : "") + '" data-id="' + t.id + '">' +
        '<div class="hmain"><b title="' + esc((t.meta && t.meta.title) || t.url || "(无标题)") + '">' + esc((t.meta && t.meta.title) || t.url || "(无标题)") + '</b>' +
        '<span>' + parts.join(" · ") + '</span>' +
        (running ? '<span class="hprog"><i style="width:' + pct + '%"></i></span>' : "") +
        '</div>' +
        '<span class="hstatus ' + esc(t.status) + '">' + esc(STATUS_TEXT[t.status] || t.status) + '</span>' +
        '<button class="icon hdel" data-del="' + t.id + '" title="删除">×</button>' +
        "</div>";
    }).join("");
    box.scrollTop = keepTop;
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
    const title = $("#ws-title"), st = $("#ws-status");
    if (!t) {
      title.textContent = "尚未开始任务";
      st.textContent = "";
      st.className = "status";
      $("#ws-error").classList.add("hidden");
      const cbtn0 = $("#btn-cancel");
      if (cbtn0) cbtn0.classList.add("hidden");
      return;
    }
    title.textContent = (t.meta && t.meta.title) || t.url || "(无标题)";
    st.className = "status " + t.status;
    // 运行中把后端推的细粒度进度文案（清洗第 X-Y 段 / 共 N 段等）一并带上：
    // 只显示阶段名时，几分钟的生成看起来像卡死
    st.textContent = (STATUS_TEXT[t.status] || t.status) + (t.status === "running"
      ? " · " + (STAGE_TEXT[t.stage] || t.stage) + (t.message && t.message !== STAGE_TEXT[t.stage] ? " · " + t.message : "")
      : "");
    // 取消入口：运行/排队中才显示（后端有 /cancel，之前前端没接）
    const cbtn = $("#btn-cancel");
    if (cbtn) cbtn.classList.toggle("hidden", !(t.status === "running" || t.status === "pending"));
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

  // 「生成文稿」按钮的可用状态与文案，以及两处字数统计
  function renderPolishBar(t) {
    const btn = $("#btn-polish"), meta = $("#note-meta"), lmeta = $("#lines-meta");
    const hasText = !!(t && t.transcript && t.transcript.length);
    // 不同风格可同时生成：按钮只为「当前风格」服务——当前风格在跑才禁用，
    // 别的风格在跑不影响继续点生成
    const runningStyles = new Set((t && t.polishing_styles) || []);
    const curRunning = runningStyles.has(curStyle);
    if (t) generatedStyles = new Set(
      (t.note_info && t.note_info.styles) ||
      Object.keys(t.variants || {}).filter((k) => t.variants[k] && t.variants[k].note));
    $$(".style-chip").forEach((b) => b.classList.toggle("generating", runningStyles.has(b.dataset.style)));
    const canRun = hasText && !curRunning &&
      (t.status === "transcribed" || t.status === "done" ||
       (t.status === "running" && t.stage === "polishing"));
    btn.disabled = !canRun;
    if (curRunning) {
      btn.textContent = "⏳ 正在生成「" + (STYLE_TEXT[curStyle] || curStyle) + "」…";
    } else if (generatedStyles.has(curStyle)) {
      btn.textContent = "↻ 重新生成「" + (STYLE_TEXT[curStyle] || curStyle) + "」";
    } else {
      btn.textContent = "✨ 生成「" + (STYLE_TEXT[curStyle] || curStyle) + "」";
    }

    // 文字稿字数：转写中随增量实时上涨，让用户看到进度
    const tc = hasText ? charCount(t.transcript) : 0;
    if (lmeta) {
      const live = !!(t && t.status === "running" && t.stage === "transcribing");
      lmeta.textContent = tc
        ? fmtNum(tc) + " 字" + (live ? " · 识别中…" : "")
        : "";
    }

    // 成稿字数：取当前正在展示的那份，而不是 note_info（它只记最后生成的风格，
    // 切到别的风格时会显示成另一篇的字数）
    const text = noteForStyle(t, curStyle);
    if (text) {
      const info = (t && t.note_info) || {};
      const sameStyle = info.style === curStyle;
      const vinfo = (((t && t.variants) || {})[curStyle] || {}).info || (sameStyle ? info : {});
      // 主信息（风格/字数/时间）正常显示，次信息（模型/用时/保留率）
      // 降一档透明度，避免一行小字全挤在一起分不清主次
      const dim = (vinfo.model ? " · " + esc(shortModel(vinfo.model)) : "") +
        (vinfo.seconds ? " · 用时 " + vinfo.seconds + "s" : "") +
        (vinfo.ratio ? " · 保留率 " + Math.round(vinfo.ratio * 100) + "%" : "");
      meta.innerHTML = esc(STYLE_TEXT[curStyle] || curStyle) + " · " +
        fmtNum(charCount(text)) + " 字" +
        (tc ? "（原文 " + fmtNum(tc) + " 字）" : "") +
        (vinfo.at ? " · " + esc(vinfo.at) : "") +
        (dim ? '<span class="meta-dim">' + dim + "</span>" : "");
    } else {
      meta.textContent = "";
    }

    // 「编辑」只给清洗文稿：它是各风格共用的底稿，可人工修订；
    // 其余风格是底稿的再创作，不提供直接编辑
    const editCleanBtn = $("#btn-edit-cleaned");
    if (editCleanBtn) {
      const canEdit = !!t && curStyle === "cleaned" && !!noteForStyle(t, "cleaned") &&
        t.status !== "running" && t.status !== "pending";
      editCleanBtn.classList.toggle("hidden", !canEdit);
    }
  }

  // 文字稿高度跟随视频框：有视频时把稿件区高度设为视频框的实际高度，
  // 视频多高它多高（不写死）；没视频时清掉内联高度，走 CSS 兜底上限。
  function syncLinesHeight() {
    const host = $("#video-host");
    const lines = $("#lines");
    if (!host || !lines) return;
    if (host.querySelector("video")) {
      // 视频面板已无标题行，文本稿面板还带一行标题：
      // 稿件区高度 = 视频高度 − 标题行占用，两块面板整体等高
      const head = lines.parentElement && lines.parentElement.querySelector(".pane-head");
      let extra = 0;
      if (head) {
        const mb = parseFloat(getComputedStyle(head).marginBottom) || 0;
        extra = head.offsetHeight + mb;
      }
      const h = Math.max(120, host.clientHeight - extra);
      lines.style.height = h + "px";
      lines.style.maxHeight = h + "px";   // 压过 CSS 的 460px 上限，否则高视频时对不齐
    } else { lines.style.height = ""; lines.style.maxHeight = ""; }
  }
  window.addEventListener("resize", syncLinesHeight);
  if (window.ResizeObserver) {
    const vh = $("#video-host");
    if (vh) new ResizeObserver(() => syncLinesHeight()).observe(vh);
  }

  function ensureVideo(t) {
    const host = $("#video-host");
    const vp = t && t.meta && t.meta.video_path;
    // 键只取「任务 + 文件名 + 封面」：meta 里的归档信息等字段变化
    // （如生成文稿后七牛备份回写 archived）不该重建播放器，
    // 否则正在播放的视频会被打断、从头加载，看起来像页面刷新。
    const fname = vp ? vp.split(/[\\/]/).pop() : "";
    const cover = (t && t.meta && t.meta.cover) || "";
    const key = currentId + "|" + fname + "|" + cover;
    if (ws.videoKey === key) return;
    // 真要重建时先存播放状态，重建后恢复，播放不中断
    const prev = (ws.videoEl && ws.videoTask === currentId)
      ? { t: ws.videoEl.currentTime || 0, paused: ws.videoEl.paused, rate: ws.videoEl.playbackRate || 1 }
      : null;
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
    const kind = (fname.match(/\.(mp3|m4a|aac|wav|flac|ogg|opus|wma)$/i)) ? "audio" : "video";
    // 有封面时作 poster：点开任务先回显封面，而不是黑屏/空白
    const poster = (kind === "video" && t && t.meta && t.meta.cover)
      ? ' poster="/api/media/' + currentId + "/" + encodeURIComponent(t.meta.cover) + '"' : "";
    host.innerHTML = '<' + kind + ' controls preload="' + (kind === "video" ? "auto" : "metadata") + '" playsinline' + poster + ' src="/api/media/'
      + currentId + "/" + encodeURIComponent(fname) + '"></' + kind + '>';
    ws.videoEl = host.querySelector(kind);
    if (prev && ws.videoEl) {
      const el = ws.videoEl;
      el.addEventListener("loadedmetadata", () => {
        try { el.currentTime = prev.t; } catch (e) {}
        if (prev.rate && prev.rate !== 1) el.playbackRate = prev.rate;
        if (!prev.paused) el.play().catch(() => {});
      }, { once: true });
    }
    if (kind === "video" && ws.videoEl) {
      // 恢复上次的倍速选择（倍速控件在视频面板头部）
      const savedRate = parseFloat(localStorage.getItem("v2n_rate") || "1");
      if (savedRate && savedRate !== 1) ws.videoEl.playbackRate = savedRate;
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

  function lineHtml(s, i) {
    return '<div class="line" data-i="' + i + '"><time>' + fmtTime(s.start)
      + "</time><span>" + esc(s.text || "") + "</span></div>";
  }

  function bindLine(line) {
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
  }

  function ensureLines(t) {
    const box = $("#lines");
    // 缓存守卫必须按「内容 + 状态」判断，不能只看 taskId。
    // 只比taskId 会漏掉这种情况：增量预览渲染过一次后 linesTask 就等于
    // currentId，此后转写阶段结束、后端停止推 partial，但最终结果比
    // 增量更多 —— 于是页面永远停留在中间那一句，表现为
    //「已经转写完了，文字稿却没显示 / 显示不全」。
    const segs = (t && t.segments) || [];
    const live = !!(t && t.status === "running" && t.stage === "transcribing");
    const sig = currentId + "|" + segs.length + "|" + (live ? "1" : "0")
      + "|" + (segs.length ? (segs[segs.length - 1].text || "") : "");
    if (ws.linesSig === sig) return;
    const keepScroll = ws.scrollTop || 0;
    const keepActive = ws.active;
    ws.linesTask = currentId;
    ws.linesSig = sig;
    ws.segs = segs;

    if (!segs.length) {
      box.innerHTML = '<div class="empty">'
        + (live ? "正在识别，文字会陆续出现…" : "暂无转写结果") + "</div>";
      ws.lineEls = [];
      ws.active = -1;
      return;
    }

    //转写中的增量是**只增不改**的（分片按时间顺序拼接），所以只追加
    // 新句子，不重建整个列表。首片压到 15 秒后分片数明显增多，
    // 每次都重建 innerHTML 会让长稿频繁重排：滚动条跳动、
    // 正在看的那句丢失选中、选中文字被清掉。
    //
    // ★追加的前提是「已有句子不会被改写」。后端 segments 是并发返回后
    // 按 start 排序的，理论上后到的分片可能插到中间（时间更早）。
    // 所以这里额外校验：已渲染部分的「最后一句文本 + 总句数」必须与
    // 新数据吻合，否则退回全量重建。宁可偶尔重排，也不能让句子错位。
    const prev = ws.lineCount || 0;
    const prevTail = ws.lineTail || "";
    const tail = segs.length ? (segs[segs.length - 1].text || "") : "";
    const samePrefix = prev > 0 && prev <= segs.length
      && ws.renderedTask === currentId
      && prev < segs.length
      && (prevTail === "" || segs[prev - 1].text === prevTail);
    const canAppend = live && samePrefix && box.querySelector(".line");

    if (canAppend) {
      for (let i = prev; i < segs.length; i++) {
        const d = document.createElement("div");
        d.innerHTML = lineHtml(segs[i], i);
        const el = d.firstChild;
        bindLine(el);
        box.insertBefore(el, box.querySelector(".pending-line"));
      }
    } else {
      box.innerHTML = segs.map(lineHtml).join("");
      ws.lineEls = Array.prototype.slice.call(
        box.querySelectorAll(".line:not(.pending-line)"));
      ws.lineEls.forEach(bindLine);
    }
    // 「正在识别后续内容…」始终压在最后一句下面
    const tip = box.querySelector(".pending-line");
    if (live) {
      if (tip) {
        box.appendChild(tip);
      } else {
        const d = document.createElement("div");
        d.className = "line pending-line";
        d.textContent = "正在识别后续内容…";
        box.appendChild(d);
      }
    } else if (tip) {
      tip.remove();
    }
    ws.lineEls = Array.prototype.slice.call(box.querySelectorAll(".line:not(.pending-line)"));
    ws.lineCount = segs.length;
    ws.lineTail = tail;
    ws.renderedTask = currentId;

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
    const generating = !!t && (t.polishing_styles || []).indexOf(curStyle) >= 0;
    const key = currentId + "|" + curStyle + "|" + (generating ? "gen|" : "") +
      (text ? text.length + "|" + text.slice(0, 40) : "none");
    if (ws.noteKey !== key) {
      ws.noteKey = key;
      box.innerHTML = text ? renderMarkdown(text)
        : generating
          ? '<div class="empty">⏳ 正在生成「' + (STYLE_TEXT[curStyle] || curStyle) + '」…进度看上方状态栏，生成完会自动显示在这里</div>'
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

  // 只滚动文本稿容器本身把当前句带到中间：scrollIntoView 会连带滚动
  // 外层页面与吸顶面板，在不少浏览器上表现为页面乱跳或干脆不跟随。
  let progScrollUntil = 0;
  function scrollLineCenter(line, smooth) {
    const box = $("#lines");
    if (!box || !line) return;
    // 标记程序滚动窗口：这段时间内的 scroll 事件不是用户手动翻页，
    // 不能据此关掉跟随（否则平滑滚动自己会把跟随关掉）
    progScrollUntil = Date.now() + 800;
    const top = line.offsetTop - (box.clientHeight - line.clientHeight) / 2;
    box.scrollTo({ top: Math.max(0, top), behavior: smooth ? "smooth" : "auto" });
  }

  // 用户手动翻页（滚轮/触摸拖动）= 想自己看：暂停跟随并给出恢复入口，
  // 否则下一句一切换就被拽回中间，没法回看前文
  function pauseFollow() {
    if (!ws.follow) return;
    ws.follow = false;
    const b = $("#btn-follow");
    if (b) b.classList.remove("hidden");
  }

  function markActive(i, scroll) {
    if (i === ws.active) return;
    if (ws.lineEls[ws.active]) ws.lineEls[ws.active].classList.remove("active");
    ws.active = i;
    const line = ws.lineEls[i];
    if (!line) return;
    line.classList.add("active");
    if (scroll && ws.follow) scrollLineCenter(line, true);
  }

  function syncActive(t, force) {
    if (!ws.segs.length) return;
    markActive(findIdx(ws.segs, t), true);
    if (force && ws.lineEls[ws.active]) scrollLineCenter(ws.lineEls[ws.active], false);
  }

  // ---------- SSE ----------
  // SSE 会因为各种原因断开：服务端 idle 上限、云端网关/反向代理的空闲超时、
  // 移动网络切换、笔记本休眠……断开后如果只是安静地结束，页面就会永远停在
  // 最后一帧 —— 用户看到的正是「已经转写完了，文字稿不出来，刷新才有」。
  // 所以这里必须自己重连；重连后后端会因为 sent_sig 归零而补推一次全量，
  // 状态自然对齐。
  const RECONNECT_MIN = 1200, RECONNECT_MAX = 15000;
  const retryAt = new Map();

  function scheduleReconnect(tid) {
    const t = tasks.get(tid);
    if (!t) return;
    // 任务已是终态，重连没有意义；等用户点历史记录时 loadDetail 会补齐
    if (t.status === "done" || t.status === "failed" || t.status === "canceled"
        || t.status === "transcribed") return;
    if (retryAt.has(tid)) return;
    // n 是「已经重试过几次」。连接成功并收到数据后由下面的 clearRetry 归零，
    // 否则这里会一直递增，退避最终顶到上限再也降不下来。
    const n = (retryAt.get(tid) || 0) + 1;
    retryAt.set(tid, n);
    const wait = Math.min(RECONNECT_MIN * Math.pow(1.6, n - 1), RECONNECT_MAX);
    setTimeout(() => {
      retryAt.delete(tid);
      follow(tid);
    }, wait);
  }

  // 连上并收到第一个事件 = 这次连接是好的，退避计数清零，
  // 下次真断时又从最短间隔开始重试。
  function clearRetry(tid) {
    if (retryAt.has(tid)) retryAt.set(tid, 0);
  }

  async function follow(tid) {
    if (following.has(tid)) return;
    following.add(tid);
    const ctrl = new AbortController();
    controllers.set(tid, ctrl);
    let reachedEof = false;
    startPolling(tid);             // 轮询兜底：代理缓冲 SSE 时也能回填进度
    try {
      const r = await fetch("/api/tasks/" + tid + "/events", { signal: ctrl.signal });
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
          clearRetry(tid);          // 收到事件 = 连接正常，退避计数归零
          lastSseAt.set(tid, Date.now());   // 标记 SSE 有数据，暂停轮询
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
            ws.linesTask = null; ws.linesSig = null;
          } else if (ev.type === "log") {
            t.events = t.events || [];
            if (!t.events.some((x) => x.t === ev.t && x.text === ev.text)) t.events.push(ev);
          } else if (ev.type === "meta") {
            // 下载一落盘就拿到了 video_path，预览区立刻可播，不用等全流程结束。
            // 但只有文件名/封面真的变了才清 videoKey 让播放器重建：
            // 新连接的首帧 meta、生成文稿后的归档回写都会推 meta，
            // 无条件重建会把正在播放的视频打断重载，看起来像页面刷新。
            const oldF = ((t.meta && t.meta.video_path) || "").split(/[\\/]/).pop();
            const oldC = (t.meta && t.meta.cover) || "";
            t.meta = ev.meta || {};
            const newF = ((t.meta && t.meta.video_path) || "").split(/[\\/]/).pop();
            const newC = (t.meta && t.meta.cover) || "";
            if (oldF !== newF || oldC !== newC) ws.videoKey = null;
          } else if (ev.type === "eof") {
            clearPoller(tid);       // 任务结束，停掉轮询兜底
            // eof 携带最终结果，先就地采用，避免依赖 refreshOne 的网络往返
            // ——那次往返失败或慢时，页面就会停在不完整的增量内容上。
            reachedEof = true;
            if (ev.segments && ev.segments.length) {
              t.segments = ev.segments;
              t.transcript = ev.transcript || t.transcript || "";
            }
            if (ev.note !== undefined && ev.note !== null && ev.note !== "") {
              t.note = ev.note; t.note_sig = ev.note_sig || "";
              t.note_info = ev.note_info || t.note_info;
              t.variants = ev.variants || t.variants;
            }
            if (ev.meta && Object.keys(ev.meta).length) t.meta = ev.meta;
            if (ev.status) { t.status = ev.status; t.stage = ev.stage || t.stage; }
            // 强制重绘：最终句数常与最后一次增量不同
            ws.linesTask = null; ws.linesSig = null;
            await refreshOne(tid);
            ws.linesTask = null; ws.linesSig = null;
            render();
            return;
          }
          render();
        }
      }
    } catch (e) {
      /* 忽略网络中断，下面统一重连 */
    } finally {
      following.delete(tid);
      controllers.delete(tid);
      // 只有「没走到 eof 且不是被我们主动 abort（切任务）」才重连。
      // 走到 eof 说明任务真的结束了，再连也是浪费；
      // 被 abort 说明用户在切任务，旧连接本就该关，重连只会变成孤儿连接。
      if (!reachedEof && !ctrl.signal.aborted) scheduleReconnect(tid);
    }
  }

  async function refreshOne(tid) {
    try {
      const d = await api("/api/tasks/" + tid);
      const cur = tasks.get(tid);
      if (!cur) { tasks.set(tid, d); return; }
      // 原地合并：保持对象引用不变，follow 持有的就是这同一个对象，
      // 不会出现「follow 写旧对象、页面读新对象」的竞态（#5）。
      // 若本地实时增量已经比详情快照更多，保留本地进度，不被旧快照回退。
      const merged = Object.assign({}, cur, d);
      if (cur.segments && cur.segments.length > (d.segments ? d.segments.length : 0)) {
        merged.segments = cur.segments;
        merged.transcript = cur.transcript;
      }
      tasks.set(tid, merged);
    } catch (e) {}
  }

  function selectTask(id) {
    if (id !== currentId) stopFollow(currentId);   // 切任务先掐掉上一个 SSE，避免孤儿连接/重连（#1）
    currentId = id;
    ws.follow = true;
    const fb = $("#btn-follow");
    if (fb) fb.classList.add("hidden");
    ws.videoTask = null; ws.videoKey = null; ws.linesTask = null; ws.linesSig = null;
    ws.noteKey = null; ws.logCount = -1;
    ws.videoEl = null; ws.segs = []; ws.lineEls = []; ws.active = -1; ws.scrollTop = 0;
    // 增量追加依赖「已渲染多少句」，换任务必须清零，
    // 否则会把上一个任务的句数当成本任务的起点
    ws.lineCount = 0; ws.renderedTask = null; ws.lineTail = "";
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
      const cur = tasks.get(id);
      if (cur) {
        // 同 refreshOne：原地合并，且本地实时增量优先（#5）
        const merged = Object.assign({}, cur, d);
        if (cur.segments && cur.segments.length > (d.segments ? d.segments.length : 0)) {
          merged.segments = cur.segments;
          merged.transcript = cur.transcript;
        }
        tasks.set(id, merged);
      } else {
        tasks.set(id, d);
      }
      const t = tasks.get(id);
      // 点开历史记录时，任务还在跑而之前没在跟随（如启动时超出跟随上限
      // 被跳过的），这里补上跟随，恢复实时进度
      if (t && (t.status === "running" || t.status === "pending")
          && !controllers.has(id)) follow(id);
      // 沿用上次查看的风格（没有才用任务最后生成的那份），并同步按钮选中态
      if (t && t.note_info && t.note_info.style && STYLE_TEXT[t.note_info.style]) {
        curStyle = viewStyle.get(id) || t.note_info.style;
        syncStyleChips();
      }
      generatedStyles = new Set(
        (t && t.note_info && t.note_info.styles) ||
        Object.keys((t && t.variants) || {}).filter((k) => t.variants[k] && t.variants[k].note)
      );
      ws.videoTask = null; ws.videoKey = null; ws.linesTask = null; ws.linesSig = null;
      ws.noteKey = null;
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
    if (pickedFiles.length) {
      // 默认就是批量：选了几个文件就逐个上传、各自建任务
      const files = pickedFiles.slice();
      pickedFiles = [];
      $("#file").value = "";
      renderPickedFiles();
      let firstId = null, okCount = 0;
      try {
        for (const f of files) {
          uploadPct = 0;
          const r = await uploadFile(f, curStyle);
          if (r && r.id) {
            okCount++;
            if (!firstId) firstId = r.id;
            else if (!tasks.has(r.id)) refreshOne(r.id);
          }
        }
      } catch (e) {
        toast(e.message, true);
      } finally {
        // 传完固定停在 100%，不要回落到 null（那会让进度行直接消失）
        uploadPct = 100;
        render();
      }
      if (firstId) afterCreate(firstId, true);
      if (files.length > 1) toast("已创建 " + okCount + " 个上传任务");
      return;
    }
    const url = $("#url").value.trim();
    if (!url) { toast("请先上传文件或粘贴链接", true); return; }
    // 默认就是批量：一行一个链接，有几行建几个任务（单行即单任务）
    await startBatch(url);
  }

  // 批量创建：输入框按行拆成多条链接，一次提交，逐条结果汇总提示。
  async function startBatch(text) {
    const urls = text.split(/\n+/).map((s) => s.trim()).filter(Boolean);
    if (!urls.length) { toast("请先粘贴链接", true); return; }
    try {
      uploadPct = null;
      const r = await api("/api/tasks/batch", {
        method: "POST",
        body: JSON.stringify({ urls: urls, style: curStyle })
      });
      const results = (r && r.results) || [];
      const ok = results.filter((x) => x.ok);
      const bad = results.filter((x) => !x.ok);
      ok.forEach((x) => { if (x.id && !tasks.has(x.id)) refreshOne(x.id); });
      if (ok.length) $("#url").value = "";
      let msg = "批量创建完成：成功 " + ok.length + " 条";
      if (bad.length) {
        msg += "，失败 " + bad.length + " 条（" +
          bad.slice(0, 2).map((x) => x.error || "未知错误").join("；") +
          (bad.length > 2 ? "…" : "") + "）";
      }
      toast(msg, bad.length > 0 && ok.length === 0, 6000);
      if (ok.length && ok[0].id) selectTask(ok[0].id);
      render();
    } catch (e) {
      toast(e.message, true);
    }
  }

  // 任务搜索：防抖 300ms 后带 q 拉列表（服务端按标题/转写过滤）。
  async function reloadHistorySearch() {
    if (!historyQuery) { renderHistory(); return; }
    try {
      const list = await api("/api/tasks?q=" + encodeURIComponent(historyQuery));
      historyItems = Array.isArray(list) ? list : [];
    } catch (e) {
      historyItems = [];
    }
    renderHistory();
  }

  // 转写纠错：把当前任务的转写原文装进编辑框。
  function openTranscriptEdit() {
    const t = tasks.get(currentId);
    if (!t) return;
    if (t.status === "running" || t.status === "pending") {
      toast("任务运行中，结束后再编辑转写", true);
      return;
    }
    $("#edit-transcript-text").value = t.transcript || "";
    $("#edit-modal").classList.remove("hidden");
  }

  async function saveTranscriptEdit() {
    const text = $("#edit-transcript-text").value.trim();
    if (!text) { toast("转写文本不能为空", true); return; }
    try {
      const r = await api("/api/tasks/" + currentId + "/transcript", {
        method: "PATCH",
        body: JSON.stringify({ transcript: text })
      });
      const t = tasks.get(currentId);
      if (t) {
        // 与后端保持一致：成稿/变体已清，状态回到已转写，可重新整理
        t.transcript = r.transcript;
        t.note = "";
        t.variants = {};
        t.note_sig = "";
        t.status = r.status;
        t.stage = r.stage;
      }
      $("#edit-modal").classList.add("hidden");
      render();
      toast("转写已保存，可点「重新整理」生成新成稿");
    } catch (e) {
      toast(e.message, true);
    }
  }

  // 清洗文稿编辑：把当前清洗稿装进编辑框（底稿编辑，保存走 PATCH /cleaned）。
  function openCleanedEdit() {
    const t = tasks.get(currentId);
    if (!t) return;
    if (t.status === "running" || t.status === "pending") {
      toast("任务运行中，结束后再编辑清洗文稿", true);
      return;
    }
    const text = noteForStyle(t, "cleaned");
    if (!text) { toast("尚无清洗文稿可编辑", true); return; }
    $("#edit-cleaned-text").value = text;
    $("#edit-cleaned-modal").classList.remove("hidden");
  }

  async function saveCleanedEdit() {
    const text = $("#edit-cleaned-text").value.trim();
    if (!text) { toast("清洗文稿不能为空", true); return; }
    try {
      const r = await api("/api/tasks/" + currentId + "/cleaned", {
        method: "PATCH",
        body: JSON.stringify({ text: text })
      });
      const t = tasks.get(currentId);
      if (t) {
        // 与后端保持一致：只换清洗缓存与 cleaned 变体，下游成稿不动
        //（其签名基于清洗文本，下次点击自动按新底稿重新生成）
        t.clean_cache = r.clean_cache;
        t.variants = r.variants;
        if (r.note_info && r.note_info.style === "cleaned") {
          t.note = r.note;
          t.note_sig = r.note_sig;
          t.note_info = r.note_info;
        }
      }
      ws.noteKey = "";   // 强制文稿区按新文本重渲染（字数随之刷新）
      $("#edit-cleaned-modal").classList.add("hidden");
      render();
      toast("清洗文稿已保存");
    } catch (e) {
      toast(e.message, true);
    }
  }

  // 命中已有记录：直接打开那条记录（不下载、不弹窗）
  async function openExisting(r) {
    const title = r.title ? "「" + r.title + "」" : "这条视频";
    const busy = r.status === "pending" || r.status === "running";
    if (!tasks.has(r.id)) await refreshOne(r.id);
    selectTask(r.id);
    toast(title + (busy ? "正在处理中，已打开该记录"
                        : "已经下载转写过了，已打开历史记录（未重复下载）"),
          false, 6000);
  }

  function afterCreate(id, isUpload) {
    tasks.set(id, { id: id, url: "", platform: "", status: "pending", stage: "queued", percent: 0, spercent: 0, meta: {}, segments: [], events: [], created_at: Date.now() / 1000, isUpload: !!isUpload });
    selectTask(id);
    follow(id);
  }

  async function polish() {
    if (!currentId) return;
    try {
      // 按钮文案对已生成风格写的是「重新生成」，就必须真重跑（force）；
      // 首次生成走普通路径（服务端幂等保护还在，不会重复扣额度）
      const force = generatedStyles.has(curStyle);
      const r = await api("/api/tasks/" + currentId + "/polish", {
        method: "POST",
        body: JSON.stringify({ style: curStyle, force: force })
      });
      if (r && r.idempotent) toast(r.message || "已复用该风格的成稿");
      // 按钮状态立刻更新，不等 SSE 的第一帧状态事件（网络慢时按钮会像没反应）；
      // 只改本地展示态，播放器元素不碰，视频继续播
      if (r && !r.idempotent) {
        const t = tasks.get(currentId);
        if (t) {
          t.polishing_styles = Array.from(new Set([...(t.polishing_styles || []), curStyle]));
          if (t.status === "done" || t.status === "transcribed") { t.status = "running"; t.stage = "polishing"; }
        }
        render();
      }
      follow(currentId);
    } catch (e) {
      toast(e.message, true);
    }
  }

  async function cancelTask() {
    if (!currentId) return;
    try {
      await api("/api/tasks/" + currentId + "/cancel", { method: "POST" });
      toast("已请求取消，当前步骤结束后停下");
      refreshOne(currentId).then(() => render());
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

  let pendingDeleteId = null;
  function confirmDelete(id) {
    const t = tasks.get(id);
    if (!t) return;
    pendingDeleteId = id;
    // 标题在 meta 里（列表行也是读 meta.title）；旧代码读 t.title
    // 永远是 undefined，弹窗一直显示「(无标题)」。
    const title = (t.meta && t.meta.title) || t.url || "(无标题)";
    $("#confirm-msg").innerHTML = "确定要删除这条记录吗？<br><b>" + esc(title)
      + "</b><br><span class=\"confirm-sub\">删除后无法恢复，视频与转写稿会一并清除。</span>";
    $("#confirm-modal").classList.remove("hidden");
  }

  // 设置面板不展示的配置项。
  // 模型相关（asr/llm 的接口地址、Token、模型名）已放开，由「模型与生成」分组呈现并回显；
  // 七牛云（媒体归档）整组按需求隐藏，改由环境变量 V2N_QINIU_* / secrets.json 托管；
  // 其余隐藏项与本项目无关的本地环境有关（浏览器读 Cookie / Cookie 文件 / 代理 / ffmpeg 路径）。
  // 注意：只是前端不渲染，后端 DEFAULTS 与接口照常支持。
  const HIDDEN_SETTINGS = [
    "cookie_browser", "cookie_file",
    "proxy", "ffmpeg_path",
    // 七牛云整组隐藏（AK/SK/Bucket/域名/前缀/保留天数/备份开关/回显方式）
    "qiniu_ak", "qiniu_sk", "qiniu_bucket", "qiniu_domain",
    "qiniu_prefix", "qiniu_archive_days", "qiniu_backup_on_finish",
    "qiniu_proxy",
  ];

  // 「内容需回显、但后端仍按密钥存储」的项：设置面板会显示已保存的真实值，
  // 但因为它们落在 secrets.json，清空输入框时必须走 clear 列表才能真正清除
  // （直接提交空值会被后端当作「未修改」而保留原值）。
  // 其余配置都是普通明文项，回显 + 改动即提交，清空即写入空值。
  const ECHO_SECRET = [
    "asr_api_key", "llm_api_key",
    "cookie_text", "cookie_text_bili", "resolver_curl",
  ];

  // 仍按「不回显」渲染的敏感项。七牛 AK/SK 已整体隐藏，故这里为空；
  // 保留结构是为了将来若取消隐藏，可直接把键加回来。
  const SECRET_UI = {};

  function shortModel(m) {
    return String(m || "").split("/").pop().split(":")[0];
  }

  // ---- 质检报告 ------------------------------------------------------------
  // 生成后质检的查看入口：默认看当前打开的任务，没有打开任务时看最近一条
  // 有成稿的任务。报告由后端异步生成（全部风格生成完自动跑，也可手动重跑）。
  let qcTaskId = null;
  let qcTimer = null;

  function qcTargetTask() {
    const cur = currentId && tasks.get(currentId);
    if (cur && Object.values(cur.variants || {}).some((v) => v && v.note)) return cur;
    let best = null;
    for (const t of tasks.values()) {
      if (Object.values(t.variants || {}).some((v) => v && v.note) &&
          (!best || (t.created_at || 0) > (best.created_at || 0))) best = t;
    }
    return best;
  }

  function renderQc(t) {
    const box = $("#qc-content");
    if (!box) return;
    if (!t) {
      box.innerHTML = "<p class='qc-empty'>还没有可质检的任务（先生成任意一篇文稿）。</p>";
      return;
    }
    const title = ((t.meta || {}).title || t.id || "").slice(0, 60);
    let h = "<p class='qc-task'>任务：" + esc(title) + "</p>";
    const qc = t.qc || {};
    const items = qc.items || {};
    if (t.qc_running) {
      h += "<p class='qc-empty'>质检生成中…（裁判模型逐篇对照风格要求评审，稍等片刻）</p>";
    }
    if (!Object.keys(items).length && !t.qc_running) {
      h += "<p class='qc-empty'>该任务还没有质检报告。点下方「重新质检」立即生成。</p>";
    }
    if (Object.keys(items).length) {
      h += "<p class='qc-meta'>质检时间 " + esc(qc.at || "") + " · 裁判模型 " +
           esc(shortModel(qc.judge_model || "")) + "（与生成模型不同，避免自己评自己）</p>";
      for (const style of ["cleaned", "general", "digest", "article", "star"]) {
        const it = items[style];
        if (!it) continue;
        const fitCls = it.fit === "符合" ? "ok" : (it.fit === "不符合" ? "bad" : "mid");
        // 整理文稿现在要求删引流/广告/无关话术，广告多的视频保留率
      // 合理地低于 85%——标红线降到 70%，只拦灾难性塌缩（如曾经的 19%）
      const lowRatio = (style === "general" || style === "cleaned") && typeof it.ratio === "number" && it.ratio < 0.7;
        h += "<div class='qc-item'>" +
          "<div class='qc-item-head'><b>" + esc(STYLE_TEXT[style] || style) + "</b>" +
          "<span class='qc-fit " + fitCls + "'>" + esc(it.fit || "未评") + "</span>" +
          (lowRatio ? "<span class='qc-fit bad'>保留率过低</span>" : "") + "</div>" +
          "<div class='qc-nums'>生成模型 " + esc(shortModel(it.model || "")) +
          " · " + fmtNum(it.chars || 0) + " 字 · 保留率 " +
          (typeof it.ratio === "number" ? Math.round(it.ratio * 100) + "%" : "-") +
          (it.seconds ? " · 用时 " + it.seconds + "s" : "") + "</div>";
        if ((it.problems || []).length) {
          h += "<div class='qc-sec'>问题</div><ul class='qc-list'>" +
            it.problems.map((p) => "<li>" + esc(p) + "</li>").join("") + "</ul>";
        }
        if ((it.suggestions || []).length) {
          h += "<div class='qc-sec'>修改建议</div><ul class='qc-list'>" +
            it.suggestions.map((p) => "<li>" + esc(p) + "</li>").join("") + "</ul>";
        }
        if (!(it.problems || []).length && !(it.suggestions || []).length) {
          h += "<div class='qc-nums'>未发现问题。</div>";
        }
        h += "</div>";
      }
    }
    box.innerHTML = h;
  }

  async function openQc() {
    const t = qcTargetTask();
    qcTaskId = t ? t.id : null;
    // 回退到了别的任务（当前任务还没文稿）时先说清楚，免得用户对着
    // 别人的报告纳闷——弹窗里只有一行小字标任务名，很容易看漏
    if (t && currentId && t.id !== currentId) {
      toast("当前任务还没有文稿，先显示最近一篇有文稿的任务报告");
    }
    renderQc(t);
    $("#qc-modal").classList.remove("hidden");
    openQcPollOnly();
  }

  async function regenQc() {
    if (!qcTaskId) { toast("没有可质检的任务", true); return; }
    try {
      // 走 api() 统一通道：带令牌头 + 401 引导（之前用裸 fetch，
      // 纯令牌登录下会直接 401 失败）
      await api("/api/tasks/" + qcTaskId + "/qc", { method: "POST", body: "{}" });
      toast("已开始质检，报告生成后自动刷新");
      const t = tasks.get(qcTaskId);
      if (t) { t.qc_running = true; renderQc(t); }
      if (!qcTimer) openQcPollOnly();
    } catch (e) { toast(String(e.message || e), true); }
  }

  function openQcPollOnly() {
    if (qcTimer) clearInterval(qcTimer);
    qcTimer = setInterval(async () => {
      if ($("#qc-modal").classList.contains("hidden")) { clearInterval(qcTimer); qcTimer = null; return; }
      if (!qcTaskId) return;
      try {
        const d = await api("/api/tasks/" + qcTaskId);
        if (d && d.id) { tasks.set(d.id, d); renderQc(d); if (!d.qc_running) { clearInterval(qcTimer); qcTimer = null; } }
      } catch (e) {}
    }, 4000);
  }

  async function openSettings() {
    // ★每次打开都重新拉，不能只依赖页面加载时那份缓存。
    // 云机器上最典型的卡死场景：管理员 git pull + 重启了后端，
    // 但用户这个标签页是更新前打开的，settingsCache 里还是旧字段列表
    // （没有 resolver_curl / 没有 groups），于是面板永远渲染不出新东西，
    // 而刷新一下就好。这个缓存不省多少流量，却正好把「已更新」藏起来。
    try {
      settingsCache = await api("/api/settings");
      settingsLoadErr = "";
    } catch (e) {
      //拉取失败时保留上一次成功的结果（若有），面板会显示错误提示
      settingsLoadErr = (e && e.message) || String(e);
    }
    const v = settingsCache.values || {}, help = settingsCache.help || {};
    const cfg = v._configured || {};
    const full = ["asr_base_url", "llm_base_url", "cookie_file", "cookie_text",
                  "cookie_text_bili", "resolver_curl", "proxy", "ffmpeg_path"];
    // 抖音与B站的 Cookie 必须分开填：域名不同，混在一起两边都会失效
    const areas = ["cookie_text", "cookie_text_bili", "resolver_curl"];
    const PH = {
      cookie_text: "【抖音】F12 → Network → 点任意请求 → Request Headers → 复制整段 Cookie"
        + "（形如 a=1; b=2）粘到这里，程序自动转 cookies.txt。也接受 Netscape 格式全文。"
        + "（已保存的内容会回显；清空并保存即清除）",
      cookie_text_bili: "【B站】在 B站登录后按同样方式复制整段 Cookie 粘到这里。"
        + "务必包含登录态字段（登录会话凭证），否则会返回 412。"
        + "（已保存的内容会回显；清空并保存即清除）",
      cookie_browser: "只填浏览器名，不要粘贴 Cookie 内容。chrome / edge / firefox / brave。"
        + "（容器/云端环境没有浏览器，优先用上面的 Cookie 文本框）",
      cookie_file: "本机已有的 Netscape cookies.txt 绝对路径，例如 D:\\cookies.txt（一般用不到）。",
      resolver_curl: "抖音解析接口的请求模板（可选，通常留空即可）。"
        + "在 dlpanda 页面上 F12 → Network → 随便点一次解析请求 → 右键「复制」→"
        + "「复制为 cURL」，把整段粘到这里。程序会自动："
        + "① 替换 _token / t0ken 为页面实时值 ② 只替换 url 字段为你要解析的链接 "
            + "③ 把 Cookie 注入浏览器会话。留空则使用内置模板。",
      qiniu_ak: "七牛云控制台 → 密钥管理 → AK。已保存的内容不会回显，留空即保持不变",
      qiniu_sk: "七牛云控制台 → 密钥管理 → SK。已保存的内容不会回显，留空即保持不变",
    };
    const rows = { cookie_text: 4, cookie_text_bili: 4, resolver_curl: 9 };

    // 按后端给的分组顺序渲染，不用 HELP 的键序——
    // resolver_curl 属于「不配也能跑」的可选项，排在末尾等于事实上看不见。
    // groups 缺失时退回原来的单列布局（老后端兼容）。
    const groups = settingsCache.groups || [];
    const known = new Set(groups.flatMap((g) => g.keys || []));
    const rest = Object.keys(help).filter(
      (k) => !known.has(k) && HIDDEN_SETTINGS.indexOf(k) < 0);
    const blocks = [];
    if (groups.length) {
      groups.forEach((g) => {
        const keys = (g.keys || []).filter(
          (k) => help[k] && HIDDEN_SETTINGS.indexOf(k) < 0);
        if (keys.length) {
          blocks.push('<div class="sgroup"><div class="sgroup-t">' + esc(g.title)
            + "</div>" + keys.map((k) => field(k, v, help, cfg, full, areas,
                                           PH, rows)).join("") + "</div>");
        }
      });
    }
if (rest.length) {
      blocks.push(rest.map((k) => field(k, v, help, cfg, full, areas, PH, rows)).join(""));
    }
    // 拉取失败时必须优先报错，且不能只报「版本过旧」——
    // 后端没起来和代码没更新是两回事，混为一谈会让用户做无用功。
    // 保留上一次成功的表单内容供参考，但错误块放最上面。
    // 提示条都不包 .sgroup —— 包进去会多出一个空分组，
    // 既打乱「首个分组是平台登录态」的顺序，也把 curl 输入框往下挤。
    if (settingsLoadErr) {
      blocks.unshift('<div class="sload-err">'
        + '<b>⚠️ 无法连接后端 /api/settings</b>（'
        + esc(settingsLoadErr) + "）<br>下面显示的是上次成功获取的内容，"
        + "可能已过期。请确认服务在运行；若是刚更新过代码，"
        + "需<b>重启服务</b>后再打开本面板。"
        + "</div>");
    } else if (!blocks.length) {
      // help 为空说明后端确实没返回任何字段——这才是「版本过旧」
      blocks.push('<div class="sload-err">'
        + "后端未返回任何设置字段，代码版本可能过旧。"
        + "<br>请执行 <b>git pull</b> 后<b>重启服务</b>，再刷新页面。"
        + "</div>");
    }
    // 渲染后立刻核对 curl 框是否真的在 DOM 里，并把结论写在面板顶部。
    // 「看不到 curl 输入框」这个问题，靠猜是猜不出来的——可能是没更新、
    // 可能接口失败、可能被隐藏规则过滤。这里把判定摊开给用户看。
    // 自检条只在真有问题（bad）时才出现：正常时它是调试信息，
    // 常驻顶部只会挤占设置面板、让每个打开设置的人先读一段无关文字
    if (!settingsLoadErr) {
      const sc = selfCheck(help, blocks.join(""));
      if (sc.indexOf("scheck bad") >= 0) blocks.unshift(sc);
    }
    $("#settings-form").innerHTML = blocks.join("");
    $("#settings-modal").classList.remove("hidden");
    // 管理员才渲染令牌管理区块（普通 token 看不到；后端也会拒绝）
    if (authState.role === "admin") renderTokenAdmin();
    // 打开即定位到「平台登录态」：用户十有八九是来改 Cookie 或 curl 模板的
    const first = $("#settings-form .sgroup");
    if (first) first.scrollIntoView({ block: "start" });
  }

  // 设置面板顶部的自检条：把「curl 输入框到底为什么看不见」摊开成可判断的几项。
  // 过去这个只能靠猜——是没更新代码、接口挂了、还是被隐藏规则过滤掉了。
  function selfCheck(help, renderedHtml) {
    const inHelp = Object.prototype.hasOwnProperty.call(help, "resolver_curl");
    const hidden = HIDDEN_SETTINGS.indexOf("resolver_curl") >= 0;
    const inDom = /data-key="resolver_curl"/.test(renderedHtml);
    let state, cls;
    if (inDom) {
      state = "✅ 解析请求模板输入框已就位，在下方「平台登录态」分组里";
      cls = "ok";
    } else if (!inHelp) {
      // 这条最容易把人带偏：页面样式是新的（前端文件已更新），
      // 但后端进程还在跑旧代码。git pull 换的是磁盘上的文件，
      // 运行中的 Python 进程不会重新加载——所以光pull 不够，必须重启。
      state = "❌ 后端未返回 resolver_curl 字段——<b>后端进程仍是旧代码</b>。"
        + "静态文件已更新，但运行中的服务不会自动重新加载，"
        + "所以这一步<b>仅刷新页面没用，必须重启服务</b>："
        + "<code>sudo systemctl restart video2note</code>，再刷新本页。"
        + "（若磁盘代码也不是最新，先 <code>git pull</code> 再重启）";
      cls = "bad";
    } else if (hidden) {
      state = "❌ resolver_curl 被前端隐藏规则过滤，属前端 bug";
      cls = "bad";
    } else {
 state = "❌ 后端返回了该字段但未渲染，请把上面信息反馈给我";
      cls = "bad";
    }
    // ★不能包在 .sgroup 里：.sgroup 是分组容器，包进去会多出一个「空分组」，
    // 既破坏「首个分组是平台登录态」的约定，也会把 curl 输入框往下挤。
    // 自检条自己横跨整行即可。
    return '<div class="scheck ' + cls + '">' + state + "</div>";
  }

  function field(k, v, help, cfg, full, areas, PH, rows) {
    const wide = full.indexOf(k) >= 0 ? " full" : "";
    const isSecret = Object.prototype.hasOwnProperty.call(SECRET_UI, k);
    // 敏感项不回显真实值，val 恒为空
    const val = isSecret ? "" : (v[k] !== undefined ? v[k] : "");
    let ctl;
    if (areas.indexOf(k) >= 0) {
      ctl = '<textarea data-key="' + k + '" rows="' + (rows[k] || 4)
        + '" placeholder="' + esc(PH[k] || "") + '">' + esc(val) + "</textarea>";
    } else {
      ctl = '<input data-key="' + k + '" value="' + esc(val) + '" placeholder="'
        + esc(PH[k] || "") + '">';
    }
    // ★状态行必须挂在「所有敏感项」上，不能只挂多行文本那三项。
    // 早前的写法把状态判断写在 areas 分支里，于是单行输入的敏感项会被渲染成
    // 一个「空的普通输入框」：填了不知道有没有生效，配错了界面毫无提示。
    // 七牛 AK/SK 正好是单行输入，直接撞上这个缺口。
    if (isSecret) {
      const on = !!cfg[SECRET_UI[k]];
      // 每个框如实报自己的状态，不被「成对才算可用」顶掉：
      // SK 没填就是没填，直接说「粘贴后点保存」最可操作；
      // 说「密钥不完整」的话，用户还得自己判断缺的是哪一个。
      ctl += '<div class="secret-row"><span class="secret-state">'
        + (on ? "✅ 已配置（内容不回显，留空保持不变）" : "未配置 —— 粘贴后点保存")
        + "</span>"
        + (on ? '<button type="button" class="btn small danger" data-clear="' + k
               + '">清除</button>' : "") + "</div>";
      // 两个都填却判定不可用最难自查，所以成对校验单独再给一行，
      // 「归档为什么没跑」在这行能直接看到。
      if ((k === "qiniu_ak" || k === "qiniu_sk") && !cfg.qiniu) {
        ctl += '<div class="secret-row"><span class="secret-state warn">'
          + "⚠️ 密钥不完整 —— AK 与 SK 需同时填写，缺一个归档不会启用</span></div>";
      }
    }
    if (k === "resolver_curl") {
      ctl += '<div class="secret-row"><button type="button" class="btn small"'
        + ' data-parse-curl="1">校验这段 curl</button>'
        + '<span class="secret-state" id="curl-check"></span></div>'
        + '<p class="field-note">解析接口改版或解析失败时，用这段模板让程序沿用'
        + '你浏览器里的真实请求；不填也能正常用内置模板。</p>';
    }
    // resolver_curl 额外加一个醒目标记，避免用户在一堆字段里找不到它
    const mark = k === "resolver_curl" ? ' data-mark="curl"' : "";
    return '<div class="field' + wide + '"' + mark + '><label>' + esc(help[k])
      + "</label>" + ctl + "</div>";
  }

  async function clearSecret(key) {
    const labels = { cookie_text: "抖音", cookie_text_bili: "B站",
                     resolver_curl: "解析请求模板",
                     qiniu_ak: "七牛云 AccessKey", qiniu_sk: "七牛云 SecretKey" };
    const label = labels[key] || key;
    const extra = key === "resolver_curl"
      ? "清除后抖音解析将回到内置模板。"
      : (key === "qiniu_ak" || key === "qiniu_sk")
        ? "清除后自动归档不会再跑（本地文件不会被删），已上传到云端的视频不受影响。"
        : "清除后下载这两个平台的视频会因缺少登录态而失败。";
    if (!confirm("确定清除已保存的" + label + "？" + extra)) return;
    try {
      settingsCache = await api("/api/settings", {
        method: "POST", body: JSON.stringify({ values: {}, clear: [key] }),
      });
      openSettings();
      toast(label + " 已清除");
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
    const clear = [];
    $("#settings-form").querySelectorAll("input, textarea").forEach((inp) => {
      const k = inp.dataset.key;
      if (!k) return;
      // 仍按「不回显」渲染的敏感项（当前 SECRET_UI 为空）：只有填了新值才提交
      if (Object.prototype.hasOwnProperty.call(SECRET_UI, k)) {
        if (inp.value.trim()) values[k] = inp.value;
        return;
      }
      const orig = String((settingsCache.values || {})[k] !== undefined ? settingsCache.values[k] : "");
      if (inp.value === orig) return;              // 未改动，跳过
      // 回显型密钥（ASR/LLM Key、Cookie、解析模板）：清空输入框 = 清除。
      // 必须走后端 clear 列表——直接提交空值会被 save_settings 当作「未修改」而保留原值。
      if (ECHO_SECRET.indexOf(k) >= 0 && !inp.value) { clear.push(k); return; }
      values[k] = inp.value;
    });
    const payload = { values: values };
    if (clear.length) payload.clear = clear;
    try {
      const r = await api("/api/settings", { method: "POST", body: JSON.stringify(payload) });
      settingsCache = r;
      $("#settings-modal").classList.add("hidden");
      const f = (r && r.cookie_files) || {};
      const written = Object.keys(f).map((k) => (k === "bilibili" ? "B站" : "抖音") + " Cookie 已写入 " + f[k]);
      toast(written.length ? "设置已保存；" + written.join("；") : "设置已保存");
    } catch (e) {
      toast(e.message, true);
    }
  }

  // 管理员令牌管理区块（仅 role===admin 时由 openSettings 注入）
  async function renderTokenAdmin() {
    const host = document.createElement("div");
    host.className = "sgroup token-admin";
    host.innerHTML =
      '<div class="sgroup-t">访问令牌（管理员）</div>'
      + '<div id="token-usage" class="secret-row"></div>'
      + '<div class="field"><label>新增令牌（仅普通用户）</label>'
      + '<div class="token-add">'
      + '<input id="tk-new" class="inp" placeholder="至少 6 位，建议随机串">'
      + '<button id="tk-add" class="btn small">添加</button>'
      + '</div></div>'
      + '<div class="secret-row">管理员唯一来源：容器环境变量 V2N_ACCESS_TOKEN / V2N_ADMIN_TOKEN，此处只能添加普通用户令牌。</div>'
      + '<div id="token-list" class="token-list"></div>';
    $("#settings-form").appendChild(host);

    try {
      const u = await api("/api/admin/usage");
      $("#token-usage").textContent = "免费次数：" + u.used + " / " + u.limit
        + "（剩余 " + u.free_remaining + "）"
        + (u.gate_active ? "；门禁已激活" : "；门禁未激活")
        + (u.enabled ? "" : "（总开关已关）");
    } catch (e) { $("#token-usage").textContent = "（读取使用量失败）"; }

    await loadTokenList();

    $("#tk-add").addEventListener("click", async () => {
      const tok = $("#tk-new").value.trim();
      if (tok.length < 6) { toast("令牌至少 6 位", true); return; }
      try {
        // 固定 role=user：管理员只能来自环境变量引导令牌，网页里建不出管理员
        await api("/api/admin/tokens", { method: "POST",
          body: JSON.stringify({ token: tok, role: "user" }) });
        $("#tk-new").value = "";
        toast("已添加令牌");
        await loadTokenList();
      } catch (e) { toast(e.message, true); }
    });
  }

  async function loadTokenList() {
    const box = $("#token-list");
    if (!box) return;
    let data;
    try { data = await api("/api/admin/tokens"); }
    catch (e) { box.innerHTML = '<div class="secret-row">读取失败</div>'; return; }
    const rows = (data.tokens || []).map((t) =>
      '<div class="token-row"><span><code>' + esc(t.token) + "</code> · "
      + (t.role === "admin" ? "管理员" : "普通") + "</span>"
      + '<button class="btn small danger" data-del="' + esc(t.token) + '">删除</button></div>'
    ).join("") || '<div class="secret-row">暂无额外令牌（引导管理员令牌在环境变量中配置）</div>';
    box.innerHTML = rows;
    box.querySelectorAll("[data-del]").forEach((b) => {
      b.addEventListener("click", async () => {
        if (!confirm("确定删除该令牌？删除后持该令牌的用户将无法继续使用。")) return;
        try {
          await api("/api/admin/tokens?token=" + encodeURIComponent(b.dataset.del),
            { method: "DELETE" });
          toast("已删除");
          await loadTokenList();
        } catch (e) { toast(e.message, true); }
      });
    });
  }

  // ---------- 事件 ----------
  document.addEventListener("click", async (e) => {
    const chk = e.target.closest("[data-parse-curl]");
    if (chk) { e.stopPropagation(); checkCurl(); return; }
    const clr = e.target.closest("[data-clear]");
    if (clr) { e.stopPropagation(); clearSecret(clr.dataset.clear); return; }
    const syncB = e.target.closest("#btn-cloud-sync");
    if (syncB) { e.stopPropagation(); syncNow(); return; }
    const del = e.target.closest("[data-del]");
    if (del) { e.stopPropagation(); confirmDelete(del.dataset.del); return; }
    const hitem = e.target.closest(".hitem");
    if (hitem) {
      selectTask(hitem.dataset.id);
      // 手机端选中即收起抽屉，否则浮层盖着结果区还要再点一次遮罩
      if (isMobile()) closeDrawers();
      return;
    }

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
        // 复制「屏幕上正在显示的」那一版：切过风格时 t.note 只是最后一次
        // 生成的风格的内容，直接复制它会复制到别的内容，必须按 curStyle 取。
        const text = noteForStyle(t, curStyle) || (t && t.note) || "";
        if (!text) { toast("还没有可复制的文稿", true); return; }
        copyText(text).then((ok) => {
          toast(ok ? "已复制" : "复制失败，请手动选中文稿复制", !ok);
        });
      }
      if (a === "download") {
        const t = tasks.get(currentId);
        const text = noteForStyle(t, curStyle) || (t && t.note) || "";
        if (!text) { toast("还没有可下载的文稿", true); return; }
        // 直接用内存里当前展示的这一版生成文件，不走后端导出：
        // 与「复制」同源，保证下到的就是屏幕上看到的风格与内容。
        const title = ((t && t.meta && t.meta.title) || "文稿").replace(/[\\/:*?"<>|]/g, "_").slice(0, 60);
        const blob = new Blob([text], { type: "text/markdown;charset=utf-8" });
        const aEl = document.createElement("a");
        aEl.href = URL.createObjectURL(blob);
        aEl.download = title + ".md";
        document.body.appendChild(aEl);
        aEl.click();
        aEl.remove();
        setTimeout(() => URL.revokeObjectURL(aEl.href), 4000);
        toast("已下载 .md");
      }
      return;
    }
    if (e.target.closest("#health")) {
      const el = $("#health");
      el.classList.add("checking");
      el.innerHTML = "检测中…";
      loadHealth(true);
      return;
    }

    if (e.target.id === "btn-start") start();
    else if (e.target.id === "btn-polish") polish();
    else if (e.target.id === "btn-cancel") cancelTask();
    else if (e.target.id === "btn-retry") retryTask();
    else if (e.target.id === "btn-history") { $("#side-left").classList.toggle("folded"); syncSideBtns(); syncDrawerMask(); }
    else if (e.target.id === "btn-logs") { $("#side-right").classList.toggle("folded"); syncSideBtns(); syncDrawerMask(); }
    // 点抽屉遮罩关闭（桌面端遮罩恒隐藏，不会触发）
    else if (e.target.id === "drawer-mask") closeDrawers();
    else if (e.target.id === "btn-settings") openSettings();
    else if (e.target.id === "btn-qc") openQc();
    else if (e.target.id === "btn-close-qc" || e.target.id === "btn-cancel-qc") $("#qc-modal").classList.add("hidden");
    else if (e.target.id === "btn-regen-qc") regenQc();
    else if (e.target.id === "btn-close-settings" || e.target.id === "btn-cancel-settings") $("#settings-modal").classList.add("hidden");
    else if (e.target.id === "btn-save-settings") saveSettings();
    // 令牌输入框：点确定去校验，关闭则放弃本次请求重试
    else if (e.target.id === "btn-ok-auth") { submitToken(); return; }
    else if (e.target.id === "auth-modal") { settleAuth(false); return; }
    else if (e.target.id === "btn-ok-confirm") {
      $("#confirm-modal").classList.add("hidden");
      const id = pendingDeleteId;
      pendingDeleteId = null;
      if (id) removeTask(id);
    }
    else if (e.target.id === "btn-cancel-confirm" || e.target.id === "btn-close-confirm") {
      $("#confirm-modal").classList.add("hidden");
      pendingDeleteId = null;
    }
    // 点击遮罩空白处也关闭确认弹窗（不删）
    else if (e.target.id === "confirm-modal") {
      $("#confirm-modal").classList.add("hidden");
      pendingDeleteId = null;
    }
    else if (e.target.id === "pick") $("#file").click();
  });

  // 已选文件的展示与管理：名字逐个列出可单独移除（选错不用整批重选），
  // 并把「开始转写」按钮文案带上文件数、高亮一下，免得用户选完文件
  // 还要跨到右栏去找那个对着空链接框的按钮才敢点
  function renderPickedFiles() {
    const dt = $("#drop-text");
    const t2 = drop ? drop.querySelector(".up-t2") : null;
    if (!pickedFiles.length) {
      if (dt) dt.textContent = "将文件拖放到此处";
      if (t2) t2.innerHTML = '或 <a href="javascript:void(0)" id="pick">点击上传</a>，支持视频 / 音频';
    } else {
      if (dt) dt.textContent = "已选择 " + pickedFiles.length + " 个文件";
      if (t2) t2.innerHTML = pickedFiles.map((f, i) =>
        '<span class="fname">' + esc(f.name) +
        ' <a href="javascript:void(0)" class="unpick" data-unpick="' + i + '" title="移除这个文件">×</a></span>'
      ).join(" ") + "<br>点右侧「开始转写」处理";
    }
    updateStartBtn();
  }
  function updateStartBtn() {
    const b = $("#btn-start");
    if (!b) return;
    const urlEmpty = !(($("#url") || {}).value || "").trim();
    const fileMode = !!(pickedFiles.length && urlEmpty);
    b.textContent = fileMode ? "开始转写（" + pickedFiles.length + " 个文件）" : "开始转写";
    b.classList.toggle("pulse", fileMode);
  }

  const drop = $("#drop");
  if (drop) {
    drop.addEventListener("click", (e) => {
      const un = e.target.closest("[data-unpick]");
      if (un) {
        e.stopPropagation();
        pickedFiles.splice(parseInt(un.dataset.unpick, 10), 1);
        renderPickedFiles();
        return;
      }
      $("#file").click();
    });
    drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("over"); });
    drop.addEventListener("dragleave", () => drop.classList.remove("over"));
    drop.addEventListener("drop", (e) => {
      e.preventDefault();
      drop.classList.remove("over");
      const fs = e.dataTransfer.files;
      if (fs && fs.length) {
        pickedFiles = Array.from(fs);
        renderPickedFiles();
      }
    });
  }
  const fileInput = $("#file");
  if (fileInput) fileInput.addEventListener("change", () => {
    const fs = fileInput.files;
    if (fs && fs.length) {
      pickedFiles = Array.from(fs);
      renderPickedFiles();
    }
  });
  const urlInput = $("#url");
  if (urlInput) urlInput.addEventListener("input", updateStartBtn);
  $("#url").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) start();
  });
  // 历史搜索：输入防抖后走服务端 q 过滤（标题/转写），清空恢复全量
  const searchInput = $("#history-search");
  if (searchInput) searchInput.addEventListener("input", () => {
    historyQuery = searchInput.value.trim();
    clearTimeout(historySearchTimer);
    historySearchTimer = setTimeout(reloadHistorySearch, 300);
  });
  // 转写编辑弹窗
  const editBtn = $("#btn-edit-transcript");
  if (editBtn) editBtn.addEventListener("click", openTranscriptEdit);
  // 播放器倍速：选择即生效并记住（localStorage），换任务时 ensureVideo 恢复
  const ratePick = $("#rate-pick");
  if (ratePick) {
    ratePick.value = localStorage.getItem("v2n_rate") || "1";
    ratePick.addEventListener("change", () => {
      localStorage.setItem("v2n_rate", ratePick.value);
      if (ws.videoEl) ws.videoEl.playbackRate = parseFloat(ratePick.value) || 1;
    });
  }
  // 画中画：小窗悬浮播放，边看边往下读稿（仅视频元素支持）
  const pipBtn = $("#btn-pip");
  if (pipBtn) pipBtn.addEventListener("click", async () => {
    const v = ws.videoEl;
    if (!v || v.tagName !== "VIDEO") { toast("当前没有可画中画的视频", true); return; }
    try { await v.requestPictureInPicture(); }
    catch (e) { toast("画中画不可用（浏览器限制）", true); }
  });
  const saveEditBtn = $("#btn-save-edit");
  if (saveEditBtn) saveEditBtn.addEventListener("click", saveTranscriptEdit);
  ["#btn-close-edit", "#btn-cancel-edit"].forEach((sel) => {
    const b = $(sel);
    if (b) b.addEventListener("click", () => $("#edit-modal").classList.add("hidden"));
  });
  // 清洗文稿编辑弹窗
  const editCleanBtn = $("#btn-edit-cleaned");
  if (editCleanBtn) editCleanBtn.addEventListener("click", openCleanedEdit);
  const saveCleanBtn = $("#btn-save-cleaned");
  if (saveCleanBtn) saveCleanBtn.addEventListener("click", saveCleanedEdit);
  ["#btn-close-cleaned", "#btn-cancel-cleaned"].forEach((sel) => {
    const b = $(sel);
    if (b) b.addEventListener("click", () => $("#edit-cleaned-modal").classList.add("hidden"));
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      $("#settings-modal").classList.add("hidden");
      $("#confirm-modal").classList.add("hidden");
      const em = $("#edit-modal");
      if (em) em.classList.add("hidden");
      const cm = $("#edit-cleaned-modal");
      if (cm) cm.classList.add("hidden");
      closeDrawers();
      pendingDeleteId = null;
      if (!$("#auth-modal").classList.contains("hidden")) settleAuth(false);
    }
    // 令牌框里回车直接提交，省得再去点按钮
    if (e.key === "Enter" && e.target.id === "auth-token-input") submitToken();
  });

  // ---------- 启动 ----------
  // 记住文字区滚动位置：转写增量会重建 DOM，重建后要还原回去
  $("#lines").addEventListener("scroll", saveScroll, { passive: true });
  // 手动翻页暂停跟随（滚轮与触摸是明确的用户意图；程序滚动走 progScrollUntil 守卫）
  $("#lines").addEventListener("wheel", () => { if (Date.now() > progScrollUntil) pauseFollow(); }, { passive: true });
  $("#lines").addEventListener("touchmove", () => { if (Date.now() > progScrollUntil) pauseFollow(); }, { passive: true });
  const followBtn = $("#btn-follow");
  if (followBtn) followBtn.addEventListener("click", () => {
    ws.follow = true;
    followBtn.classList.add("hidden");
    if (ws.videoEl) syncActive(ws.videoEl.currentTime, true);
  });

  // ---------- 手机端抽屉工具 ----------
  // ≤720px 时侧栏是浮层抽屉（见 style.css 对应断点）：
  // 展开要显示遮罩并锁页面滚动，收起则相反；桌面端遮罩恒不显示。
  const mqMobile = window.matchMedia("(max-width: 720px)");
  function isMobile() { return mqMobile.matches; }
  function closeDrawers() {
    $("#side-left").classList.add("folded");
    $("#side-right").classList.add("folded");
    syncSideBtns();
    syncDrawerMask();
  }
  function syncDrawerMask() {
    const mask = $("#drawer-mask");
    if (!mask) return;
    const open = isMobile() && (
      !$("#side-left").classList.contains("folded") ||
      !$("#side-right").classList.contains("folded"));
    mask.classList.toggle("hidden", !open);
    // 抽屉打开时锁 body 滚动，避免抽屉内滑一下带着背景一起滚
    document.body.style.overflow = open ? "hidden" : "";
  }
  // 跨断点（如横竖屏切换、拉伸窗口）时清理抽屉态：
  // 回到桌面宽度后遮罩必须消失，否则会挡住整个页面。
  mqMobile.addEventListener("change", () => syncDrawerMask());

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

  // 渲染顶栏环境自述。独立成函数是为了能「不阻塞页面启动」地调用，
  // 并且支持点击重新检测（refresh=1 强制重新探测，跳过后端缓存）。
  async function loadHealth(refresh) {
    // 顶栏「检测中…」一旦卡死，说明 /api/health 请求没回来——
    // 云机器上多半是后端进程没起来 / 被预览代理(monkeycode-ai.online)缓冲掐住。
    // 这里加 6s 超时 + 失败自动重试，绝不无限转圈：超时就亮「后端未连接」，
    // 后端恢复后下次轮询自动刷新回来。
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), 6000);
    let h;
    try {
      const r = await fetch("/api/health" + (refresh ? "?refresh=1" : ""), {
        headers: { "Content-Type": "application/json" },
        signal: ctrl.signal,
      });
      if (!r.ok) throw new Error("请求失败 " + r.status);
      h = await r.json();
    } catch (e) {
      clearTimeout(timer);
      const el = $("#health");
      el.classList.remove("checking");
      el.innerHTML = '<span class="bad" title="后端无响应：进程可能没启动，'
        + '或预览代理缓冲了请求。点此手动重试">后端未连接</span>';
      // 非手动触发才自动重试：后端临时抽风/刚重启时自愈，不必用户守着点
      if (!refresh) setTimeout(() => loadHealth(false), 8000);
      return;
    }
    clearTimeout(timer);
    const bits = [];
    bits.push(h.ffmpeg ? "ffmpeg 就绪" : '<span class="bad">缺少 ffmpeg</span>');
    bits.push(h.yt_dlp ? "yt-dlp 就绪" : '<span class="bad">缺少 yt-dlp</span>');
    // 抖音通道：必须报真实原因。「装了 playwright 库但没装浏览器内核」
    // 时import 是成功的，只显示 ready 会让用户跑到下载时才炸出
    // Executable doesn't exist，title 里直接给出修复命令。
    if (h.playwright) {
      bits.push('<span class="ok" title="' + esc(h.playwright_msg || "")
        + '">抖音通道就绪</span>');
    } else {
      bits.push('<span class="bad" title="' + esc(h.playwright_msg || "")
        + '">抖音通道不可用</span>');
    }
    const v = h.version || {};
    // 把版本号亮出来：云机器上「看不到某个功能」时，第一件事就是确认
    // 浏览器连的是不是最新代码，而不是靠猜缓存/没重启
    if (v.commit || v.assets) {
      bits.push('<span class="ver" title="git commit / 静态资源版本戳">v'
        + esc(v.commit || "?") + (v.assets ? " · " + esc(v.assets) : "") + "</span>");
    } else {
      // ★ 后端连版本自述都没有 = 它跑的是 63f7a8e 之前的代码。
      // 这种情况顶栏必须开口，否则用户看到的是一个「一切正常」的页面，
      // 只有设置面板里才冒出那句「后端未返回 resolver_curl」——
      // 而那句会被理解成前端坏了。版本不一致必须在这里就说清楚。
      bits.push('<span class="bad" title="后端进程仍在运行旧代码，'
        + 'git pull 只更新磁盘文件，不会让运行中的进程重新加载">'
        + "后端代码过旧（无版本自述），请重启服务</span>");
    }
    if (v.has_groups === false) {
      bits.push('<span class="bad">后端版本过旧，请 git pull 后重启</span>');
    }
    const el = $("#health");
    // 顶栏是给使用者看的：依赖明细与版本戳是管理员排查信息。
    // 非管理员只看结论（正常 / 哪项异常），管理员保持全量。
    const hasBad = bits.some((b) => b.indexOf('class="bad"') >= 0);
    el.classList.toggle("has-bad", hasBad);
    if (typeof authState !== "undefined" && authState.role && authState.role !== "admin") {
      el.innerHTML = hasBad ? bits.filter((b) => b.indexOf('class="bad"') >= 0).join(" · ") : '<span class="ok">服务正常</span>';
    } else {
      el.innerHTML = bits.join(" · ");
    }
    el.classList.remove("checking");
    el.title = "点击重新检测依赖";
  }

  async function boot() {
    // 手机端默认收起历史栏：抽屉浮层不该在首屏就占住屏幕
    if (isMobile()) $("#side-left").classList.add("folded");
    syncSideBtns();
    syncDrawerMask();
    await refreshAuthState();   // 先拿角色/门禁状态，决定设置按钮与横幅
    try {
      const list = await api("/api/tasks");
      list.forEach((t) => tasks.set(t.id, t));
      const sorted = list.slice().sort((a, b) => b.created_at - a.created_at);
      if (sorted.length) {
        currentId = sorted[0].id;
        // 只跟随最近 3 个活跃任务：每个 follow = 1 条 SSE + 1 个轮询器。
        // 浏览器对同域名的并发连接数有限（HTTP/1.1 下 6 条），僵尸任务一多
        // 挂死的 SSE 会占满连接池，/api/health 排不上队 → 顶栏永远「检测中」。
        // 其余活跃任务在点开时由 loadDetail 补上跟随。
        const active = sorted.filter((t) => t.status === "running" || t.status === "pending");
        active.slice(0, 3).forEach((t) => follow(t.id));
      }
      render();
      // 默认选中最近一条，并拉详情回显视频/ 转写 / 文稿
      if (currentId) loadDetail(currentId);
    } catch (e) {}
    // 健康检查**不 await**：它要探 ffmpeg 子进程与 Chromium 内核，
    // 曾经把整个页面启动卡住（内核异常时更会等到超时）。
    // 顶栏那一行文字晚几百毫秒出现无所谓的，绝不能挡住页面可用。
    const healthEl = $("#health");
    healthEl.classList.add("checking");
    healthEl.innerHTML = "检测中…";
    loadHealth(false);
    // 设置项加载失败不能静默：留一个标记，openSettings 据此给出明确提示。
    // 云机器上最常见的原因是代码没更新——接口存在但字段不同，或直接连不上。
    try {
      settingsCache = await api("/api/settings");
      settingsLoadErr = "";
    } catch (e) {
      settingsLoadErr = (e && e.message) || String(e);
      settingsCache = {};
    }
    nudgePendingSync();
  }

  // 备份到云端有可能漏掉几个（当时没配密钥、网络抖了一下）。
  // 这类「看起来没事、清理掉就丢数据」的状态必须让用户看得见，
  // 所以启动时查一次：有待同步的就提示一次，点一下补传，不打断正常使用。
  let syncNudged = false;
  async function nudgePendingSync() {
    if (syncNudged) return;
    syncNudged = true;
    let st;
    try {
      st = await api("/api/archive/status");
    } catch (e) {
      return;                       // 拿不到就算了，不能因为提示挡住页面
    }
    if (!st || !st.configured || !st.pending_count) return;
    const mb = (st.pending_bytes || 0) / 1048576;
    toast(st.pending_count + " 个视频还没同步到云端（约 "
      + (mb >= 1024 ? (mb / 1024).toFixed(1) + "GB" : Math.round(mb) + "MB")
      + "），点顶栏「☁ 同步」补传", false, 8000);
    syncBtnOn();
  }

  function syncBtnOn() {
    const b = $("#btn-cloud-sync");
    if (b) b.classList.remove("hidden");
  }

  async function syncNow() {
    try {
      const r = await api("/api/archive/sync", { method: "POST" });
      toast(r.started ? ("已开始同步 " + r.started + " 个视频") : "没有待同步的视频");
      if (r.started) {
        setTimeout(() => { syncNudged = false; nudgePendingSync(); }, 8000);
      }
    } catch (e) {
      toast(e.message, true);
    }
  }

  // 后台标签页会被浏览器冻结计时器，SSE 也可能被网关悄悄掐掉。
  // 用户切回来时页面还停在离开前那一帧，看起来就像「转写完了但没显示」。
  // 这里在页面重新可见时主动对账一次：拉详情、重绘、并重连 SSE。
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState !== "visible" || !currentId) return;
    const t = tasks.get(currentId);
    if (!t) return;
    // 已经是终态就不用折腾了，详情在点历史记录时就拉全了
    if (t.status === "done" || t.status === "failed" || t.status === "canceled"
        || t.status === "transcribed") return;
    retryAt.delete(currentId);
    // 只重绘文本稿，不清 videoKey：播放器是否重建由 ensureVideo 的键
    // （任务+文件名+封面）决定，切回标签页不该打断正在播的视频
    ws.linesTask = null; ws.linesSig = null;
    refreshOne(currentId).then(() => {
      ws.linesTask = null; ws.linesSig = null;
      render();
      follow(currentId);
    });
  });

  boot();
})();
