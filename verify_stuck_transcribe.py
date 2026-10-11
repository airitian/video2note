"""复现「语音转写一直 0%」：任务早已 transcribed，但页面没更新。

现场事实（云端截图 + 本地任务 JSON）
------------------------------------
截图：语音切片 100%、语音转写 **0%**、文案「开始语音转写」。
任务 JSON：status=transcribed、stage=transcribed、spercent=100、11 句、562 字。
→ **后端早就跑完了，是前端停在最后一帧。**

本脚本直接喂真实任务状态给 SSE 生成器，看它到底推不推 partial / eof。
"""
import asyncio
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import core.store as store
import core.main as main

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name + (("  -> " + detail) if detail and not cond else ""))


# ---------------------------------------------------------------------------
# 场景：任务已经 transcribed（SSE 连上来的那一刻就是终态）
# 这是最要命的情况——前端刚建连接时后端就已经结束了。
print("[场景] 任务在SSE 连上之前就已 transcribed")

t = store.Task(id="stuck")
raw = json.loads(pathlib.Path("data/tasks/52a97e194458.json").read_text("utf-8"))
t.status = "transcribed"
t.stage = "transcribed"
t.percent = 100
t.spercent = 100
t.message = "开始语音转写"
t.segments = raw["segments"]
t.transcript = raw["transcript"]
store._tasks["stuck"] = t


async def run(tid):
    resp = await main.task_events(tid)
    bag = []
    async for c in resp.body_iterator:
        bag.append(c)
    txt = "".join(bag)
    if isinstance(bag[0] if bag else "", bytes):  # pragma: no cover
        txt = txt.decode("utf-8")
    return txt


txt = asyncio.run(run("stuck"))
kinds = [json.loads(p[6:])["type"] for p in txt.split("\n\n")
         if p.startswith("data: ") and p[6:].strip()]

print("    收到事件序列：", kinds)
check("#1 连上即补推 partial（前端能立刻拿到文字）", "partial" in kinds, str(kinds))
check("#2 收到 eof", "eof" in kinds, str(kinds))
check("#3 eof 带着 segments 兜底", '"segments"' in txt, txt[:160])
check("#4 不会空占连接（生成器正常结束）", True)

# ---------------------------------------------------------------------------
# ★核心怀疑：前端拿到 eof 后，还会再调 refreshOne 吗？
# 若 eof 之后前端只 render、不刷新，而 ws.linesSig 守卫把它挡掉……
print()
print("[核心] 前端 partial 处理后是否强制重绘")

app_js = pathlib.Path("static/app.js").read_text("utf-8")
seg = app_js[app_js.find('ev.type === "partial"'):]
seg = seg[:seg.find('} else if')]
print(seg.rstrip())

check("#5 partial 会清掉 linesSig 缓存守卫（强制重绘）",
      "ws.linesSig = null" in seg, seg[:200])

# ---------------------------------------------------------------------------
print()
print("[场景] 任务还在 running 且已有分片（SSE 中途连上）")
t2 = store.Task(id="mid")
t2.status = "running"
t2.stage = "transcribing"
t2.spercent = 42
t2.segments = raw["segments"][:3]
t2.transcript = "\n".join(s["text"] for s in t2.segments)
store._tasks["mid"] = t2

async def run_bounded(tid, max_events=4, timeout=5):
    """running 任务的生成器会一直发心跳、永不返回，所以用超时截断，
    只验证「中途连上能拿到 partial、且不会误发 eof」即可。"""
    resp = await main.task_events(tid)
    bag = []
    timed_out = False
    try:
        async with asyncio.timeout(timeout):
            async for c in resp.body_iterator:
                bag.append(c)
                if len(bag) >= max_events:
                    break
    except TimeoutError:
        timed_out = True
    txt = "".join(bag)
    if bag and isinstance(bag[0], bytes):
        txt = txt.decode("utf-8")
    return txt, timed_out


txt2, to2 = asyncio.run(run_bounded("mid"))
kinds2 = [json.loads(p[6:])["type"] for p in txt2.split("\n\n")
          if p.startswith("data: ") and p[6:].strip()]
print("    收到事件序列：", kinds2[:6], "| 连接持续(超时截断)=", to2)
check("#6 running 中途连上能拿到 partial", "partial" in kinds2, str(kinds2))
check("#7 running 时不误发 eof（否则会提前断连）", "eof" not in kinds2, str(kinds2))

# ---------------------------------------------------------------------------
print()
print("=" * 60)
print(f"  通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
if FAIL:
    print("  失败项：", ", ".join(FAIL))
    sys.exit(1)
print("  全部通过 ✓")
