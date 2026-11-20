"""视频查重：同一个作品只下载/转写一次，命中已有记录就直接复用并跳转。

为什么不能只比 URL：抖音分享出来的永远是**短链**（v.douyin.com/9NbpTr1zTTc），
同一条视频每次分享生成的短链都可能不同，而作品 ID（aweme_id / BV 号）唯一。
所以主键是「作品 ID」，URL 规范化比对只作兜底（短链解析失败时用）。

判定规则：
- 只对抖音 / B站生效（本地文件上传走 platform=local，不参与查重）；
- failed / canceled 的任务不算命中——用户就是想重来；
- pending / running 优先命中（正在处理时再贴同一链接，应该跳过去看进度）；
- 其余按「有转写/成稿优先 + 时间新」排序，跳到信息最全的那条记录。

已实测的真实重复：061117971747 与 e6878eca2846 是同一条抖音
（aweme_id 7692317364544752911），当时被重复下载并转写了两次。
"""
from __future__ import annotations

import re
import time
from typing import Any
from urllib.parse import urlsplit

from . import store

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# 这些状态不参与命中：失败/取消的任务允许重新创建
SKIP_STATUS = frozenset({"failed", "canceled"})
# 正在进行中的任务优先命中，让用户看到进度而不是又开一份
ACTIVE_STATUS = frozenset({"pending", "running"})

# 从 URL 里直接抠作品 ID（长链不需要任何网络请求）
_ID_PATTERNS = {
    "douyin": (
        re.compile(r"/(?:share/)?(?:video|note|slides)/(\d{8,})"),
        re.compile(r"[?&#](?:modal_id|aweme_id|item_ids)=(\d{8,})"),
    ),
    "bilibili": (
        re.compile(r"/(BV[0-9A-Za-z]{10})(?![0-9A-Za-z])"),
        re.compile(r"[?&]bvid=(BV[0-9A-Za-z]{10})"),
        re.compile(r"/av(\d+)", re.IGNORECASE),
    ),
}

# 只有这些域名需要跟随重定向才能拿到作品 ID
_SHORT_HOSTS = {
    "douyin": ("v.douyin.com",),
    "bilibili": ("b23.tv", "bili2233.cn"),
}

# 短链解析结果缓存：url -> (作品ID, 时间戳)
_id_cache: dict[str, tuple[str, float]] = {}
_CACHE_TTL = 3600.0
_CACHE_MAX = 512


def id_from_url(url: str, platform: str) -> str:
    """从链接里抠作品 ID；抠不到返回空串（不联网）"""
    pats = _ID_PATTERNS.get(platform)
    if not pats or not url:
        return ""
    for p in pats:
        m = p.search(url)
        if m:
            return m.group(1)
    return ""


def _follow(url: str, timeout: float) -> str:
    """跟随重定向，只取最终 URL（不下载页面正文）。

    ★逐跳复检（SSRF）：短链第一跳是公网域名不代表落点安全，
    302 跳到 169.254/内网地址同样要拦。统一走 ssrf.safe_final_url，
    它对每一跳 Location 都重做公网校验，任一跳不合法即放弃解析
    （查重只是增强功能，放弃后还有 URL 规范化比对兜底）。
    """
    try:
        from .ssrf import safe_final_url

        return safe_final_url(url, timeout=timeout)
    except Exception:
        return ""


def resolve_id(url: str, platform: str, timeout: float = 6.0) -> str:
    """拿到作品 ID：长链直接抠，短链跟随重定向后再抠；失败返回空串

    这是查重里唯一的网络动作，超时/失败都不影响任务创建——
    外面还有 URL 规范化比对兜底。
    """
    vid = id_from_url(url, platform)
    if vid:
        return vid
    hosts = _SHORT_HOSTS.get(platform)
    if not hosts or not url:
        return ""
    try:
        host = (urlsplit(url).hostname or "").lower()
    except Exception:
        return ""
    if not any(host == h or host.endswith("." + h) for h in hosts):
        return ""

    now = time.time()
    hit = _id_cache.get(url)
    if hit and now - hit[1] < _CACHE_TTL:
        return hit[0]

    final = _follow(url, timeout)
    vid = id_from_url(final, platform) if final else ""
    if vid:
        if len(_id_cache) >= _CACHE_MAX:
            _id_cache.clear()
        _id_cache[url] = (vid, now)
    return vid


def _key_url(url: str) -> str:
    """URL 规范化：去掉协议、www、查询串（分享参数每次都不同）、尾部斜杠"""
    s = (url or "").strip()
    if not s:
        return ""
    try:
        p = urlsplit(s)
        host = (p.hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]
        path = re.sub(r"/+$", "", p.path or "")
        return f"{host}{path}".lower()
    except Exception:
        return s.lower()


def _task_ids(t: store.Task) -> set[str]:
    """任务身上能拿到的所有作品 ID（meta 键名各通道不统一，全收）"""
    meta = t.meta or {}
    ids: set[str] = set()
    for k in ("id", "video_id", "aweme_id", "bvid", "aid"):
        v = str(meta.get(k) or "").strip()
        if v:
            ids.add(v)
    for u in (t.url, str(meta.get("webpage_url") or "")):
        v = id_from_url(str(u or ""), t.platform)
        if v:
            ids.add(v)
    return ids


def _task_keys(t: store.Task) -> set[str]:
    """任务的规范化 URL 集合（原始链接 + 解析后的规范链接）"""
    keys = set()
    for u in (t.url, str((t.meta or {}).get("webpage_url") or "")):
        k = _key_url(str(u or ""))
        if k:
            keys.add(k)
    return keys


def find_reusable(url: str, platform: str, timeout: float = 6.0,
                  vid: str | None = None) -> tuple[Any, str]:
    """查重：返回 (命中的任务, 命中依据 "id"/"url")；没命中返回 (None, "")

    vid 可由调用方预先解析好传进来（把可能要走网络的重定向放到锁外做，
    避免一个慢请求把其它任务的创建也堵住）。
    """
    if not url:
        return None, ""
    # 抖音/B站按作品 ID 精确去重；其它平台（yt-dlp 支持的站）退化为 URL 比对
    by_id_supported = platform in _ID_PATTERNS

    if vid is None:
        try:
            vid = resolve_id(url, platform, timeout=timeout) if by_id_supported else ""
        except Exception:
            vid = ""
    key = _key_url(url)

    by_id: list[store.Task] = []
    by_url: list[store.Task] = []
    try:
        tasks = store.list_tasks()
    except Exception:
        return None, ""

    for t in tasks:
        if t.platform != platform or t.status in SKIP_STATUS:
            continue
        if vid and vid in _task_ids(t):
            by_id.append(t)
        elif key and key in _task_keys(t):
            by_url.append(t)

    hits = by_id or by_url
    if not hits:
        return None, ""

    active = [t for t in hits if t.status in ACTIVE_STATUS]
    if active:
        return active[0], "id" if by_id else "url"

    # 优先跳到信息最全的那条：有成稿 > 有转写 > 时间新
    hits.sort(key=lambda t: (bool((t.note or "").strip()),
                             bool((t.transcript or "").strip()),
                             t.created_at), reverse=True)
    return hits[0], "id" if by_id else "url"


def summary(t: store.Task) -> str:
    """给前端/日志用的一句话描述"""
    meta = t.meta or {}
    title = str(meta.get("title") or "").strip()
    if not title:
        title = f"记录 {t.id}"
    return f"{title}（{t.status}）"
