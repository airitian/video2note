"""验证「单连接限速」假设（精简版）：16MB 窗口，1 连接 vs 4 连接。"""
import sys
import time
import tempfile
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

from core import dlpanda

URL = sys.argv[1] if len(sys.argv) > 1 else "https://v.douyin.com/Mx9cOjcy8Ws/"
try:
    res = dlpanda.resolve(URL, platform="douyin")
    _, media_url, _ = dlpanda.pick_media(res.video_url, res.audio_url)
except Exception as e:
    print("resolve 失败:", e); sys.exit(1)
HDR = {"Referer": "https://www.douyin.com/", "User-Agent": dlpanda.UA}

import requests
WIN = 16 << 20  # 16MB 窗口
TMP = Path(tempfile.gettempdir())


def fetch_range(s: int, e: int, idx: int) -> int:
    r = requests.get(media_url, headers={**HDR, "Range": f"bytes={s}-{e}"},
                     timeout=(20, 120), stream=True)
    got = 0
    with open(TMP / f"pr_{idx}.bin", "wb") as f:
        for p in r.iter_content(1 << 16):
            f.write(p); got += len(p)
    return got


def run(conns: int) -> None:
    step = WIN // conns
    t = time.perf_counter()
    with ThreadPoolExecutor(max_workers=conns) as ex:
        fs = [ex.submit(fetch_range, i * step,
                        WIN - 1 if i == conns - 1 else i * step + step - 1, i)
              for i in range(conns)]
        n = sum(f.result() for f in fs)
    dt = time.perf_counter() - t
    print(f"  {conns:2d} 连接: {dt:5.1f}s  {n/dt/1e6:6.2f} MB/s", flush=True)


print(f"媒体 host: {media_url.split('/')[2]}\n=== 16MB 窗口 ===", flush=True)
run(1)
run(4)
