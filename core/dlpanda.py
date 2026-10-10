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

# Chromium 启动参数。业务解析与健康检查必须用同一份——曾因两处各写一份
# 而出现反向误报：健康检查少传 --disable-dev-shm-usage，服务器 /dev/shm
# 默认仅 64MB（容器与部分云主机都是），Chromium 一启动就崩，
# health 报「不可用」而业务其实能用。
# --no-sandbox：以 root 运行时 Chromium 沙箱（user namespace）
#              不可用，必须关掉；普通用户去掉也无妨。
# --disable-dev-shm-usage：共享内存改用 /tmp，绕开 64MB 上限。
_LAUNCH_ARGS = ["--disable-blink-features=AutomationControlled",
                "--no-sandbox", "--disable-dev-shm-usage"]


def _exe() -> str:
    """当前解释器的绝对路径。

    给用户的修复命令里不能写死 `python`：Debian/Ubuntu 默认只有 python3
    （PEP 394 规范），写死等于给出一条必然 command not found 的建议。
    用 sys.executable 可确保命令指向的正是跑服务的那个环境
    （浏览器内核必须装进同一个解释器，装到别处等于没装）。
    """
    import sys
    return sys.executable or "python3"

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


def _load_template() -> dict | None:
    """读取用户配置的 curl 模板；没有或解析失败则回退内置路由。

    用户模板的价值在于：站点改了 token 字段名时，不用等我们发版。
    """
    try:
        from .config import load_settings

        raw = (load_settings().get("resolver_curl") or "").strip()
    except Exception:
        return None
    if not raw:
        return None
    try:
        from . import curlparse

        tpl = curlparse.parse_curl(raw)
        # 抖音强制走独立路由：用户从首页抓的包会指向 /zh-CN，
        # 但那个路由对抖音无效（实测首页解析拿不到作品数据）
        tpl["path"] = PLATFORM_PATH["douyin"]
        return tpl
    except Exception:
        return None


def _cookies_to_pairs(cookie_str: str) -> list[dict]:
    """把 'a=1; b=2' 转成 Playwright 需要的 [{name, value}]"""
    out = []
    for part in cookie_str.split(";"):
        k, _, v = part.partition("=")
        k, v = k.strip(), v.strip()
        if k and v:
            out.append({"name": k, "value": v})
    return out


def resolve(url: str, platform: str = "douyin", timeout_ms: int = 45000) -> ParseResult:
    """用真实浏览器解析，返回媒体直链。

    必须走浏览器的原因见模块 docstring：Cloudflare 的 cf_clearance
    与浏览器指纹绑定，纯 HTTP 无法伪造。

    支持用户自定义 curl 模板（设置项「抖音解析请求模板」）：
    模板里的 _token / t0ken 等动态字段一律用页面实时值替换，
    只有 url 换成用户提交的链接。
    """
    path = PLATFORM_PATH.get(platform)
    if not path:
        raise DlpandaError(f"dlpanda 暂不支持 {platform}")

    tpl = _load_template() if platform == "douyin" else None

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise DlpandaError("未安装 playwright，无法使用 dlpanda 解析通道") from e

    page_html = ""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=_LAUNCH_ARGS)
        try:
            ctx = browser.new_context(user_agent=UA, locale="zh-CN",
                                      viewport={"width": 1440, "height": 900})
            # 用户模板里的 Cookie 注入浏览器上下文 —— 这是让请求带上
            # cf_clearance 的唯一有效方式（脚本裸发一律被 403）
            if tpl and tpl.get("cookies"):
                try:
                    ctx.add_cookies(_cookies_to_pairs(tpl["cookies"]))
                except Exception:
                    pass          # 域名不匹配等，忽略：模板 Cookie 未必完整

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

            # 动态字段一律取页面实时值，不信模板里的旧值
            live = pg.evaluate(
                """() => {
                    const get = (s) => document.querySelector(s)?.value || '';
                    return {_token: get('[name="_token"]'),
                            t0ken: get('#token'),
                            csrf: get('[name="csrf-token"]')};
                }""")
            if not live.get("_token"):
                raise DlpandaError("未能取得会话令牌（站点结构可能已变更）")

            form = dict((tpl or {}).get("form") or {})
            for key in curl_dyn_keys():
                if key in form and live.get(_dyn_source(key)):
                    form[key] = live[_dyn_source(key)]
            if not form.get("_token"):
                form["_token"] = live["_token"]
            if not form.get("t0ken"):
                form["t0ken"] = live.get("t0ken", "")
            # url 永远用用户提交的那条
            form["url"] = url

            post_path = (tpl or {}).get("path", path)
            page_html = pg.evaluate(
                """async (a) => {
                    const fd = new FormData();
                    for (const [k, v] of Object.entries(a.form)) {
                        if (v !== '' && v != null) fd.append(k, v);
                    }
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
                {"form": form, "path": post_path},
            )
        finally:
            browser.close()

    if not page_html:
        raise DlpandaError("解析请求未返回内容")
    return parse_html(page_html)


def curl_dyn_keys() -> tuple[str, ...]:
    from .curlparse import DYNAMIC_FIELDS

    return DYNAMIC_FIELDS


def _dyn_source(key: str) -> str:
    """动态字段名 -> 页面实时值来源

    模板里叫 _token / token / csrf_token / authenticity_token 的都映射到
    同一个实时令牌；t0ken 是站点自己的隐藏域，单独取。
    """
    if key == "t0ken":
        return "t0ken"
    return "_token"


def probe(url: str, referer: str | None = None) -> tuple[bool, str]:
    """探测地址能否下载，只取前 64KB。返回 (可用, 说明)。

    Referer 不是可选项：实测 douyinvod.com 域的地址不带Referer 直接 403，
    带上就正常 206。所以先试无 Referer，失败再带 Referer 复测一次——
    否则会把「能用」误判成「失效」，进而错误地降级到音频。
    """
    if not url:
        return False, "地址为空"

    try:
        from curl_cffi import requests as cr

        def fetch(h):
            return cr.get(url, headers=h, impersonate="chrome136",
                          timeout=(10, 45), verify=False)
    except ImportError:
        import requests

        def fetch(h):
            return requests.get(url, headers=h, timeout=(10, 45), stream=False)

    last = ""
    for ref in ((None, referer) if referer else (None,)):
        h = {"User-Agent": UA, "Range": "bytes=0-65535"}
        if ref:
            h["Referer"] = ref
        try:
            r = fetch(h)
        except Exception as e:
            last = f"{type(e).__name__}: {str(e)[:60]}"
            continue
        # 状态码非 2xx 一律视为不可用。这里必须显式判断：
        # curl_cffi 遇到 403 不会抛异常，只看有无异常会把 403 误判成可用。
        if not (200 <= r.status_code < 300):
            last = (f"HTTP {r.status_code}"
                    f"（{(r.headers.get('content-type') or '')[:28]}）")
            continue
        body = r.content
        if not body:
            last = "响应体为空"
            continue
        cr_ = r.headers.get("content-range", "")
        total = (cr_.rsplit("/", 1)[1] if "/" in cr_
                 else r.headers.get("content-length") or "?")
        tag = "（补 Referer 后可用）" if ref else ""
        return True, (f"HTTP {r.status_code} | 实收 {len(body)} 字节 "
                      f"| 声明 {total} {tag}")
    return False, last


def pick_media(video_url: str | None, audio_url: str | None,
               referer: str | None = None) -> tuple[str | None, str | None, str]:
    """按「视频优先，不可用则退音频」挑选可下载的媒体。

    返回 (类型, 地址, 说明)；类型为 None 表示两者都不可用。
    抖音地址在多数情况下需Referer，默认用抖音站点作为来源。
    """
    ref = referer or "https://www.douyin.com/"
    vfail = "无视频地址"
    if video_url:
        ok, info = probe(video_url, ref)
        if ok:
            return "VIDEO", video_url, info
        vfail = info
    afail = "无音频地址"
    if audio_url:
        ok, info = probe(audio_url, ref)
        if ok:
            return "AUDIO", audio_url, f"{info}（视频不可用：{vfail}）"
        afail = info
    return None, None, f"视频: {vfail} / 音频: {afail}"


def download(url: str, dest: Path, referer: str = "https://www.douyin.com/",
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

    if progress:
        progress(8.0, f"解析成功：{result.title[:40]}")

    # 视频优先；不可用时自动退到音频 —— 图文作品、或视频地址失效
    # （实测有地址会因缺 Referer 返回 403）都不该让整个任务失败。
    kind, media_url, info = pick_media(result.video_url, result.audio_url)
    if progress:
        label = "视频" if kind == "VIDEO" else "音频"
        progress(10.0, f"{label}可用：{info}")
    if not media_url:
        raise DlpandaError(f"没有可下载的媒体。{info}")

    out = workdir / f"source{_ext_for(media_url)}"
    download(media_url, out, progress=progress)

    audio_path = workdir / "audio.mp3"
    if kind == "VIDEO":
        # 与 yt-dlp 路径保持一致：确保浏览器可预览，再抽音频给 ASR
        playable = audio.ensure_playable(out, max_height)
        try:
            audio.extract_audio(playable, audio_path)
        except Exception as e:
            raise DlpandaError(f"视频无可用音轨，无法转写：{e}") from e
    else:
        # 已经是音频，直接转成 ASR 要的格式
        playable = out
        try:
            audio.extract_audio(out, audio_path)
        except Exception as e:
            raise DlpandaError(f"音频转码失败：{e}") from e

    if progress:
        progress(100.0, "下载完成")

    return {
        "title": result.title or "抖音视频",
        "uploader": result.author,
        "id": result.aweme_id,
        "webpage_url": url,
        "extractor": "dlpanda",
        "audio_only": "1" if kind == "AUDIO" else "",
        "media_note": info,
        "video_url": media_url,
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


def browser_ready() -> tuple[bool, str]:
    """Chromium 内核是否真的装好了。

    为什么不能只看 import：`pip install playwright` 只装 Python 包，
    浏览器二进制是另一套东西，默认落在 ~/.cache/ms-playwright/。
    两者独立，服务器上「装了库没装内核」是最常见的坑，
    表现为一启动就报
        BrowserType.launch: Executable doesn't exist at ...
        /root/.cache/ms-playwright/chromium-XXXX/chrome-linux/chrome
    而`import playwright` 依然成功——不实际启动一次就分辨不出来。

    这里真启动一个 headless 实例再关掉，多花不到一秒，
    但能把「看着正常、一用就炸」提前暴露在健康检查里。

    启动参数必须与业务侧chromium.launch 处保持一致，否则会出现
    「健康检查说不可用、业务其实能用」的反向误报：服务器 /dev/shm
    默认只有 64MB，缺 --disable-dev-shm-usage 时 Chromium 一启动就崩，
    报的还是一句含糊的「Target page, context or browser has been closed」。
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False, f"未安装 playwright 库（{_exe()} -m pip install playwright）"
    try:
        with sync_playwright() as p:
            b = p.chromium.launch(headless=True, args=_LAUNCH_ARGS)
            b.close()
        return True, ""
    except Exception as e:
        first = str(e).strip().splitlines()[0]
        if "Executable doesn't exist" in first or "playwright install" in first:
            # 不能写死 "python"：容器里通常只有 python3，写死等于
            # 给出一条必然 command not found 的建议。用当前解释器，
            # 确保命令指向的正是跑服务的那个环境。
            return False, ("已装 playwright 库但缺 Chromium 内核，执行："
                           f"{_exe()} -m playwright install chromium"
                           "（内核与 Python 包是两套独立的东西，"
                           "必须装到跑服务的这个解释器里）")
        # 这句含糊的报错实际常见于两类问题，不分类用户就只能瞎试：
        # 1) 内核与playwright 库版本对不上；2) 系统库缺失
        if "has been closed" in first or "Target closed" in first:
            return False, ("Chromium 内核已装但启动即崩溃（" + first + "）。"
                           f"多为两类原因：①内核与 playwright 库版本不匹配，"
                           f"执行 {_exe()} -m playwright install --force chromium；"
                           f"②系统库缺失，执行 "
                           f"{_exe()} -m playwright install --with-deps chromium")
        return False, f"Chromium 启动失败：{first}"


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