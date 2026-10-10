# -*- coding: utf-8 -*-
"""mux 合并验证：核心是「能复制就不重编码」

背景（实测数据，12 分钟 B站视频 146MB + 10.5MB 音轨）：
    全 copy          2.8s
    copy + faststart 3.2s
    视频 copy + 音频重编码 AAC  28.3s   ← 旧实现默认走这条
    纯磁盘复制 146MB 0.1s（证明不是磁盘慢）

所以默认必须先试 -c copy，只有音轨编码浏览器放不出时才重编码。
"""
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, ".")

from core import audio, config

PASS = FAIL = 0


def ck(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {extra}")


FF = config.ff_bin("ffmpeg")


def _run(args):
    return subprocess.run(args, capture_output=True, text=True,
                          encoding="utf-8", errors="ignore")


# ---------------------------------------------------------------- 一、编码探测
print("=" * 64)
print("一、audio_codec / video_codec 解析")
print("=" * 64)

FAKE_LOGS = [
    ("Stream #0:1(und): Audio: aac (LC) (mp4a / 0x6134706D), 44100 Hz", "aac"),
    ("Stream #0:1(und): Audio: mp3, 44100 Hz, stereo, fltp", "mp3"),
    ("Stream #0:1(und): Audio: opus, 48000 Hz, stereo, fltp", "opus"),
    ("Stream #0:1(und): Audio: flac, 44100 Hz", "flac"),
    ("Stream #0:0: Video: h264 (avc1)", ""),          # 只有视频轨 -> ''
]


class _FakeCP:
    def __init__(self, err):
        self.stderr = err
        self.stdout = ""
        self.returncode = 0


_orig_run = audio._run
for log, want in FAKE_LOGS:
    audio._run = lambda a, _l=log: _FakeCP(_l)
    got = audio.audio_codec(Path("x.mp4"))
    ck(f"解析 {want or '(无音轨)'!r}", got == want, f"got={got!r}")
audio._run = _orig_run

ck("aac 属于浏览器可播",
   any("aac".startswith(b) for b in audio.BROWSER_AUDIO_CODECS))
ck("opus 不属于浏览器可播",
   not any("opus".startswith(b) for b in audio.BROWSER_AUDIO_CODECS))

# ------------------------------------------------- 二、三级回退策略（不真跑 ffmpeg）
print("=" * 64)
print("二、回退策略：copy 优先、音轨不兼容才重编码")
print("=" * 64)

calls: list[list[str]] = []


def _fake_ffmpeg(args):
    if "-version" in args:          # check_ffmpeg() 的探活，不算业务调用
        return subprocess.CompletedProcess(args, 0, "ffmpeg version fake", "")
    calls.append(args)
    n = len(calls)
    if n == 1 and SCENARIO["copy_rc"] != 0:
        return subprocess.CompletedProcess(args, SCENARIO["copy_rc"], "", "boom")
    return subprocess.CompletedProcess(args, 0, "", "")


def _fake_codec_after_copy(path):
    """第一次（copy）之后报什么编码"""
    return SCENARIO["copy_codec"] if len(calls) == 1 else "aac"


SCENARIO = {"copy_rc": 0, "copy_codec": "aac"}
tmp = Path(tempfile.mkdtemp())
out = tmp / "o.mp4"


def _prepare():
    calls.clear()
    out.write_bytes(b"x")            # 让 out.exists() 且 size>0
    audio._run = _fake_ffmpeg
    audio.audio_codec = _fake_codec_after_copy


def _restore():
    audio._run = _orig_run
    audio.audio_codec = _real_codec


_real_codec = audio.audio_codec

# 场景1：copy 成功且是 aac —— 只应调用一次，且参数是 -c copy
SCENARIO = {"copy_rc": 0, "copy_codec": "aac"}
_prepare()
audio.mux(Path("v.mp4"), Path("a.m4a"), out)
ck("aac：只跑一次 ffmpeg", len(calls) == 1, f"calls={len(calls)}")
a0 = calls[0] if calls else []
ck("aac：用的是 -c copy", "-c" in a0 and a0[a0.index("-c") + 1] == "copy", str(a0))
_restore()

# 场景2：copy 成功但音轨是 opus —— 必须再跑一次重编码
SCENARIO = {"copy_rc": 0, "copy_codec": "opus"}
_prepare()
audio.mux(Path("v.mp4"), Path("a.m4a"), out)
ck("opus：触发第二次尝试", len(calls) == 2, f"calls={len(calls)}")
ck("opus：第二次是重编码 aac",
   len(calls) == 2 and "-c:a" in calls[1] and "aac" in calls[1], str(calls[1:2]))
_restore()

# 场景3：copy 失败（非 0 退出）—— 直接走重编码
SCENARIO = {"copy_rc": 1, "copy_codec": "aac"}
_prepare()
audio.mux(Path("v.mp4"), Path("a.m4a"), out)
ck("copy 失败：走重编码", len(calls) == 2 and "-c:a" in calls[1], str(calls[1:2]))
_restore()

# ---------------------------------------------------------------- 三、真跑一次
print("=" * 64)
print("三、真实合并（造 6 秒素材，验证能用且音轨是 aac）")
print("=" * 64)

vsrc = tmp / "v.mp4"
asrc = tmp / "a.m4a"
ck("造视频素材", _run([FF, "-hide_banner", "-y", "-f", "lavfi", "-i",
                       "testsrc=size=320x240:rate=15", "-t", "6",
                       "-c:v", "libx264", "-pix_fmt", "yuv420p", str(vsrc)]).returncode == 0)
ck("造音频素材", _run([FF, "-hide_banner", "-y", "-f", "lavfi", "-i",
                       "sine=frequency=440:duration=6", "-c:a", "aac", str(asrc)]).returncode == 0)

if vsrc.exists() and asrc.exists():
    real_out = tmp / "real.mp4"
    try:
        audio.mux(vsrc, asrc, real_out)
        ok = real_out.exists() and real_out.stat().st_size > 0
    except Exception as e:                                  # noqa: BLE001
        ok = False
        print("   mux 异常:", e)
    ck("真实合并成功", ok)
    if ok:
        ck("输出视频轨 h264", audio.video_codec(real_out) == "h264",
           audio.video_codec(real_out))
        ck("输出音频轨 aac", audio.audio_codec(real_out) == "aac",
           audio.audio_codec(real_out))
        d = audio.probe_duration(real_out)
        ck("时长约 6 秒", 5.5 < d < 6.6, f"{d}")

print("=" * 64)
print(f"结果：{PASS} 通过 / {FAIL} 失败")
print("=" * 64)
sys.exit(1 if FAIL else 0)
