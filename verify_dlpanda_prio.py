# -*- coding: utf-8 -*-
"""download_media 三级优先级的真实调度验证（不实际下载大文件）"""
import sys
from pathlib import Path

sys.path.insert(0, ".")

PASS = FAIL = 0
CALLS = []


def ck(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {extra}")


from core import downloader, dlpanda

WORK = Path("data/tmp/verify_prio")
WORK.mkdir(parents=True, exist_ok=True)

print("=" * 64)
print("一、抖音 -> 优先走接口，接口成功则不再调 yt-dlp")
print("=" * 64)

orig_api = dlpanda.download_video
orig_ydl = downloader._ytdlp_download
orig_dy = None
try:
    from core import douyin as dy_mod
    orig_dy = dy_mod.download
except Exception:
    dy_mod = None

DONE = {"video_path": str(WORK / "s.mp4"), "audio_path": str(WORK / "a.mp3"),
        "title": "T", "extractor": "dlpanda"}

CALLS.clear()


def fake_api(url, workdir, max_height=1080, progress=None):
    CALLS.append("api")
    return dict(DONE)


def fake_ydl(*a, **k):
    CALLS.append("ytdlp")
    return dict(DONE)


def fake_dy(*a, **k):
    CALLS.append("pyktok")
    return dict(DONE)


dlpanda.download_video = fake_api
downloader._ytdlp_download = fake_ydl
if dy_mod:
    dy_mod.download = fake_dy

r = downloader.download_media("https://v.douyin.com/abc/", WORK)
ck("接口被调用", "api" in CALLS, CALLS)
ck("yt-dlp 未被调用", "ytdlp" not in CALLS, CALLS)
ck("pyktok 未被调用", "pyktok" not in CALLS, CALLS)
ck("返回结果原样透传", r.get("extractor") == "dlpanda")

print()
print("=" * 64)
print("二、接口失败 -> 降级 yt-dlp")
print("=" * 64)

CALLS.clear()


def fail_api(*a, **k):
    CALLS.append("api")
    raise dlpanda.DlpandaError("被 Cloudflare 拦截（站点要求人工验证）")


dlpanda.download_video = fail_api
r = downloader.download_media("https://v.douyin.com/abc/", WORK)
ck("接口被调用", "api" in CALLS, CALLS)
ck("降级到 yt-dlp", "ytdlp" in CALLS, CALLS)
ck("接口失败时不进 pyktok", "pyktok" not in CALLS, CALLS)

print()
print("=" * 64)
print("三、接口与 yt-dlp 都失败 -> pyktok 兜底，且错误含接口原因")
print("=" * 64)

CALLS.clear()


def boom_ydl(*a, **k):
    CALLS.append("ytdlp")
    raise downloader.DownloadError("Fresh cookies are needed")


downloader._ytdlp_download = boom_ydl
def fail_pyktok(*a, **k):
    CALLS.append("pyktok")
    raise RuntimeError("pyktok 签名失败：Cookie 缺失")


if dy_mod:
    dy_mod.download = fail_pyktok

try:
    downloader.download_media("https://v.douyin.com/abc/", WORK)
    ck("应抛错", False)
except downloader.DownloadError as e:
    msg = str(e)
    ck("接口被调用", CALLS[0] == "api", CALLS)
    ck("接口失败后调用 yt-dlp", "ytdlp" in CALLS, CALLS)
    ck("错误含接口失败原因", "Cloudflare" in msg, msg[:120])
    ck("错误含 yt-dlp 原因", "Fresh cookies" in msg)
    if dy_mod:
        ck("pyktok 兜底被调用", "pyktok" in CALLS, CALLS)
        ck("错误含 pyktok 原因", "pyktok 也失败" in msg)

print()
print("=" * 64)
print("四、B站 -> 不走抖音接口，仍用 yt-dlp")
print("=" * 64)

# 前一节故意让 yt-dlp 持续失败，这里必须恢复正常实现，
# 否则后面的用例会一直撞上同一个假异常（测试自身状态泄漏）。
downloader._ytdlp_download = fake_ydl
CALLS.clear()
r = downloader.download_media("https://www.bilibili.com/video/BV1xx411c7mD", WORK)
ck("接口未被调用", "api" not in CALLS, CALLS)
ck("yt-dlp 被调用", "ytdlp" in CALLS, CALLS)

print()
print("=" * 64)
print("五、prefer_api=False 可跳过接口（对比排查用）")
print("=" * 64)

CALLS.clear()
r = downloader.download_media("https://v.douyin.com/abc/", WORK, prefer_api=False)
ck("接口未调用", "api" not in CALLS, CALLS)
ck("直接走 yt-dlp", "ytdlp" in CALLS, CALLS)

print()
print("=" * 64)
print("六、依赖缺失时应跳过而非崩溃")
print("=" * 64)

CALLS.clear()
orig_avail = dlpanda.available
dlpanda.available = lambda: False
r = downloader.download_media("https://v.douyin.com/abc/", WORK)
ck("available()=False 时跳过接口", "api" not in CALLS, CALLS)
ck("仍能走 yt-dlp", "ytdlp" in CALLS, CALLS)
dlpanda.available = orig_avail

print()
print("=" * 64)
print("七、非法链接")
print("=" * 64)

try:
    downloader.download_media("这不是链接", WORK)
    ck("非法链接应报错", False)
except Exception as e:
    ck("非法链接 -> 明确报错", "未能从输入中识别" in str(e), str(e))

# 还原
dlpanda.download_video = orig_api
downloader._ytdlp_download = orig_ydl
if dy_mod and orig_dy:
    dy_mod.download = orig_dy

print()
print("=" * 64)
print(f"结果：{PASS} 通过 / {FAIL} 失败")
print("=" * 64)
sys.exit(1 if FAIL else 0)