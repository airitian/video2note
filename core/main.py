"""FastAPI 应用入口"""
from __future__ import annotations

import asyncio
import json
import shutil
import uuid
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import audio, downloader, exporters, store
from .downloader import detect_platform, extract_url
from .config import (HELP, MEDIA_DIR, ROOT, ensure_cookie_file, ensure_dirs,
                     load_settings, normalize_browser, public_settings,
                     save_settings)
from .pipeline import (_apply_variant, _mark_idempotent_hit, _sync_variant_meta,
                       polish_needed, submit, submit_polish)

ensure_dirs()

app = FastAPI(title="视频转笔记", version="1.1.0")
STATIC = ROOT / "static"

STYLES = ("general", "note", "article", "clean")
MEDIA_TYPES = {"video.mp4": "video/mp4", "audio.mp3": "audio/mpeg"}
ALLOWED_MEDIA = set(MEDIA_TYPES)


class CreateIn(BaseModel):
    url: str
    style: str = "general"
    language: str = "auto"
    model: str = ""


class PolishIn(BaseModel):
    style: str = "general"


class SettingsIn(BaseModel):
    values: dict


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
    return (STATIC / "index.html").read_text("utf-8")


@app.get("/api/settings")
def get_settings():
    return {"values": public_settings(), "help": HELP}


@app.post("/api/settings")
def post_settings(body: SettingsIn):
    # 「从浏览器读取」只接受浏览器名，误粘贴 Cookie 文本时直接报错而不是静默保存
    if "cookie_browser" in body.values:
        name, err = normalize_browser(body.values.get("cookie_browser") or "")
        if err:
            raise HTTPException(status_code=400, detail=err)
        body.values["cookie_browser"] = name
    save_settings(body.values)
    # Cookie 文本有变化时立刻落盘，供后续下载使用。
    # 即使被清空也要调一次——ensure_cookie_file 内部会删掉残留文件，
    # 否则页面回显为空、下载却仍在用旧 Cookie。
    files: dict[str, str] = {}
    if "cookie_text" in body.values:
        p = ensure_cookie_file("douyin")
        if p:
            files["douyin"] = p
    if "cookie_text_bili" in body.values:
        p = ensure_cookie_file("bilibili")
        if p:
            files["bilibili"] = p
    return {"ok": True, "values": public_settings(), "help": HELP,
            "cookie_files": files}


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
    return {"ffmpeg": ff, "ffmpeg_msg": ffmsg, "yt_dlp": yt}


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
        while True:
            t = store.get(tid)
            if not t:
                yield 'data: {"type":"eof"}\n\n'
                break
            while last < len(t.events):
                e = t.events[last]
                last += 1
                yield "data: " + json.dumps({"type": "log", **e}, ensure_ascii=False) + "\n\n"
            yield "data: " + json.dumps(
                {"type": "state", "status": t.status, "stage": t.stage,
                 "percent": t.percent, "spercent": t.spercent,
                 "message": t.message, "error": t.error},
                ensure_ascii=False) + "\n\n"
            if t.status in ("done", "failed", "canceled"):
                yield 'data: {"type":"eof"}\n\n'
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
    mt = MEDIA_TYPES.get(name)
    if not mt:
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
