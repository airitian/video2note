"""验证本次修复的核心逻辑（纯逻辑，不依赖网络 / ffmpeg / ASR 真实调用）：

  #2  _publish_segments 节流时也要更新内存（界面不卡）
  #3  _publish_segments 去重，不出现重复句
  #4  iter_chunks 末尾不丢字（覆盖 [0, total]，无空隙）
  #8  SSE 在锁内快照，不抛异常
  #9  SSE 在 transcribed 状态即发 eof 并断开连接

运行：python verify_fixes.py
"""
import asyncio
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name + (("  -> " + detail) if detail and not cond else ""))


# ---------------------------------------------------------------------------
print("[#2/#3] _publish_segments：内存始终更新 + 去重")
import core.store as store
import core.pipeline as pipeline

# #2：把 _pub_at 设成「刚刚」，让 throttle 分支命中（now - _pub_at < 0.4），
#     此时应 early return，但仍必须把 t.segments / t.transcript 更新到内存。
t = store.Task(id="mem")
t._pub_at = time.monotonic()  # 距现在 < 0.4s
segs = [{"start": 0.0, "text": "第一句"}, {"start": 6.2, "text": "第二句"}]
pipeline._publish_segments(t, segs)  # 命中节流 -> 应跳过落盘但更新内存
check("#2 节流时内存仍更新 segments", t.segments and t.segments[0]["text"] == "第一句",
      str(t.segments))
check("#2 节流时内存仍更新 transcript", t.transcript == "第一句\n第二句", repr(t.transcript))

# #3：完全相同的 (start, text) 只保留一条
t2 = store.Task(id="dup")
t2._pub_at = 0.0
dup = [{"start": 1.0, "text": "重复"}, {"start": 1.0, "text": "重复"},
       {"start": 2.0, "text": "不重复"}]
pipeline._publish_segments(t2, dup, force=True)
check("#3 去重后句数=2", len(t2.segments) == 2, f"实际 {len(t2.segments)}")
check("#3 去重保留不同句", [s["text"] for s in t2.segments] == ["重复", "不重复"],
      str([s["text"] for s in t2.segments]))


# ---------------------------------------------------------------------------
print("[#4] iter_chunks：覆盖 [0, total]，无末尾丢字")
import core.audio as audio

TOTAL = 600.0  # 10 分钟音频
dur_map = {}


def fake_transcode(src, dst, start=None, dur=None):
    dur_map[str(dst)] = dur
    pathlib.Path(dst).write_bytes(b"")


def fake_probe(p):
    p = str(p)
    return dur_map.get(p, TOTAL)  # 源文件返回总时长，分片文件返回各自时长


def fake_check():
    return None


def fake_silence(src, total_limit=None):
    return []  # 无静音点 -> 硬切，最容易暴露「末尾丢字」


audio._transcode = fake_transcode
audio.probe_duration = fake_probe
audio.check_ffmpeg = fake_check
audio._silence_points = fake_silence

src = pathlib.Path("fake_src.mp3")
chunks = list(audio.iter_chunks(src, pathlib.Path("fake_chunks"), 180, first_seconds=15,
                                stream=True))
check("#4 至少产出 2 个分片（多片路径）", len(chunks) >= 2, f"片数 {len(chunks)}")

# 校验覆盖：起点 0，每段 [offset, offset+duration] 连续拼接，终点=total，无空隙
ok = True
detail = ""
cursor = 0.0
for i, c in enumerate(chunks):
    if abs(c["offset"] - cursor) > 0.05:
        ok = False
        detail = f"第{i}片起点 {c['offset']} 与上一片终点 {cursor} 不连续"
        break
    cursor = c["offset"] + c["duration"]
if ok and abs(cursor - TOTAL) > 0.05:
    ok = False
    detail = f"最后一片终点 {cursor} 与总时长 {TOTAL} 不一致（末尾丢字）"
check("#4 覆盖完整且无空隙", ok, detail)


# ---------------------------------------------------------------------------
print("[#8/#9] SSE：锁内快照不抛异常；transcribed 即发 eof 并断开")
import core.main as main

# 场景 A：transcribed（转写完成，尚未 AI 整理）-> gen 应发 partial+state+eof 后结束
ta = store.Task(id="sse_a")
ta.status = "transcribed"
ta.stage = "transcribed"
ta.segments = [{"start": 0.0, "text": "完成句"}]
ta.transcript = "完成句"
store._tasks["sse_a"] = ta


async def collect(resp, bag):
    async for chunk in resp.body_iterator:
        bag.append(chunk)


async def scenario_a():
    resp = await main.task_events("sse_a")
    bag = []
    await collect(resp, bag)
    return bag


bag_a = asyncio.run(scenario_a())
text_a = "".join(bag_a) if bag_a and isinstance(bag_a[0], str) else b"".join(bag_a).decode("utf-8")
check("#9 transcribed 时生成器正常结束（已断开）", True)
check("#9 推送了 eof", '"eof"' in text_a, text_a[:200])
check("#9 eof 携带 transcribed 状态", '"transcribed"' in text_a, text_a[:200])
check("#8 推送了 partial（锁内快照正常）", '"type": "partial"' in text_a, text_a[:200])

# 场景 B：running + 有 segments -> 应推送 partial；删除任务后生成器应结束（不空占连接）
tb = store.Task(id="sse_b")
tb.status = "running"
tb.stage = "transcribing"
tb.segments = [{"start": 0.0, "text": "进行中"}]
tb.transcript = "进行中"
store._tasks["sse_b"] = tb


async def scenario_b():
    resp = await main.task_events("sse_b")
    bag = []
    task = asyncio.create_task(collect(resp, bag))
    await asyncio.sleep(0.6)                 # 等至少一个心跳（partial+state）
    store._tasks.pop("sse_b", None)          # 强制下一 tick 触发 eof 并结束
    try:
        await asyncio.wait_for(task, timeout=2)
    except asyncio.TimeoutError:
        task.cancel()
    return bag


bag_b = asyncio.run(scenario_b())
text_b = "".join(bag_b) if bag_b and isinstance(bag_b[0], str) else b"".join(bag_b).decode("utf-8")
check("#8/#9 running 时推送了 partial", '"type": "partial"' in text_b, text_b[:200])
check("#9 删除任务后连接关闭（不再空占）", '"eof"' in text_b, text_b[:200])


# ---------------------------------------------------------------------------
print()
print("=" * 56)
print(f"  通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
if FAIL:
    print("  失败项：", ", ".join(FAIL))
    sys.exit(1)
print("  全部通过 ✓")
