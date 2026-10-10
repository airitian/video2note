"""dlpanda.com 解析接口客户端

为什么需要它
------------
抖音对服务器 IP 风控极严：分享页返回 `_$jsvmprt` JS 虚拟机挑战脚本，
yt-dlp 报 "Fresh cookies are needed"，而补上 uifid 后错误变成
"Signature Not Found" —— 说明缺的是 `a_bogus` 动态签名，
伪造会被判`Sign Invalid`。IP 风控发生在业务代码执行之前，
拿不到真签名。

dlpanda 提供了一条**已解决该问题的通道**：解析由它完成，
我们只拿它返回的无水印 CDN 直链。

协议要点（实测所得，非猜测）
----------------------------
1. 端点按平台分开，抖音是 ``/zh-CN/douyin``（不是首页 /zh-CN）。
   从 ``window.downloadRoutes`` 可确认douyin 走独立路径。
2. POST multipart 字段：``_token``（CSRF，随会话变）+ ``url`` + ``t0ken``
   （隐藏域里的固定值，实测为``b8b6c49aToTA``，两个平台一致）。
3. 响应 ``Accept: text/html``，**不是 JSON** —— 用 ``application/json``
   会拿不到数据（只有反馈表单才吃 JSON）。
4. 视频地址在 ``data-download-url="..."`` 属性和
   ``<video><source src="...">`` 里，两处都有，协议相对路径需补 ``https:``。
5. 结果状态看 ``data-state="..."``：success / unsupported /
   private / rate_limited / security_check / timeout / network_error …

架构取舍
--------
**解析**这一步必须走真实浏览器（Playwright）。实测纯 HTTP（curl_cffi
带完整浏览器指纹 + 全套Cookie 含cf_clearance）仍被 Cloudflare 403，
CF 的 clearance 绑定了浏览器指纹细节，无法用脚本伪造。

但**下载**那一步不需要浏览器：返回的地址是抖音官方 CDN
（``*.zjcdn.com`` / ``*.douyinstatic.com``），带 ``access-control-allow-origin: *``，
普通 HTTP 直接下即可。

所以正确分工是：**浏览器只用来解析，下载走轻量 HTTP**，
不把38MB 视频塞进浏览器上下文 —— 实测那样会因默认 30s 超时而失败。
"""
from __future__ import annotations

import html as _html
import re
from dataclasses import dataclass, field
from pathlib import Path

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36")

BASE = "https://dlpanda.com"
# 平台 -> 解析路径（取自 window.downloadRoutes）
PLATFORM_PATH = {
    "douyin": "/zh-CN/douyin",
    "bilibili": "/zh-CN/bilibili",
}

# data-state -> 给人看的错误文案。
# technicalCode来自站点自带的 state catalog，语义比 HTTP 状态码可靠。
STATE_ERROR = {
    "unsupported": "该站点不支持这种链接格式（只支持公开的抖音分享链接）",
    "private": "视频为私密 / 已删除 / 受限，无法解析",
    "rate_limited": "解析请求过于频繁，被站点限流，请稍后重试",
    "security_check": "站点要求人工安全验证（Turnstile），暂时无法自动解析",
    "timeout": "站点解析超时，请重试",
    "network_error": "站点自身网络请求失败",
    "server_error": "站点服务端出错，请稍后重试",
}

_T0KEN_RE = re.compile(r'id="token"\s+value="([^"]+)"')
_TOKEN_RE = re.compile(r'name="_token"\s+value="([^"]+)"')
_STATE_RE = re.compile(r'data-state="([^"]+)"')
_VIDEO_SRC_RE = re.compile(r"<video.*?<source\s+src=\"([^\"]+)\"", re.S)
_AUDIO_SRC_RE = re.compile(r"<audio.*?<source\s+src=\"([^\"]+)\"", re.S)
_DOWNLOAD_URL_RE = re.compile(r'data-download-url="([^"]+)"')
_TITLE_RE = re.compile(r'<h2[^>]*>(.*?)</h2>', re.S)


class DlpandaError(RuntimeError):
    """解析失败。message 已可直接展示给用户。"""


@dataclass
class ParseResult:
    """一次解析的结果。video_url 为 None 表示只有音频（图文作品）。"""
    title: str = ""
    video_url: str | None = None
    audio_url: str | None = None
    cover_url: str | None = None
    author: str = ""
    aweme_id: str = ""
    extra: dict = field(default_factory=dict)


def _abs(url: str) -> str:
    """协议相对地址补全。CDN 返回的正是这种形式。"""
    url = _html.unescape(url.strip())
    if url.startswith("//"):
        return "https:" + url
    return url


def _strip_tags(s: str) -> str:
    return _html.unescape(re.sub(r"<[^>]+>", "", s)).strip()


def parse_html(page_html: str) -> ParseResult:
    """从解析响应 HTML 中提取结果。

    单独拆出来是为了能脱离浏览器单测 —— 解析逻辑与浏览器解耦后，
    可以直接喂保存下来的 HTML 验证，不必每次起浏览器。
    """
    state_m = _STATE_RE.search(page_html)
    state = state_m.group(1) if state_m else ""

    if state != "success":
        hint = STATE_ERROR.get(state, f"站点返回未知状态：{state or '空'}")
        raise DlpandaError(hint)

    video_m = _VIDEO_SRC_RE.search(page_html)
    download_m = _DOWNLOAD_URL_RE.search(page_html)
    raw_video = video_m.group(1) if video_m else (
        download_m.group(1) if download_m else None
    )
    audio_m = _AUDIO_SRC_RE.search(page_html)

    title_m = _TITLE_RE.search(page_html)
    title = _strip_tags(title_m.group(1)) if title_m else ""

    # 作者与作品 ID 在结果区的 chip 标签里
    chips = [_strip_tags(x) for x in
             re.findall(r'<span class="border border-black/20[^"]*">(.*?)</span>',
                        page_html, re.S)]
    author = chips[0] if chips else ""
    aweme_id = next((c for c in chips if c.isdigit()), "")

    cover_m = re.search(r'data-result-thumbnail[^>]*>.*?<img[^>]+src="([^"]+)"',
                        page_html, re.S)

    if not raw_video and not audio_m:
        raise DlpandaError("站点返回成功但未给出媒体地址（页面结构可能已变更）")

    return ParseResult(
        title=title,
        video_url=_abs(raw_video) if raw_video else None,
        audio_url=_abs(audio_m.group(1)) if audio_m else None,
        cover_url=_abs(cover_m.group(1)) if cover_m else None,
        author=author,
        aweme_id=aweme_id,
        extra={"state": state},
    )


def resolve(url: str, platform: str = "douyin", timeout_ms: int = 45000) -> ParseResult:
    """用真实浏览器解析，返回媒体直链。

    必须走浏览器的原因见模块 docstring：Cloudflare 的 cf_clearance
    与浏览器指纹绑定，纯 HTTP 无法伪造。
    """
    path = PLATFORM_PATH.get(platform)
    if not path:
        raise DlpandaError(f"dlpanda 暂不支持 {platform}")

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise DlpandaError("未安装 playwright，无法使用 dlpanda 解析通道") from e

    page_html = ""
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled",
                  "--no-sandbox", "--disable-dev-shm-usage"],
        )
        try:
            ctx = browser.new_context(user_agent=UA, locale="zh-CN",
                                      viewport={"width": 1440, "height": 900})
            pg = ctx.new_page()
            # CF 挑战需要真实浏览器执行 JS 才能放行
            pg.goto(BASE + path, wait_until="domcontentloaded",
                    timeout=timeout_ms)
            try:
                pg.wait_for_function(
                    "() => !!document.querySelector('[name=\"_token\"]')",
                    timeout=8000,
                )
            except Exception:
                pass                      # 页面结构变了，后面的取值会给出明确错误

            if "Attention Required" in (pg.title() or ""):
                raise DlpandaError("被Cloudflare 拦截（站点要求人工验证）")

            token_m = pg.evaluate(
                "() => document.querySelector('[name=\"_token\"]')?.value || ''")
            t0_m = pg.evaluate(
                "() => document.querySelector('#token')?.value || ''")
            if not token_m:
                raise DlpandaError("未能取得会话令牌（站点结构可能已变更）")

            page_html = pg.evaluate(
                """async (a) => {
                    const fd = new FormData();
                    fd.append('_token', a.token);
                    fd.append('url', a.url);
                    fd.append('t0ken', a.t0);
                    const r = await fetch(a.path, {
                        method: 'POST',
                        body: fd,
                        headers: {
                            'Accept': 'text/html',
                            'X-Requested-With': 'XMLHttpRequest'
                        },
                        credentials: 'same-origin'
                    });
                    return await r.text();
                }""",
                {"token": token_m, "url": url, "t0": t0_m, "path": path},
            )
        finally:
            browser.close()

    if not page_html:
        raise DlpandaError("解析请求未返回内容")
    return parse_html(page_html)


def download(url: str, dest: Path, referer: str = BASE + "/",
             progress=None, chunk: int = 1 << 18) -> Path:
    """下载 CDN 直链到本地。

    这一步不需要浏览器：抖音 CDN 带 `access-control-allow-origin: *`
    且支持 Range（实测返回 206），普通 HTTP 即可。

    做了断点续传：抖音视频常有几十 MB，中断后重跑不用从头下。
    """
    if not url:
        raise DlpandaError("没有可下载的视频地址")

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")

    try:
        from curl_cffi import requests as cr

        session = cr.Session(impersonate="chrome136", verify=False)
        get = session.get
    except ImportError:
        import requests

        session = requests.Session()
        get = session.get

    headers = {"Referer": referer, "User-Agent": UA}
    have = tmp.stat().st_size if tmp.exists() else 0

    if have:
        headers["Range"] = f"bytes={have}-"
        if progress:
            progress(20.0, f"断点续传（已有 {have / 1048576:.1f} MB）")

    total = 0
    try:
        resp = get(url, headers=headers, timeout=(20, 300), stream=True)
        resp.raise_for_status()

        # 服务端若忽略 Range 回了 200，就得从零重写，否则会拼出坏文件
        resuming = resp.status_code == 206
        if have and not resuming:
            have = 0

        cr_ = resp.headers.get("content-range") or ""
        if resuming and "/" in cr_:
            total = int(cr_.rsplit("/", 1)[1])
        else:
            total = int(resp.headers.get("content-length") or 0)
            have = 0

        mode = "ab" if resuming else "wb"
        got = have
        last_pct = -5.0
        with open(tmp, mode) as f:
            for part in resp.iter_content(chunk):
                if not part:
                    continue
                f.write(part)
                got += len(part)
                if progress and total:
                    pct = got * 100.0 / total
                    if pct - last_pct >= 5:
                        last_pct = pct
                        progress(20.0 + pct * 0.75,
                                 f"下载中 {got / 1048576:.1f}/{total / 1048576:.1f} MB")
    except Exception as e:
        raise DlpandaError(f"下载失败：{e}") from e

    if got <= 0:
        tmp.unlink(missing_ok=True)
        raise DlpandaError("下载内容为空")

    # 声明了大小就必须对得上，否则文件是截断的，交给 ASR 只会得到空结果
    if total and got < total:
        raise DlpandaError(
            f"下载不完整（{got}/{total} 字节），可重试续传")

    tmp.replace(dest)
    if progress:
        progress(95.0, f"下载完成 {got / 1048576:.1f} MB")
    return dest


def download_video(url: str, workdir: Path, max_height: int = 1080,
                   progress=None) -> dict:
    """解析 + 下载 + 抽音频，返回与 _ytdlp_download 一致的 meta dict。

    这样上层 pipeline 无需关心视频是从哪条通道拿到的。
    """
    from . import audio

    workdir.mkdir(parents=True, exist_ok=True)
    result = resolve(url, platform="douyin")

    if not result.video_url:
        raise DlpandaError("该作品是图文，没有视频轨")

    if progress:
        progress(8.0, f"解析成功：{result.title[:40]}")

    out = workdir / f"source{_ext_for(result.video_url)}"
    download(result.video_url, out, progress=progress)

    # 与 yt-dlp 路径保持一致：确保浏览器可预览，再抽音频给 ASR
    playable = audio.ensure_playable(out, max_height)
    audio_path = workdir / "audio.mp3"
    try:
        audio.extract_audio(playable, audio_path)
    except Exception as e:
        raise DlpandaError(f"视频无可用音轨，无法转写：{e}") from e

    if progress:
        progress(100.0, "下载完成")

    return {
        "title": result.title or "抖音视频",
        "uploader": result.author,
        "id": result.aweme_id,
        "webpage_url": url,
        "extractor": "dlpanda",
        "audio_url": result.audio_url or "",
        "cover_url": result.cover_url or "",
        "video_codec": audio.video_codec(playable) or "",
        "video_path": str(playable),
        "audio_path": str(audio_path),
    }


def _ext_for(url: str) -> str:
    if ".mp4" in url.lower():
        return ".mp4"
    if ".webm" in url.lower():
        return ".webm"
    if ".mov" in url.lower():
        return ".mov"
    return ".mp4"


def available() -> bool:
    """运行环境是否具备该通道的依赖。"""
    try:
        import playwright  # noqa: F401
    except ImportError:
        return False
    try:
        from curl_cffi import requests  # noqa: F401
    except ImportError:
        return False
    return True