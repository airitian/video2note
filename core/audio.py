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
    """合并视频轨与音频轨为可直接在浏览器播放的 mp4"""
    check_ffmpeg()
    out.parent.mkdir(parents=True, exist_ok=True)
    ff = ff_bin("ffmpeg")
    common = ["-map", "0:v:0", "-map", "1:a:0", "-shortest", "-movflags", "+faststart"]
    attempts = [
        ["-c:v", "copy", "-c:a", "aac", "-b:a", "128k"],
        ["-c:v", "libx264", "-preset", "veryfast", "-crf", "26", "-c:a", "aac", "-b:a", "128k"],
    ]
    last = ""
    for enc in attempts:
        r = _run([ff, "-hide_banner", "-y", "-i", str(video), "-i", str(audio)] + enc + common + [str(out)])
        last = (r.stderr or "")[-400:]
        if r.returncode == 0 and out.exists() and out.stat().st_size > 0:
            return out
    raise AudioError(f"视频合并失败：{last}")


def video_codec(path: Path) -> str:
    """探测视频轨编码名（h264 / hevc / ...），没有视频轨时返回 ''"""
    r = _run([ff_bin("ffmpeg"), "-hide_banner", "-i", str(path)])
    txt = r.stderr or ""
    m = re.search(r"Stream #\d+:\d+.*?:\s*Video:\s*([a-z0-9_]+)", txt)
    return (m.group(1).lower() if m else "")


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


def _silence_points(path: Path, noise: str = "-35dB", min_dur: float = 0.6) -> list[float]:
    """返回静音区间的中点，作为优先切分点"""
    r = _run([ff_bin("ffmpeg"), "-hide_banner", "-i", str(path),
              "-af", f"silencedetect=noise={noise}:d={min_dur}", "-f", "null", "-"])
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
    """输出 16k 单声道 mp3 分片（同时完成标准化），返回 [{path, offset, duration}]"""
    check_ffmpeg()
    outdir.mkdir(parents=True, exist_ok=True)
    total = probe_duration(src)
    if total <= 0:
        raise AudioError("无法读取音频时长，文件可能已损坏。")

    if total <= chunk_seconds:
        dst = outdir / "part_000.mp3"
        _transcode(src, dst)
        return [{"path": str(dst), "offset": 0.0, "duration": probe_duration(dst)}]

    silences = _silence_points(src)
    cuts = _split_points(total, float(chunk_seconds), silences)
    bounds = [0.0] + cuts + [total]
    chunks: list[dict] = []
    for i in range(len(bounds) - 1):
        start, end = bounds[i], bounds[i + 1]
        dst = outdir / f"part_{i:03d}.mp3"
        _transcode(src, dst, start, end - start)
        chunks.append({"path": str(dst), "offset": start, "duration": probe_duration(dst)})
    return chunks


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
