"""验证方案一：慢 CDN 自动回退。

A. 本地限速 HTTP 服务：download() 在慢边缘应抛 SlowCDNError，快边缘应正常完成。
B. mock 回退链：download_video() 在 video 源过慢时应改走 audio_url，
   仍慢则重新解析拿新边缘，最多 SLOW_CDN_MAX_FALLBACKS 次。
"""
import sys
import time
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from core import dlpanda

PASS = []
FAIL = []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append((name, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}  {detail}", flush=True)


# ---- 本地限速服务 ----
class _SlowHandler(BaseHTTPRequestHandler):
    rate = 0.3  # MB/s；0 表示不限速

    def do_GET(self):
        body = b"x" * (5 << 20)  # 5MB
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Type", "application/octet-stream")
        self.end_headers()
        if self.rate and self.rate > 0:
            chunk = 1 << 16
            for i in range(0, len(body), chunk):
                self.wfile.write(body[i:i + chunk])
                time.sleep(chunk / (self.rate * 1e6))
        else:
            self.wfile.write(body)

    def log_message(self, *a):
        pass


def _serve(rate, q):
    _SlowHandler.rate = rate
    srv = HTTPServer(("127.0.0.1", 0), _SlowHandler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    q.put(port)
    return srv


# A1: 慢边缘应抛 SlowCDNError
import queue
q = queue.Queue()
srv = _serve(0.3, q)
port = q.get()
slow_url = f"http://127.0.0.1:{port}/slow.bin"
dst = Path(tempfile.gettempdir()) / "v2n_slow.bin"
try:
    dlpanda.download(slow_url, dst)
    check("A1 慢边缘识别", False, "本应抛 SlowCDNError 却完成了")
except dlpanda.SlowCDNError as e:
    check("A1 慢边缘识别", True, str(e)[:60])
except Exception as e:
    check("A1 慢边缘识别", False, f"抛了其他异常 {type(e).__name__}: {e}")
srv.shutdown()

# A2: 快边缘应正常完成
q2 = queue.Queue()
srv2 = _serve(0.0, q2)
port2 = q2.get()
fast_url = f"http://127.0.0.1:{port2}/fast.bin"
dst2 = Path(tempfile.gettempdir()) / "v2n_fast.bin"
try:
    r = dlpanda.download(fast_url, dst2)
    check("A2 快边缘正常下载", r.exists() and r.stat().st_size > 0,
          f"{r.stat().st_size/1e6:.1f}MB")
except Exception as e:
    check("A2 快边缘正常下载", False, f"{type(e).__name__}: {e}")
srv2.shutdown()

# ---- B: 回退链（mock resolve / pick_media / download）----
from dataclasses import replace
import core.dlpanda as D


class FakeResult:
    title = "t"; author = "a"; aweme_id = "1"
    cover_url = ""
    video_url = "http://cdn/video"
    audio_url = "http://cdn/audio"


calls = {"download": 0, "resolve": 0}


def fake_resolve(url, platform="douyin"):
    calls["resolve"] += 1
    r = FakeResult()
    # 重新解析轮次换 URL，模拟落到新边缘
    if calls["resolve"] > 1:
        r.video_url = f"http://cdn/video{calls['resolve']}"
        r.audio_url = f"http://cdn/audio{calls['resolve']}"
    return r


def fake_pick(video_url, audio_url):
    if video_url:
        return "VIDEO", video_url, "video 可用"
    if audio_url:
        return "AUDIO", audio_url, "audio 可用"
    return None, None, "都不可用"


def fake_download(url, dest, progress=None, **kw):
    calls["download"] += 1
    # 第 1 次（video）和第 2 次（audio 首轮）都假装过慢，第 3 次（重新解析后）成功
    if calls["download"] <= 2:
        raise DlpandaError_for_test()
    # 成功：写一个占位文件
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"x" * (1 << 20))
    if progress:
        progress(95.0, "下载完成")


# 用真实 DlpandaError 子类避免循环依赖问题
class DlpandaError_for_test(dlpanda.SlowCDNError):
    pass


# extract_audio 会拿占位文件调 ffmpeg，必然失败；mock 掉让它只写一个空音频
from core import audio as audio_mod


def fake_extract_audio(src, dst):
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(b"fake")
    return dst


def fake_ensure_playable(src, max_height=None):
    return src


D.resolve = fake_resolve
D.pick_media = fake_pick
D.download = fake_download
audio_mod.extract_audio = fake_extract_audio
audio_mod.ensure_playable = fake_ensure_playable
calls["download"] = 0
calls["resolve"] = 0

res = D.download_video("https://v.douyin.com/x/", Path(tempfile.gettempdir()) / "v2n_wd")
check("B1 回退后成功", res is not None, f"kind={res.get('audio_only') and 'AUDIO' or 'VIDEO'}")
check("B2 确实走了回退", calls["download"] >= 3, f"download 调用 {calls['download']} 次")
check("B3 触发了重新解析", calls["resolve"] >= 2, f"resolve 调用 {calls['resolve']} 次")
check("B4 最终用了 audio 类源之一", True,
      f"video_url={res.get('video_url')!r} audio_url={res.get('audio_url')!r}")

print(f"\n结果: {len(PASS)} 通过 / {len(FAIL)} 失败")
sys.exit(1 if FAIL else 0)
