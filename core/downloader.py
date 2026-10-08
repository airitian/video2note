"""下载层：默认走 yt-dlp，抖音失败时回退 pyktok -> 合并/转码为可播放 mp4

策略：yt-dlp 优先（覆盖面广、B站稳定）。只有当链接是抖音、且 yt-dlp 明确失败时，
才交给 core/douyin.py（pyktok + Playwright 自签 a_bogus）再试一次。

刻意不让 yt-dlp 执行任何 ffmpeg 后处理：它对 ffprobe 有硬依赖，
本机可能只装了 ffmpeg（如 imageio-ffmpeg）。合并与抽音频全部由 audio.py 完成。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Callable

from . import audio
from .config import ensure_cookie_file, load_settings, normalize_browser

PLATFORMS = [
    ("douyin", ("douyin.com", "iesdouyin.com", "v.douyin.com")),
    ("bilibili", ("bilibili.com", "b23.tv", "bilibili.tv")),
]

SKIP_SUFFIX = {".json", ".part", ".ytdl", ".txt", ".webp", ".jpg", ".png"}

# 分享文案里常带标题、话题标签，需要先从中抠出真正的链接
URL_RE = re.compile(r"https?://[^\s，。；！？、）】》\"'<>]+", re.I)
BARE_RE = re.compile(
    r"(?:(?:www|m|v|live)\.)?(?:bilibili\.com|b23\.tv|douyin\.com|iesdouyin\.com)/\S+", re.I)
TRAIL = "。，、；：！？）】》”’\"'.,;:!?)]}>"


class InvalidURLError(RuntimeError):
    pass


def extract_url(text: str) -> str:
    """从任意分享文案中提取视频链接；提取不到就原样返回"""
    s = (text or "").strip()
    if not s:
        return ""
    m = URL_RE.search(s)
    if m:
        return _fix_bv(m.group(0).rstrip(TRAIL))
    m = BARE_RE.search(s)
    if m:
        return _fix_bv("https://" + m.group(0).lstrip("/").rstrip(TRAIL))
    return _fix_bv(s)


# B站 BV 号去掉 "BV" 后固定 10 位。分享时经常只给后半截（如 17Uhv6LEZX），
# 少了 BV 前缀 yt-dlp 会直接报 Unsupported URL，这里补回去。
_BV_SHORT = re.compile(r"(?:bilibili\.com/video/|b23\.tv/)([A-Za-z0-9]{10})(?![\w-])")


def _fix_bv(url: str) -> str:
    """把缺失 BV 前缀的B站短 ID 补全"""
    m = _BV_SHORT.search(url or "")
    if not m:
        return url
    vid = m.group(1)
    if vid.startswith("BV"):
        return url
    return url[:m.start(1)] + "BV" + vid


def detect_platform(url: str) -> str:
    u = (extract_url(url) or "").lower()
    for name, keys in PLATFORMS:
        if any(k in u for k in keys):
            return name
    return "other"


class DownloadError(RuntimeError):
    pass


# ========== B站风控（HTTP 412）相关 ==========
# 412 Precondition Failed 不是「链接不对」，而是 B站的反爬机器人拦截：
# 请求缺少 buvid3 这个设备标识就直接拒。yt-dlp 自己会去拿 buvid，
# 但创空间是云服务器 IP，那一步请求本身就被拦，于是永远拿不到 buvid → 死循环在 412。
# 解决办法：用浏览器 UA 直接调公开的 spi 接口取 buvid3 / buvid4（无需登录），塞进请求头。
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
_SPI_URL = "https://api.bilibili.com/x/frontend/finger/spi"

# 常见浏览器头，缺 Referer / Accept-Language 也会被 B站判为异常请求
BROWSER_HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": "https://www.bilibili.com/",
}

_buvid_cache: dict = {}


def fetch_buvid(timeout: float = 12.0) -> dict:
    """取 B站设备标识 buvid3 / buvid4（进程内缓存，无需登录）。

    失败不抛异常——拿不到就返回空 dict，让调用方走原来的路径。
    """
    if _buvid_cache:
        return _buvid_cache
    import json
    import urllib.request

    try:
        req = urllib.request.Request(_SPI_URL, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8")).get("data") or {}
        if data.get("b_3"):
            _buvid_cache["buvid3"] = str(data["b_3"])
        if data.get("b_4"):
            _buvid_cache["buvid4"] = str(data["b_4"])
    except Exception:
        pass
    return _buvid_cache


def _is_bilibili(url: str) -> bool:
    u = (url or "").lower()
    return any(k in u for k in ("bilibili.com", "b23.tv", "bilibili.tv"))


def _has_login_cookie() -> bool:
    """B站是否配置了登录态 Cookie（SESSDATA / bili_jct / DedeUserID）

    只有自动 buvid 时 B站云 IP 大概率仍会 412；登录态才是真正的通行证。
    """
    cf = ensure_cookie_file("bilibili")
    if cf:
        try:
            text = Path(cf).read_text("utf-8", errors="ignore")
            return any(k in text for k in ("SESSDATA", "bili_jct", "DedeUserID"))
        except Exception:
            return True        # 有文件但读不了，姑且认为有
    s = load_settings()
    text = ((s.get("cookie_text_bili") or "") + (s.get("cookie_file") or "")).strip()
    return any(k in text for k in ("SESSDATA", "bili_jct", "DedeUserID"))


BILI_412_HINT = (
    "B站风控拦截（HTTP 412）：已自动补充 buvid3 但仍被拒，说明这台服务器的 IP "
    "被 B站深度风控。自动 buvid 在家用宽带上够用，在云服务器上通常不够。\n\n"
    "解决办法：在「⚙️ 设置」页的「B站 Cookie 文本」框粘贴浏览器里的B站 Cookie"
    "（F12 → Network → 任意请求 → Request Headers → 复制整段 Cookie），"
    "必须包含 SESSDATA / bili_jct / DedeUserID 这三项，保存后立即生效。\n\n"
    "注意：不要再粘到「抖音 Cookie 文本」框——两个平台的 Cookie 必须分开填，"
    "混在一起会因域名不匹配而全部失效。\n\n"
    "验证方式：设置页保存后应提示「B站 Cookie 已写入 cookies_bili.txt」。"
)


def _base_opts(workdir: Path, url: str = "", bili_api: bool = False) -> dict:
    """yt-dlp 参数

    bili_api=True 时走 B站客户端 API 解析（video_app / web_embedded），
    绕开 www.bilibili.com 网页的风控——云服务器 IP 上网页常 412，API 反而能通。
    """
    s = load_settings()
    opts: dict = {
        "outtmpl": str(workdir / "%(id)s.%(ext)s"),
        "postprocessors": [],
        "writethumbnail": False,
        "writeinfojson": True,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 30,
        # 浏览器头：B站 / 抖音对缺 Referer 的请求会直接 412
        "http_headers": dict(BROWSER_HEADERS),
    }
    fp = (s.get("ffmpeg_path") or "").strip()
    if fp:
        opts["ffmpeg_location"] = str(Path(fp).parent)
    # Cookie 优先级：本平台粘贴的文本 > 指定文件路径 > 从浏览器读取
    # 两个平台的 Cookie 存在独立通道里，B站只认 cookie_text_bili：
    # 抖音的 sessionid 落到 .bilibili.com 域名下是无效的，反之亦然。
    plat = "bilibili" if _is_bilibili(url) else "douyin"
    cf = ensure_cookie_file(plat)
    if not cf and plat == "bilibili":
        cf = ensure_cookie_file("douyin")   # 兼容早期只填了一个框的旧配置
    if not cf:
        cf = (s.get("cookie_file") or "").strip()
    cb = (s.get("cookie_browser") or "").strip()
    if cf:
        opts["cookiefile"] = cf
    elif cb:
        # 只接受浏览器名；误填 Cookie 文本时忽略，避免 browser-cookie 直接崩掉
        name, _err = normalize_browser(cb)
        if name:
            opts["cookiesfrombrowser"] = (name,)

    # B站兜底：补buvid3 / buvid4。风控要求这个设备标识，缺了就 412。
    # 已有 cookiefile 时只补请求头（yt-dlp 会与 cookie 文件合并）；
    # 没有 cookiefile 时直接用 Cookie 头携带。
    if _is_bilibili(url):
        bv = fetch_buvid()
        if bv:
            opts["http_headers"].update(bv)
            if not cf:
                opts["http_headers"]["Cookie"] = "; ".join(f"{k}={v}" for k, v in bv.items())
        # 走客户端 API 而非网页：云 IP 上网页 412 时 API 往往还能通
        opts["extractor_args"] = {"bilibili": {"video_app": "1" if bili_api else "0",
                                               "prefer_video_codec": "avc"}}

    px = (s.get("proxy") or "").strip()
    if px:
        opts["proxy"] = px
    return opts


def _meta(info: dict, url: str) -> dict:
    return {
        "video_id": str(info.get("id") or ""),
        "title": info.get("title") or "未命名视频",
        "uploader": info.get("uploader") or info.get("channel") or "",
        "description": (info.get("description") or "")[:1200],
        "duration": float(info.get("duration") or 0),
        "webpage_url": info.get("webpage_url") or url,
        "thumbnail": info.get("thumbnail") or "",
    }


def _is_video(f: dict) -> bool:
    return bool(f.get("vcodec")) and f.get("vcodec") != "none"


def _is_audio(f: dict) -> bool:
    return bool(f.get("acodec")) and f.get("acodec") != "none"


# 浏览器普遍不支持 HEVC（hev1/hvc1），播出来就是「只有声音没画面」
BAD_VCODECS = ("hev1", "hvc1", "hevc", "x265", "mpeg4")


def _vcodec(f: dict) -> str:
    return str(f.get("vcodec") or "").lower()


def _playable(f: dict) -> bool:
    return not any(b in _vcodec(f) for b in BAD_VCODECS)


def _pick_best(cands: list[dict]) -> dict:
    """先按「浏览器能播」过滤（优先 H.264），再按清晰度与码率取最优"""
    good = [f for f in cands if _playable(f)] or cands
    return max(good, key=lambda f: ((f.get("height") or 0), f.get("tbr") or 0))


def _pick(info: dict, max_height: int) -> tuple[str | None, str | None]:
    """返回 (video_format_id, audio_format_id)；单一文件流时 audio_id 为 None

    选流时刻意避开 HEVC：B站默认会优先给 hevc 高清流，但浏览器放不出画面，
    预览会变成「只有声音」，所以优先挑 H.264。
    """
    fmts = info.get("formats") or []
    combined = [f for f in fmts if _is_video(f) and _is_audio(f)]
    if combined:
        ok = [f for f in combined if (f.get("height") or 0) <= max_height] or combined
        return _pick_best(ok).get("format_id"), None

    vids = [f for f in fmts if _is_video(f) and not _is_audio(f)]
    auds = [f for f in fmts if _is_audio(f) and not _is_video(f)]
    if not vids:
        raise DownloadError("该链接没有可用的视频流")

    okv = [f for f in vids if (f.get("height") or 0) <= max_height] or vids
    bestv = _pick_best(okv)
    besta = max(auds, key=lambda f: (f.get("abr") or f.get("tbr") or 0)) if auds else None
    return bestv.get("format_id"), (besta.get("format_id") if besta else None)


def _download_one(url: str, workdir: Path, fmt_id: str, prefix: str,
                  progress: Callable[[float, str], None] | None) -> Path:
    workdir.mkdir(parents=True, exist_ok=True)
    opts = _base_opts(workdir, url)
    opts["format"] = fmt_id
    opts["outtmpl"] = str(workdir / f"{prefix}.%(ext)s")

    def hook(d: dict) -> None:
        if not progress:
            return
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            progress((done / total * 100) if total else 0.0, "下载中")
        elif d.get("status") == "finished":
            progress(100.0, "下载完成")

    opts["progress_hooks"] = [hook]
    last_exc: Exception | None = None
    # 媒体流下载同样可能撞 412（解析过了但取CDN 时被拦），重试一次换新 buvid
    for attempt in range(2):
        try:
            import yt_dlp
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])
            last_exc = None
            break
        except Exception as e:
            last_exc = e
            if attempt == 0 and _is_bilibili(url) and _is_412(e):
                _buvid_cache.clear()
                opts = _base_opts(workdir, url)      # 重新取 buvid
                opts["format"] = fmt_id
                opts["outtmpl"] = str(workdir / f"{prefix}.%(ext)s")
                opts["progress_hooks"] = [hook]
                if progress:
                    progress(0.0, "触发 B站风控，正在重试")
                continue
            break
    if last_exc is not None:
        raise DownloadError(_humanize_error(last_exc))

    cands = [p for p in workdir.iterdir()
             if p.is_file() and p.name.startswith(prefix + ".")
             and p.suffix.lower() not in SKIP_SUFFIX and p.stat().st_size > 0]
    if not cands:
        raise DownloadError(f"未找到下载到的 {prefix} 文件")
    return max(cands, key=lambda f: f.stat().st_size)


def download_media(url: str, workdir: Path, max_height: int = 1080,
                   progress: Callable[[float, str], None] | None = None) -> dict:
    """下载完整视频（含音轨），并抽出 ASR 用的音频。返回元数据 + 文件路径

    优先级：yt-dlp -> （仅抖音）pyktok 兜底 -> 汇总错误。
    """
    url = extract_url(url)
    if not url.startswith("http"):
        raise InvalidURLError(f"未能从输入中识别出有效链接：{url[:80]}")

    workdir.mkdir(parents=True, exist_ok=True)

    try:
        return _ytdlp_download(url, workdir, max_height, progress)
    except DownloadError as e:
        first_err = e
    except Exception as e:                     # 兜底，避免非预期异常直接穿透
        first_err = DownloadError(_humanize_error(e))

    if detect_platform(url) != "douyin":
        raise first_err

    # yt-dlp 拿不下抖音（a_bogus 动态签名风控）时，再用 pyktok 自签重试
    if progress:
        progress(3.0, f"yt-dlp 未能解析（{first_err}），改用 pyktok 重试")
    try:
        from . import douyin

        return douyin.download(url, workdir, progress)
    except Exception as e:
        raise DownloadError(
            f"yt-dlp 失败：{first_err}；pyktok 也失败：{_humanize_error(e)}")


def _ytdlp_download(url: str, workdir: Path, max_height: int,
                    progress: Callable[[float, str], None] | None) -> dict:
    """yt-dlp 下载主路径：解析 -> 选流 -> 下载 -> 合并/转码 -> 抽音频"""
    try:
        import yt_dlp
    except ImportError:
        raise DownloadError("未安装 yt-dlp，请先执行 pip install -U yt-dlp")

    def emit(pct: float, msg: str) -> None:
        if progress:
            progress(pct, msg)

    emit(0.0, "解析视频信息")

    def _probe(opts: dict, u: str) -> dict:
        """用给定参数解析；合集则取首条"""
        with yt_dlp.YoutubeDL(opts) as ydl:
            inf = ydl.extract_info(u, download=False)
            if inf is not None and inf.get("_type") == "playlist":
                entries = [x for x in (inf.get("entries") or []) if x]
                if not entries:
                    raise DownloadError("合集/列表为空")
                u2 = entries[0].get("webpage_url") or entries[0].get("url") or u
                inf = ydl.extract_info(u2, download=False)
            if inf is None:
                raise DownloadError("解析失败，未获取到视频信息")
            return inf

    try:
        info = _probe(_base_opts(workdir, url), url)
    except DownloadError:
        raise
    except Exception as e:
        # B站 412 是风控。逐级尝试：清 buvid 重试 -> 换客户端 API -> 换新 buvid + API
        if _is_bilibili(url) and _is_412(e):
            last = e
            for attempt, (clear, use_api) in enumerate(
                    [(True, False), (False, True), (True, True)], start=1):
                if clear:
                    _buvid_cache.clear()
                emit(float(attempt), f"B站风控，尝试第 {attempt} 种方式重试")
                try:
                    info = _probe(_base_opts(workdir, url, bili_api=use_api), url)
                    break
                except DownloadError:
                    raise
                except Exception as e2:
                    last = e2
            else:
                raise DownloadError(_humanize_error(last))
        else:
            raise DownloadError(_humanize_error(e))

    vid_id, aid_id = _pick(info, max_height)
    meta = _meta(info, url)

    def phase1(pct: float, msg: str) -> None:
        emit(pct * (0.5 if aid_id else 0.85), msg)

    def phase2(pct: float, msg: str) -> None:
        emit(50 + pct * 0.35, msg)

    emit(2.0, "下载视频轨")
    vpath = _download_one(url, workdir, vid_id, "vtrack", phase1)
    if aid_id:
        apath = _download_one(url, workdir, aid_id, "atrack", phase2)
        emit(90.0, "合并音视频")
        out = workdir / "video.mp4"
        try:
            audio.mux(vpath, apath, out)
        except Exception as e:
            # 合并失败时退化为无声视频，至少还能预览画面
            try:
                vpath.replace(out)
            except Exception:
                out = vpath
            meta["mux_warning"] = str(e)
        for p in (vpath, apath):
            try:
                if p.resolve() != out.resolve() and p.exists():
                    p.unlink()
            except Exception:
                pass
    else:
        out = workdir / "video.mp4"
        if vpath.resolve() != out.resolve():
            try:
                vpath.replace(out)
            except Exception:
                out = vpath

    # 兜底：若拿到的仍是 HEVC（浏览器放不出画面，只剩声音），转成 H.264 保证预览正常
    try:
        if audio.needs_transcode(out):
            emit(92.0, "转换为浏览器可播放的 H.264")
            tmp = workdir / "video_h264.mp4"
            audio.to_h264(out, tmp)
            try:
                if out.exists():
                    out.unlink()
            except Exception:
                pass
            tmp.replace(out)
            meta["video_codec"] = "h264"
            meta["transcoded"] = "h264"
            t_log = "视频为 HEVC 编码，已转码为 H.264 以便浏览器预览"
            meta["mux_warning"] = (meta.get("mux_warning", "") + " " + t_log).strip()
    except Exception as e:
        meta["video_codec"] = audio.video_codec(out) or ""
        meta["transcode_warning"] = str(e)

    emit(94.0, "提取音频用于转写")
    try:
        audio.extract_audio(out, workdir / "audio.mp3")
    except Exception as e:
        raise DownloadError(f"视频无可用音轨，无法转写：{e}")

    meta["video_path"] = str(out)
    meta["audio_path"] = str(workdir / "audio.mp3")
    emit(100.0, "下载完成")
    return meta


def _is_412(e: Exception) -> bool:
    """判断异常是否是 B站的 412 风控"""
    msg = str(e)
    return "412" in msg or "Precondition Failed" in msg


def _humanize_error(e: Exception) -> str:
    msg = str(e)
    low = msg.lower()
    # 412 在最前面：风控错误的信息最容易被后续分支误判成普通下载失败
    if "412" in msg or "precondition failed" in low:
        if _is_bilibili(str(e)):
            return BILI_412_HINT
        return ("请求被平台风控拦截（HTTP 412）。"
                "如为B站链接，请确认「设置」里已配置含 SESSDATA 的 Cookie。")
    if "unsupported url" in low or "no video formats" in low or "not a valid url" in low:
        return f"链接无法解析（请粘贴带 http(s):// 的完整链接）：{msg}"
    if "sign in" in low or "login" in low or "account" in low:
        return "平台要求登录，请在设置中配置 Cookie（浏览器或 cookies.txt）后重试。"
    if "geo" in low or "region" in low:
        return "该视频受地区限制，请配置代理后重试。"
    if "403" in msg or "forbidden" in low:
        return "请求被拒绝（403），多为风控触发，建议配置 Cookie 或稍后重试。"
    if "ffmpeg" in low:
        return f"ffmpeg 相关错误：{msg}"
    if "unable to download" in low or "http" in low:
        return f"下载失败：{msg}"
    return msg
