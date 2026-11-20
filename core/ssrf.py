"""SSRF 防护：用户提交的 URL 在发起任何出站请求前先做公网校验。

过去建任务只校验 startswith("http")，于是 http://169.254.169.254/（云元
数据）、http://127.0.0.1:xxxx/（本机其它服务）都能被当作「视频链接」
提交，服务端会替攻击者去请求内网，错误信息还会经任务 error 回显，
等于一个现成的内网探测器。这里统一收口：

- 只允许 http / https；
- 主机名 DNS 解析后，任一地址落入回环/私网/链路本地/保留/组播段即拒绝
  （含 169.254.169.254、localhost、0.0.0.0）；
- 跟随重定向时逐跳复检（短链第一跳合法、第二跳跳进内网同样拦）。

本地开发若确需访问内网地址（如自建解析服务），可设
V2N_ALLOW_PRIVATE_URLS=1 整体放行（默认关，生产不要开）。
"""
from __future__ import annotations

import ipaddress
import os
import socket
from urllib.parse import urljoin, urlsplit

_BLOCKED_HOSTNAMES = {"localhost"}
_BLOCKED_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home")


def _allow_private() -> bool:
    return (os.getenv("V2N_ALLOW_PRIVATE_URLS") or "").strip() == "1"


def _ip_blocked(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True     # 解析不出合法 IP 的一律按危险处理
    return (addr.is_loopback or addr.is_private or addr.is_link_local
            or addr.is_reserved or addr.is_multicast or addr.is_unspecified)


def validate_public_url(url: str) -> str:
    """校验 URL 可安全出站访问，返回原 URL；不合法抛 ValueError（带原因）。"""
    u = (url or "").strip()
    if not u:
        raise ValueError("URL 为空")
    parts = urlsplit(u)
    if parts.scheme not in ("http", "https"):
        raise ValueError(f"只允许 http/https 链接，收到协议：{parts.scheme or '(无)'}")
    host = (parts.hostname or "").strip().lower()
    if not host:
        raise ValueError("URL 缺少主机名")
    if _allow_private():
        return u
    if host in _BLOCKED_HOSTNAMES or host.endswith(_BLOCKED_SUFFIXES):
        raise ValueError(f"不允许访问本机/内网主机：{host}")
    # 主机名本身就是 IP 字面量时直接判，省一次解析
    try:
        ipaddress.ip_address(host)
        is_literal = True
    except ValueError:
        is_literal = False
    if is_literal:
        if _ip_blocked(host):
            raise ValueError(f"不允许访问内网/保留地址：{host}")
        return u
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except OSError:
        raise ValueError(f"无法解析主机名：{host}")
    ips = {info[4][0] for info in infos if info and info[4]}
    if not ips:
        raise ValueError(f"无法解析主机名：{host}")
    for ip in ips:
        if _ip_blocked(ip):
            raise ValueError(
                f"主机 {host} 解析到内网/保留地址 {ip}，已拒绝（防 SSRF）")
    return u


def safe_final_url(url: str, timeout: float = 6.0, max_hops: int = 5) -> str:
    """逐跳跟随重定向并复检每一跳，返回最终 URL；任何一跳不合法/失败返回 ""。

    供短链解析（dedupe）使用：短链本身合法不代表落点合法，
    302 到内网地址同样必须拦下。
    """
    import requests

    current = validate_public_url(url)
    for _ in range(max_hops + 1):
        try:
            r = requests.get(current, allow_redirects=False, stream=True,
                             timeout=timeout,
                             headers={"User-Agent": "Mozilla/5.0"})
        except Exception:
            return ""
        try:
            if r.status_code in (301, 302, 303, 307, 308):
                loc = r.headers.get("Location") or ""
                if not loc:
                    return current
                nxt = urljoin(current, loc)
                try:
                    current = validate_public_url(nxt)
                except ValueError:
                    return ""
                continue
            return str(r.url or current)
        finally:
            r.close()
    return current
