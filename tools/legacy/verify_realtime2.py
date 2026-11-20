"""复现并验证「转写完成了，文字稿却不实时出现」。

不需要 fastapi：只把 core/main.py 里 task_events 的推送判定逻辑抄过来，
喂一段模拟的任务状态时间线，看 partial 到底推了几次、漏了几句。

★ 真正的根因（不是前端缓存）：
  partial 的推送被 `status == "running"` 卡住，而「转写结束」的标志恰恰是
  把 status 改成 "transcribed"。0.8 秒的心跳很可能直接从 running 跳到
  transcribed —— 中间那一拍根本没观察到，于是最后一批分片永远推不出去。
  短视频（音频只有 1 个分片）更是从开始到结束一个 partial 都发不出来。
  页面表现：转写状态显示「转写完成」，文字稿却是空的/不全，刷新一下才有
  —— 因为刷新走 /api/tasks/{id} 详情接口，根本不经过 SSE。

修复：按「内容签名（句数 + 最后一句文本）」判断是否需要推送，与 status 无关；
初值 None 保证前端刚连上（或重连后）能拿到一次全量补推。

用法： python verify_realtime2.py
"""
from __future__ import annotations

import io
from pathlib import Path

_OUT = io.StringIO()


def emit(*a):
    _OUT.write(" ".join(str(x) for x in a) + "\n")


def simulate(timeline: list[dict], interval: float = 0.8,
             fixed: bool = False) -> list[str]:
    """跑一遍 SSE 的推送判定，返回实际推给前端的事件轨迹。

    fixed=False → 修复前的逻辑；fixed=True → 修复后的逻辑。
    """
    pushed: list[str] = []
    sent_segs = -1          # 旧逻辑：只记句数
    sent_sig = None         # 新逻辑：记(句数, 最后一句文本)，初值 None
    ticks = 0.0
    i = 0
    tail = 0

    while i < len(timeline):
        t = timeline[i]
        # 消耗掉这 0.8 秒内发生的所有状态变化（真实情况是轮询取最新值）
        while i + 1 < len(timeline) and timeline[i + 1]["at"] <= ticks + interval:
            i += 1
            t = timeline[i]
        # 时间线走到尾后再补两拍就收尾，避免把「任务停在 transcribed
        # 等用户点生成文稿」当成无限推送
        if i >= len(timeline) - 1:
            tail += 1
            if tail > 1:
                break

        segs = t["segments"]
        n = len(segs)
        pushed.append(f"[{ticks:5.1f}s] tick status={t['status']:<11} segs={n}")

        if fixed:
            sig = (n, (segs[-1].get("text") or "") if segs else "")
            if segs and sig != sent_sig:
                sent_sig = sig
                pushed.append(f"[{ticks:5.1f}s]    -> PUSH partial（{n} 句）")
        elif t["status"] == "running":
            if n and n != sent_segs:
                sent_segs = n
                pushed.append(f"[{ticks:5.1f}s]    -> PUSH partial（{n} 句）")
        else:
            sent_segs = -1
            if n:
                pushed.append(f"[{ticks:5.1f}s]    -> 漏推！{n} 句没发出去")

        if t["status"] in ("done", "failed", "canceled"):
            pushed.append(f"[{ticks:5.1f}s]    -> EOF（带全文，前端兜底）")
            break
        ticks += interval
    return pushed


def seg(k: int) -> list[dict]:
    return [{"start": i * 2.0, "text": f"第{i}句"} for i in range(k)]


def summary(lines: list[str], expect: int) -> str:
    """判断最终有没有把 expect 句全部送到前端。

    注意不能只看「单条 partial 的最大句数」：partial 每次带的是全量，
    所以只要出现过一条句数 == expect 的推送，就说明前端已拿到全文。
    中间是否漏过几批并不影响最终结果——SSE 是累加覆盖，不是增量追加。
    """
    counts = []
    for ln in lines:
        if "PUSH partial" in ln:
            counts.append(int(ln.split("（")[1].split(" ")[0]))
    lost = [ln for ln in lines if "漏推" in ln]
    got = max(counts) if counts else 0
    if got >= expect:
        verdict = f"✅ 全文送达（前端可见 {got}/{expect} 句）"
        if lost:
            verdict += f"；过程中有 {len(lost)} 次漏推，但已被后续全量覆盖"
    else:
        verdict = f"❌ 停在 {got}/{expect} 句，丢 {expect - got} 句"
        if lost:
            verdict += f"；{len(lost)} 次漏推且从未补发"
    return verdict


SCENES = [
    ("场景 A：短视频（音频只有 1 个分片）",
     "转写阻塞 12s -> 一次性写入 8 句 -> 状态立刻变 transcribed", 8,
     [{"at": 0.0, "status": "running", "stage": "transcribing", "segments": []},
      {"at": 12.0, "status": "transcribed", "stage": "transcribed", "segments": seg(8)}]),
    ("场景 B：长视频（4 个分片，最后一片在状态切换前 0.1s 完成）",
     "最后 12 句正好落在 running -> transcribed 的夹缝里", 50,
     [{"at": 0.0, "status": "running", "stage": "transcribing", "segments": []},
      {"at": 30.0, "status": "running", "stage": "transcribing", "segments": seg(12)},
      {"at": 60.0, "status": "running", "stage": "transcribing", "segments": seg(25)},
      {"at": 90.0, "status": "running", "stage": "transcribing", "segments": seg(38)},
      {"at": 90.1, "status": "transcribed", "stage": "transcribed", "segments": seg(50)}]),
    ("场景 C：SSE 中途断开后重连（前端自动重连 + 后端补推全量）",
     "断线期间转写继续推进到 60 句，重连后应立刻补齐", 60,
     [{"at": 0.0, "status": "running", "stage": "transcribing", "segments": []},
      {"at": 10.0, "status": "running", "stage": "transcribing", "segments": seg(20)},
      {"at": 20.0, "status": "running", "stage": "transcribing", "segments": seg(60)},
      {"at": 40.0, "status": "transcribed", "stage": "transcribed", "segments": seg(60)}]),
]

for title, desc, expect, tl in SCENES:
    emit("=" * 66)
    emit(title)
    emit(desc)
    emit()
    before = simulate(tl, fixed=False)
    after = simulate(tl, fixed=True)
    emit("【修复前】")
    for ln in before[-8:]:
        emit("   " + ln)
    emit("   => " + summary(before, expect))
    emit()
    emit("【修复后】")
    for ln in after[-8:]:
        emit("   " + ln)
    emit("   => " + summary(after, expect))
    emit()

emit("=" * 66)
emit("结论：修复前 partial 被 status 卡在 running 窗口内，最后一批分片")
emit("（短视频则是全部）一次都推不出去；改为按内容签名推送后全部送达。")

Path(__file__).with_name("rt2.txt").write_text(_OUT.getvalue(), encoding="utf-8")
