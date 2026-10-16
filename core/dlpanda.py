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
**解析**这一步必须走真实浏览器。实测纯 HTTP（curl_cffi
带完整浏览器指纹 + 全套Cookie 含cf_clearance）仍被 Cloudflare 403，
CF 的 clearance 绑定了浏览器指纹细节，无法用脚本伪造。
另有一条**直连抖音**通道（_resolve_direct）：不经过任何第三方解析站，
用浏览器过抖音自己的 JS 挑战，再用 CDP 拦截 aweme/detail 响应拿直链。
dlpanda.com 托管在 Cloudflare 后面，国内 VPS 常常整体不可达，此时它是唯一活路。
（驱动用 Selenium + 系统 Chromium。早期版本用 Playwright，但本部署是
 Alpine + Python 3.14，PyPI 上没有对应 wheel，且 Docker Hub 基础镜像
 在国内镜像源上全部拉不下来，故改用 Selenium。）

但**下载**那一步不需要浏览器：返回的地址是抖音官方 CDN
（``*.zjcdn.com`` / ``*.douyinstatic.com``），带 ``access-control-allow-origin: *``，
普通 HTTP 直接下即可。

所以正确分工是：**浏览器只用来解析，下载走轻量 HTTP**，
不把38MB 视频塞进浏览器上下文 —— 实测那样会因默认 30s 超时而失败。
"""
from __future__ import annotations

import html as _html
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36")

# Chromium 启动参数。业务解析与健康检查必须用同一份——曾因两处各写一份
# 而出现反向误报：健康检查少传 --disable-dev-shm-usage，服务器 /dev/shm
# 默认仅 64MB（容器与部分云主机都是），Chromium 一启动就崩，
# health 报「不可用」而业务其实能用。
# --no-sandbox：以 root 运行时 Chromium 沙箱（user namespace）
#              不可用，必须关掉；普通用户去掉也无妨。
# --disable-dev-shm-usage：共享内存改用 /tmp，绕开 64MB 上限。
_LAUNCH_ARGS = ["--disable-blink-features=AutomationControlled",
                "--no-sandbox", "--disable-dev-shm-usage",
                "--accept-lang=zh-CN"]


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


class SlowCDNError(DlpandaError):
    """下载速度过低，应切换更快的源（备用地址 / 重新解析拿新边缘）。

    不是「下载失败」——连接是通的、能拿到数据，只是边缘在限速。
    交由 download_video 做回退，而不是当成硬错误抛给用户。
    """


# 慢 CDN 自动回退阈值：下载开头探一段，若实测吞吐低于此值就中断并换源。
# 抖音快边缘实测 24 MB/s、慢边缘 0.5 MB/s，2 MB/s 留足余量仍能拦住慢边缘。
SLOW_CDN_THRESHOLD_MBPS = 2.0
# 探速窗口：至少下这么多字节、且耗时超过这么久，才开始判断，避免抖动误判。
SLOW_CDN_MIN_PROBE_BYTES = 2 << 20      # 2 MB
SLOW_CDN_MIN_PROBE_SECONDS = 1.5        # 1.5 s
# 回退总次数上限（video -> audio -> 重新解析×N），防止边缘全慢时无限重试。
SLOW_CDN_MAX_FALLBACKS = 3


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



def _chromedriver_exe():
    # 定位 chromedriver。Alpine 的 chromium-chromedriver 包把它放在
    # /usr/bin/chromedriver（软链到 /usr/lib/chromium/chromedriver）。
    from shutil import which
    p = which("chromedriver")
    if p:
        return p
    for c in ("/usr/bin/chromedriver", "/usr/lib/chromium/chromedriver"):
        if os.path.exists(c):
            return c
    return None


def _new_driver(timeout_ms: int = 45000):
    # 起一个无头 Chromium（Selenium 驱动）。
    # 分工与旧 Playwright 方案一致：浏览器只负责过 Cloudflare 挑战并发起
    # 同源 POST；下载那一步仍走 curl_cffi，不把视频塞进浏览器。
    import tempfile
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service

    binary = _chromium_exe()
    if not binary:
        raise DlpandaError("未找到 Chromium 浏览器，执行：apk add chromium")
    drv = _chromedriver_exe()
    if not drv:
        raise DlpandaError("未找到 chromedriver，执行：apk add chromium-chromedriver")

    opts = Options()
    opts.binary_location = binary
    for a in _LAUNCH_ARGS:
        opts.add_argument(a)
    opts.add_argument("--headless=new")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--lang=zh-CN")
    opts.add_argument("--window-size=1440,900")
    # root 下 Chromium 对 user-data-dir 有归属保护，每次给独立临时目录
    opts.add_argument("--user-data-dir=" + tempfile.mkdtemp(prefix="v2n-chrome-"))
    opts.add_argument("--user-agent=" + UA)
    try:
        opts.add_experimental_option("excludeSwitches", ["enable-automation"])
        opts.add_experimental_option("useAutomationExtension", False)
    except Exception:
        pass
    # performance 日志是 CDP 拦截详情响应的前提，没有它 get_log 拿不到东西
    try:
        opts.set_capability("goog:loggingPrefs", {"performance": "ALL"})
    except Exception:
        pass

    # eager：等 DOMContentLoaded 就返回，不等图片/广告等慢资源。
    # _token 是服务端渲染的隐藏域，DOM 就绪时早已在页面里；
    # 而 dlpanda 页面挂着第三方统计，load 事件能拖几十秒。
    opts.page_load_strategy = "eager"
    driver = webdriver.Chrome(service=Service(drv), options=opts)
    driver.set_page_load_timeout(max(20, int(timeout_ms / 1000)))
    driver.set_script_timeout(60)
    _harden_fingerprint(driver)
    return driver


# 浏览器指纹加固（抖音 webmssdk 风控对抗，2026-10-16 实测有效）。
#
# webmssdk 会采集 navigator/screen/WebGL/hardware 一整套环境指纹做评分，
# headless + Linux + 800x600 + en-US 的组合基本必被拦截：表现为
# aweme/detail 返回 200 但 content-length:0，页面一直「视频数据加载中」。
# 下面这套伪装让它看起来就是一台普通的中文 Windows 台式机。
#
# 注意两个顺序问题：
#   1) addScriptToEvaluateOnNewDocument 必须在导航前调用，页面每个新文档
#      （含 iframe）加载前都会跑，navigator 伪装才赶得上 webmssdk 采集；
#   2) setDeviceMetricsOverride 才能改 screen.width/height——Chromium 的
#      --window-size 只改窗口可视区，screen.* 仍是 headless 默认 800x600，
#      而风控读的正是 screen.*。
_HARDEN_JS = """
(function(){
  try { Object.defineProperty(navigator, 'webdriver', {get: () => undefined}); } catch(e) {}
  try { Object.defineProperty(navigator, 'platform', {get: () => 'Win32'}); } catch(e) {}
  try {
    Object.defineProperty(navigator, 'language', {get: () => 'zh-CN'});
    Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN', 'zh']});
  } catch(e) {}
  try { Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => 8}); } catch(e) {}
  try { Object.defineProperty(navigator, 'deviceMemory', {get: () => 8}); } catch(e) {}
  try {
    var orig = WebGLRenderingContext.prototype.getParameter;
    WebGLRenderingContext.prototype.getParameter = function(p) {
      if (p === 37445) return 'Google Inc. (Intel)';
      if (p === 37446) return 'ANGLE (Intel, Intel(R) UHD Graphics Direct3D11 vs_5_0 ps_5_0)';
      return orig.call(this, p);
    };
  } catch(e) {}
  try {
    if (window.WebGL2RenderingContext) {
      var o2 = WebGL2RenderingContext.prototype.getParameter;
      WebGL2RenderingContext.prototype.getParameter = function(p) {
        if (p === 37445) return 'Google Inc. (Intel)';
        if (p === 37446) return 'ANGLE (Intel, Intel(R) UHD Graphics Direct3D11 vs_5_0 ps_5_0)';
        return o2.call(this, p);
      };
    }
  } catch(e) {}
})();
"""


def _harden_fingerprint(driver) -> None:
    # 两项都做：JS 层改 navigator/WebGL，CDP 层改 screen 分辨率。
    # 任何一项失败都不阻塞解析（只是风控分低一点）。
    try:
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument",
                               {"source": _HARDEN_JS})
    except Exception:
        pass
    try:
        driver.execute_cdp_cmd("Emulation.setDeviceMetricsOverride", {
            "width": 1920, "height": 1080, "deviceScaleFactor": 1,
            "mobile": False, "screenWidth": 1920, "screenHeight": 1080,
            "positionX": 0, "positionY": 0})
    except Exception:
        pass



# ---- 通道主机可达性探测（带缓存，避免每次解析都现测）----
# 为什么需要：dlpanda.com 在 Cloudflare 后面，国内不少 VPS 到不了
# （TCP 通但 TLS 挂死，或直接超时）。不探测就会在一条死路上耗掉
# 几十秒才失败，白白拖慢任务。探测结果缓存 5 分钟，与浏览器探测同量级。
_probe_cache: dict = {}
_PROBE_TTL = 300.0


def _https_ok(u: str, timeout: float = 4.0) -> bool:
    # 用 curl_cffi 做一次真实 HTTPS 探测：拿到任何 HTTP 状态码都算可达，
    # 只有连接/TLS 超时才判不可达。Cloudflare 被墙时表现正是后者。
    import time as _t
    key = u
    now = _t.monotonic()
    c = _probe_cache.get(key)
    if c and now - c[0] < _PROBE_TTL:
        return c[1]
    ok = False
    try:
        from curl_cffi import requests as cr
        r = cr.get(u, headers={"User-Agent": UA}, impersonate="chrome136",
                   timeout=(timeout, timeout), verify=False)
        ok = r.status_code < 500
    except Exception:
        ok = False
    _probe_cache[key] = (now, ok)
    return ok


def _dy_reachable() -> bool:
    return _https_ok("https://www.douyin.com/")


def _dlpanda_reachable() -> bool:
    return _https_ok("https://dlpanda.com/zh-CN/douyin")


def _resolve_direct(url: str, timeout_ms: int = 60000,
                    attempts: int = 3) -> ParseResult:
    # 直连抖音：真实浏览器过 JS 挑战落地视频页，CDP 拦截详情接口拿直链。
    # 只依赖国内可达主机（www.douyin.com / *.douyinvod.com）。
    #
    # 为什么要重试：抖音风控是概率性的——同一浏览器同一链接，有时直接
    # 放行，有时 aweme/detail 返回 200 空 body（页面一直「视频数据加载中」，
    # 实测指纹不伪装时稳定复现）。换一个全新浏览器会话（新 user-data-dir、
    # 新 cookie、新的指纹随机量）命中放行窗口的几率明显更高。
    last_err = None
    for i in range(max(1, attempts)):
        d = _new_driver(timeout_ms)
        seen = set()
        try:
            try:
                d.execute_cdp_cmd("Network.enable", {})
            except Exception:
                pass
            try:
                d.get(url)
            except TimeoutException:
                pass          # eager 下重定向可能还没跑完，靠下面轮询兜底
            # 分享链接会 302 到 /video/<aweme_id>；轮询等落地
            deadline = time.time() + 30
            while time.time() < deadline:
                if "/video/" in (d.current_url or ""):
                    break
                time.sleep(1.0)
            # 落地后 SPA 才发详情 XHR。轮询等真数据，不要只睡几秒——
            # 详情接口回来的早晚不稳定（实测 3~13s 都出现过）。
            body = None
            poll_deadline = time.time() + 25
            while time.time() < poll_deadline:
                body = _grab_detail_json(d, seen)
                if body:
                    return _parse_detail(body)
                time.sleep(1.0)
            last_err = DlpandaError("未能捕获视频详情数据（页面结构可能已变更或被风控）")
        except DlpandaError:
            raise
        except Exception as e:
            last_err = DlpandaError("直连解析失败：%s" % str(e).splitlines()[0][:120])
        finally:
            try:
                d.quit()
            except Exception:
                pass
        # 换会话重试；间隔几秒让风控窗口轮转
        if i + 1 < max(1, attempts):
            time.sleep(3.0)
    raise last_err or DlpandaError("直连解析失败")


def _grab_detail_json(d, seen=None) -> str | None:
    # 从 performance 日志里找 aweme/detail 的响应体。
    # 必须显式开 goog:loggingPrefs=performance，否则 get_log 拿不到东西。
    # seen：已取过体的 requestId 集合（轮询时避免反复 getResponseBody）。
    import json as _json
    if seen is None:
        seen = set()
    try:
        logs = d.get_log("performance")
    except Exception:
        return None
    for e in logs:
        try:
            msg = _json.loads(e["message"])["message"]
        except Exception:
            continue
        if msg.get("method") != "Network.responseReceived":
            continue
        p = msg.get("params", {})
        if "aweme/v1/web/aweme/detail" not in p.get("response", {}).get("url", ""):
            continue
        rid = p.get("requestId")
        if not rid or rid in seen:
            continue
        seen.add(rid)
        try:
            r = d.execute_cdp_cmd("Network.getResponseBody", {"requestId": rid})
        except Exception:
            continue
        b = r.get("body") or ""
        if r.get("base64Encoded"):
            import base64
            try:
                b = base64.b64decode(b).decode("utf-8", "replace")
            except Exception:
                continue
        if "aweme_detail" in b:
            return b
    return None


def _parse_detail(body: str) -> ParseResult:
    # 详情 JSON -> ParseResult。字段路径来自实测响应：
    # aweme_detail.video.play_addr.url_list / bit_rate[].play_addr
    # aweme_detail.music.play_url.url_list（图文作品只有音频）
    import json as _json
    try:
        data = _json.loads(body)
    except Exception as e:
        raise DlpandaError("视频详情数据解析失败：%s" % e)
    detail = data.get("aweme_detail") or {}
    if not detail:
        raise DlpandaError("详情响应中没有 aweme_detail（可能需登录或已删除）")

    def _urls(addr):
        return [u for u in ((addr or {}).get("url_list") or [])
                if isinstance(u, str) and u.startswith("http")]

    video = detail.get("video") or {}
    video_url = None
    cands = _urls(video.get("play_addr"))
    if not cands:
        # play_addr 缺失时按清晰度档位兜底
        for br in video.get("bit_rate") or []:
            cands = _urls((br or {}).get("play_addr"))
            if cands:
                break
    if cands:
        video_url = cands[0]

    audio_url = None
    au = _urls((detail.get("music") or {}).get("play_url"))
    if au:
        audio_url = au[0]

    cover_url = None
    for c in (video.get("cover") or {}, video.get("origin_cover") or {}):
        cu = _urls(c)
        if cu:
            cover_url = cu[0]
            break

    if not video_url and not audio_url:
        raise DlpandaError("详情中没有可播放的媒体地址（可能需登录或已删除）")

    return ParseResult(
        title=(detail.get("desc") or "").strip(),
        video_url=video_url,
        audio_url=audio_url,
        cover_url=cover_url,
        author=((detail.get("author") or {}).get("nickname") or "").strip(),
        aweme_id=str(detail.get("aweme_id") or ""),
        extra={"state": "success", "channel": "direct"},
    )


def resolve(url: str, platform: str = "douyin", timeout_ms: int = 45000,
            prefer: str | None = None) -> ParseResult:
    # 解析分享链接，返回媒体直链。
    #
    # 通道优先级（prefer 可强制指定 "direct" / "dlpanda"）：
    #   1. direct  浏览器直连抖音：过 JS 挑战 + CDP 拦截详情接口
    #   2. dlpanda dlpanda.com 解析接口
    # 按「主机可达性」自动排序：dlpanda.com 在 Cloudflare 后面，
    # 国内 VPS 常常到得了抖音却到不了 dlpanda，此时直连才是唯一活路。
    if platform != "douyin":
        raise DlpandaError(f"dlpanda 暂不支持 {platform}")

    order = [prefer] if prefer in ("direct", "dlpanda") else []
    if not order:
        dy_ok = _dy_reachable()
        dl_ok = _dlpanda_reachable()
        if dy_ok and dl_ok:
            order = ["direct", "dlpanda"]
        elif dy_ok:
            order = ["direct"]
        elif dl_ok:
            order = ["dlpanda"]
        else:
            raise DlpandaError("解析通道均不可达：本机既连不上抖音站，"
                               "也连不上 dlpanda 解析站")

    last = None
    for ch in order:
        try:
            if ch == "direct":
                return _resolve_direct(url, timeout_ms)
            return _resolve_dlpanda(url, timeout_ms)
        except Exception as e:
            last = e
            continue
    raise last if last else DlpandaError("解析失败")


def _resolve_dlpanda(url: str, platform: str = "douyin",
                     timeout_ms: int = 45000) -> ParseResult:
    # dlpanda 解析接口通道（原 resolve 实现）。
    #
    # 必须走浏览器的原因见模块 docstring：Cloudflare 的 cf_clearance
    # 与浏览器指纹绑定，纯 HTTP 无法伪造。
    #
    # 支持用户自定义 curl 模板（设置项「抖音解析请求模板」）：
    # 模板里的 _token / t0ken 等动态字段一律用页面实时值替换，
    # 只有 url 换成用户提交的链接。
    path = PLATFORM_PATH.get(platform)
    if not path:
        raise DlpandaError(f"dlpanda 暂不支持 {platform}")

    tpl = _load_template() if platform == "douyin" else None

    from selenium.common.exceptions import TimeoutException, WebDriverException
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait

    driver = _new_driver(timeout_ms)
    try:
        # 模板 Cookie 必须先注入再加载解析页：selenium 的 add_cookie
        # 要求当前已在该域名下，所以先开一个同域页面。
        if tpl and tpl.get("cookies"):
            try:
                driver.get(BASE + "/")
                for ck in _cookies_to_pairs(tpl["cookies"]):
                    try:
                        driver.add_cookie(ck)
                    except Exception:
                        pass
            except Exception:
                pass

        try:
            driver.get(BASE + path)
        except TimeoutException:
            raise DlpandaError("解析页面加载超时（站点可能被风控或网络不通）")

        # 等会话令牌出现；等不到也不要立刻失败，下面会给出明确错误
        try:
            WebDriverWait(driver, 25).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, '[name="_token"]')))
        except Exception:
            pass

        title = driver.title or ""
        if "Attention Required" in title or "Just a moment" in title:
            raise DlpandaError("被 Cloudflare 拦截（站点要求人工验证）")

        # 动态字段一律取页面实时值，不信模板里的旧值
        live = driver.execute_script(
            "const g = (s) => (document.querySelector(s) || {}).value || '';"
            "return {_token: g('[name=\"_token\"]'),"
            "        t0ken: g('#token'),"
            "        csrf: g('[name=\"csrf-token\"]')};")
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
        # 在浏览器上下文里同源 POST，带上 CF 放行后的 Cookie
        page_html = driver.execute_async_script(
            "const a = arguments[0];"
            "const done = arguments[arguments.length - 1];"
            "const fd = new FormData();"
            "for (const [k, v] of Object.entries(a.form)) {"
            "  if (v !== '' && v != null) fd.append(k, v);"
            "}"
            "fetch(a.path, {method: 'POST', body: fd,"
            "  headers: {'Accept': 'text/html', 'X-Requested-With': 'XMLHttpRequest'},"
            "  credentials: 'same-origin'})"
            "  .then(r => r.text())"
            "  .then(t => done(t))"
            "  .catch(e => done('FETCH_ERR:' + (e && e.message ? e.message : e)));",
            {"form": form, "path": post_path})
    except DlpandaError:
        raise
    except TimeoutException:
        raise DlpandaError("解析请求在浏览器内超时")
    except WebDriverException as e:
        raise DlpandaError("浏览器驱动出错：%s" % str(e).splitlines()[0][:120])
    except Exception as e:
        raise DlpandaError("解析失败：%s" % e)
    finally:
        try:
            driver.quit()
        except Exception:
            pass

    if not page_html:
        raise DlpandaError("解析请求未返回内容")
    if isinstance(page_html, str) and page_html.startswith("FETCH_ERR:"):
        raise DlpandaError("解析请求失败：%s" % page_html[10:130])
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
             progress=None, chunk: int = 1 << 18,
             slow_threshold_mbps: float = SLOW_CDN_THRESHOLD_MBPS,
             min_probe_bytes: int = SLOW_CDN_MIN_PROBE_BYTES,
             min_probe_seconds: float = SLOW_CDN_MIN_PROBE_SECONDS) -> Path:
    """下载 CDN 直链到本地。

    这一步不需要浏览器：抖音 CDN 带 `access-control-allow-origin: *`
    且支持 Range（实测返回 206），普通 HTTP 即可。

    做了断点续传：抖音视频常有几十 MB，中断后重跑不用从头下。

    慢 CDN 探测：开头探一段（min_probe_bytes / min_probe_seconds），
    若实测吞吐低于 slow_threshold_mbps，删除本次残片并抛 SlowCDNError，
    交由 download_video 切换更快的源（抖音边缘限速是随机的，换源常能翻盘）。
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
        t0 = time.perf_counter()
        probed = False
        slow_hit = False
        slow_msg = ""
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
                # 慢 CDN 探速：探窗满足后才判断一次，避免开头抖动误杀快边缘
                if not probed and got >= min_probe_bytes \
                        and (time.perf_counter() - t0) >= min_probe_seconds:
                    probed = True
                    speed = got / (time.perf_counter() - t0) / 1e6
                    if speed < slow_threshold_mbps:
                        # 先退出 with 关掉文件句柄，再删残片——
                        # Windows 下不能删除仍被本进程打开的文件，否则会抛
                        # WinError 32 而非 SlowCDNError，回退链就接不住了。
                        # 残片属于当前被限速的边缘，换源后无法续传，必须清掉。
                        slow_hit = True
                        slow_msg = (f"下载速度过慢 {speed:.2f} MB/s（阈值 "
                                    f"{slow_threshold_mbps:.1f}），切换更快源")
                        break
        if slow_hit:
            tmp.unlink(missing_ok=True)
            raise SlowCDNError(slow_msg)
    except SlowCDNError:
        raise
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

    下载阶段带慢 CDN 自动回退：抖音边缘限速是随机的（同一条链接实测
    快边缘 24 MB/s、慢边缘 0.5 MB/s），抽中慢边缘会让 24MB 下 48 秒。
    download() 开头探速，过慢就抛 SlowCDNError，这里按
    「video_url -> audio_url -> 重新解析拿新边缘」依次回退，
    最多 SLOW_CDN_MAX_FALLBACKS 次，仍全慢才当硬错误。
    """
    from . import audio

    workdir.mkdir(parents=True, exist_ok=True)
    result = resolve(url, platform="douyin")

    if progress:
        progress(8.0, f"解析成功：{result.title[:40]}")

    out = workdir / "source.mp4"

    # 候选源：视频优先，audio_url 作为兜底（可能落在不同 CDN 边缘）。
    # 全部慢时再走 re_resolve 拿 fresh URLs（新边缘）。每条都重新 pick_media
    # 探测可用性，跳过 403/失效的地址。
    def _candidates():
        yield result.video_url, "VIDEO"
        if result.audio_url:
            yield result.audio_url, "AUDIO"
        # 重新解析：同一条链接换解析时机可能落到更快边缘
        for _ in range(SLOW_CDN_MAX_FALLBACKS):
            try:
                r2 = resolve(url, platform="douyin")
            except Exception:
                return
            if r2.video_url:
                yield r2.video_url, "VIDEO"
            if r2.audio_url:
                yield r2.audio_url, "AUDIO"

    used_kind = None
    used_url = None
    last_err = None
    attempts = 0
    for cand_url, cand_kind in _candidates():
        if not cand_url:
            continue
        attempts += 1
        if attempts > SLOW_CDN_MAX_FALLBACKS:
            break
        # 切换源前清掉上一个被限速边缘留下的残片，避免 download 误判续传
        out.with_suffix(out.suffix + ".part").unlink(missing_ok=True)
        try:
            # 视频/音频用各自探测（带 Referer 复测），不可用则跳过本候选
            kind, media_url, info = pick_media(
                cand_url if cand_kind == "VIDEO" else None,
                cand_url if cand_kind == "AUDIO" else None)
            if not media_url:
                last_err = f"{cand_kind} 源不可用：{info}"
                continue
            used_kind = kind
            used_url = media_url
            if progress:
                label = "视频" if kind == "VIDEO" else "音频"
                tag = f"（回退第{attempts}次）" if attempts > 1 else ""
                progress(10.0, f"{label}可用{tag}：{info}")
            # 修正扩展名（不同候选可能是 .mp4 / .webm / .mov）
            real_out = workdir / f"source{_ext_for(media_url)}"
            if real_out != out:
                out.with_suffix(out.suffix + ".part").unlink(missing_ok=True)
                out = real_out
            download(media_url, out, progress=progress)
            last_err = None
            break
        except SlowCDNError as e:
            last_err = str(e)
            if progress:
                progress(12.0, f"源过慢，切换更快地址（{attempts}/{SLOW_CDN_MAX_FALLBACKS}）")
            continue
        except DlpandaError as e:
            last_err = str(e)
            continue

    if last_err is not None and used_url is None:
        raise DlpandaError(f"没有可下载的媒体。{last_err}")

    audio_path = workdir / "audio.mp3"
    if used_kind == "VIDEO":
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
        "audio_only": "1" if used_kind == "AUDIO" else "",
        "media_note": "",
        "video_url": used_url if used_kind == "VIDEO" else "",
        "audio_url": used_url if used_kind == "AUDIO" else "",
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


def _chromium_exe():
    # 定位 Chromium 可执行文件（只查文件系统，不启动浏览器）。
    # Alpine 的 chromium 包装在 /usr/lib/chromium/chrome；
    # 也保留 playwright 的安装位置，便于将来换回该部署形态。
    for c in ("/usr/lib/chromium/chrome", "/usr/bin/chromium",
              "/usr/bin/chromium-browser", "/usr/bin/google-chrome",
              "/usr/bin/google-chrome-stable"):
        if os.path.exists(c):
            return c
    root = os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or os.path.expanduser(
        "~/.cache/ms-playwright")
    try:
        entries = sorted(Path(root).glob("chromium*"))
    except OSError:
        return None
    for d in entries:
        for rel in ("chrome-linux/chrome", "chrome-linux64/chrome"):
            p = d / rel
            if p.exists():
                return str(p)
    return None


def _browser_ready_uncached() -> tuple[bool, str]:
    # 真启动一次 Chromium 确认可用。慢，只在缓存失效时调用。
    try:
        import selenium  # noqa: F401
    except ImportError:
        return False, "未安装 selenium，执行：/opt/venv/bin/pip install selenium"
    if not _chromedriver_exe():
        return False, "未找到 chromedriver，执行：apk add chromium-chromedriver"
    if not _chromium_exe():
        return False, "未找到 Chromium 内核，执行：apk add chromium"
    try:
        d = _new_driver(timeout_ms=15000)
        try:
            d.get("about:blank")
        finally:
            try:
                d.quit()
            except Exception:
                pass
        return True, ""
    except Exception as e:
        msg = str(e).strip() or type(e).__name__
        first = msg.splitlines()[0]
        return False, "Chromium 启动失败：%s" % first[:160]


# 探测结果缓存：Chromium 内核装没装、能不能起来，在进程生命周期内
# 几乎不变。每次请求都真启动一次浏览器是最贵的做法——健康检查被前端
# 放在页面启动路径上，于是「打开页面」直接变成「等 Chromium 起一次」，
# 内核有问题时更要等满 Playwright 的 30 秒超时。
_browser_cache: tuple[float, tuple[bool, str]] | None = None
_BROWSER_TTL = 300.0          # 成功结果缓存 5 分钟
_BROWSER_TTL_FAIL = 30.0      # 失败结果缓存短一些，便于装完内核尽快恢复
_browser_lock = threading.Lock()


def browser_ready(max_age: float | None = None) -> tuple[bool, str]:
    """Chromium 内核是否真的可用（带缓存）。

    为什么要有缓存：健康检查在页面启动路径上，每次都真启动浏览器要
    1~3 秒，内核损坏时更会挂到超时——用户看到的是「网页打不开」，
    而实际卡在一个跟网页毫无关系的后台探测上。内核状态在几分钟内
    不会变，缓存完全够用；真要立刻复查用 max_age=0。

    线程安全：并发首个请求会真正探测，其余请求等同一个结果，
    不会一起启动 N 个 Chromium（那会互相抢 CPU，反而更慢）。
    """
    global _browser_cache
    now = time.monotonic()
    ttl = max_age
    if ttl is None:
        c = _browser_cache
        if c is not None:
            ok = c[1][0]
            ttl = _BROWSER_TTL if ok else _BROWSER_TTL_FAIL
            if now - c[0] < ttl:
                return c[1]
    with _browser_lock:
        # 双检：等锁期间可能已被别的线程探测完
        c = _browser_cache
        if c is not None and ttl is not None and time.monotonic() - c[0] < ttl:
            return c[1]
        val = _browser_ready_uncached()
        _browser_cache = (time.monotonic(), val)
        return val


def available() -> bool:
    # 运行环境是否具备该通道的依赖。
    try:
        import selenium  # noqa: F401
    except ImportError:
        return False
    try:
        from curl_cffi import requests  # noqa: F401
    except ImportError:
        return False
    if not _chromedriver_exe() or not _chromium_exe():
        return False
    return True
