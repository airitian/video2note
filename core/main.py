"""FastAPI 应用入口"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import re
import shutil
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               RedirectResponse, Response, StreamingResponse)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import audio, downloader, exporters, store
from .downloader import detect_platform, extract_url
from .config import (ACCESS_TOKEN, ADMIN_TOKEN, HELP, MEDIA_DIR, ROOT, SECRET_KEYS,
                     SETTINGS_GROUPS, all_valid_tokens, ensure_cookie_file,
                     ensure_dirs, free_remaining, gate_active, is_admin,
                     load_settings, load_tokens, migrate_secrets, normalize_browser,
                     pro_free_limit, pro_gate_enabled, pro_used, public_settings,
                     reset_pro_usage, save_settings, token_role,
                     add_token as cfg_add_token, remove_token as cfg_remove_token)
from .pipeline import (_apply_variant, _mark_idempotent_hit, _sync_variant_meta,
                       polish_needed, retry as pipeline_retry,
                       submit, submit_polish)

ensure_dirs()
# 旧版本的密钥躺在 settings.json 里（会被接口读回浏览器），搬到独立文件。
# 放在 ensure_dirs 之后：迁移要写文件，数据目录必须已存在。
migrate_secrets()

# ── 健康检查：重活只在专属单线程跑，/api/health 永远零阻塞 ───────────────
# /api/health 要探测 ffmpeg 子进程、真启动一次 Chromium、fork git——
# 任一都耗时（内核在时 Chromium 启动 1~3s）。若放在请求路径上，
# 这些重活会占用 FastAPI 默认线程池的 worker，多个请求叠加就会把
# /api/tasks、上传、SSE 等正常接口饿死（用户感知为「网站变卡 / 打不开」）。
# 所以：重活只在下面这个专属单线程里跑；/api/health 改成 async，
# 永远只读共享快照，不碰请求线程池——无论内核探测多慢都不影响正常访问。
import concurrent.futures

_health_lock = threading.Lock()
_health_snapshot: dict | None = None
_health_stop = threading.Event()
_health_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=1, thread_name_prefix="health-probe")


def _compute_health(force: bool = False) -> dict:
    """真正做重活：fork ffmpeg / 启动 Chromium / git。只在专属线程调用。"""
    if force:
        # refresh=1 时清掉各层缓存，确保真的重新探测而非读旧值
        audio._ffmpeg_cache = None
        global _git_rev_cache, _assets_cache
        _git_rev_cache = None
        _assets_cache = None
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
    pw_msg = ""
    try:
        from .dlpanda import browser_ready

        # force 时绕过 browser_ready 自身缓存，真正重起一次内核确认
        pw, pw_msg = browser_ready(max_age=0.0 if force else None)
    except Exception as e:
        pw, pw_msg = False, f"检测失败：{e}"
    try:
        from .dlpanda import available as _dlp_ok

        dy = bool(_dlp_ok()) and pw
    except Exception:
        dy = False
    return {"ffmpeg": ff, "ffmpeg_msg": ffmsg, "yt_dlp": yt,
            "douyin_api": dy, "playwright": pw, "playwright_msg": pw_msg,
            **_build_info(refresh=force)}


def _refresh_health(force: bool = False) -> None:
    """算一次健康快照写进共享 dict（锁保护）。探测失败不影响已有快照。"""
    global _health_snapshot
    try:
        snap = _compute_health(force=force)
    except Exception:
        return
    with _health_lock:
        _health_snapshot = snap


def _health_loop() -> None:
    """启动后在专属线程里周期刷新快照：保持数据常新，且绝不碰请求线程。"""
    _refresh_health()
    while not _health_stop.wait(20):
        _refresh_health()


_ARCHIVE_STOP = threading.Event()


def _archive_loop() -> None:
    """每天跑一次归档清理，回收冷任务的源视频占用。

    独立于请求线程：即便某轮上传很慢，也不会拖慢任何页面操作。
    """
    from .qiniu_archive import cleanup
    time.sleep(90)          # 服务刚起来，先别抢占资源
    log = logging.getLogger("uvicorn.error")
    while not _ARCHIVE_STOP.is_set():
        try:
            s = cleanup()
            # 未配置凭据只是「功能没开」，天天写日志很吵，静默即可
            if not s.get("reason"):
                freed = (s.get("freed") or 0) / 1048576
                skipped = s.get("skipped") or []
                if freed or skipped:
                    log.info(f"媒体归档：释放 {freed:.1f} MB，"
                             f"{len(skipped)} 个因校验未通过保留本地")
        except Exception as e:      # noqa: BLE001 - 后台线程不该把进程带崩
            logging.getLogger("uvicorn.error").warning(f"媒体归档异常：{e}")
        _ARCHIVE_STOP.wait(86400)


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    """启动即在专属线程预热 + 周期刷新健康检查；请求路径始终只读快照。

    不用 @app.on_event("startup")：FastAPI 0.110+ 已废弃，会刷 DeprecationWarning。
    """
    # 先清僵尸：上次进程遗留的「处理中」任务，执行线程已随旧进程消失，
    # 不清理的话前端会对它们各开一条 SSE + 轮询，挂死连接占满浏览器并发池，
    # 顶栏就永远「检测中」。标成 failed 后重试按钮出现，可智能续跑。
    try:
        from .pipeline import recover_interrupted
        n = recover_interrupted()
        if n:
            logging.getLogger("uvicorn.error").info(f"已清理 {n} 个服务重启遗留的中断任务")
    except Exception:
        pass  # 清理失败不挡启动
    threading.Thread(target=_health_loop, daemon=True).start()
    threading.Thread(target=_archive_loop, daemon=True).start()
    yield
    _health_stop.set()
    _ARCHIVE_STOP.set()
    _health_executor.shutdown(wait=False)


# lifespan 必须在构造 app 之前定义好，否则这里拿不到这个名字
app = FastAPI(title="视频转笔记", version="1.1.0", lifespan=_lifespan)
STATIC = ROOT / "static"

# ---- 访问控制 / 多令牌 / 门禁 ----------------------------------------
# 设计目标：
#   1) 默认开放：不要求令牌也能使用（前 pro_free_limit 次成功转写免费）。
#   2) 门禁：成功转写达到 pro_free_limit 且已配置至少一个令牌后，
#      任务创建类接口要求持有效令牌，否则返回 402（后端强制校验）。
#   3) 管理员：引导令牌 ADMIN_TOKEN（兼容旧 V2N_ACCESS_TOKEN）或网页里登记的
#      admin 令牌，可打开「设置」并管理其它令牌；普通令牌看不到设置按钮，
#      设置类接口后端一律 401/403。
# 令牌四种携带方式：Authorization: Bearer / X-Access-Token / cookie / ?token=。
# cookie 用于 <video src="/api/media/..."> 这类无法自定义 header 的直连请求。

def _extract_token(request: Request) -> str:
    """从四种方式中取出令牌（空串表示未携带）。"""
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    tok = (request.headers.get("x-access-token") or "").strip()
    if tok:
        return tok
    tok = (request.cookies.get("v2n_token") or "").strip()
    if tok:
        return tok
    return (request.query_params.get("token") or "").strip()


def _is_protected_create(request: Request) -> bool:
    """任务创建 / 重跑类接口：门禁激活时要求令牌。"""
    if request.method != "POST":
        return False
    p = request.url.path
    if p in ("/api/tasks", "/api/tasks/upload", "/api/tasks/upload-raw"):
        return True
    return bool(re.fullmatch(r"/api/tasks/[^/]+/(polish|retry)", p))


@app.middleware("http")
async def _access_guard(request: Request, call_next):
    path = request.url.path
    tok = _extract_token(request)
    # 1) 配置 / 管理员令牌管理：仅管理员（或开发模式全开）
    if path.startswith("/api/settings") or path.startswith("/api/admin/"):
        if not is_admin(tok):
            return JSONResponse(
                {"detail": "需要管理员令牌", "code": "NEED_ADMIN"},
                status_code=401 if not tok else 403)
        return await call_next(request)
    # 2) 任务创建类：门禁激活且未持有效令牌 -> 拒绝（后端强制校验）
    if gate_active() and _is_protected_create(request):
        if token_role(tok) is None:
            return JSONResponse(
                {"detail": "免费次数已用完，请提供访问令牌后继续",
                 "code": "TOKEN_REQUIRED"},
                status_code=402)
    return await call_next(request)

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


class LoginIn(BaseModel):
    token: str = ""


@app.get("/api/auth/state")
def auth_state(request: Request):
    """前端据此判断：是否要弹令牌、当前角色、门禁与剩余次数。"""
    tok = _extract_token(request)
    role = token_role(tok) or "none"
    return {
        "admin_configured": bool(ADMIN_TOKEN),
        "role": role,
        "token_required": gate_active(),
        "gate_active": gate_active(),
        "free_remaining": free_remaining(),
        "pro_used": pro_used(),
        "pro_limit": pro_free_limit(),
    }


@app.post("/api/auth/login")
def auth_login(body: LoginIn, request: Request):
    """校验令牌后写入 httpOnly cookie，后续请求由浏览器自动携带。

    用 cookie 而不是让前端存 localStorage，是为了让
    <video src="/api/media/..."> 这类无法自定义 header 的请求也能通过校验。
    """
    tok = (body.token or "").strip()
    # 没有任何令牌体系：开发模式，直接放行（视为管理员）
    if not all_valid_tokens():
        resp = JSONResponse({"ok": True,
                              "role": "admin" if not ADMIN_TOKEN else "none"})
        if tok:
            resp.set_cookie("v2n_token", tok, httponly=True, samesite="lax",
                            max_age=30 * 24 * 3600)
        return resp
    if not tok or token_role(tok) is None:
        raise HTTPException(401, "访问令牌不正确")
    resp = JSONResponse({"ok": True, "role": token_role(tok)})
    resp.set_cookie("v2n_token", tok, httponly=True, samesite="lax",
                    max_age=30 * 24 * 3600)
    return resp


@app.post("/api/auth/logout")
def auth_logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("v2n_token")
    return resp


# ============================ 管理员：令牌与门禁 ============================
# 下面所有 /api/admin/* 由中间件强制仅管理员可访问（后端校验，前端无法绕过）。
class TokenIn(BaseModel):
    token: str = ""
    role: str = "user"      # admin | user


@app.get("/api/admin/tokens")
def admin_list_tokens():
    """列出已登记令牌（明文，仅管理员可见）与门禁状态。仅管理员。"""
    toks = [{"token": t["token"], "role": t["role"]} for t in load_tokens()]
    return {
        "tokens": toks,
        "admin_token_set": bool(ADMIN_TOKEN),
        "gate": {"active": gate_active(), "used": pro_used(),
                 "limit": pro_free_limit(), "free_remaining": free_remaining(),
                 "enabled": pro_gate_enabled()},
    }


@app.post("/api/admin/tokens")
def admin_add_token(body: TokenIn):
    """新增令牌。仅管理员。

    固定按普通用户（role=user）创建——管理员唯一来源是容器环境变量里的
    引导令牌（V2N_ACCESS_TOKEN / V2N_ADMIN_TOKEN），网页里刻意建不出管理员，
    避免某个用户 token 泄露后直接被提权到设置层。body.role 被忽略。
    """
    try:
        cfg_add_token(body.token, "user")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "role": "user"}


@app.delete("/api/admin/tokens")
def admin_delete_token(token: str = ""):
    """删除已登记令牌（引导 ADMIN_TOKEN 不能在此删，改环境变量生效）。仅管理员。"""
    tok = (token or "").strip()
    if not tok:
        raise HTTPException(status_code=400, detail="缺少 token")
    if ADMIN_TOKEN and hmac.compare_digest(tok, ADMIN_TOKEN):
        raise HTTPException(status_code=400,
                            detail="引导管理员令牌不能在此删除（请改环境变量 V2N_ADMIN_TOKEN）")
    cfg_remove_token(tok)
    return {"ok": True}


@app.get("/api/admin/usage")
def admin_usage():
    """查看免费次数使用量。仅管理员。"""
    return {"used": pro_used(), "limit": pro_free_limit(),
            "free_remaining": free_remaining(), "gate_active": gate_active(),
            "enabled": pro_gate_enabled()}


@app.post("/api/admin/usage/reset")
def admin_usage_reset():
    """清零免费次数计数（重新发放免费额度）。仅管理员。"""
    reset_pro_usage()
    return {"ok": True, "used": 0, "free_remaining": free_remaining()}


class PolishIn(BaseModel):
    style: str = "general"


class SettingsIn(BaseModel):
    values: dict
    # 显式要求清空的敏感项（Token / Cookie）。
    # 敏感项不再回传，前端无法区分「没改」和「想清空」，所以必须单独传。
    clear: list[str] = []


def _safe_tid(tid: str) -> str:
    """校验任务 id：不合法一律 404，绝不拿去拼路径。

    ★ 必须白名单，不能黑名单。路由参数 tid 的匹配规则是 [^/]+，
    既拦不住 ".."，在 Windows 上也拦不住反斜杠路径。而 tid 会参与
    拼接 MEDIA_DIR / tid：
        MEDIA_DIR / ".."  ->  data/media/..  ==  DATA_DIR 本身
    于是 DELETE /api/tasks/.. 会让 shutil.rmtree 把 DATA_DIR 整个删掉
    （tasks/、media/、settings.json、secrets.json、cookies.txt 全没），
    且 ignore_errors=True 不留任何痕迹。

    任务 id 由 store.create() 生成为 uuid4().hex[:12]，用严格白名单最稳。
    """
    if not re.fullmatch(r"[0-9a-f]{12}", tid or ""):
        raise HTTPException(404, "任务不存在")
    return tid


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
async def health(refresh: int = 0):
    """环境自述。永远是异步、零阻塞：

    - 直接返回「专属探测线程」维护的快照，不碰 FastAPI 请求线程池；
    - 重活（起 Chromium / fork ffmpeg / git）只在 _health_executor 里跑；
    - refresh=1 触发一次后台重探，但本次仍返回当前快照（不等待结果）。

    这样无论内核探测多慢，/api/tasks、上传、SSE 等正常接口都不会被拖垮。
    """
    if refresh:
        # 触发后台重探，不等待——本次仍返回已有的新鲜快照
        _health_executor.submit(_refresh_health, True)
    with _health_lock:
        snap = _health_snapshot
    if snap is None:
        # 极冷场景（刚启动还没探完第一次）：返回轻量占位，绝不阻塞；
        # 专属线程已在探测，下一次请求就有数据。
        snap = {"ffmpeg": None, "ffmpeg_msg": "", "yt_dlp": None,
                "douyin_api": None, "playwright": None, "playwright_msg": "",
                **_build_info()}
    # 显式关掉代理缓冲 + 禁止缓存：预览网关(monkeycode-ai.online)会缓冲
    # 响应，曾导致 SSE 推流到不了浏览器；即便普通 JSON 也带上，双保险。
    return Response(
        content=json.dumps(snap, ensure_ascii=False),
        media_type="application/json",
        headers={"Cache-Control": "no-store, no-transform",
                 "X-Accel-Buffering": "no"},
    )


_git_rev_cache: tuple[float, str] | None = None
_GIT_REV_TTL = 30.0


def _git_rev(max_age: float | None = None) -> str:
    """当前 commit 短哈希。

    这会 fork 一个 git 子进程（实测约 300ms），而它挂在 /api/health 上，
    也就等于挂在页面启动路径上——每次打开网页都白白 fork 一次 git。
    commit 在进程生命周期内不会变（除非有人在运行中切了分支，
    那种情况 30 秒后自然跟上），所以缓存起来。
    """
    global _git_rev_cache
    ttl = _GIT_REV_TTL if max_age is None else max_age
    c = _git_rev_cache
    if c is not None and ttl > 0 and (time.monotonic() - c[0]) < ttl:
        return c[1]
    try:
        import subprocess
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(ROOT), capture_output=True, text=True, timeout=3)
        rev = (out.stdout or "").strip()
    except Exception:
        rev = ""
    _git_rev_cache = (time.monotonic(), rev)
    return rev


_assets_cache: tuple[float, str] | None = None
_ASSETS_TTL = 30.0


def _assets_stamp(max_age: float | None = None) -> str:
    """index.html 里的静态资源版本戳。

    index.html 在磁盘上不会自己变，但每次健康检查都重读一遍没必要，
    与 git commit 一起按同样的 TTL 缓存。
    """
    global _assets_cache
    ttl = _ASSETS_TTL if max_age is None else max_age
    c = _assets_cache
    if c is not None and ttl > 0 and (time.monotonic() - c[0]) < ttl:
        return c[1]
    stamp = ""
    try:
        html = (STATIC / "index.html").read_text("utf-8")
        m = re.search(r"v=(\d+[a-z]*)", html)
        stamp = m.group(1) if m else ""
    except Exception:
        stamp = ""
    _assets_cache = (time.monotonic(), stamp)
    return stamp


def _build_info(refresh: bool = False) -> dict:
    """版本与能力自述。

    「云机器上改了代码但页面没变」这类问题，光靠猜是猜不出来的——
    到底是没 git pull、pull 了没重启、还是浏览器缓存，得让人一眼看到。
    页面右下角会显示这个 commit，点「设置」也能看到接口返回了哪些字段。

    refresh=True（/api/health?refresh=1）时跳过缓存重新读，
    这样刚 git pull 完点一下顶栏就能看到新版本号。
    """
    max_age = 0.0 if refresh else None
    return {
        "version": {
            "commit": _git_rev(max_age=max_age),
            "assets": _assets_stamp(max_age=max_age),
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


@app.get("/api/archive/status")
def archive_status():
    """本地媒体占用 + 归档配置概览，供人工判断要不要清理。"""
    from . import store
    from .qiniu_archive import _source_files, cfg, configured, pending
    c = cfg()
    able, why = configured(c)
    total = 0
    per = []
    pend = []
    pend_bytes = 0
    for t in store.list_tasks():
        mdir = MEDIA_DIR / t.id
        if not mdir.is_dir():
            continue
        fs = _source_files(mdir)
        if not fs:
            continue
        sz = sum(f.stat().st_size for f in fs)
        total += sz
        per.append({"tid": t.id, "size": sz, "files": [f.name for f in fs]})
        # 已落盘但云端还没有的：清理掉这些就等于丢数据，必须让用户看得见
        n = pending(t)
        if n and able:
            pend.append({"tid": t.id, "pending": n, "size": sz})
            pend_bytes += sz
    per.sort(key=lambda x: -x["size"])
    return {
        "configured": able,
        "reason": why,
        "bucket": c["bucket"],
        "domain": c["domain"],
        "local_bytes": total,
        "local_mb": round(total / 1048576, 1),
        "pending_tasks": pend[:100],
        "pending_count": sum(p["pending"] for p in pend),
        "pending_bytes": pend_bytes,
        "tasks": per[:100],
    }


@app.post("/api/archive/sync")
def archive_sync(tid: str = "", limit: int = 0):
    """手动把「还没备份到云端的源视频」补传上去。

    自动备份可能因为当时没配密钥、网络抖了一下而漏掉几个，
    这时任务列表会显示待同步数量，用户点一下这个就把它们补上。
    不指定 tid 则补全量（默认上限 20 个，避免一次拖太久）。

    ★只上传，不删任何本地文件——删除始终属于 cleanup 的职责。
    """
    if tid:
        _safe_tid(tid)          # tid 会拿去查任务、拼路径，先过白名单
    from . import store
    from .qiniu_archive import cfg, configured, pending, schedule_backup
    able, why = configured(cfg())
    if not able:
        raise HTTPException(400, f"未配置对象存储：{why}")
    lim = limit or 20
    targets = []
    if tid:
        t = store.get(tid)
        if not t:
            raise HTTPException(404, "任务不存在")
        if pending(t):
            targets.append(t)
    else:
        for t in store.list_tasks():
            if pending(t):
                targets.append(t)
                if len(targets) >= lim:
                    break
    for t in targets:
        schedule_backup(t, priority="instant")
    return {"ok": True, "started": len(targets),
            "tids": [t.id for t in targets]}


@app.post("/api/archive/run")
def archive_run(days: int = 0, dry_run: int = 1, limit: int = 0):
    """手动触发归档清理。

    dry_run 默认为 1：只算不改，先看清楚会释放哪些空间，
    确认无误再显式传 dry_run=0。真正执行时仍然遵循
    「云端 stat 校验大小一致才删本地」的铁律。
    """
    from .qiniu_archive import cleanup
    return cleanup(days or None, dry_run=bool(dry_run), limit=limit or None)


@app.get("/api/tasks/{tid}")
def get_task(tid: str):
    t = store.get(tid)
    if not t:
        raise HTTPException(404, "任务不存在")
    return t.to_dict()


@app.delete("/api/tasks/{tid}")
def delete_task(tid: str):
    _safe_tid(tid)          # 先校验，再拼路径（拼错会删掉整个 data 目录）
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
        # 记录上次推过的内容签名（句数 + 最后一句文本）。
        # 用内容而不是句数：重排序、补写场景下句数可能不变但文本变了。
        # 初值 None → 第一次 tick 必然推一次全量，客户端刚连上就能拿到已有内容。
        sent_sig = None
        # 状态指纹：任一字段变化都算「有进展」，用来给 idle 归零
        last_state = None
        # meta 里带着 video_path，下载一落盘就推给前端，
        # 预览区不必等到整个任务跑完才出现
        sent_vp = ""
        while True:
            # ★在锁内一次性快照任务状态（#8）：pipeline 线程正在并发改写
            # t.segments / t.transcript / t.status，若边读边被改写，json.dumps
            # 可能看到「半新」的内容（例如正在替换的列表引用、或状态与进度不一致）。
            # 这里先快照成局部变量，再在锁外慢慢序列化，既安全又不拖长持锁时间。
            with store._lock:
                t = store.get(tid)
                if t is None:
                    yield 'data: {"type":"eof"}\n\n'
                    return
                snap = {
                    "status": t.status, "stage": t.stage, "percent": t.percent,
                    "spercent": t.spercent, "message": t.message, "error": t.error,
                    "failed_stage": t.failed_stage,
                    "segments": list(t.segments or []),
                    "transcript": t.transcript or "",
                    "note": t.note or "", "note_sig": t.note_sig or "",
                    "note_info": dict(t.note_info or {}),
                    "variants": dict(t.variants or {}),
                    "meta": dict(t.meta or {}),
                }
                vp = snap["meta"].get("video_path") or ""
                evs = list(t.events)
            # 视频路径：下载一落盘就推，预览区不必等全流程结束
            if vp and vp != sent_vp:
                sent_vp = vp
                yield "data: " + json.dumps({"type": "meta", "meta": snap["meta"]},
                                            ensure_ascii=False) + "\n\n"
            # 日志：只推新增
            while last < len(evs):
                e = evs[last]
                last += 1
                yield "data: " + json.dumps({"type": "log", **e}, ensure_ascii=False) + "\n\n"
            # 转写进度推送：只要内容有变化就推，**不看 status**。
            # ★这里曾经写成 `if t.status == "running"`，是个隐蔽但致命的坑：
            # 转写结束的标志恰恰是把 status 改成 "transcribed"，所以最后一批
            # 分片一定落在「非 running」的窗口里 —— 而 0.8 秒的心跳很可能
            # 直接从 running 跳到 transcribed，中间那一次都没观察到。
            # 结果就是短视频（单分片）从头到尾一个 partial 都推不出去，
            # 长视频则丢掉结尾几句。页面表现为「已经转写完了，文字稿空的，
            # 刷新一下才有」——因为刷新走详情接口，不依赖 SSE。
            # 现在改成按内容签名（句数 + 最后一句文本）判断，与状态无关：
            # 重连后 sent_sig 归零，还会把当前全量补推一次，天然自愈。
            segs = snap["segments"]
            # 显式拆开写：`a or b if c else d` 的优先级很容易看错
            last_text = (segs[-1].get("text") or "") if segs else ""
            sig = (len(segs), last_text)
            if segs and sig != sent_sig:
                sent_sig = sig
                yield "data: " + json.dumps(
                    {"type": "partial", "segments": segs,
                     "transcript": snap["transcript"],
                     "spercent": snap["spercent"]}, ensure_ascii=False) + "\n\n"
                idle = 0          # 有实质进展就不计入空闲
            # 状态指纹：下载、抽音频、切片这些阶段没有 segments，
            # 但同样是在推进。旧代码只看 segments，于是下载一个长视频
            # 就会把 idle 顶满 12 分钟然后被服务端掐断连接。
            state_sig = (snap["status"], snap["stage"], snap["percent"],
                         snap["spercent"], snap["message"])
            if state_sig != last_state:
                last_state = state_sig
                idle = 0
            yield "data: " + json.dumps(
                {"type": "state", "status": snap["status"], "stage": snap["stage"],
                 "percent": snap["percent"], "spercent": snap["spercent"],
                 "message": snap["message"], "error": snap["error"],
                 "failed_stage": snap["failed_stage"]},
                ensure_ascii=False) + "\n\n"
            # 终态（含转写完成 transcribed）即收尾：发 eof 兜底并断开连接。
            # ★旧逻辑只在 done/failed/canceled 才断，transcribed 后连接会一直
            # 空占着（最长 2 小时，#9）——既浪费连接，又让「转写完了但页面
            # 不更新」这类问题更难排查。现在 transcribed 也断：前端拿到 eof
            # 后即显示最终文字稿，polish 阶段再由前端开一条新 SSE。
            if snap["status"] in ("done", "failed", "canceled", "transcribed"):
                # eof 带上最终结果兜底：任务可能在两次心跳之间直接结束，
                # 此时前端一次 partial 都没收到过，光靠增量会拿不到任何内容。
                yield "data: " + json.dumps(
                    {"type": "eof", "status": snap["status"],
                     "segments": segs,
                     "transcript": snap["transcript"],
                     "note": snap["note"], "note_sig": snap["note_sig"],
                     "note_info": snap["note_info"],
                     "variants": snap["variants"],
                     "meta": snap["meta"]},
                    ensure_ascii=False) + "\n\n"
                return
            idle += 1
            # idle 只统计「任务还在跑但确实没进展」的时间。
            # 原来它是只增不减的：900 × 0.8s = 12 分钟后连接必然被服务端
            # 掐断，而前端断开后没有任何重连 —— 长视频必然触发，
            # 表现同样是「转写完成了但页面不更新，刷新一下才有」。
            # 现在有 partial 推送就归零；另外把上限放宽到 2 小时，
            # 即使是超长音频也不会被误杀（前端已有自动重连兜底）。
            if idle > 9000:
                return
            await asyncio.sleep(0.8)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _proxy_mode() -> bool:
    try:
        return (load_settings().get("qiniu_proxy") or "0").strip() == "1"
    except Exception:
        return False


def _cloud_playback(tid: str, name: str, request: Request):
    """本地媒体已被清理时的云端回显。返回 None 表示云端也没有这份文件。

    两种模式，默认走第一条：
      0 → 302 直跳云端地址：浏览器自己去 CDN 拉，服务器不占带宽，最快
      1 → 服务端代拉再转发：部署机出网受限、或空间设为私有时只能这样
    """
    from . import store
    from .qiniu_archive import remote_info, open_remote

    t = store.get(tid)
    if t is None:
        return None
    info = remote_info(t, name)
    if not info:
        # 没记过归档信息不代表云端一定没有——下载完就备份那一步还没跑时
        # 本地就被删了才会走到这里，只能如实告诉调用方没有。
        return None
    url = info["url"]

    if not _proxy_mode():
        return RedirectResponse(url, status_code=302)

    # ---- 代理模式：Range 必须透传，否则 <video> 拖不动进度条 ----
    size = int(info.get("size") or 0)
    rng = (request.headers.get("range") or "").strip()
    start, end = None, None
    if rng.lower().startswith("bytes="):
        spec = rng[6:].split(",")[0].strip()
        s, _, e = spec.partition("-")
        if s:
            start = int(s)
        if e:
            end = min(int(e), size - 1) if size else int(e)
    try:
        up = open_remote(url, start, end)
    except Exception as e:
        raise HTTPException(502, f"从对象存储取回失败：{e}")
    if up.status_code >= 400:
        up.close()
        raise HTTPException(404, f"对象存储返回 {up.status_code}")

    out = {"Accept-Ranges": up.headers.get("Accept-Ranges") or "bytes"}
    # 上游可能做了 gzip；这时 Content-Length 说的是压缩后的长度，
    # 而我们吐出的是解压后的字节，照抄会让浏览器提前判定结束。
    encoded = bool(up.headers.get("Content-Encoding"))
    for h in ("Content-Type", "Content-Range", "Content-Length",
              "ETag", "Last-Modified"):
        v = up.headers.get(h)
        if v and not (encoded and h == "Content-Length"):
            out[h] = v
    # Content-Type 交给 StreamingResponse 的 media_type 参数下发。
    # 同时在 headers 里再放一份会在响应里出现两个 Content-Type，
    # 部分浏览器/播放器遇到重复头会直接判这个响应无效。
    mime = out.pop("Content-Type", None) or _media_type(name) \
        or "application/octet-stream"

    def gen():
        try:
            # decode_content 要在开始读之前设；晚了就只能拿到原始压缩块
            if encoded and hasattr(up, "raw"):
                up.raw.decode_content = True
            for chunk in up.iter_content(65536):
                if chunk:
                    yield chunk
        finally:
            up.close()

    return StreamingResponse(gen(), status_code=up.status_code if up.status_code in (200, 206) else 200,
                             headers=out, media_type=mime)


@app.get("/api/media/{tid}/{name}")
def media(tid: str, name: str, request: Request, download: int = 0):
    """视频/音频流式服务，支持 HTTP Range（浏览器拖动进度条必需）"""
    _safe_tid(tid)          # 与 delete 同理：tid 参与路径拼接，必须先过白名单
    mt = _media_type(name)
    if not mt:
        raise HTTPException(404, "不支持的媒体文件")
    # 防目录穿越：只允许任务目录下的文件名，不接受任何路径分隔符
    if "/" in name or "\\" in name or name.startswith("."):
        raise HTTPException(404, "不支持的媒体文件")
    p = MEDIA_DIR / tid / name
    if not p.is_file():
        # 本地文件已被清理（过期释放 / 用户手动删除）。此时从对象存储回显：
        # 前端与 <video src> 的地址「/api/media/{tid}/{name}」一律不用改，
        # 表单没有重传入口，任务列表点开就该能播。
        remote = _cloud_playback(tid, name, request)
        if remote is not None:
            return remote
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
