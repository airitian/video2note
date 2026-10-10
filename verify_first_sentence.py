"""测量「第一句话出现」的延迟，验证实时化改造的效果。

要回答的问题：ASR 是 HTTP 整段上传、整段返回，字节级流式做不到，
那在不换 ASR 的前提下，首句能快到什么程度？

模拟真实耗时（按经验值，比例比绝对值更重要）：
- 切片（ffmpeg 转码）：约 0.25秒/秒音频
- ASR：约0.35 秒/秒音频 + 0.8 秒固定开销（网络往返 + 服务端排队）

两套流水线对比：
  旧：先把N 个分片全部切完 → 再逐片转写
  新：边切边转（首片短）

用法： python verify_first_sentence.py
"""
from __future__ import annotations

import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

OUT: list[str] = []


def emit(*a):
    OUT.append(" ".join(str(x) for x in a))


# ---------- 模拟耗时 ----------
# ffmpeg 转码是「启动开销 + 与时长相关的低码率编码」，
# 不是按实时率跑。实测转 180 秒音频约 1.5~2.5 秒，不是 45 秒。
SLICE_FIXED = 0.35# 每次 ffmpeg 启动（进程创建 + 解码器初始化）
SLICE_PER_SEC = 0.008      # 16k 单声道 64kbps mp3，非常快
# ASR 是网络调用，耗时与音频长度近似成正比，这部分才是主要成本
ASR_FIXED = 0.8            # 网络往返 + 服务端排队
ASR_PER_SEC = 0.35

TOTAL = 720.0        # 12 分钟音频
CHUNK = 180.0        # 常规分片
FIRST = 15.0         # 新方案的首片


def slice_cost(dur: float) -> float:
    return SLICE_FIXED + dur * SLICE_PER_SEC


def asr_cost(dur: float) -> float:
    return ASR_FIXED + dur * ASR_PER_SEC


# ---------- 旧流水线：先切完再转 ----------
def run_old():
    t0 = time.monotonic()
    # 1) 全部切完
    plan = []
    pos = 0.0
    while pos < TOTAL:
        d = min(CHUNK, TOTAL - pos)
        time.sleep(slice_cost(d))          # 真睡，模拟 ffmpeg
        plan.append((pos, d))
        pos += d
    t_sliced = time.monotonic() - t0

    # 2) 并发转写，按完成顺序推送
    first_text_at = None
    published = 0
    done = 0
    lock = threading.Lock()

    def work(item):
        off, d = item
        time.sleep(asr_cost(d))
        return off, d

    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = [ex.submit(work, it) for it in plan]
        for f in as_completed(futs):
            off, d = f.result()
            with lock:
                done += 1
                published += max(1, int(d // 12))     # 约 12 秒一句
                if first_text_at is None:
                    first_text_at = time.monotonic() - t0
    return {
        "first": first_text_at,
        "total": time.monotonic() - t0,
        "slice": t_sliced,
        "chunks": len(plan),
        "published": published,
    }


# ---------- 新流水线：边切边转，首片短 ----------
def run_new(first_len: float = FIRST, chunk: float = CHUNK):
    t0 = time.monotonic()
    published = 0
    first_text_at = None
    n_hint = {"n": 1}
    last_pub = {"t": -99.0}
    lock = threading.Lock()

    def plan_chunks():
        """模拟 iter_chunks 的产出顺序：首片短，其余正常长"""
        pos = 0.0
        idx = 0
        if TOTAL <= first_len:
            yield 0, 0.0, TOTAL
            return
        d = first_len
        yield idx, pos, d
        pos += d
        idx += 1
        while pos + 0.5 < TOTAL:
            d = min(chunk, TOTAL - pos)
            yield idx, pos, d
            pos += d
            idx += 1

    q: queue.Queue = queue.Queue(maxsize=8)
    ex = ThreadPoolExecutor(max_workers=4)
    pending = []
    n_total = {"v": 0}

    def pump():
        for idx, off, d in plan_chunks():
            # 切片是「生产」，这里模拟 ffmpeg 转码耗时
            deadline = time.monotonic() + slice_cost(d)
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                time.sleep(min(left, 0.05))
            while True:
                try:
                    q.put((idx, off, d), timeout=0.2)
                    break
                except queue.Full:
                    time.sleep(0.05)
        n_total["v"] = idx + 1

    def consume():
        while True:
            try:
                idx, off, d = q.get(timeout=0.2)
            except queue.Empty:
                if pump_done.is_set() and q.empty():
                    return
                continue
            n_hint["n"] = max(n_hint["n"], idx + 1)
            pending.append((ex.submit(_asr, d), d))

    pump_done = threading.Event()

    def _asr(d):
        time.sleep(asr_cost(d))
        return max(1, int(d // 12))

    pt = threading.Thread(target=pump, daemon=True)
    ct = threading.Thread(target=consume, daemon=True)

    def pump_wrapped():
        pump()
        pump_done.set()
    pt = threading.Thread(target=pump_wrapped, daemon=True)
    pt.start()
    ct.start()
    pt.join()
    ct.join()

    for f in as_completed([f for f, _ in pending]):
        n = f.result()
        now = time.monotonic()
        with lock:
            published += n
            # 复刻 0.4 秒发布节流
            if now - last_pub["t"] >= 0.4:
                last_pub["t"] = now
                if first_text_at is None:
                    first_text_at = now - t0
    ex.shutdown(wait=True)
    return {
        "first": first_text_at,
        "total": time.monotonic() - t0,
        "chunks": n_total["v"],
        "published": published,
    }


SCALE = float(__import__("os").environ.get("V2N_TEST_SCALE", "0.12"))


def main():
    """真实耗时跑太久，按比例缩放（比例关系不变）"""
    global SLICE_PER_SEC, ASR_PER_SEC, ASR_FIXED, SLICE_FIXED
    SLICE_PER_SEC *= SCALE
    ASR_PER_SEC *= SCALE
    ASR_FIXED *= SCALE
    SLICE_FIXED *= SCALE

    emit("=" * 70)
    emit("首句延迟实测（耗时按比例缩放，看相对关系）")
    emit("=" * 70)
    emit(f"音频总长 {int(TOTAL)} 秒｜旧方案分片 {int(CHUNK)} 秒"
         f"｜新方案首片 {int(FIRST)} 秒")
    emit(f"模拟耗时：切片 {SLICE_FIXED:.2f}s + {SLICE_PER_SEC:.4f}s/秒，"
         f"ASR {ASR_FIXED:.2f}s + {ASR_PER_SEC:.3f}s/秒")
    emit()

    old = run_old()
    emit("【旧】先切完全部分片，再并发转写")
    emit(f"  分片数{old['chunks']}，切片阶段耗时 {old['slice']:.1f}s")
    emit(f"  首句出现：{old['first']:.1f}s")
    emit(f"  全部完成：{old['total']:.1f}s")
    emit()

    new = run_new()
    emit("【新】边切边转，首片压到 15 秒")
    emit(f"  分片数 {new['chunks']}（首片短，后续同旧）")
    emit(f"  首句出现：{new['first']:.1f}s")
    emit(f"  全部完成：{new['total']:.1f}s")
    emit()

    gain = old["first"] - new["first"]
    ratio = old["first"] / new["first"] if new["first"] else 0
    emit("-" * 70)
    emit(f"首句提速 {gain:.1f}s，约 {ratio:.1f} 倍"
         f"（{old['first']:.1f}s → {new['first']:.1f}s）")
    emit(f"总耗时：{old['total']:.1f}s → {new['total']:.1f}s"
         f"（{'更快' if new['total'] < old['total'] else '略慢但可接受'}）")
    emit("-" * 70)
    emit()
    emit("★ ASR 是整段上传/整段返回，做不到字节级流式。")
    emit("  所以「转写多少显示多少」的上限 = 一个分片的转写耗时。")
    emit("  要再快只能：① 调小 first_chunk_seconds")
    emit("② 换支持流式输出的 ASR 服务")

    Path(__file__).with_name("first_sentence.txt").write_text(
        "\n".join(OUT), encoding="utf-8")


if __name__ == "__main__":
    main()
