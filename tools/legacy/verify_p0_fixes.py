"""P0-1 / P0-2 / P0-3 修复验证

安全约定：
- P0-1 用**直接调用端点函数**验证（不走 TestClient，避免触发 lifespan 的
  recover_interrupted 改写任务状态），且只测非法 id，_safe_tid 会在到达
  rmtree 之前就抛 404，不会真的删任何东西。
- 测试前后比对 data/ 快照，确保零副作用。
"""
import re
import sys
import time
import threading
import queue
import tempfile
from pathlib import Path

PY = sys.executable
ok = fail = 0


def check(name, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  PASS  {name}" + (f"  ({detail})" if detail else ""))
    else:
        fail += 1
        print(f"  FAIL  {name}" + (f"  ({detail})" if detail else ""))


def snapshot_data():
    base = Path("data")
    return sorted(str(p) for p in base.rglob("*") if p.is_file())


print("=" * 66)
print("P0-1  DELETE tid 未校验 -> 一个请求可删光 DATA_DIR")
print("=" * 66)

before = snapshot_data()

from fastapi import HTTPException
from core import main as M
from core.config import DATA_DIR, MEDIA_DIR

# 1) _safe_tid 白名单本身
for bad in ["..", "../..", "..\\..", "abc", "", "0d531e9ce1e", "0D531E9CE1ED",
            "0d531e9ce1ed; rm -rf /", "....//....//"]:
    try:
        M._safe_tid(bad)
        check(f"拒绝非法 tid {bad!r}", False, "竟然通过了")
    except HTTPException as e:
        check(f"拒绝非法 tid {bad!r}", e.status_code == 404, f"HTTP {e.status_code}")

check("放行合法 12 位 hex", M._safe_tid("0d531e9ce1ed") == "0d531e9ce1ed")

# 2) 真实端点：非法 tid 必须在触碰文件系统之前被拦下
for bad in ["..", "%2E%2E"]:
    try:
        M.delete_task(bad)
        check(f"delete_task({bad!r}) 被拦截", False, "未抛异常 —— 危险")
    except HTTPException as e:
        check(f"delete_task({bad!r}) 被拦截", e.status_code == 404, f"HTTP {e.status_code}")

# 3) 媒体端点同样校验
try:
    M.media("..", "secrets.json", request=None, download=0)
    check("media(..) 被拦截", False, "未抛异常")
except HTTPException as e:
    check("media(..) 被拦截", e.status_code == 404, f"HTTP {e.status_code}")

after = snapshot_data()
check("测试期间 data/ 零改动", before == after,
      f"{len(before)} -> {len(after)} 个文件")
check("关键文件仍在", (DATA_DIR / "secrets.json").is_file()
      and (DATA_DIR / "settings.json").is_file()
      and (DATA_DIR / "tasks").is_dir())

print()
print("=" * 66)
print("P0-2  切片线程异常 -> pump_done 未置位 -> consume 永久死锁")
print("=" * 66)

from core import audio

# 1) 前提：损坏音频确实会让 iter_chunks 抛异常（这是死锁的触发条件）
bad = Path(tempfile.gettempdir()) / "v2n_bad.mp3"
bad.write_bytes(b"not a real mp3" * 200)
threw = None
try:
    for _, _ in zip(range(2), audio.iter_chunks(
            bad, Path(tempfile.gettempdir()) / "v2n_chunks", 60)):
        pass
except BaseException as e:
    threw = e
check("损坏音频使 iter_chunks 抛异常", threw is not None,
      f"{type(threw).__name__}: {str(threw)[:40]}" if threw else "未抛")

# 2) 修复后的 pump/consume 结构（与 pipeline.py 完全一致）
def simulate(pump_raises):
    pump_done = threading.Event()
    finished = threading.Event()
    q = queue.Queue(maxsize=4)
    pump_err = []

    def pump():
        try:
            if pump_raises:
                raise RuntimeError("模拟切片失败")
            q.put((0, "fake"))
        except BaseException as e:      # noqa: BLE001
            pump_err.append(e)
        finally:
            pump_done.set()

    def consume():
        while True:
            try:
                q.get(timeout=0.5)
            except queue.Empty:
                if finished.is_set():
                    return
                if pump_done.is_set() and q.empty():
                    return
                continue

    pt = threading.Thread(target=pump, daemon=True)
    ct = threading.Thread(target=consume, daemon=True)
    t0 = time.perf_counter()
    pt.start()
    ct.start()
    pt.join()
    ct.join(timeout=8)
    return (not ct.is_alive()), pump_err, time.perf_counter() - t0


exited, errs, cost = simulate(pump_raises=True)
check("pump 崩溃后 consume 正常退出", exited, f"{cost:.2f}s")
check("pump 异常被带回主线程", len(errs) == 1 and isinstance(errs[0], RuntimeError),
      str(errs[0]) if errs else "无")

# 3) 回归：正常路径不能受影响
exited2, errs2, cost2 = simulate(pump_raises=False)
check("正常路径 consume 仍正常退出", exited2 and not errs2, f"{cost2:.2f}s")

# 4) 静态检查：源码里 pump_done.set() 必须在 finally 中
src = Path("core/pipeline.py").read_text(encoding="utf-8")
body = src.split("def pump() -> None:")[1].split("def consume()")[0]
check("pump_done.set() 位于 finally 块内", "finally:" in body and
      body.index("finally:") < body.index("pump_done.set()"),
      "源码结构正确")
check("pump 异常会被写入 pump_err", "pump_err.append(e)" in body)
check("run() 会把 pump 异常抛出", "raise pump_err[0]" in src)

print()
print("=" * 66)
print("P0-3  ffmpeg_path 可指向任意可执行文件 -> RCE")
print("=" * 66)

from core.config import ff_bin

# 攻击者路径：上传的可执行文件被当作 ffmpeg。
# 用 monkeypatch 注入，绝不去写真实的 settings.json。
evil = Path(tempfile.gettempdir()) / "evil_calc.exe"
evil.write_bytes(b"MZ fake executable")
import core.config as C

orig_load = C.load_settings
try:
    C.load_settings = lambda: {"ffmpeg_path": str(evil)}
    got = ff_bin("ffmpeg")
    accepted = (got == str(evil))
    check("拒绝把任意 exe 当 ffmpeg", not accepted,
          f"返回 {Path(got).name}" if got else "空")
    check("回落到可用 ffmpeg", bool(got), str(got))
finally:
    C.load_settings = orig_load

after2 = snapshot_data()
check("全程 data/ 未被改动", after == after2)

print()
print("=" * 66)
print(f"结果：{ok} 通过 / {fail} 失败")
print("=" * 66)
sys.exit(1 if fail else 0)
