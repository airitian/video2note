"""浏览器抓包 curl 文本 -> 可直接复用的请求模板

背景
----
解析接口常带 CSRF token / Cookie / 动态指纹（如 dlpanda 的 `_token`、
`t0ken`、`cf_clearance`）。这些每次会话都会变，手工填既容易错又没法维护。
让用户直接粘贴浏览器「复制为 cURL」的内容，程序负责拆解与替换。

能力边界（重要）
----------------
这里只做**解析与模板填充**，不发请求。原因是浏览器抓包里的指纹类
请求头（Cookie / sec-ch-ua / User-Agent）绑在真实浏览器会话上，
脱离浏览器发送会被 Cloudflare 之类的风控直接 403 —— 实测
curl_cffi + 完整 `cf_clearance` 仍然拿不到 dlpanda 的页面。

所以正确用法是：程序用这些参数**驱动真实浏览器**，
让浏览器自己带上这些头与 Cookie，而不是脚本裸发。
"""
from __future__ import annotations

import re
import shlex
from urllib.parse import unquote

# curl 抓包里这些头由浏览器自动生成，手工带上反而会与真实指纹冲突
_BROWSER_OWNED = {
    "host", "connection", "content-length", "sec-fetch-dest", "sec-fetch-mode",
    "sec-fetch-site", "sec-fetch-user", "upgrade-insecure-requests", "te",
    "accept-encoding",
}

# 这些头即使写着也应丢弃 —— 它们描述的是「发起请求的客户端」，
# 而我们是在浏览器内发请求，用浏览器自己的即可
_DROP_ALWAYS = {
    "referer", "origin", "cookie", "user-agent", "sec-ch-ua",
    "sec-ch-ua-mobile", "sec-ch-ua-platform", "accept",
}

#匹配 Content-Disposition 行里的 name="..."
# 不能用$ 锚定：切分后该行后面还跟着字段值
_FORM_RE = re.compile(r'name="([^"]+)"')
_LINE_CONT = re.compile(r"\\\s*\n")

# zsh/bash 的 ANSI-C 引用 $'...' 里，转义序列此时仍是字面量
# （shlex 不展开它），必须自己解，否则 \r\n 会被当成普通字符。
_ESCAPES = {
    r"\r": "\r", r"\n": "\n", r"\t": "\t", r"\0": "\0",
    r"\'": "'", r'\"': '"', r"\\": "\\", r"\a": "\a", r"\b": "\b",
    r"\f": "\f", r"\v": "\v",
}


def _unescape_ansi(s: str) -> str:
    """展开 ANSI-C 转义（只处理 \\r \\n \\t 等，不动其他反斜杠序列）

    不完整展开是有意的：URL 里的 ``\\`` 与 ``\\%`` 等不应被改写。
    """
    if "\\" not in s:
        return s
    return re.sub(r"\\([rntabfv0'\"\\\\])",
                  lambda m: _ESCAPES["\\" + m.group(1)], s)


def parse_curl(text: str) -> dict:
    """把浏览器「复制为 cURL」解析成结构化请求。

    支持：多行续行（反斜杠）、单行、引号包裹、`-H`/`--header`/`-b`/`--cookie`/
    `--data-raw`/`--data`/`-d`/`--url`、以及 `$'...'` ANSI-C 形式的 body。

    返回：{"url": str, "method": str, "headers": {...}, "form": {字段: 值}}
    未识别的部分不会报错，静默忽略 —— 抓包里常有大量无关头。
    """
    if not text or not text.strip():
        raise ValueError("内容为空，请粘贴浏览器「复制为 cURL」的结果")

    body_text = _LINE_CONT.sub(" ", text).strip()
    try:
        tokens = shlex.split(body_text, posix=True)
    except ValueError:
        # zsh 里的 $'...' 含真实换行，shlex 会失配，退回宽松切分
        tokens = body_text.split(" -")

    out = {
        "url": "",
        "method": "POST" if any("data" in t or t == "-d" for t in tokens) else "GET",
        "headers": {},
        "form": {},
        "cookies": "",
    }

    i = 0
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        if tok in ("-H", "--header", "-b", "--cookie", "--url",
                   "-X", "--request", "--data-raw", "--data", "-d",
                   "--data-binary", "-F", "--form"):
            if i + 1 >= n:
                break                   # 末尾孤立的 flag，忽略
            nxt = tokens[i + 1]
            i += 2
        else:
            i += 1
            continue

        if tok in ("-H", "--header"):
            out["headers"].update(_parse_header(nxt))
        elif tok in ("-b", "--cookie"):
            if nxt:
                # 单独存：Cookie 不进 headers，而是之后由浏览器上下文注入。
                # 实测 curl_cffi 带完整 cf_clearance 仍被 CF 403，
                # 说明 clearance 与浏览器指纹绑定，必须走浏览器。
                out["cookies"] = nxt
        elif tok == "--url":
            out["url"] = nxt
        elif tok in ("-X", "--request"):
            out["method"] = nxt.upper()
        elif tok in ("--data-raw", "--data", "-d", "--data-binary"):
            nxt = _unescape_ansi(nxt)
            if nxt.startswith("$"):          # 残留的 $'...' 前缀
                nxt = nxt[1:]
            out["form"].update(_parse_form_body(nxt))
            if out["method"] == "GET":
                out["method"] = "POST"
        elif tok in ("-F", "--form"):
            name, _, v = nxt.partition("=")
            if name:
                out["form"][name] = v

    if not out["url"]:
        m = re.search(r"https?://[^\s'\"]+", body_text)
        if m:
            out["url"] = m.group(0)

    if not out["url"]:
        raise ValueError("没能从内容里识别出请求地址（是否包含 --url 或以 http 开头）")

    # 剥掉浏览器自己会重发的头（描述客户端身份，由真实浏览器接管）
    for k in list(out["headers"]):
        if k.lower() in _DROP_ALWAYS or k.lower() in _BROWSER_OWNED:
            out["headers"].pop(k)

    return out


def _parse_header(raw: str) -> dict:
    k, sep, v = raw.partition(":")
    if not sep:
        return {}
    return {k.strip(): v.strip()}


def _parse_form_body(body: str) -> dict:
    """解析 body。

    支持两种常见形态：
    - multipart/form-data：CRLF 分段，每段有Content-Disposition
    - application/x-www-form-urlencoded：``a=1&b=2``（Chrome 复制
      简单表单时会用这种，比 multipart 更常见）
    """
    body = body.strip()
    if not body:
        return {}

    # urlencoded：没有分隔符且不含 Content-Disposition
    if "Content-Disposition" not in body and "=" in body and "&" in body:
        out: dict[str, str] = {}
        for pair in body.split("&"):
            k, _, v = pair.partition("=")
            k = k.strip()
            if k:
                out[k] = unquote(v)
        if out:
            return out

    form: dict[str, str] = {}
    for seg in re.split(r"\r?\n(?=(?:--|Content-Disposition))", body):
        m = _FORM_RE.search(seg)
        if not m:
            continue
        name = m.group(1)
        parts = re.split(r"\r?\n\r?\n", seg, maxsplit=1)
        if len(parts) < 2:
            continue
        val = parts[1]
        val = re.split(r"\r?\n--", val, maxsplit=1)[0]
        val = val.strip("\r\n")
        if name and val:
            form[name] = unquote(val) if "%" in val else val
    return form


# ---- 用于替换的字段（url 之外）----
# 这些是「会随会话变化」的部分，用程序实时取到的值替换用户粘贴的旧值
DYNAMIC_FIELDS = ("_token", "token", "t0ken", "csrf_token", "authenticity_token")


def summarize(parsed: dict) -> dict:
    """给前端看的概要：哪些字段会被动态替换、哪些是固定保留项"""
    form = parsed.get("form", {})
    ck = parsed.get("cookies", "")
    # url 每次都由用户输入替换，不算动态字段也不算固定字段，单列出来
    dynamic = [f for f in DYNAMIC_FIELDS if f in form]
    return {
        "url": parsed.get("url", ""),
        "method": parsed.get("method", "POST"),
        "headers": sorted(parsed.get("headers", {}).keys()),
        "cookie_present": bool(ck),
        "cookie_count": len([c for c in ck.split(";") if "=" in c]),
        "fields": sorted(form.keys()),
        "dynamic": dynamic,
        "has_url_field": "url" in form,
        "static": [k for k in form
                   if k not in dynamic and k != "url"],
    }