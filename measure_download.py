"""分段测量「下载阶段」耗时：解析 / CDN下载 / ffmpeg转码 各占多少。

复刻 download_video() 的内部调用顺序，但每一步单独打点，定位真正的慢点。
用法：python measure_download.py [douyin_url]
"""
import sys
import time
import tempfile
from pathlib import Path

from core import dlpanda, audio

URL = sys.argv[1] if len(sys.argv) > 1 else "https://v.douyin.com/Mx9cOjcy8Ws/"
wd = Path(tempfile.mkdtemp(prefix="v2n_meas_"))


def mark(stage: str, t0: float):
    print(f"  [{stage}] 用时 {time.perf_counter() - t0:6.1f}s", flush=True)


print(f"目标: {URL}\n", flush=True)

# 1) 解析：真实 Chromium + 过 Cloudflare + dlpanda POST
t = time.perf_counter()
res = dlpanda.resolve(URL, platform="douyin")
mark("resolve 解析(Chromium+CF)", t)
print(f"     title={res.title!r}  video_url={bool(res.video_url)} audio_url={bool(res.audio_url)}", flush=True)

# 2) 挑选媒体 + 3) CDN 下载
out = wd / f"source{dlpanda._ext_for(res.video_url or res.audio_url or '.mp4')}"
t = time.perf_counter()
kind, media_url, info = dlpanda.pick_media(res.video_url, res.audio_url)
mark("pick_media 探测可用源", t)
print(f"     kind={kind} info={info}", flush=True)

t = time.perf_counter()
dlpanda.download(media_url, out)
mark("download CDN下载", t)
sz = out.stat().st_size / 1e6 if out.exists() else 0
print(f"     文件 {sz:.1f} MB  ({out})", flush=True)

# 4) ensure_playable（H.264 直接返回，HEVC 才重编码）
t = time.perf_counter()
playable = audio.ensure_playable(out, 1080)
mark("ensure_playable 转码校验", t)

# 5) extract_audio（重编码 16k 单声道 mp3 给 ASR）
t = time.perf_counter()
audio.extract_audio(playable, wd / "audio.mp3")
mark("extract_audio 抽音频", t)

print("\n=== 结论 ===")
print("resolve 与 download 是串行的前两步；resolve 每次都冷启动 Chromium，")
print("dlpanda.com 又过 Cloudflare，这两项通常是「下载阶段」最重的两笔。")
