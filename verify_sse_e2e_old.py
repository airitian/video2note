"""反向验证：把 verify_sse_e2e.py 的判定逻辑换回修复前的版本，
确认三个场景确实会失败。

一个只会通过的测试等于没有测试。这份脚本证明「修复前是坏的」，
从而说明 sse_e2e.txt 里的 ✅ 是有意义的。

用法： python verify_sse_e2e_old.py
"""
from __future__ import annotations

import json
from pathlib import Path

import verify_sse_e2e as e2e

OUT: list[str] = []
_orig_emit = e2e.emit


import asyncio  # noqa: E402

await_sleep = asyncio.sleep

# ---------- 修复前的判定逻辑（core/main.py 旧版）----------
async def gen_old(tid: str):
    t = e2e.STORE[tid]
    last = 0
    idle = 0
    sent_segs = -1
    sent_vp = ""
    while True:
        vp = (t.meta or {}).get("video_path") or ""
        if vp and vp != sent_vp:
            sent_vp = vp
            yield "data: " + json.dumps({"type": "meta", "meta": t.meta},
                                        ensure_ascii=False) + "\n\n"
        while last < len(t.events):
            ev = t.events[last]
            last += 1
            yield "data: " + json.dumps({"type": "log", **ev}, ensure_ascii=False) + "\n\n"
        # ★旧版：partial 被 status == "running" 卡住
        if t.status == "running":
            n = len(t.segments or [])
            if n and n != sent_segs:
                sent_segs = n
                yield "data: " + json.dumps(
                    {"type": "partial", "segments": t.segments,
                     "transcript": t.transcript,
                     "spercent": t.spercent}, ensure_ascii=False) + "\n\n"
        elif t.status != "running":
            sent_segs = -1
        yield "data: " + json.dumps(
            {"type": "state", "status": t.status, "stage": t.stage,
             "percent": t.percent, "spercent": t.spercent,
             "message": t.message, "error": t.error,
             "failed_stage": t.failed_stage}, ensure_ascii=False) + "\n\n"
        if t.status in ("done", "failed", "canceled"):
            yield "data: " + json.dumps(
                {"type": "eof", "status": t.status,
                 "segments": t.segments or [], "transcript": t.transcript or "",
                 "note": t.note or "", "note_sig": t.note_sig or "",
                 "note_info": t.note_info or {}, "variants": t.variants or {},
                 "meta": t.meta or {}}, ensure_ascii=False) + "\n\n"
            break
        idle += 1
        # ★旧版：idle 只增不减，900拍 = 12 分钟必掐断
        if idle > 900:
            break
        await await_sleep(0.8)


e2e.gen = gen_old

SCEN = [
    ("场景 1：短视频单分片", e2e.sc_short, 8, True),
    ("场景 2：长视频末批与状态切换相撞", e2e.sc_long, 50, True),
    ("场景 3：下载阶段无分片但状态推进", e2e.sc_idle, 1, True),
]

# 复用 run_scenario，但把结果写进我们自己的缓冲区
results = []
for name, fn, expect, want_eof in SCEN:
    results.append(e2e.run_scenario("[旧逻辑] " + name, fn, expect, want_eof))

_path = Path(__file__).with_name("sse_e2e.txt")
_txt = _path.read_text(encoding="utf-8")
_path.write_text(
    _txt + "\n\n" + "=" * 68 + "\n"
    + "以下为「修复前」的同一组场景（应全部失败）\n"
    + "=" * 68 + "\n\n"
    + "\n".join(e2e.OUT) + "\n\n"
    + f"旧逻辑通过数：{sum(results)}/{len(results)}"
    + "（预期 0；若有场景通过，说明该场景不具鉴别力）\n",
    encoding="utf-8")
