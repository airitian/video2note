"""抖音专用下载：优先 pyktok（能绕过 a_bogus 签名风控），失败回退 yt-dlp

yt-dlp 的抖音提取器（tiktok.py）调aweme/detail 接口时，抖音要求
`a_bogus`/`X-Bogus` 动态签名参数，而 yt-dlp 未实现，会直接抛出
误导性的 "Fresh cookies (not necessarily logged in) are needed"。
pyktok 自带完整签名实现，因此抖音链接一律先走这里。

B站等其他平台仍走 yt-dlp，不受本模块影响。
"""
from __future__ import annotations

import json
import re
import urllib.parse
from pathlib import Path
from typing import Callable

from .config import _sanitize_cookie_component, load_settings

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36")


class DouyinError(RuntimeError):
    pass


# 抖音的失败原因有三种，解法完全不同，不能混为一谈：
#
#   missing  没配 Cookie   -> 去设置页粘贴 Cookie
#   blocked  IP 被风控     -> 配代理或换网络（换 Cookie 没用！）
#
# ★ 踩过的坑：曾经用 `aweme/v1/web/user/profile/self` 的 status_code=8
#   当作「登录态作废」的判据 —— **这个判据是错的**，已被对照实验推翻：
#   无 Cookie / 伪造 sessionid / 随机垃圾串 打这个接口，返回值**完全相同**
#   都是 status_code=8。说明 8 只表示「这个接口需要签名参数」，与登录态无关。
#   后来用真实 Chromium 验证，Cookie 登录态完全有效，
#   真正卡住的是 IP 被风控（滑块验证码）—— 属于第三种原因。
#
# 所以这里**不再猜测 Cookie 是否过期**，只如实区分「没配 Cookie」和「被风控」。
# 后者靠页面特征判定（验证码中间页 / JS 挑战页），不靠接口返回码。
COOKIE_MISSING_HINT = (
    "未配置抖音 Cookie，无法解析。\n\n"
    "实测确认：未配置 Cookie 时，抖音分享页返回的是一段 JS 虚拟机挑战脚本"
    "（`_$jsvmprt`），里面没有任何作品数据，yt-dlp 也会报"
    "\"Fresh cookies (not necessarily logged in) are needed\"（这句措辞有误导，"
    "并不是\"放久了的 Cookie\"问题）。\n\n"
    "解决办法：在「⚙️ 设置」页的「抖音 Cookie 文本」框粘贴浏览器里的抖音 Cookie，"
    "保存后立即生效（F12 → Network → 任意 douyin.com 请求 → "
    "Request Headers → 复制整段 Cookie）。\n\n"
    "注意：抖音与B站 Cookie 必须分开填，混填会因域名不匹配全部失效。"
)

COOKIE_BLOCKED_HINT = (
    "请求被抖音风控拦截（IP 层面），**不是 Cookie 的问题**。\n\n"
    "实测现象：用真实 Chromium 加载已登录的 Cookie 打开作品页，"
    "停在「验证码中间页」，要求拖动滑块完成验证。\n\n"
    "关键点：这种情况**重新获取 Cookie 也解决不了** —— "
    "拦在前面的是 IP 风险识别，与登录态无关。\n\n"
    "解决办法（按推荐顺序）：\n"
    "1. 在「⚙️ 设置」页的**代理**栏填一个可用代理（最直接）\n"
    "2. 换网络环境（如手机热点）\n"
    "3. 关掉可能劫持流量的代理软件后重试\n\n"
    "抖音对云服务器 IP 的风控尤其严格，家用宽带一般不触发。"
)

# 兼容旧引用
DOUYIN_COOKIE_HINT = COOKIE_MISSING_HINT

# 抖音风控拦截的页面特征
_BLOCK_MARKERS = ("验证码中间页", "请完成下列验证后继续",
                  "captcha", "_$jsvmprt", "__ac_signature")


def cookie_state() -> str:
    """探测抖音请求的受阻原因：ok / missing / blocked / unknown

    只区分「没配 Cookie」与「被风控」，**不猜测 Cookie 是否过期**——
    公开接口在缺签名参数时一律返回 status_code=8，无论 Cookie 是否有效
    （已用对照实验验证），拿它判断登录态必然误报。
    """
    ck = _cookie_header()
    if not ck:
        return "missing"
    try:
        import requests

        r = requests.get(
            "https://www.iesdouyin.com/share/video/1/",
            headers={"User-Agent": UA, "Cookie": ck,
                     "Referer": "https://www.iesdouyin.com/"},
            timeout=(8, 20),
        )
        html = r.text or ""
    except Exception:
        return "unknown"
    low = html.lower()
    if any(m in low or m in html for m in _BLOCK_MARKERS):
        return "blocked"
    return "ok"


def cookie_hint() -> str:
    """按实际受阻原因返回提示

    两种情况解法相反：missing 要去填 Cookie，blocked 要换 IP 或加代理。
    弄反会让用户白折腾 —— 之前就发生过「明明配了 Cookie 却被告知没配」。
    """
    return COOKIE_BLOCKED_HINT if cookie_state() == "blocked" else COOKIE_MISSING_HINT


def _pyktok_supports_douyin(api_cls: type) -> bool:
    """判断这个 TikTokApi 类能否为国内抖音生成签名

    海外版 TikTokApi（由 pyktok 误装）在 create_sessions 里硬编码 tiktok.com，
    国内网络下必定超时；只有真正的抖音签名实现才可用。
    判据：类所在模块路径。真正的抖音实现不会来自 TikTokApi 包。
    """
    mod = getattr(api_cls, "__module__", "") or ""
    return not mod.startswith("TikTokApi")


def _cookie_header() -> str:
    """把配置里的 cookie_text 拼成请求头用的 Cookie 串

    Cookie 里常混有中文（如 `SEARCH_RESULT_LIST_TYPE={"keyword":"搜索词"}`），
    而 HTTP 头只能按 latin-1 编码，直接发送会抛
    `'latin-1' codec can't encode characters in position N`。
    这里对名/值统一做 ASCII 化处理，与落盘逻辑保持一致。
    """
    s = load_settings()
    text = (s.get("cookie_text") or "").strip()
    if not text:
        return ""
    # 去掉我们为落盘而加的 `# domain=xxx` 提示行
    text = re.sub(r"^#\s*domain\s*=\s*\S+[ \t]*\n?", "", text, flags=re.I | re.M)
    parts = []
    for chunk in re.split(r"[;\n]", text):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        k, _, v = chunk.partition("=")
        k = _sanitize_cookie_component(k.strip())
        if not k:
            continue
        parts.append(f"{k}={_sanitize_cookie_component(v.strip())}")
    return "; ".join(parts)


def is_douyin(url: str) -> bool:
    u = (url or "").lower()
    return any(k in u for k in ("douyin.com", "iesdouyin.com"))


def _referer(url: str) -> str:
    """抖音短链需要先跟随到真实地址；从短链域名也能推断"""
    return "https://www.douyin.com/"


def _cookie_dict() -> dict:
    """把 cookie_text 解析成 dict，交给 pyktok 设置会话"""
    out: dict[str, str] = {}
    for part in _cookie_header().split(";"):
        part = part.strip()
        if "=" in part:
            k, _, v = part.partition("=")
            if k.strip():
                out[k.strip()] = v.strip()
    return out


def _resolve_short(url: str) -> str:
    """把 v.douyin.com 短链展开成带 aweme_id 的完整地址"""
    if "v.douyin.com" not in url:
        return url
    import requests

    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=(10, 25),
                         allow_redirects=True)
        final = r.url
        m = re.search(r"/(?:video|note)/(\d+)", final) or re.search(r"modal_id=(\d+)", final)
        if m:
            return f"https://www.douyin.com/video/{m.group(1)}"
        return final or url
    except Exception:
        return url


def _pick_url(data: dict) -> str:
    """从 play_addr / download_addr 里挑一个可用直链（去掉水印参数）

    直链通常嵌在 `video.play_addr` 下（抖音 aweme/detail 的标准结构），
    但移动端分享页抓到的结构可能直接摊在顶层，所以两层都要找。
    """
    scopes = [data]
    vid = data.get("video")
    if isinstance(vid, dict):
        scopes.insert(0, vid)          # video 优先，标准结构都在这

    for scope in scopes:
        for key in ("play_addr", "download_addr", "playAddr", "downloadAddr"):
            item = scope.get(key) or {}
            if not isinstance(item, dict):
                continue
            urls = item.get("url_list") or item.get("urlList") or []
            for u in urls:
                u = (u or "").replace("http://", "https://")
                if u:
                    return u

    # 兜底：老结构，字段值本身就是 URL 字符串
    for scope in scopes:
        for key in ("playApi", "videoApi", "play_url", "download_url"):
            v = scope.get(key) or ""
            if isinstance(v, str) and v.startswith("http"):
                return v.replace("http://", "https://")
    raise DouyinError("未找到可用的视频直链")


def _header_for(url: str, referer: str) -> dict:
    h = {
        "User-Agent": UA,
        "Referer": referer,
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    ck = _cookie_header()
    if ck:
        h["Cookie"] = ck
    return h


def fetch_info(url: str) -> dict:
    """用 pyktok 获取作品信息，返回统一格式的 dict

    pyktok 的 TikTokApi 会用 Playwright 起浏览器执行 JS 签名，产出
    a_bogus / X-Bogus，这是 yt-dlp 目前做不到的部分。

    pyktok 不可用时（未安装 / 无 Playwright 内核）不能直接失败——
    必须继续走 _scrape_mobile 抓移动端分享页，那条路只要 Cookie 对就能出数据。
    """
    real = _resolve_short(url)
    cookies = _cookie_dict()

    # pyktok 缺失属于「兜底手段不可用」，不是「抖音解析失败」：
    # 记下来继续往下走，最终报错时才把它作为原因说明一并给出。
    sign_err = ""
    try:
        detail = _pyktok_detail(real, cookies)
    except DouyinError as e:
        detail, sign_err = None, str(e)

    if not detail:
        detail, err = _scrape_mobile(real)
        if not detail:
            raise DouyinError(err or sign_err or "pyktok 未返回作品数据")
        return _normalize(detail)
    return _normalize(detail)


def _pyktok_detail(url: str, cookies: dict) -> dict | None:
    """用 pyktok 生成 X-Bogus 签名，再由本进程发 HTTP 请求拿详情 JSON

    不走 pyktok 的 make_request：它在页面上下文里 fetch 跨域接口会被 CORS 拦掉
    （Page.evaluate: Failed to fetch）。这里只用 generate_x_bogus 做签名，
    请求自己发，行为与 yt-dlp 抓包一致。

    注意 pyktok 默认导航 tiktok.com（国内不可达），必须用 starting_url 指向抖音。

    pyktok 是可选依赖（requirements 里列了但服务器常默认不装）。导入必须放在
    try 内：否则 `ModuleNotFoundError` 会在异常处理之外抛出，
    既给不出可读提示，也拦不下本该走的 _scrape_mobile 兜底。
    """
    import asyncio

    ck_list = [{"name": k, "value": v, "domain": ".douyin.com", "path": "/"}
               for k, v in cookies.items()]
    vid = ""
    m = re.search(r"/(?:video|note)/(\d+)", url) or re.search(r"modal_id=(\d+)", url)
    if m:
        vid = m.group(1)
    if not vid:
        raise DouyinError("无法从链接中解析出作品 ID")

    api_url = ("https://www.douyin.com/aweme/v1/web/aweme/detail/"
               f"?aweme_id={vid}&device_platform=webapp&aid=6383"
               "&channel=channel_pc_web&version_code=190500&version_name=19.5.0"
               "&cookie_enabled=true&platform=PC&browser_language=zh-CN"
               "&msToken=&a_bogus=")

    try:
        from pyktok import TikTokApi
    except Exception as e:
        raise DouyinError(_friendly_sign_error(e)) from e

    # 关键校验：`pyktok` 这个名字在 PyPI 上被两个包占用。
    #   -真 pyktok 0.0.31（支持抖音）依赖的是另一个包
    #   - `pyktok` 里的 `from TikTokApi import TikTokApi` 会去装**海外版** TikTokApi 7.x
    # 装错时 `from pyktok import TikTokApi` 依然成功（拿到的是海外版类），
    # 但它内部硬编码 tiktok.com，国内网络必然30s 超时—— 表现为"兜底已启用却永远失败"，
    # 比直接报错更浪费时间。这里提前识别并快速失败。
    if not _pyktok_supports_douyin(TikTokApi):
        raise DouyinError(cookie_hint())

    async def sign() -> dict:
        api = TikTokApi(logging_level=40)
        try:
            await api.create_sessions(
                headless=True,
                num_sessions=1,
                starting_url="https://www.douyin.com/",
                cookies=ck_list or None,
            )
            return await api.generate_x_bogus(api_url)
        finally:
            try:
                await api.stop_playwright()
            except Exception:
                pass

    try:
        signed = asyncio.run(sign())
    except Exception as e:
        raise DouyinError(_friendly_sign_error(e)) from e

    params = {k: v for k, v in (signed or {}).items()
              if k.lower() in ("x-bogus", "a_bogus", "ms-token", "mstoken")
              and v not in (None, "")}
    if not params:
        raise DouyinError(f"pyktok 未生成可用签名参数（返回 {signed}）")

    qs = urllib.parse.urlencode(params)
    full = f"{api_url}&{qs}"

    import requests

    try:
        r = requests.get(full, headers=_header_for(url, "https://www.douyin.com/"),
                         timeout=(10, 30))
        data = r.json()
    except Exception as e:
        raise DouyinError(f"详情接口请求失败：{e}") from e
    return data if isinstance(data, dict) else None


def _scrape_mobile(url: str) -> tuple[dict | None, str]:
    """从 www.iesdouyin.com/share/ 页面里抠出 RENDER_DATA / _ROUTER_DATA"""
    import requests

    vid = ""
    m = re.search(r"/(?:video|note)/(\d+)", url) or re.search(r"modal_id=(\d+)", url)
    if m:
        vid = m.group(1)
    if not vid:
        return None, "无法从链接中解析出作品 ID"

    target = f"https://www.iesdouyin.com/share/video/{vid}/"
    try:
        r = requests.get(target, headers=_header_for(target, "https://www.iesdouyin.com/"),
                         timeout=(10, 25))
        html = r.text or ""
    except Exception as e:
        return None, f"分享页请求失败：{e}"

    for pat, parser in (
        (r'<script id="RENDER_DATA"[^>]*>(.*?)</script>', "uri"),
        (r'<script id="_ROUTER_DATA"[^>]*>(.*?)</script>', "plain"),
    ):
        m = re.search(pat, html, re.S)
        if not m:
            continue
        raw = m.group(1)
        try:
            if parser == "uri":
                from urllib.parse import unquote
                raw = unquote(raw)
            data = json.loads(raw)
        except Exception:
            continue
        item = (data.get("loaderData") or {}) if parser == "uri" else data
        if parser == "plain":
            item = (data.get("loaderData") or {}).get(
                f"video_(id)/page", {}) or {}
        d = ((item or {}).get("videoInfoRes") or {}).get("item_list") or []
        if d:
            return d[0], ""

    # 两个数据脚本都没有，但响应体很长 —— 这是抖音的 JS 挑战页
    # （`_$jsvmprt` 虚拟机），意味着请求被当成爬虫拦下了。
    # 直接说"需要 Cookie"比"可能已删除"有用得多。
    if "_$jsvmprt" in html or "__ac_signature" in html:
        return None, cookie_hint()

    return None, "分享页未返回作品数据（可能需要登录或已删除）"


def _normalize(item: dict) -> dict:
    video = item.get("video") or {}
    duration = int(video.get("duration") or item.get("duration") or 0) / 1000.0
    author = item.get("author") or {}
    return {
        "video_id": str(item.get("aweme_id") or item.get("awemeId") or ""),
        "title": (item.get("desc") or "").strip() or "未命名视频",
        "uploader": (author.get("nickname") or "").strip(),
        "description": (item.get("desc") or "")[:1200],
        "duration": duration,
        "webpage_url": f"https://www.douyin.com/video/{item.get('aweme_id') or ''}",
        "thumbnail": ((item.get("video") or {}).get("cover") or {}).get("url_list", [""])[0],
        "_direct_url": _pick_url(item),
        "_origin": "pyktok",
    }


def _friendly_sign_error(e: Exception) -> str:
    """把 Playwright / 依赖缺失的长篇报错压缩成一句能读懂的话"""
    msg = str(e)
    # pyktok 本身没装：requirements 里是可选依赖，服务器常默认不装
    if isinstance(e, ImportError) and "pyktok" in msg.lower():
        return ("抖音兜底不可用：服务器未安装 pyktok。"
                "如需启用请执行 `pip install pyktok` 并"
                "`python -m playwright install --with-deps chromium`；"
                "否则请依靠 yt-dlp 解析（抖音 Cookie 已配好即可）。")
    if isinstance(e, ModuleNotFoundError) and "pyktok" in msg.lower():
        return ("抖音兜底不可用：服务器未安装 pyktok。"
                "如需启用请执行 `pip install pyktok` 并"
                "`python -m playwright install --with-deps chromium`；"
                "否则请依靠 yt-dlp 解析（抖音 Cookie 已配好即可）。")
    if "playwright install" in msg and "Executable doesn't exist" in msg:
        return ("抖音兜底失败：服务器未安装 Playwright 浏览器内核。"
                "如需在服务端启用抖音兜底，请执行 `playwright install --with-deps chromium`；"
                "否则请更新 Cookie 或改用yt-dlp 解析。")
    if "Executable doesn't exist" in msg:
        return "抖音兜底失败：缺少 Playwright 浏览器内核，请执行 playwright install chromium"
    return f"pyktok 签名失败：{type(e).__name__}: {msg[:200]}"


def download(url: str, workdir: Path,
             progress: Callable[[float, str], None] | None = None) -> dict:
    """下载抖音视频到 workdir，返回与yt-dlp 路径一致的 meta dict"""
    import requests

    def emit(pct: float, msg: str) -> None:
        if progress:
            progress(pct, msg)

    emit(3.0, "解析抖音作品信息（pyktok）")
    meta = fetch_info(url)
    direct = meta.pop("_direct_url")
    referer = _referer(meta.get("webpage_url") or url)

    workdir.mkdir(parents=True, exist_ok=True)
    out = workdir / "video.mp4"

    emit(12.0, "下载视频文件")
    with requests.get(direct, headers=_header_for(direct, referer),
                      stream=True, timeout=(30, 300)) as r:
        if r.status_code >= 400:
            raise DouyinError(f"视频下载失败（HTTP {r.status_code}），可能 Cookie 已过期")
        total = int(r.headers.get("Content-Length") or 0)
        got = 0
        tmp = out.with_suffix(".part")
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 18):
                if not chunk:
                    continue
                f.write(chunk)
                got += len(chunk)
                if total:
                    emit(12.0 + min(got / total * 78, 78.0), "下载中")
        if total and got < total * 0.9:
            tmp.unlink(missing_ok=True)
            raise DouyinError(f"下载不完整（{got}/{total} 字节）")
        tmp.replace(out)

    emit(92.0, "解析音频用于转写")
    from . import audio as _audio

    _audio.extract_audio(out, workdir / "audio.mp3")

    meta["video_path"] = str(out)
    meta["audio_path"] = str(workdir / "audio.mp3")
    meta["parser"] = "pyktok"
    emit(100.0, "下载完成")
    return meta