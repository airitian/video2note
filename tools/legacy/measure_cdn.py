"""同一抖音 CDN 直链，用三种 HTTP 客户端比下载吞吐，定位 0.5MB/s 的根因。

- curl_cffi(chrome136 伪装)：当前 app 用的
- requests(普通)：无伪装
- 系统 curl -k：绕过所有 Python 封装
"""
import sys
import time
import tempfile
from pathlib import Path

from core import dlpanda

URL = sys.argv[1] if len(sys.argv) > 1 else "https://v.douyin.com/Mx9cOjcy8Ws/"
res = dlpanda.resolve(URL, platform="douyin")
_, media_url, _ = dlpanda.pick_media(res.video_url, res.audio_url)
print(f"media_url host: {media_url.split('/')[2]}\n", flush=True)

HDR = {"Referer": "https://www.douyin.com/", "User-Agent": dlpanda.UA}
TMP = Path(tempfile.gettempdir())
N = 8 << 20  # 最多下 8MB 测速


def speed(label: str, fn) -> None:
    dst = TMP / f"cdn_{label}.bin"
    t = time.perf_counter()
    try:
        n = fn(dst)
    except Exception as e:
        print(f"  {label:24s} 失败: {type(e).__name__}: {str(e)[:80]}", flush=True)
        return
    dt = time.perf_counter() - t
    print(f"  {label:24s} {n/1e6:7.2f}MB  {dt:6.1f}s  {n/dt/1e6:6.2f} MB/s", flush=True)


# 1) curl_cffi impersonate（当前实现）
def dl_cffi(dst):
    from curl_cffi import requests as cr
    with cr.Session(impersonate="chrome136", verify=False) as s:
        r = s.get(media_url, headers=HDR, timeout=(20, 120), stream=True)
        r.raise_for_status()
        got = 0
        with open(dst, "wb") as f:
            for p in r.iter_content(1 << 18):
                f.write(p); got += len(p)
                if got >= N: break
        return got


# 2) 普通 requests
def dl_req(dst):
    import requests
    with requests.Session() as s:
        r = s.get(media_url, headers=HDR, timeout=(20, 120), stream=True)
        r.raise_for_status()
        got = 0
        with open(dst, "wb") as f:
            for p in r.iter_content(1 << 18):
                f.write(p); got += len(p)
                if got >= N: break
        return got


# 3) 系统 curl
def dl_curl(dst):
    import subprocess
    r = subprocess.run(
        ["curl", "-k", "-sS", "-L", "-r", f"0-{N-1}", "-A", dlpanda.UA,
         "-e", "https://www.douyin.com/", "-o", str(dst), media_url],
        capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[:120])
    return dst.stat().st_size


print("=== 8MB 测速对比 ===", flush=True)
speed("curl_cffi(chrome136)", dl_cffi)
speed("requests(普通)", dl_req)
speed("system curl -k", dl_curl)
