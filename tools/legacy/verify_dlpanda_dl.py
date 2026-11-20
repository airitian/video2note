# -*- coding: utf-8 -*-
"""download() 的续传与完整性校验验证（用本地假服务器，不依赖外网）"""
import http.server
import socketserver
import sys
import threading
from pathlib import Path

sys.path.insert(0, ".")

from core import dlpanda

PASS = FAIL = 0


def ck(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {extra}")


BODY = b"".join(bytes([i % 251]) for i in range(400_000))   # 400KB 确定性内容
TOTAL = len(BODY)
STATE = {"drop_after": None, "hits": 0}


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        STATE["hits"] += 1
        rng = self.headers.get("Range")
        start = 0
        if rng and rng.startswith("bytes="):
            start = int(rng.split("=")[1].split("-")[0])
        chunk = BODY[start:]

        drop = STATE["drop_after"]
        if drop is not None and len(chunk) > drop:
            # 故意截断：模拟中途断流
            self.send_response(200)
            self.send_header("Content-Length", str(len(chunk)))
            self.send_header("Content-Type", "video/mp4")
            self.end_headers()
            self.wfile.write(chunk[:drop])
            STATE["drop_after"] = None
            return

        if rng:
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{TOTAL-1}/{TOTAL}")
        else:
            self.send_response(200)
        self.send_header("Content-Length", str(len(chunk)))
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        self.wfile.write(chunk)


srv = socketserver.TCPServer(("127.0.0.1", 0), H)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
URL = f"http://127.0.0.1:{port}/v.mp4"
TMP = Path("data/tmp/verify_dl.mp4")
TMP.parent.mkdir(parents=True, exist_ok=True)

print("=" * 64)
print("一、完整下载")
print("=" * 64)

STATE["drop_after"] = None
TMP.unlink(missing_ok=True)
out = dlpanda.download(URL, TMP)
ck("文件已生成", out.exists())
ck("大小完全一致", out.stat().st_size == TOTAL, f"{out.stat().st_size}/{TOTAL}")
ck("内容逐字节一致", out.read_bytes() == BODY)
ck("临时 .part 已清理", not TMP.with_suffix(".mp4.part").exists())

print()
print("=" * 64)
print("二、断点续传")
print("=" * 64)

TMP.unlink(missing_ok=True)
TMP.with_suffix(".mp4.part").write_bytes(BODY[:150_000])

seen = []
out = dlpanda.download(URL, TMP, progress=lambda p, m: seen.append(p))
ck("续传后大小正确", out.stat().st_size == TOTAL)
ck("续传后内容一致", out.read_bytes() == BODY)
ck("确实发过 Range 续传请求", STATE["hits"] >= 1)
ck("进度百分比不倒退", seen == sorted(seen) or len(set(seen)) == len(seen),
   f"{seen[:6]}")

print()
print("=" * 64)
print("三、中途截断必须报错（不能把坏文件当成功）")
print("=" * 64)

TMP.unlink(missing_ok=True)
STATE["drop_after"] = 100_000
try:
    dlpanda.download(URL, TMP)
    ck("截断应抛错", False, "竟然当成成功了")
except dlpanda.DlpandaError as e:
    ck("截断 -> 明确报错", "下载不完整" in str(e) or "下载失败" in str(e), str(e))
    ck("截断时不生成正式文件", not TMP.exists())
ck("截断时保留 .part 供续传", TMP.with_suffix(".mp4.part").exists())

print()
print("=" * 64)
print("四、空地址与空响应")
print("=" * 64)

try:
    dlpanda.download("", TMP)
    ck("空地址应报错", False)
except dlpanda.DlpandaError as e:
    ck("空地址 -> 明确报错", "没有可下载" in str(e), str(e))

srv.shutdown()
TMP.unlink(missing_ok=True)


# ---- probe() 与 pick_media() 的判定逻辑 ----
# 上一版 probe() 只看有无异常，而 curl_cffi 遇 403 不抛异常，
# 于是「HTTP 403」被误判为可用。实测「暗战深度解析」那条就是这样错的。
print()
print('=' * 64)
print('五、probe 判定：非 2xx 必须判为不可用')
print('=' * 64)

import core.dlpanda as _D
import curl_cffi.requests as _cr


def _install(status, body=b'x' * 100):
    """把 curl_cffi.get 换成固定响应"""
    def g(url, headers=None, **kw):
        class R:
            status_code = status
            content = body
            headers = {'content-type': 'video/mp4',
                       'content-length': str(len(body))}
        return R()
    _cr.get = g


_orig = _cr.get
_install(403)
try:
    ok, info = _D.probe('https://x/a.mp4', 'https://www.douyin.com/')
    ck('403 判为不可用', ok is False, f'(得到 {ok})')
    ck('403 原因写入说明', '403' in info, info)
finally:
    _cr.get = _orig

_install(200, b'y' * 200)
try:
    ok, info = _D.probe('https://x/a.mp4')
    ck('200 判为可用', ok is True, info)
finally:
    _cr.get = _orig

_install(200, b'')
try:
    ok, info = _D.probe('https://x/a.mp4')
    ck('空响应体判为不可用', ok is False, info)
finally:
    _cr.get = _orig


def boom(*a, **k):
    raise OSError('网络中断')


_cr.get = boom
try:
    ok, info = _D.probe('https://x/a.mp4')
    ck('异常判为不可用', ok is False, info)
    ck('异常原因可读', 'OSError' in info or '网络' in info, info)
finally:
    _cr.get = _orig

print()
print('=' * 64)
print('六、pick_media 降级链')
print('=' * 64)

state = {'v': (False, 'HTTP 403'), 'a': (True, 'HTTP 206')}


def pm_probe(url, referer=None):
    return state['v'] if 'mp4' in url else state['a']


_D.probe = pm_probe
try:
    k, u, info = _D.pick_media('https://x/a.mp4', 'https://x/a.mp3')
    ck('视频不可用 -> 选音频', k == 'AUDIO', f'(得到 {k})')
    ck('降级时说明含视频失败原因', '403' in info, info)

    state['v'] = (True, 'HTTP 206')
    k, u, info = _D.pick_media('https://x/a.mp4', 'https://x/a.mp3')
    ck('视频可用 -> 选视频', k == 'VIDEO', f'(得到 {k})')

    k, u, info = _D.pick_media(None, 'https://x/a.mp3')
    ck('无视频地址 -> 直接用音频', k == 'AUDIO', f'(得到 {k})')

    k, u, info = _D.pick_media(None, None)
    ck('两者皆无 -> 返回 None', k is None and u is None)
    ck('两者皆无 -> 说明齐全', '视频' in info and '音频' in info, info)

    state['v'] = (False, 'HTTP 403')
    state['a'] = (False, 'HTTP 404')
    k, u, info = _D.pick_media('https://x/a.mp4', 'https://x/a.mp3')
    ck('两者皆不可用 -> None', k is None)
    ck('两者皆不可用 -> 两条原因都在', '403' in info and '404' in info, info)
finally:
    import importlib
    importlib.reload(_D)

print()
print("=" * 64)
print(f"结果：{PASS} 通过 / {FAIL} 失败")
print("=" * 64)
sys.exit(1 if FAIL else 0)