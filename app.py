"""☁️ 【Gradio 版】视频转笔记 Video2Note —— ModelScope 创空间入口

链路：粘贴链接 / 上传音视频 → 下载并抽音频 → 静音点切片 → 在线 ASR 转写 → LLM 整理成稿

启动：
    pip install -r requirements.txt
    python app.py           # 监听 0.0.0.0:7860

环境变量（也可在页面「设置」页签填写，落到持久化目录的 settings.json）：
    V2N_ASR_API_KEY / MOARK_API_TOKEN          模力方舟语音识别密钥
    V2N_LLM_API_KEY / PATEWAY_API_KEY          大模型密钥（留空则复用 ASR 的）
    V2N_ASR_MODEL / V2N_LLM_MODEL              模型名
    V2N_LLM_PROTOCOL                           anthropic（/v1/messages）或 openai
    V2N_DATA_DIR                               数据目录（默认 /mnt/workspace/video2note）
    SERVER_PORT / DEBUG_MODE / MAX_WORKERS     运行时配置

另一个版本 🖥️【本地版】是 run.py（FastAPI + static/ 自绘界面，默认 8765 端口）。
两者共用 core/ 业务内核与 data/ 配置，可同时运行。
"""
from __future__ import annotations

# ========== 1. 标准库 ==========
import inspect
import os
import shutil
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Generator

# ========== 2. 三方依赖 ==========
try:
    import gradio as gr
except ImportError:  # pragma: no cover
    raise ImportError("请先安装依赖：pip install -r requirements.txt")

# ========== 3. 路径与内核导入 ==========
ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import audio, downloader, pipeline, store  # noqa: E402
from core.config import (  # noqa: E402
    ASR_MODELS,
    DATA_DIR,
    HELP,
    MEDIA_DIR,
    SECRET_KEYS,
    ensure_cookie_file,
    ensure_dirs,
    load_settings,
    normalize_browser,
    public_settings,
    save_settings,
)

ensure_dirs()

# ========== 4. 配置常量（密钥一律来自环境变量 / 页面填写，禁止硬编码）==========
APP_TITLE = "视频转笔记 Video2Note"
APP_DESCRIPTION = (
    "粘贴抖音 / B站链接或直接上传音视频，自动完成 **下载 → 音频切片 → 语音转写 → AI 整理**，"
    "输出带时间轴的原始稿与可直接使用的 Markdown 笔记。"
)
APP_VERSION = "2.0.0"

SERVER_HOST = os.environ.get("SERVER_HOST", "0.0.0.0")
SERVER_PORT = int(os.environ.get("SERVER_PORT", "7860"))
DEBUG_MODE = os.environ.get("DEBUG_MODE", "false").lower() == "true"
MAX_WORKERS = int(os.environ.get("MAX_WORKERS", "2"))
TEMP_DIR = Path(os.environ.get("GRADIO_TEMP_DIR", str(DATA_DIR / "tmp")))
TEMP_DIR.mkdir(parents=True, exist_ok=True)

STAGE_TEXT = {
    "queued": "等待开始",
    "downloading": "下载视频",
    "extracting": "提取音频",
    "slicing": "语音切片",
    "transcribing": "语音转写",
    "transcribed": "等待 AI 整理",
    "polishing": "AI 整理",
    "done": "完成",
}
STATUS_TEXT = {
    "pending": "排队中",
    "running": "处理中",
    "transcribed": "转写完成",
    "done": "已完成",
    "failed": "失败",
    "canceled": "已取消",
}
LLM_PROTOCOLS = [
    ("Anthropic（/v1/messages + x-api-key）", "anthropic"),
    ("OpenAI（/v1/chat/completions）", "openai"),
]
STYLES = [
    ("整理文稿（忠实原文·推荐）", "general"),
    ("结构化笔记", "note"),
    ("公众号文章", "article"),
    ("仅清洗润色", "clean"),
]
AUDIO_VIDEO_EXT = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma",
                   ".mp4", ".mov", ".mkv", ".webm", ".avi", ".flv", ".m4v"}


# ========== 5. gradio 版本自适应（4.x / 5.x / 6.x 参数差异）==========
def supported_kwargs(func: Callable, **kwargs: Any) -> dict:
    """按签名过滤参数：老版本没有的参数自动丢弃，保证单份代码跨版本可用"""
    try:
        params = inspect.signature(func).parameters
    except (TypeError, ValueError):
        return kwargs
    return {k: v for k, v in kwargs.items() if k in params or "kwargs" in params}


def comp(component: Callable, *args: Any, **kwargs: Any) -> Any:
    """构造 Gradio 组件，自动剔除当前版本不支持的参数"""
    return component(*args, **supported_kwargs(component, **kwargs))


# ========== 6. 环境自检（启动一次，缺失项仅提示，不阻断启动）==========
def check_environment() -> list[str]:
    """返回环境自检结果（缺失项以 ⚠️ 标记）"""
    lines: list[str] = []
    try:
        audio.check_ffmpeg()
        lines.append("✅ ffmpeg 就绪")
    except Exception:
        lines.append("⚠️ 缺少 ffmpeg（pip install imageio-ffmpeg）")
    try:
        import yt_dlp  # noqa: F401
        lines.append("✅ yt-dlp 就绪")
    except ImportError:
        lines.append("⚠️ 缺少 yt-dlp（pip install -U yt-dlp）")

    s = load_settings()
    lines.append("✅ 已配置 ASR 密钥" if s.get("asr_api_key")
                 else "⚠️ 未配置 ASR 密钥（页面「设置」填写或设置环境变量 V2N_ASR_API_KEY）")
    lines.append("✅ 已配置 LLM 密钥" if (s.get("llm_api_key") or s.get("asr_api_key"))
                 else "⚠️ 未配置 LLM 密钥")
    lines.append(f"📁 数据目录 `{DATA_DIR}`")
    return lines


# ========== 7. 工具函数 ==========
def file_to_path(upload: Any) -> str:
    """gr.File 在不同版本可能返回 str / FileData(dict) / 临时文件对象，统一取路径"""
    if upload is None:
        return ""
    if isinstance(upload, str):
        return upload
    if isinstance(upload, dict):  # Gradio FileData：{"path": ..., "orig_name": ...}
        return str(upload.get("path") or upload.get("name") or "")
    return str(getattr(upload, "name", upload) or "")


def fmt_time(sec: float) -> str:
    sec = max(0, int(sec or 0))
    m, s = divmod(sec, 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def transcript_timeline(segments: list[dict], limit: int = 6000) -> str:
    """转写稿渲染成可复制的「[mm:ss] 文本」时间轴"""
    if not segments:
        return ""
    rows = [f"[{fmt_time(s.get('start', 0))}] {s.get('text', '')}" for s in segments]
    text = "\n".join(rows)
    if len(text) > limit:
        text = text[:limit] + "\n\n…（内容较长仅预览，导出可获得完整内容）"
    return text


def status_markdown(t: store.Task | None) -> str:
    """任务状态卡片"""
    if t is None:
        return "尚未开始任务。"
    meta = t.meta or {}
    bits = [
        f"### {meta.get('title') or t.url or '未命名'}",
        "**状态**：" + STATUS_TEXT.get(t.status, t.status)
        + (f" · {STAGE_TEXT.get(t.stage, t.stage)}" if t.status == "running" else ""),
    ]
    if meta.get("uploader"):
        bits.append(f"**来源**：{meta['uploader']}")
    if meta.get("duration"):
        bits.append(f"**时长**：{fmt_time(meta['duration'])}")
    if t.transcript:
        bits.append(f"**转写**：{len(t.segments)} 句 / {len(t.transcript)} 字")
    if t.error:
        bits.append(f"\n> ❌ {t.error}")
    elif t.message:
        bits.append(f"\n> {t.message}")
    recent = (t.events or [])[-30:]
    bits.append(f"\n<details><summary>运行日志（{len(t.events or [])} 条）</summary>\n")
    bits.append("\n".join(f"- `{e['text']}`" for e in recent) or "- （无）")
    bits.append("\n</details>")
    return "\n".join(bits)


def video_value(t: store.Task | None) -> str | None:
    p = (t.meta or {}).get("video_path") if t else ""
    return str(p) if p and Path(p).is_file() else None


def history_rows() -> list[list[Any]]:
    """历史记录表格数据"""
    rows = []
    for t in store.list_tasks():
        rows.append([
            t.id,
            (t.meta or {}).get("title") or t.url or "未命名",
            (t.meta or {}).get("uploader") or t.platform or "-",
            STATUS_TEXT.get(t.status, t.status),
            len(t.transcript or ""),
            datetime.fromtimestamp(t.created_at).strftime("%m-%d %H:%M"),
        ])
    return rows


def note_view(t: store.Task | None) -> str:
    """文稿区展示内容：顶部标注当前风格/字数/时间，方便确认看到的是哪一版"""
    if not t or not t.note:
        return "*转写完成后，在上方选一种风格点「✨ 生成文稿」*"
    info = t.note_info or {}
    done = sorted(k for k, v in (t.variants or {}).items() if (v or {}).get("note"))
    head = [f"`{info.get('style', '-')}` · {info.get('chars', len(t.note))} 字 · {info.get('at', '')}"]
    if len(done) > 1:
        head.append("　已生成：" + "、".join(done))
    return "<div class='note-meta-line'>" + "　".join(head) + "</div>\n\n" + t.note


def bundle(t: store.Task | None, tid: str = "") -> tuple:
    """统一打包 UI 输出，顺序与事件绑定的 outputs 一致"""
    return (
        (t.id if t else tid) or "",
        video_value(t),
        status_markdown(t),
        transcript_timeline(t.segments) if t else "",
        note_view(t),
        history_rows(),
    )


# ========== 8. 业务函数 ==========
def create_task(url: str, upload: Any, style: str) -> store.Task:
    """校验输入并创建任务；可预期的错误统一转成gr.Error 提示"""
    ensure_dirs()
    options = {"style": style}

    src = file_to_path(upload)
    if src:
        src_path = Path(src)
        if src_path.suffix.lower() not in AUDIO_VIDEO_EXT:
            raise gr.Error(f"不支持的文件类型：{src_path.suffix or '(无扩展名)'}，请上传音视频文件")
        t = store.create(src_path.name, "local", options)
        dest = MEDIA_DIR / t.id / f"source{src_path.suffix.lower()}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_path, dest)
        t.options["source_file"] = str(dest)
        t.log("info", f"已接收文件：{src_path.name}（{dest.stat().st_size / 1048576:.1f} MB）")
    else:
        clean_url = downloader.extract_url(url or "")
        if not clean_url.startswith("http"):
            raise gr.Error("未识别到有效链接，请粘贴包含 http(s):// 的视频链接")
        t = store.create(clean_url, downloader.detect_platform(clean_url), options)
        t.log("info", f"任务创建：{clean_url}")

    store.save(t)
    return t


def run_streamed(t: store.Task,
                 worker: Callable[[Callable[[str, float, str], None]], None],
                 progress: gr.Progress) -> Generator[tuple, None, None]:
    """后台线程跑流水线，主线程按 emit 事件流式回推 UI，避免长时间无响应"""
    events: list[tuple[str, float, str]] = []
    lock = threading.Lock()

    def emit(stage: str, pct: float, msg: str) -> None:
        with lock:
            events.append((stage, pct, msg))

    progress(0.02, desc="任务启动")
    yield bundle(t)

    # 主线程用短 join 轮询，保证 Ctrl+C/超时后线程可随进程退出
    thread = threading.Thread(target=worker, args=(emit,), daemon=True)
    thread.start()

    cursor = 0
    while thread.is_alive():
        thread.join(0.4)
        with lock:
            batch, cursor = events[cursor:], len(events)
        if batch:
            stage, _, msg = batch[-1]
            progress(max(0.02, min(t.percent / 100.0, 0.99)),
                     desc=f"{STAGE_TEXT.get(stage, stage)} · {msg}")
            yield bundle(t)

    thread.join()
    if t.error:
        raise gr.Error(t.error)
    progress(1.0, desc="完成")
    yield bundle(t)


def start_transcribe(url: str, upload: Any, style: str,
                     progress: gr.Progress = gr.Progress()) -> Generator[tuple, None, None]:
    """入口一：链接或上传文件 → 语音转写（不含AI 整理）"""
    try:
        t = create_task(url, upload, style)
    except gr.Error:
        raise
    except Exception as e:
        if DEBUG_MODE:
            traceback.print_exc()
        raise gr.Error(f"创建任务失败：{e}")

    yield from run_streamed(t, lambda emit: pipeline.transcribe_sync(t, emit=emit), progress)


def polish_text(tid: str, style: str,
                progress: gr.Progress = gr.Progress()) -> Generator[tuple, None, None]:
    """入口二：对已转写任务按指定风格生成文稿（去口语化 / 纠错 / 排版）

    每种风格的成稿单独缓存，来回切换风格直接复用，不会重复调用模型。
    """
    t = store.get((tid or "").strip())
    if not t:
        raise gr.Error("任务不存在或已被清理")
    if not t.transcript:
        raise gr.Error("该任务还没有转写文本，请先完成转写")
    if t.status == "running":
        raise gr.Error("任务正在处理中，请稍后再试")

    need, reason = pipeline.polish_needed(t, style)
    if not need:
        # 命中该风格缓存：把稿切回展示位，立即回显
        pipeline._apply_variant(t, style)
        pipeline._sync_variant_meta(t)
        pipeline._mark_idempotent_hit(t, reason)
        yield bundle(t)
        return

    yield from run_streamed(
        t, lambda emit: pipeline.polish_sync(t, style, emit=emit), progress)
    if t.error:
        raise gr.Error(t.error)


def load_task(tid: str) -> tuple:
    """历史记录回读：载入后完整回显视频、转写与当前风格文稿"""
    t = store.get((tid or "").strip())
    if not t:
        raise gr.Error("任务不存在或已被清理")
    return bundle(t)


def delete_task(tid: str) -> tuple:
    """删除任务及其媒体文件，释放磁盘"""
    if tid:
        store.remove(tid)
        shutil.rmtree(MEDIA_DIR / tid, ignore_errors=True)
    return bundle(None, tid="")


def clean_media(days: float) -> str:
    """清理过期媒体文件，避免创空间磁盘被占满"""
    days = float(days or 0)
    if days < 0:
        raise gr.Error("保留天数不能为负")
    cutoff = time.time() - days * 86400
    freed, removed = 0, 0
    known = {t.id for t in store.list_tasks()}
    if MEDIA_DIR.exists():
        for d in MEDIA_DIR.iterdir():
            if not d.is_dir() or d.stat().st_mtime > cutoff:
                continue
            size = sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
            shutil.rmtree(d, ignore_errors=True)
            freed += size
            removed += 1
            if d.name in known:
                store.remove(d.name)
    return f"🧹 已清理 {removed} 个任务的媒体文件，释放 {freed / 1048576:.1f} MB（保留最近 {days:g} 天）"


# ========== 9. 设置管理 ==========
def save_ui_settings(asr_key: str, llm_key: str, asr_base: str, llm_base: str,
                     protocol: str, max_tokens: str,
                     asr_model: str, llm_model: str, chunk_seconds: float,
                     concurrency: float, cookie: str, cookie_text: str,
                     cookie_text_bili: str) -> str:
    """保存页面配置到持久化 settings.json；密钥留空表示不修改"""
    patch: dict[str, str] = {
        "asr_base_url": (asr_base or "").strip(),
        "llm_base_url": (llm_base or "").strip(),
        "llm_protocol": (protocol or "").strip(),
        "llm_max_tokens": str(int(float(max_tokens or 8192))),
        "asr_model": (asr_model or "").strip(),
        "llm_model": (llm_model or "").strip(),
        "chunk_seconds": str(int(chunk_seconds or 600)),
        "max_concurrency": str(int(concurrency or 4)),
        "cookie_browser": (cookie or "").strip(),
        "cookie_text": (cookie_text or "").strip(),
        "cookie_text_bili": (cookie_text_bili or "").strip(),
    }
    if (asr_key or "").strip():
        patch["asr_api_key"] = asr_key.strip()
    if (llm_key or "").strip():
        patch["llm_api_key"] = llm_key.strip()

    # 「从浏览器读取」只填浏览器名；误粘贴 Cookie 文本时拦下并提示，不写坏配置
    if patch["cookie_browser"]:
        patch["cookie_browser"], err = normalize_browser(patch["cookie_browser"])
        if err:
            return "❌ " + err

    save_settings(patch)

    # 两个平台各自落盘；即使被清空也要调用，内部会删掉残留文件，
    # 否则页面回显为空、下载却仍在用旧 Cookie。
    notes = []
    for label, plat in (("抖音", "douyin"), ("B站", "bilibili")):
        p = ensure_cookie_file(plat)
        notes.append(f"{label} Cookie 已写入 `{p}`" if p else f"{label} Cookie 已清空")

    masked = public_settings()
    masked.setdefault("asr_api_key", "")
    return ("✅ 配置已保存（ASR 密钥：" + (masked.get("asr_api_key") or "未设置")
            + " / LLM 密钥：" + (masked.get("llm_api_key") or "复用 ASR 密钥")
            + "；" + "；".join(notes)
            + f"），数据目录 `{DATA_DIR}`")


def settings_summary() -> str:
    """当前配置摘要（密钥自动脱敏）"""
    return "\n".join("* " + line for line in check_environment())


# ========== 10. Gradio 界面 ==========
CUSTOM_CSS = """
/* 与 WorkBuddy 客户端 / FastAPI 版保持同一套字体栈 */
:root {
  --wb-font: "PingFang SC", -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  --wb-font-mono: "Source Code Pro", ui-monospace, Consolas, Monaco, monospace;
}
.gradio-container, .gradio-container * { font-family: var(--wb-font); }
.gradio-container pre, .gradio-container code { font-family: var(--wb-font-mono); }

.stage-hint { color: #6b7280; font-size: 12px; }
/* 同一行对照：视频与文字稿高度接近，滚动文字稿时视频保持可见 */
.v2n-video video { max-height: 330px; object-fit: contain; background: #0e1116; }
.v2n-transcript textarea { font-size: 13px; line-height: 1.75; }
/* 风格选择：按钮式单选，比下拉框更快看清当前选中项 */
.v2n-style-pick .wrap { gap: 8px; }
.v2n-style-pick label { border: 1px solid var(--border, #d0d5dd); border-radius: 18px;
    padding: 6px 14px; margin: 0; cursor: pointer; font-size: 13px; }
.v2n-style-pick label.selected { background: var(--primary, #7c3aed); color: #fff;
    border-color: var(--primary, #7c3aed); font-weight: 600; }
/* 文稿顶部元信息：显示当前风格 / 字数 / 生成时间 */
.note-meta-line { color: #6b7280; font-size: 12px; border-bottom: 1px solid #e4e7ec;
    padding-bottom: 8px; margin-bottom: 12px; }
@media (max-width: 1280px) {
  .v2n-video video { max-height: 260px; }
}
"""

_default = load_settings()
# 由环境变量托管的配置项（部署平台注入密钥时，页面只读，避免误改与回显）
_from_env = (public_settings() or {}).get("_from_env") or {}

with comp(gr.Blocks, title=APP_TITLE, theme=gr.themes.Soft(), css=CUSTOM_CSS,
          analytics_enabled=False) as demo:
    comp(gr.Markdown, value=f"# 🎬 {APP_TITLE}\n{APP_DESCRIPTION}")
    comp(gr.Markdown, value=f"`v{APP_VERSION}` · " + " · ".join(check_environment()))

    task_id = gr.State("")

    with gr.Tabs():
        # ---------------- 工作台 ----------------
        with gr.TabItem("🎧 转写工作台"):
            # 同一行三栏：输入区 | 视频预览 | 转写文字稿 —— 视频与文字稿左右相邻，边播边对稿
            with gr.Row():
                # 左：输入区
                with gr.Column(scale=4, min_width=280):
                    comp(gr.Markdown, value="### ① 输入来源")
                    with gr.Tabs():
                        with gr.TabItem("🔗 粘贴链接"):
                            url_box = comp(
                                gr.Textbox,
                                label="视频链接",
                                placeholder="支持抖音 / B站分享文案整段粘贴，例如：\n"
                                            "【标题】 https://www.bilibili.com/video/BV1xx411c7mD",
                                lines=4,
                            )
                        with gr.TabItem("📁 上传文件"):
                            upload_box = comp(gr.File, label="上传音视频文件（mp4 / mov / mp3 / wav …）")

                    style_dd = comp(gr.Radio, choices=STYLES, value="general",
                                    label="文稿风格", elem_classes=["v2n-style-pick"])
                    start_btn = comp(gr.Button, value="🚀 开始转写", variant="primary")
                    comp(gr.Markdown,
                         value="<div class='stage-hint'>流程：下载视频 → 音频切片 → 语音转写；"
                               "完成后再选风格点下方「生成文稿」。</div>")

                # 中：视频预览（与右侧文字稿同一行）
                with gr.Column(scale=5, min_width=320):
                    comp(gr.Markdown, value="### 🎬 视频预览")
                    video_out = comp(gr.Video, label="播放窗口",
                                     elem_classes=["v2n-video"], height=330)
                    status_md = comp(gr.Markdown, value="尚未开始任务。")

                # 右：转写文字稿（与左侧视频同一行，便于逐句对照）
                with gr.Column(scale=5, min_width=320):
                    comp(gr.Markdown, value="### 📝 转写文字稿")
                    transcript_tb = comp(gr.Textbox, label="[mm:ss] 时间轴原文（可整体复制）",
                                         elem_classes=["v2n-transcript"],
                                         lines=18, max_lines=28, show_copy_button=True)
                    comp(gr.Markdown,
                         value="<div class='stage-hint'>格式：<code>[分:秒] 文本</code>，"
                               "可与左侧视频播放进度对照；导出 SRT 可用于剪辑对轨。</div>")

            # 下一行：AI 文稿（独占整行，宽度更大便于阅读）
            with gr.Column():
                comp(gr.Markdown, value="### ✨ AI 文稿")
                with gr.Row():
                    style_dd2 = comp(gr.Radio, choices=STYLES, value="general",
                                     label="选择风格重新生成", scale=3,
                                     elem_classes=["v2n-style-pick"])
                    polish_btn = comp(gr.Button, value="✨ 生成文稿",
                                      variant="primary", scale=2)
                comp(gr.Markdown,
                     value="<div class='stage-hint'>同一种风格生成过一次后，再次点「生成文稿」"
                           "会直接复用已生成的稿，不会重复消耗额度；换风格则重新生成一份，"
                           "各风格互不覆盖。</div>")
                note_md = comp(gr.Markdown, value="*转写完成后在上方选一种风格，点「✨ 生成文稿」*")

        # ---------------- 历史记录 ----------------
        with gr.TabItem("🗂 历史记录"):
            history_df = comp(gr.Dataframe,
                              headers=["任务ID", "标题", "来源", "状态", "字数", "时间"],
                              value=history_rows(), wrap=True)
            with gr.Row():
                refresh_btn = comp(gr.Button, value="🔄 刷新列表")
                selected_tb = comp(gr.Textbox, label="任务ID（点击表格行回填）", value="")
                load_btn = comp(gr.Button, value="📂 载入该任务", variant="primary")
                del_btn = comp(gr.Button, value="🗑 删除该任务")
            comp(gr.Markdown,
                 value="<div class='stage-hint'>点击表格行选中，再点「📂 载入该任务」"
                       "即可在「转写工作台」完整回显视频、转写文字稿与当前文稿，"
                       "并可对该任务按任意风格重新生成。</div>")

        # ---------------- 设置 ----------------
        with gr.TabItem("⚙️ 设置"):
            comp(gr.Markdown, value="### 环境自检")
            env_md = comp(gr.Markdown, value=settings_summary())
            comp(gr.Markdown, value="### 密钥配置")
            with gr.Row():
                asr_key_tb = comp(gr.Textbox, label=HELP["asr_api_key"], type="password",
                                  interactive=not _from_env.get("asr_api_key"),
                                  placeholder=("已由环境变量 %s 托管，页面不可修改"
                                               % _from_env["asr_api_key"])
                                  if _from_env.get("asr_api_key") else "留空表示不修改")
                llm_key_tb = comp(gr.Textbox, label=HELP["llm_api_key"], type="password",
                                  interactive=not _from_env.get("llm_api_key"),
                                  placeholder=("已由环境变量 %s 托管，页面不可修改"
                                               % _from_env["llm_api_key"])
                                  if _from_env.get("llm_api_key") else "留空则复用 ASR 密钥")
            with gr.Row():
                asr_base_tb = comp(gr.Textbox, label=HELP["asr_base_url"], value=_default["asr_base_url"])
                llm_base_tb = comp(gr.Textbox, label=HELP["llm_base_url"], value=_default["llm_base_url"])
            with gr.Row():
                proto_dd = comp(gr.Dropdown, choices=LLM_PROTOCOLS,
                                value=_default.get("llm_protocol", "anthropic"),
                                label=HELP["llm_protocol"])
                maxtok_tb = comp(gr.Textbox, label=HELP["llm_max_tokens"],
                                 value=str(_default.get("llm_max_tokens", "8192")))
            with gr.Row():
                set_asr_model_tb = comp(gr.Dropdown, choices=ASR_MODELS,
                                        value=_default["asr_model"],
                                        allow_custom_value=True,
                                        visible=False)
                set_llm_model_tb = comp(gr.Textbox, label=HELP["llm_model"],
                                        value=_default["llm_model"], visible=False)
            with gr.Row():
                chunk_sl = comp(gr.Slider, 120, 1800, step=60,
                                value=int(_default.get("chunk_seconds") or 600),
                                visible=False, label=HELP["chunk_seconds"])
                conc_sl = comp(gr.Slider, 1, 8, step=1,
                               value=int(_default.get("max_concurrency") or 4),
                               visible=False, label=HELP["max_concurrency"])
            cookie_text_tb = comp(gr.Textbox, label=HELP["cookie_text"], lines=4,
                                  value=_default.get("cookie_text", ""),
                                  placeholder="粘贴抖音网页的 Cookie 整段（a=1; b=2）；"
                                              "也可直接粘贴 Netscape cookies.txt 全文")
            cookie_bili_tb = comp(gr.Textbox, label=HELP["cookie_text_bili"], lines=4,
                                  value=_default.get("cookie_text_bili", ""),
                                  placeholder="粘贴 B站网页的 Cookie 整段，必须含SESSDATA")
            cookie_tb = comp(gr.Textbox, label=HELP["cookie_browser"],
                             value=_default.get("cookie_browser", ""),
                             placeholder="chrome / edge / firefox（容器环境不可用，优先用上面的 Cookie 文本）")
            save_btn = comp(gr.Button, value="💾 保存配置", variant="primary")
            save_msg = comp(gr.Markdown, value="")

            comp(gr.Markdown, value="---")
            comp(gr.Markdown, value="### 磁盘维护")
            with gr.Row():
                days_sl = comp(gr.Slider, 0, 30, step=1, value=3, label="清理多少天前的媒体文件")
                clean_btn = comp(gr.Button, value="🧹 立即清理")
            clean_msg = comp(gr.Markdown, value="")

    comp(gr.Markdown, value="---")
    comp(gr.Markdown,
         value=f"**{APP_TITLE}** · v{APP_VERSION} · 仅供个人学习使用，请遵守各平台内容版权规定。")

    # ---------------- 事件绑定 ----------------
    OUTPUTS = [task_id, video_out, status_md, transcript_tb, note_md, history_df]

    start_btn.click(
        fn=start_transcribe,
        inputs=[url_box, upload_box, style_dd],
        outputs=OUTPUTS,
        concurrency_limit=MAX_WORKERS,
    )
    polish_btn.click(
        fn=polish_text,
        inputs=[task_id, style_dd2],
        outputs=OUTPUTS,
        concurrency_limit=MAX_WORKERS,
    )
    # 切换风格后把顶部的风格选择同步过去，避免两处风格不一致
    style_dd2.change(fn=lambda s: gr.update(value=s), inputs=[style_dd2], outputs=[style_dd])
    refresh_btn.click(fn=lambda: history_rows(), inputs=None, outputs=[history_df])

    def on_select(rows: Any, evt: gr.SelectData) -> str:
        """点击历史表格行 → 回填任务ID（Dataframe 值可能是 list 或 dict）"""
        data = rows.get("data", []) if isinstance(rows, dict) else (rows or [])
        idx = evt.index[0] if isinstance(evt.index, (list, tuple)) else evt.index
        try:
            return str(data[int(idx)][0])
        except Exception:
            return ""

    history_df.select(fn=on_select, inputs=[history_df], outputs=[selected_tb])
    load_btn.click(fn=load_task, inputs=[selected_tb], outputs=OUTPUTS)
    del_btn.click(fn=delete_task, inputs=[selected_tb], outputs=OUTPUTS)

    save_btn.click(
        fn=save_ui_settings,
        inputs=[asr_key_tb, llm_key_tb, asr_base_tb, llm_base_tb,
                proto_dd, maxtok_tb,
                set_asr_model_tb, set_llm_model_tb, chunk_sl, conc_sl,
                cookie_tb, cookie_text_tb, cookie_bili_tb],
        outputs=[save_msg],
    )
    clean_btn.click(fn=clean_media, inputs=[days_sl], outputs=[clean_msg])


# ========== 11. 启动 ==========
def launch_kwargs() -> dict:
    """按当前 gradio 版本过滤 launch 参数（theme / css 在 6.x 起由 launch 接收）"""
    return supported_kwargs(
        demo.launch,
        server_name=SERVER_HOST,
        server_port=SERVER_PORT,
        share=False,
        debug=DEBUG_MODE,
        show_error=True,
        max_threads=40,
        allowed_paths=[str(MEDIA_DIR), str(TEMP_DIR)],
        theme=gr.themes.Soft(),
        css=CUSTOM_CSS,
        quiet=not DEBUG_MODE,
    )


def main() -> None:
    print("=" * 62)
    print(f"  {APP_TITLE}  v{APP_VERSION}")
    print("=" * 62)
    for line in check_environment():
        print("  " + line)
    print(f"  服务地址 http://{SERVER_HOST}:{SERVER_PORT}")
    print("=" * 62)
    # 队列保证长耗时任务不阻塞其它请求
    demo.queue()
    demo.launch(**launch_kwargs())


if __name__ == "__main__":
    main()
