"""音频处理：探测时长、转码标准化、长音频按静音点切片"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from .config import ff_bin


class AudioError(RuntimeError):
    pass


def _run(args: list[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="ignore")
    except FileNotFoundError:
        # 可执行文件不存在（例如只装了 ffmpeg 没有 ffprobe），交由调用方走兜底逻辑
        return subprocess.CompletedProcess(args, 127, "", f"executable not found: {args[0]}")


def check_ffmpeg() -> None:
    r = _run([ff_bin("ffmpeg"), "-version"])
    if r.returncode != 0:
        raise AudioError(
            "未检测到 ffmpeg。请安装 ffmpeg 并加入 PATH，或在设置里填写 ffmpeg 完整路径，"
            "也可执行 pip install imageio-ffmpeg 使用其自带二进制。"
        )


def probe_duration(path: Path) -> float:
    r = _run([ff_bin("ffprobe"), "-v", "quiet", "-print_format", "json",
              "-show_format", str(path)])
    if r.returncode == 0:
        try:
            return float(json.loads(r.stdout)["format"]["duration"])
        except Exception:
            pass
    # 没有 ffprobe 时，用 ffmpeg -i 的 Duration 行兜底
    r2 = _run([ff_bin("ffmpeg"), "-hide_banner", "-i", str(path)])
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", r2.stderr or "")
    if m:
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    return 0.0


def transcode(src: Path, dst: Path) -> Path:
    """把任意音视频文件统一转成 16k 单声道 mp3"""
    check_ffmpeg()
    dst.parent.mkdir(parents=True, exist_ok=True)
    _transcode(src, dst)
    return dst


def extract_audio(src: Path, dst: Path) -> Path:
    """从视频文件中抽出 16k 单声道 mp3，供 ASR 使用"""
    check_ffmpeg()
    dst.parent.mkdir(parents=True, exist_ok=True)
    r = _run([ff_bin("ffmpeg"), "-hide_banner", "-y", "-i", str(src),
              "-vn", "-ac", "1", "-ar", "16000", "-c:a", "libmp3lame", "-b:a", "64k", str(dst)])
    if r.returncode != 0 or not dst.exists():
        raise AudioError(f"从视频提取音频失败：{(r.stderr or '')[-400:]}")
    return dst


def mux(video: Path, audio: Path, out: Path) -> Path:
    """合并视频轨与音频轨为可直接在浏览器播放的 mp4

    优先「音视频都直接复制」。实测同一条 12 分钟 B站视频：
    全 copy 只要 3 秒，而把音轨重编码成 AAC 要 28 秒
    —— 重编码是整条链路最大的一笔浪费，能省则省。
    只有复制出来的音轨浏览器放不出（如 opus/flac 塞进 mp4）时才退回重编码。
    """
    check_ffmpeg()
    out.parent.mkdir(parents=True, exist_ok=True)
    ff = ff_bin("ffmpeg")
    common = ["-map", "0:v:0", "-map", "1:a:0", "-shortest", "-movflags", "+faststart"]
    attempts = [
        ["-c", "copy"],
        ["-c:v", "copy", "-c:a", "aac", "-b:a", "128k"],
        ["-c:v", "libx264", "-preset", "veryfast", "-crf", "26", "-c:a", "aac", "-b:a", "128k"],
    ]
    last = ""
    for i, enc in enumerate(attempts):
        r = _run([ff, "-hide_banner", "-y", "-i", str(video), "-i", str(audio)] + enc + common + [str(out)])
        last = (r.stderr or "")[-400:]
        if r.returncode == 0 and out.exists() and out.stat().st_size > 0:
            c = audio_codec(out)
            playable = (not c) or any(c.startswith(b) for b in BROWSER_AUDIO_CODECS)
            # 复制成功但音轨浏览器放不出，且还有备用方案时，继续尝试重编码
            if i < len(attempts) - 1 and not playable:
                continue
            return out
    raise AudioError(f"视频合并失败：{last}")


def video_codec(path: Path) -> str:
    """探测视频轨编码名（h264 / hevc / ...），没有视频轨时返回 ''"""
    r = _run([ff_bin("ffmpeg"), "-hide_banner", "-i", str(path)])
    txt = r.stderr or ""
    m = re.search(r"Stream #\d+:\d+.*?:\s*Video:\s*([a-z0-9_]+)", txt)
    return (m.group(1).lower() if m else "")


def audio_codec(path: Path) -> str:
    """探测音频轨编码名（aac / mp3 / opus / ...），没有音频轨时返回 ''"""
    r = _run([ff_bin("ffmpeg"), "-hide_banner", "-i", str(path)])
    txt = r.stderr or ""
    m = re.search(r"Stream #\d+:\d+.*?:\s*Audio:\s*([a-z0-9_]+)", txt)
    return (m.group(1).lower() if m else "")


# 塞进 mp4 后浏览器仍能放出声音的音频编码
BROWSER_AUDIO_CODECS = ("aac", "mp3", "mp4a", "alac")


# 浏览器（Chrome/Edge/Firefox）普遍放不出画面、只剩声音的编码
UNPLAYABLE_CODECS = ("hevc", "hev1", "hvc1", "x265")


def needs_transcode(path: Path) -> bool:
    c = video_codec(path)
    return bool(c) and any(c.startswith(b) for b in UNPLAYABLE_CODECS)


def to_h264(src: Path, dst: Path, crf: int = 26) -> Path:
    """把不可播放的视频（如 HEVC）转成 H.264 mp4，保证浏览器预览有画面"""
    check_ffmpeg()
    dst.parent.mkdir(parents=True, exist_ok=True)
    ff = ff_bin("ffmpeg")
    r = _run([ff, "-hide_banner", "-y", "-i", str(src),
              "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf),
              "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
              "-movflags", "+faststart", str(dst)])
    if r.returncode != 0 or not dst.exists() or dst.stat().st_size == 0:
        raise AudioError(f"转码为 H.264 失败：{(r.stderr or '')[-400:]}")
    return dst


def ensure_playable(src: Path, max_height: int | None = None) -> Path:
    """确保浏览器能播放，返回可用的文件路径（多数情况就是原文件本身）

    抖音 / B站拿到的流绝大多数已经是 H.264，直接原样返回；
    只有 HEVC 这类「有声音没画面」的编码才转码。
    max_height 保留参数位但**不用于降分辨率**：为预览去重新编码
    既慢又损画质（实测 12 分钟视频重编码要几十秒），不值得。
    """
    if not needs_transcode(src):
        return src
    tmp = src.with_name(src.stem + "_h264.mp4")
    try:
        to_h264(src, tmp)
        if src.exists():
            src.unlink()
        tmp.replace(src)
        return src
    except Exception:
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass
        raise


def _silence_points(path: Path, noise: str = "-35dB", min_dur: float = 0.6,
                    total_limit: float | None = None) -> list[float]:
    """返回静音区间的中点，作为优先切分点

    total_limit：只扫描音频开头这么多秒。
    首片切片时用它避开「为了找一个切点先扫完整个文件」——
    ffmpeg 的 silencedetect 是全量扫描，长视频要好几秒，
    而首片只需要开头一小段的静音点。
    """
    args = [ff_bin("ffmpeg"), "-hide_banner", "-i", str(path)]
    if total_limit and total_limit > 0:
        args += ["-t", f"{total_limit:.3f}"]
    args += ["-af", f"silencedetect=noise={noise}:d={min_dur}", "-f", "null", "-"]
    r = _run(args)
    starts: list[float] = []
    ends: list[float] = []
    for line in (r.stderr or "").splitlines():
        m = re.search(r"silence_start:\s*([\d.]+)", line)
        if m:
            starts.append(float(m.group(1)))
        m = re.search(r"silence_end:\s*([\d.]+)", line)
        if m:
            ends.append(float(m.group(1)))
    pts = []
    for s, e in zip(starts, ends):
        pts.append((s + e) / 2)
    for s in starts[len(ends):]:
        pts.append(s + min_dur / 2)
    return pts


def _split_points(total: float, chunk: float, silences: list[float]) -> list[float]:
    if total <= chunk:
        return []
    pts: list[float] = []
    pos = 0.0
    while pos + chunk < total:
        target = pos + chunk
        lo, hi = target - chunk * 0.25, target + chunk * 0.25
        cand = [p for p in silences if lo <= p <= hi]
        cut = min(cand, key=lambda p: abs(p - target)) if cand else target
        cut = min(cut, total - 1.0)
        if cut <= pos + 5:
            cut = min(pos + chunk, total - 1.0)
        pts.append(round(cut, 3))
        pos = cut
    return pts


def prepare_chunks(src: Path, outdir: Path, chunk_seconds: int = 600) -> list[dict]:
    """输出 16k 单声道 mp3 分片（同时完成标准化），返回 [{path, offset, duration}]

    保留给「一次性切完」的调用方。流水线路径请用 iter_chunks()。
    """
    return list(iter_chunks(src, outdir, chunk_seconds, stream=False))


def iter_chunks(src: Path, outdir: Path, chunk_seconds: int = 600,
                first_seconds: int = 0, stream: bool = True):
    """逐片产出分片，**边切边交**，让调用方立刻开始转写。

    为什么必须流式：原来 prepare_chunks 会把 N 个分片全部转码完才返回，
    转写线程只能干等。12 分钟视频切 4 片时，光切片就要十几秒——
    这段时间里界面一个字都不会动。改成产出即返回后，
    第 0 片转码完就能立刻送 ASR，切片与转写真正并行。

    first_seconds：首片单独指定的较短长度（秒）。默认 0 = 与其他片一样。
    首片短的意义在于**首句出现的等待时间**：ASR 耗时与音频长度近似成正比，
    180 秒的片要等 30~60 秒才出第一句；首片压到 15 秒则约 3~6 秒。
    代价是总片数变多，但切片与转写并行后总时长反而更短。

    stream=False 时退化成一次性 list（等价于旧的 prepare_chunks）。
    """
    check_ffmpeg()
    outdir.mkdir(parents=True, exist_ok=True)
    total = probe_duration(src)
    if total <= 0:
        raise AudioError("无法读取音频时长，文件可能已损坏。")

    # 短音频：一片搞定。
    # 判断用 first * 1.5 而不是 first：20 秒音频配first=15 时若按
    # `total <= first` 判断会走切片路径，把尾巴 5 秒切成一个 4.5 秒的
    # 碎片——既多一次 ASR 调用，又让首句更慢（第一片只有 15 秒）。
    # 余量留得比首片大一点，才能真正「一次说完」。
    first = first_seconds if (first_seconds and first_seconds > 0) else chunk_seconds
    if total <= first * 1.5:
        dst = outdir / "part_000.mp3"
        _transcode(src, dst)
        yield {"path": str(dst), "offset": 0.0, "duration": probe_duration(dst)}
        return

    # 静音点检测要扫一遍全音频，是一次性的前置开销。
    # 只在真需要多片时才做；首片单独定位，不依赖静音点，
    # 这样「切首片」不必等「扫完全程」。
    if stream:
        # 首片：尽量落在静音点，找不到就按长度硬切。
        # 只扫开头 first*2 秒 —— ffmpeg 的 silencedetect 是顺序扫描，
        # 扫全文件要好几秒，而首片只需要开头一小段。
        # ★不能把这个结果复用给后面的片：它们要的是文件后段的静音点，
        # 而这里只拿到开头的，于是后面全部退化成硬切（会在句子中间断开）。
        silences_head = _silence_points(src, total_limit=first * 2.0)
        cut = _near_silence(silences_head, first)
        if cut <= 5:
            cut = min(float(first), total)
        dst = outdir / "part_000.mp3"
        _transcode(src, dst, 0.0, cut)
        yield {"path": str(dst), "offset": 0.0, "duration": probe_duration(dst)}

        # 余下部分：整体扫静音点，按 chunk_seconds 切
        silences = _silence_points(src)
        offset = cut
        idx = 1
        while offset + 0.5 < total:
            remain = total - offset
            size = min(float(chunk_seconds), remain)
            cut2 = _near_silence(silences, offset + size, lo=offset + 5.0)
            if cut2 <= offset + 5:
                cut2 = offset + size
            # ★不能写 total - 0.5：那样最后一片永远差0.5 秒，
            # 每段末尾几个字会被切掉。这里允许切到音频真正末尾。
            cut2 = min(cut2, total)
            dst = outdir / f"part_{idx:03d}.mp3"
            _transcode(src, dst, offset, cut2 - offset)
            yield {"path": str(dst), "offset": offset,
                   "duration": probe_duration(dst)}
            offset = cut2
            idx += 1
        return

    # ---- 旧的非流式路径：一次算好所有切点 ----
    # 注意：这里同样用 yield 而不是 return out。
    # 生成器函数里的 return值 只是 StopIteration.value，
    # list() 拿不到——写成 `return out` 会让调用方收到空列表（静默失败）。
    silences = _silence_points(src)
    cuts = _split_points(total, float(chunk_seconds), silences)
    bounds = [0.0] + cuts + [total]
    for i in range(len(bounds) - 1):
        start, end = bounds[i], bounds[i + 1]
        dst = outdir / f"part_{i:03d}.mp3"
        _transcode(src, dst, start, end - start)
        yield {"path": str(dst), "offset": start, "duration": probe_duration(dst)}


def _near_silence(silences: list[float], target: float,
                  lo: float = 0.0, window: float | None = None) -> float:
    """在 target 附近挑最接近的静音点；没有就返回 0（调用方自行兜底）"""
    win = window if window is not None else max(10.0, target * 0.15)
    cand = [p for p in silences if lo <= p <= target + win]
    if not cand:
        return 0.0
    return min(cand, key=lambda p: abs(p - target))


def _transcode(src: Path, dst: Path, start: float | None = None, dur: float | None = None) -> None:
    args = [ff_bin("ffmpeg"), "-hide_banner", "-y"]
    if start is not None:
        args += ["-ss", f"{start:.3f}"]
    args += ["-i", str(src)]
    if dur is not None:
        args += ["-t", f"{dur:.3f}"]
    args += ["-vn", "-ac", "1", "-ar", "16000", "-c:a", "libmp3lame", "-b:a", "64k", str(dst)]
    r = _run(args)
    if r.returncode != 0 or not dst.exists():
        raise AudioError(f"音频转码失败：{(r.stderr or '')[-400:]}")
