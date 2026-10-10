"""FastAPI 应用入口"""
from __future__ import annotations

import asyncio
import json
import re
import shutil
import uuid
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import audio, downloader, exporters, store
from .downloader import detect_platform, extract_url
from .config import (HELP, MEDIA_DIR, ROOT, SECRET_KEYS, SETTINGS_GROUPS,
                     ensure_cookie_file, ensure_dirs, load_settings,
                     migrate_secrets, normalize_browser, public_settings,
                     save_settings)
from .pipeline import (_apply_variant, _mark_idempotent_hit, _sync_variant_meta,
                       polish_needed, retry as pipeline_retry,
                       submit, submit_polish)

ensure_dirs()
# 旧版本的密钥躺在 settings.json 里（会被接口读回浏览器），搬到独立文件。
# 放在 ensure_dirs 之后：迁移要写文件，数据目录必须已存在。
migrate_secrets()

app = FastAPI(title="视频转笔记", version="1.1.0")
STATIC = ROOT / "static"

STYLES = ("general", "note", "article", "clean")

# 按扩展名给媒体流定Content-Type。
# 不用固定白名单的原因：上传的本地文件落盘名是 source<原后缀>，
# 可能是 source.mp4 / source.mkv / source.mov 等任意视频格式；
# 固定只认 video.mp4 会让上传的视频预览不出来。
MEDIA_TYPES = {
    ".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/quicktime",
    ".webm": "video/webm", ".mkv": "video/x-matroska", ".avi": "video/x-msvideo",
    ".flv": "video/x-flv", ".ts": "video/mp2t", ".3gp": "video/3gpp",
    ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".aac": "audio/aac",
    ".wav": "audio/wav", ".flac": "audio/flac", ".ogg": "audio/ogg",
    ".opus": "audio/opus", ".wma": "audio/x-ms-wma",
}


def _media_type(name: str) -> str:
    """媒体文件 -> Content-Type；不在白名单内返回空字符串"""
    return MEDIA_TYPES.get(Path(name).suffix.lower(), "")


class CreateIn(BaseModel):
    url: str
    style: str = "general"
    language: str = "auto"
    model: str = ""


class PolishIn(BaseModel):
    style: str = "general"


class SettingsIn(BaseModel):
    values: dict
    # 显式要求清空的敏感项（Token / Cookie）。
    # 敏感项不再回传，前端无法区分「没改」和「想清空」，所以必须单独传。
    clear: list[str] = []


def _clean_style(s: str | None) -> str:
    return s if s in STYLES else "general"


def _task_options(style: str, language: str, model: str) -> dict:
    return {
        "style": _clean_style(style),
        "language": (language or "auto").strip() or "auto",
        "asr_model": (model or "").strip(),
    }


@app.get("/api/tasks/{tid}/export")
def export_task(tid: str, fmt: str = "md"):
    t = store.get(tid)
    if not t:
        raise HTTPException(404, "任务不存在")
    if fmt not in exporters.FORMATS:
        raise HTTPException(400, "不支持的格式")
    content, name, mime = exporters.render(t, fmt)
    tmp = Path(store.TASK_DIR) / f"{tid}.{uuid.uuid4().hex[:6]}.export"
    tmp.write_text(content, encoding="utf-8")
    return FileResponse(str(tmp), filename=name, media_type=mime)


@app.get("/", response_class=HTMLResponse)
def index():
    """首页。

    必须显式 no-store：浏览器对没有缓存头的 HTML 会走启发式缓存，
    用 Last-Modified 猜测有效期。结果就是「代码明明更新了、页面还是旧的」，
    云机器上尤其明显（改完 git pull + 重启，但浏览器仍渲染旧 HTML，
    于是新的设置面板分组、新的字数显示全都看不到）。
    """
    return Response(
        (STATIC / "index.html").read_text("utf-8"),
        media_type="text/html; charset=utf-8",
        headers={"Cache-Control": "no-store, must-revalidate",
                 "Pragma": "no-cache", "Expires": "0"},
    )


@app.get("/api/settings")
def get_settings():
    """设置面板数据。

    groups 决定渲染顺序与分组标题——前端按它排，而不是按 HELP 的键序。
    resolver_curl 这类「不配也能跑」的可选项如果只按字母/插入序排到末尾，
    等于事实上看不见，所以顺序必须显式指定。
    """
    return {
        "values": public_settings(),
        "help": HELP,
        "groups": [{"title": t, "keys": ks} for t, ks in SETTINGS_GROUPS],
    }


@app.post("/api/settings")
def post_settings(body: SettingsIn):
    # 「从浏览器读取」只接受浏览器名，误粘贴 Cookie 文本时直接报错而不是静默保存
    if "cookie_browser" in body.values:
        name, err = normalize_browser(body.values.get("cookie_browser") or "")
        if err:
            raise HTTPException(status_code=400, detail=err)
        body.values["cookie_browser"] = name
    # curl 模板先试解析再存：粘错（少了引号、不是 curl 格式）要立刻告知，
    # 否则错误会延后到下载时才暴露成难懂的解析失败
    curl_summary = None
    raw_curl = (body.values.get("resolver_curl") or "").strip()
    if raw_curl:
        from .curlparse import parse_curl, summarize

        try:
            curl_summary = summarize(parse_curl(raw_curl))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"curl 模板无法识别：{e}")
    save_settings(body.values, clear_secrets=set(body.clear))
    # Cookie 文本有变化时立刻落盘，供后续下载使用。
    # 即使被清空也要调一次——ensure_cookie_file 内部会删掉残留文件，
    # 否则页面显示「未配置」、下载却仍在用旧 Cookie。
    files: dict[str, str] = {}
    if "cookie_text" in body.values or "cookie_text" in body.clear:
        p = ensure_cookie_file("douyin")
        if p:
            files["douyin"] = p
    if "cookie_text_bili" in body.values or "cookie_text_bili" in body.clear:
        p = ensure_cookie_file("bilibili")
        if p:
            files["bilibili"] = p
    return {"ok": True, "values": public_settings(), "help": HELP,
            "groups": [{"title": t, "keys": ks} for t, ks in SETTINGS_GROUPS],
            "cookie_files": files, "curl": curl_summary}


@app.post("/api/settings/check-curl")
def check_curl(body: SettingsIn):
    """只校验不保存。用于设置页的「校验这段 curl」按钮。"""
    raw = (body.values.get("resolver_curl") or "").strip()
    if not raw:
        return {"ok": True, "summary": None, "message": "未填写，将使用内置模板"}
    from .curlparse import parse_curl, summarize

    try:
        s = summarize(parse_curl(raw))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"curl 模板无法识别：{e}")
    bits = [f"地址 {s['url']}"]
    if s["fields"]:
        bits.append("字段 " + "、".join(s["fields"]))
    if s["dynamic"]:
        bits.append("将自动替换 " + "、".join(s["dynamic"]) + "（取页面实时值）")
    if s["cookie_present"]:
        bits.append(f"Cookie {s['cookie_count']} 条已识别，将注入浏览器")
    return {"ok": True, "summary": s, "message": "；".join(bits)}


@app.get("/api/health")
def health():
    try:
        audio.check_ffmpeg()
        ff, ffmsg = True, ""
    except Exception as e:
        ff, ffmsg = False, str(e)
    try:
        import yt_dlp  # noqa
        yt = True
    except ImportError:
        yt = False
    # 抖音首选通道（dlpanda）。要真启动一次 Chromium 才知道内核在不在：
    # 「装了库没装内核」时 import 是成功的，光看 available() 会一路显示
    # 正常，直到用户真跑任务才炸出 Executable doesn't exist。
    pw_msg = ""
    try:
        from .dlpanda import browser_ready

        pw, pw_msg = browser_ready()
    except Exception as e:
        pw, pw_msg = False, f"检测失败：{e}"
    try:
        from .dlpanda import available as _dlp_ok

        dy = bool(_dlp_ok()) and pw
    except Exception:
        dy = False
    return {"ffmpeg": ff, "ffmpeg_msg": ffmsg, "yt_dlp": yt,
            "douyin_api": dy, "playwright": pw,
            "playwright_msg": pw_msg, **_build_info()}


def _git_rev() -> str:
    try:
        import subprocess
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(ROOT), capture_output=True, text=True, timeout=3)
        return (out.stdout or "").strip()
    except Exception:
        return ""


def _build_info() -> dict:
    """版本与能力自述。

    「云机器上改了代码但页面没变」这类问题，光靠猜是猜不出来的——
    到底是没 git pull、pull 了没重启、还是浏览器缓存，得让人一眼看到。
    页面右下角会显示这个 commit，点「设置」也能看到接口返回了哪些字段。
    """
    static = {}
    try:
        html = (STATIC / "index.html").read_text("utf-8")
        m = re.search(r"v=(\d+[a-z]*)", html)
        static["assets"] = m.group(1) if m else ""
    except Exception:
        static["assets"] = ""
    return {
        "version": {
            "commit": _git_rev(),
            "assets": static["assets"],
            # 关键能力位：前端据此判断后端是不是新版本
            "has_groups": bool(SETTINGS_GROUPS),
            "settings_keys": len(HELP),
            "has_resolver_curl": "resolver_curl" in HELP,
        }
    }


@app.get("/api/tasks")
def list_tasks():
    return [t.to_dict(with_events=False) for t in store.list_tasks()]


@app.post("/api/tasks")
def create_task(body: CreateIn):
    # 分享文案常带标题/话题，先抠出真正的链接
    url = extract_url(body.url)
    if not url or not url.startswith("http"):
        raise HTTPException(400, "未识别到有效链接，请粘贴含 http(s):// 的视频链接")
    platform = detect_platform(url)
    t = store.create(url, platform, _task_options(body.style, body.language, body.model))
    t.log("info", f"任务创建：{url}")
    store.save(t)
    submit(t)
    return {"id": t.id}


@app.post("/api/tasks/upload")
async def upload_task(
    file: UploadFile = File(...),
    language: str = Form("auto"),
    model: str = Form(""),
    style: str = Form("general"),
):
    name = (file.filename or "").strip()
    if not name:
        raise HTTPException(400, "请选择要上传的文件")
    t = store.create(name, "local", _task_options(style, language, model))
    suffix = Path(name).suffix.lower() or ".bin"
    dest = MEDIA_DIR / t.id / f"source{suffix}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    size = 0
    with dest.open("wb") as out:
        while chunk := await file.read(1024 * 1024):
            out.write(chunk)
            size += len(chunk)
    if size == 0:
        store.remove(t.id)
        shutil.rmtree(MEDIA_DIR / t.id, ignore_errors=True)
        raise HTTPException(400, "上传的文件是空的")
    t.options["source_file"] = str(dest)
    t.log("info", f"已接收文件：{name}（{size / 1048576:.1f} MB）")
    store.save(t)
    submit(t)
    return {"id": t.id}


@app.post("/api/tasks/upload-raw")
async def upload_task_raw(
    request: Request,
    style: str = "general",
    language: str = "auto",
    model: str = "",
    name: str = "upload.bin",
):
    """原始字节流直传（不走 multipart）。

    Starlette 的 multipart 解析是纯 Python，实测 158MB 要几分钟；
    裸流直传就是内核拷贝速度。前端已切到这个端点，
    旧的 multipart 端点保留兼容，两个入口最后落同一个处理逻辑。
    """
    # 文件名只取 basename，剥掉任何路径成分，防止怪名字写出目录
    name = Path(name.replace("\\", "/")).name.strip() or "upload.bin"
    t = store.create(name, "local", _task_options(style, language, model))
    suffix = Path(name).suffix.lower() or ".bin"
    dest = MEDIA_DIR / t.id / f"source{suffix}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    size = 0
    try:
        with dest.open("wb") as out:
            async for chunk in request.stream():
                out.write(chunk)
                size += len(chunk)
    except BaseException:
        store.remove(t.id)
        shutil.rmtree(MEDIA_DIR / t.id, ignore_errors=True)
        raise
    if size == 0:
        store.remove(t.id)
        shutil.rmtree(MEDIA_DIR / t.id, ignore_errors=True)
        raise HTTPException(400, "上传的文件是空的")
    t.options["source_file"] = str(dest)
    t.log("info", f"已接收文件：{name}（{size / 1048576:.1f} MB）")
    store.save(t)
    submit(t)
    return {"id": t.id}


class PolishIn(BaseModel):
    style: str = "general"
    force: bool = False      # 忽略幂等缓存，强制重新整理


@app.post("/api/tasks/{tid}/polish")
def polish_task(tid: str, body: PolishIn):
    t = store.get(tid)
    if not t:
        raise HTTPException(404, "任务不存在")
    if not t.transcript:
        raise HTTPException(400, "该任务还没有转写文本")
    style = _clean_style(body.style)
    # 幂等：输入未变化且已有成稿时直接复用，不重复调用 LLM
    need, reason = polish_needed(t, style, body.force)
    if not need:
        # 命中缓存：把该风格已生成的稿切回展示位，用户立刻能看到内容
        _apply_variant(t, style)
        _sync_variant_meta(t)
        _mark_idempotent_hit(t, reason)
        return {"ok": True, "idempotent": True, "message": reason,
                "style": style, "note_info": t.note_info}
    if t.status == "running":
        raise HTTPException(400, "任务正在处理中，请稍后再试")
    t._cancel = False
    submit_polish(t, style, force=body.force)
    return {"ok": True, "idempotent": False, "message": reason}


@app.post("/api/tasks/{tid}/retry")
def retry_task(tid: str):
    """从失败处重试：整理阶段失败只重跑 LLM，其余阶段重跑整条流程"""
    t = store.get(tid)
    if not t:
        raise HTTPException(404, "任务不存在")
    if t.status not in ("failed", "canceled"):
        raise HTTPException(400, "只有失败或已取消的任务才能重试")
    action = pipeline_retry(t)
    if action == "busy":
        raise HTTPException(400, "任务正在处理中，请稍后再试")
    return {"ok": True, "action": action,
            "message": "正在重试 AI 整理" if action == "polish" else "正在重新转写"}


@app.get("/api/tasks/{tid}")
def get_task(tid: str):
    t = store.get(tid)
    if not t:
        raise HTTPException(404, "任务不存在")
    return t.to_dict()


@app.delete("/api/tasks/{tid}")
def delete_task(tid: str):
    ok = store.remove(tid)
    # 同时释放该任务的视频/音频，避免磁盘占用累积
    shutil.rmtree(MEDIA_DIR / tid, ignore_errors=True)
    return {"ok": ok}


@app.post("/api/tasks/{tid}/cancel")
def cancel_task(tid: str):
    return {"ok": store.request_cancel(tid)}


@app.get("/api/tasks/{tid}/events")
async def task_events(tid: str):
    async def gen():
        last = 0
        idle = 0
        # 记录上次推过的句数，只有新增分片才推 partial，
        # 否则 0.8 秒一次心跳会把同一份全文反复推一遍
        sent_segs = -1
        # meta 里带着 video_path，下载一落盘就推给前端，
        # 预览区不必等到整个任务跑完才出现
        sent_vp = ""
        while True:
            t = store.get(tid)
            if not t:
                yield 'data: {"type":"eof"}\n\n'
                break
            vp = (t.meta or {}).get("video_path") or ""
            if vp and vp != sent_vp:
                sent_vp = vp
                yield "data: " + json.dumps({"type": "meta", "meta": t.meta},
                                            ensure_ascii=False) + "\n\n"
            while last < len(t.events):
                e = t.events[last]
                last += 1
                yield "data: " + json.dumps({"type": "log", **e}, ensure_ascii=False) + "\n\n"
            # 转写进度推送：只要句数增加就推，不限定 stage。
            # 曾经只在 stage == "transcribing" 时推，但转写结束到
            # stage 切走之间存在窗口，最后几个分片正好落在窗口里，
            # 前端就永远停在倒数第几句 —— 表现为「已经转写完了，
            # 文字稿却没显示 / 显示不全」。末尾那个分片往往是最重要的。
            if t.status == "running":
                n = len(t.segments or [])
                if n and n != sent_segs:
                    sent_segs = n
                    yield "data: " + json.dumps(
                        {"type": "partial", "segments": t.segments,
                         "transcript": t.transcript,
                         "spercent": t.spercent}, ensure_ascii=False) + "\n\n"
            elif t.status != "running":
                sent_segs = -1
            yield "data: " + json.dumps(
                {"type": "state", "status": t.status, "stage": t.stage,
                 "percent": t.percent, "spercent": t.spercent,
                 "message": t.message, "error": t.error,
                 "failed_stage": t.failed_stage},
                ensure_ascii=False) + "\n\n"
            if t.status in ("done", "failed", "canceled"):
                # eof 带上最终结果兜底：任务可能在两次心跳之间直接结束，
                # 此时前端一次 partial 都没收到过，光靠增量会拿不到任何内容。
                yield "data: " + json.dumps(
                    {"type": "eof", "status": t.status,
                     "segments": t.segments or [],
                     "transcript": t.transcript or "",
                     "note": t.note or "", "note_sig": t.note_sig or "",
                     "note_info": t.note_info or {},
                     "variants": t.variants or [],
                     "meta": t.meta or {}},
                    ensure_ascii=False) + "\n\n"
                break
            idle += 1
            if idle > 900:
                break
            await asyncio.sleep(0.8)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/media/{tid}/{name}")
def media(tid: str, name: str, request: Request, download: int = 0):
    """视频/音频流式服务，支持 HTTP Range（浏览器拖动进度条必需）"""
    mt = _media_type(name)
    if not mt:
        raise HTTPException(404, "不支持的媒体文件")
    # 防目录穿越：只允许任务目录下的文件名，不接受任何路径分隔符
    if "/" in name or "\\" in name or name.startswith("."):
        raise HTTPException(404, "不支持的媒体文件")
    p = MEDIA_DIR / tid / name
    if not p.is_file():
        raise HTTPException(404, "媒体文件不存在（可能已被清理）")

    size = p.stat().st_size
    start, end, status = 0, size - 1, 200
    headers = {"Accept-Ranges": "bytes"}
    if download:
        headers["Content-Disposition"] = f'attachment; filename="{name}"'

    rng = request.headers.get("range")
    if rng and rng.lower().startswith("bytes="):
        spec = rng[6:].split(",")[0].strip()
        s, _, e = spec.partition("-")
        if s:
            start = int(s)
        if e:
            end = min(int(e), size - 1)
        if start > end or start >= size:
            return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
        status = 206
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"

    length = end - start + 1
    headers["Content-Length"] = str(length)

    def gen():
        with p.open("rb") as f:
            f.seek(start)
            left = length
            while left > 0:
                chunk = f.read(min(65536, left))
                if not chunk:
                    break
                left -= len(chunk)
                yield chunk

    return StreamingResponse(gen(), status_code=status, headers=headers, media_type=mt)


app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")
