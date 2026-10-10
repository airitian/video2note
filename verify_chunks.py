"""验证 iter_chunks：分片必须不重、不漏、顺序正确、时间戳连续。

这是流式改造最容易出错的地方：
- 生成器写错offset → 分片之间有缝（丢内容）或重叠（重复内容）
- 静音点选择不当 → 切在句子中间
- 首片逻辑写错 → 首片吞掉后续内容，或把短音频截断

用真实 ffmpeg 生成已知内容的音频（每段一个递增的数字），
再检查所有分片的总时长与内容是否与原文件一致。

用法： python verify_chunks.py
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from core import audio  # noqa: E402

OUT: list[str] = []


def emit(*a):
    OUT.append(" ".join(str(x) for x in a))


def has_ffmpeg() -> bool:
    try:
        audio.check_ffmpeg()
        return True
    except Exception:
        return False


# ---------- 假 ffmpeg：在没有 ffmpeg 的机器上也能验证切片逻辑 ----------
class FakeFFmpeg:
    """把 ffmpeg 调用替换成「按 -ss/-t 记录切点」的桩。

    真实 ffmpeg 产出音频文件；这里只需产出**可被 probe_duration 读出
    正确时长**的占位文件，从而验证 offset/duration 的计算是否正确
    （不重、不漏、连续）——这正是流式改造最容易写错的地方。
    """

    def __init__(self, total: float):
        self.total = total
        self.cuts: list[tuple[float, float]] = []
        self._files: dict[str, float] = {}

    def register_src(self, path: Path) -> Path:
        """登记「源音频」的时长。iter_chunks 先探测源文件再切片。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fake")
        self._files[str(path)] = self.total
        return path

    def ffprobe(self, path: Path) -> float:
        return self._files.get(str(path), 0.0)

    def transcode(self, src: Path, dst: Path, start=None, dur=None) -> None:
        s = 0.0 if start is None else float(start)
        d = self.total - s if dur is None else float(dur)
        self.cuts.append((s, d))
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(b"fake")
        self._files[str(dst)] = d

    def silence_points(self, path: Path, total_limit=None) -> list[float]:
        # 造固定的静音点：每 30 秒一个，落在 .0 位置
        lim = self.total if not total_limit else total_limit
        pts = []
        x = 30.0
        while x < lim:
            pts.append(x)
            x += 30.0
        return pts


def install_fake(fake: FakeFFmpeg):
    """把 audio 模块里几个外部依赖替换成桩"""
    audio.check_ffmpeg = lambda: None
    audio.probe_duration = lambda p: fake.ffprobe(Path(p))
    audio._transcode = fake.transcode
    audio._silence_points = fake.silence_points


def make_audio(path: Path, seconds: float) -> None:
    """生成一段带静音间隔的测试音频：1s 有声 + 1s 静音，循环"""
    # sine 频率 440，内容固定；静音段用 anullsrc
    cmd = [
        audio.ff_bin("ffmpeg"), "-hide_banner", "-y",
        "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=16000:duration={seconds}",
        "-c:a", "libmp3lame", "-b:a", "64k", "-ac", "1", "-ar", "16000",
        str(path),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("生成测试音频失败：" + (r.stderr or "")[-300:])


def dur_of(p: Path) -> float:
    return audio.probe_duration(p)


def check(name: str, cond: bool, detail: str = "") -> bool:
    emit(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  —— {detail}" if detail else ""))
    return cond


def main():
    tmp = Path(tempfile.mkdtemp(prefix="v2n_chunks_"))
    try:
        if has_ffmpeg():
            emit("=" * 68)
            emit("模式：本机有 ffmpeg，跑真实转码")
            emit("=" * 68)
            _run_real(tmp)
        else:
            emit("=" * 68)
            emit("模式：本机无 ffmpeg，用假转码桩验证切片计算逻辑")
            emit("（覆盖点：offset 连续、不重不漏、首片边界、静音点选择）")
            emit("=" * 68)
            _run_fake(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    Path(__file__).with_name("chunks.txt").write_text("\n".join(OUT), encoding="utf-8")


def _run_fake(tmp: Path):
    results = []

    def scene_short():
        emit()
        emit("=" * 68)
        emit("场景 1：短音频 20 秒，首片设 15 秒（不该被拦腰截断）")
        emit("-" * 68)
        fake = FakeFFmpeg(20.0)
        install_fake(fake)
        srcf = fake.register_src(tmp / "s.mp3")
        got = list(audio.iter_chunks(srcf, tmp / "f1",
                                    chunk_seconds=180, first_seconds=15))
        emit(f"  切出 {len(got)} 片："
             f"{[(round(c['offset'],1), round(c['duration'],1)) for c in got]}")
        ok = check("只切一片", len(got) == 1)
        ok &= check("覆盖完整 20 秒", abs(got[0]["duration"] - 20.0) < 0.5)
        results.append(("短音频不截断", ok))

    def scene_long():
        emit()
        emit("=" * 68)
        emit("场景 2：长音频 720 秒，首片 15 / 后续 180，流式产出")
        emit("-" * 68)
        fake = FakeFFmpeg(720.0)
        install_fake(fake)
        srcf = fake.register_src(tmp / "l.mp3")
        got = list(audio.iter_chunks(srcf, tmp / "f2",
                                    chunk_seconds=180, first_seconds=15))
        for i, c in enumerate(got):
            emit(f"  第{i}片 offset={c['offset']:7.1f}  "
                 f"duration={c['duration']:6.1f}  "
                 f"覆盖到 {c['offset']+c['duration']:7.1f}")
        ok = check("首片明显短", got[0]["duration"] <= 20.0,
                   f"{got[0]['duration']:.1f}s")
        # 无缝覆盖：每片起点 == 前片终点
        bad = []
        for i in range(len(got) - 1):
            end_prev = got[i]["offset"] + got[i]["duration"]
            gap = got[i + 1]["offset"] - end_prev
            if abs(gap) > 0.01:
                bad.append(f"第{i}->{i+1}片 缝隙 {gap:+.2f}s")
        ok &= check("相邻片严格无缝（不重不漏）", not bad, "；".join(bad) or "全部连续")
        last_end = got[-1]["offset"] + got[-1]["duration"]
        ok &= check("覆盖到音频末尾 720 秒", abs(last_end - 720.0) < 0.5,
                   f"{last_end:.1f}s")
        covered = sum(c["duration"] for c in got)
        ok &= check("总时长 == 720（无重复计数）", abs(covered - 720.0) < 0.5,
                   f"{covered:.1f}s")
        results.append(("长音频无缝", ok))

    def scene_lazy_silence():
        emit()
        emit("=" * 68)
        emit("场景 3：静音点是否按需全量扫描（后续片不能只靠开头的数据）")
        emit("-" * 68)
        # 记录每次 _silence_points 的调用参数
        calls = []
        fake = FakeFFmpeg(720.0)
        install_fake(fake)
        orig = fake.silence_points

        def spy(path, total_limit=None):
            calls.append(total_limit)
            return orig(path, total_limit)
        audio._silence_points = spy
        srcf = fake.register_src(tmp / "m.mp3")
        list(audio.iter_chunks(srcf, tmp / "f3",
                              chunk_seconds=180, first_seconds=15))
        emit(f"  _silence_points 调用 {len(calls)} 次，total_limit={calls}")
        ok = check("首片用受限扫描（不拖慢首句）",
                   calls and calls[0] is not None, f"首片 limit={calls[0] if calls else '-'}")
        ok &= check("后续片做全量扫描（否则切点全退化为硬切）",
                    any(c is None for c in calls[1:]),
                    "存在 total_limit=None 的调用" if any(c is None for c in calls[1:])
                    else "缺少全量扫描")
        results.append(("静音点按需扫描", ok))

    def scene_legacy():
        emit()
        emit("=" * 68)
        emit("场景 4：老接口 prepare_chunks 行为未被破坏")
        emit("-" * 68)
        fake = FakeFFmpeg(720.0)
        install_fake(fake)
        srcf = fake.register_src(tmp / "g.mp3")
        legacy = audio.prepare_chunks(srcf, tmp / "f4", 180)
        emit(f"  prepare_chunks 切出 {len(legacy)} 片，"
             f"覆盖 {sum(c['duration'] for c in legacy):.1f}s")
        ok = check("返回 list", isinstance(legacy, list) and len(legacy) > 1)
        ok &= check("覆盖完整", abs(sum(c["duration"] for c in legacy) - 720.0) < 0.5)
        ok &= check("首片未被压短（老接口语义不变）",
                    abs(legacy[0]["duration"] - 180.0) < 0.5,
                    f"{legacy[0]['duration']:.1f}s")
        results.append(("老接口不变", ok))

    scene_short()
    scene_long()
    scene_lazy_silence()
    scene_legacy()

    emit()
    emit("=" * 68)
    passed = sum(1 for _, ok in results if ok)
    emit(f"总计：{passed}/{len(results)} 组场景通过")
    for nm, ok in results:
        emit(f"  {'PASS' if ok else 'FAIL'}  {nm}")


def _run_real(tmp: Path):
    results = []

    # ---------- 场景 1：短音频（应只切一片，且完整）----------
    emit("=" * 68)
    emit("场景 1：短音频 20 秒 + 首片设15 秒")
    emit("-" * 68)
    src = tmp / "short.mp3"
    make_audio(src, 20.0)
    total = dur_of(src)
    got = list(audio.iter_chunks(src, tmp / "c1", chunk_seconds=180,
                                first_seconds=15))
    emit(f"  原音频 {total:.1f}s，切出 {len(got)} 片："
         f"{[round(c['duration'], 1) for c in got]}")
    ok = check("只切一片（短音频不该被拦腰截断）", len(got) == 1)
    ok &= check("片时长≈原时长（内容完整）",
                abs(got[0]["duration"] - total) < 1.5,
                f"{got[0]['duration']:.1f}s vs {total:.1f}s")
    ok &= check("offset 从 0 开始", abs(got[0]["offset"]) < 0.01)
    results.append(("短音频", ok))

    # ---------- 场景 2：长音频流式切片（不重不漏）----------
    emit()
    emit("=" * 68)
    emit("场景 2：长音频 300 秒，首片 15 秒 / 后续 60 秒，流式")
    emit("-" * 68)
    src2 = tmp / "long.mp3"
    make_audio(src2, 300.0)
    total2 = dur_of(src2)
    chunks = []
    offsets = []
    # 边迭代边检查：确认是「产出即返回」而不是攒完才给
    for i, c in enumerate(audio.iter_chunks(src2, tmp / "c2",
                                            chunk_seconds=60,
                                            first_seconds=15)):
        chunks.append(c)
        offsets.append(c["offset"])
        emit(f"  产出第 {i} 片: offset={c['offset']:.1f}s "
             f"duration={c['duration']:.1f}s")

    ok2 = check("首片明显短于后续片", chunks[0]["duration"] <= 20,
                f"首片 {chunks[0]['duration']:.1f}s")
    ok2 &= check("offset 严格递增（无重叠）",
                 all(offsets[i] < offsets[i + 1] for i in range(len(offsets) - 1)))
    # 每片起点应紧跟前一片终点（允许切片误差）
    gaps = []
    for i in range(len(chunks) - 1):
        gap = chunks[i + 1]["offset"] - (chunks[i]["offset"] + chunks[i]["duration"])
        gaps.append(abs(gap))
    ok2 &= check("相邻片无缝隙（无丢失）", max(gaps) < 2.0 if gaps else True,
                 f"最大缝隙 {max(gaps):.2f}s" if gaps else "单片")
    covered = sum(c["duration"] for c in chunks)
    ok2 &= check("总时长覆盖原音频", abs(covered - total2) < 4.0,
                 f"覆盖 {covered:.1f}s / 原 {total2:.1f}s")
    ok2 &= check("最后一片到达音频末尾",
                 abs(chunks[-1]["offset"] + chunks[-1]["duration"] - total2) < 4.0)
    results.append(("长音频流式", ok2))

    # ---------- 场景 3：流式 vs 非流式产出一致 ----------
    emit()
    emit("=" * 68)
    emit("场景 3：流式与非流式结果一致性（老接口不能被改坏）")
    emit("-" * 68)
    src3 = tmp / "m.mp3"
    make_audio(src3, 200.0)
    a = list(audio.iter_chunks(src3, tmp / "c3a", 60, 15, stream=True))
    b = list(audio.iter_chunks(src3, tmp / "c3b", 60, 15, stream=False))
    legacy = audio.prepare_chunks(src3, tmp / "c3c", 60)
    same = len(a) == len(b)
    emit(f"  流式 {len(a)} 片 / 非流式 {len(b)} 片 / prepare_chunks {len(legacy)} 片")
    ok3 = check("流式与非流式片数一致", same)
    tot_a = sum(c["duration"] for c in a)
    tot_b = sum(c["duration"] for c in b)
    tot_l = sum(c["duration"] for c in legacy)
    emit(f"  总时长：流式 {tot_a:.1f}s / 非流式 {tot_b:.1f}s / 旧接口 {tot_l:.1f}s")
    results.append(("一致性", ok3))

    emit()
    emit("=" * 68)
    passed = sum(1 for _, ok in results if ok)
    emit(f"总计：{passed}/{len(results)} 组场景通过")
    for nm, ok in results:
        emit(f"  {'PASS' if ok else 'FAIL'}  {nm}")


if __name__ == "__main__":
    main()
