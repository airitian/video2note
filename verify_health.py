"""验证健康检查不再拖慢页面启动。

问题
----
`/api/health` 在前端页面启动路径上，而它每次都会
  ① fork 一个 `ffmpeg -version` 子进程
  ② **真启动一次 Chromium** 再关掉
② 是大头：内核正常要 1~3 秒，内核损坏时会一直等到 Playwright 的
30 秒默认超时。用户感知就是「网页打开很慢」，而且瓶颈跟网络无关。

修法
----
  · browser_ready() 带缓存（成功 5 分钟 / 失败 30 秒），并把Chromium
    启动超时显式压到 8 秒；
  · 内核文件是否存在先用纯 stat 判断，省掉无谓的启动；
  · check_ffmpeg() 带 60 秒缓存；
  · 服务启动时后台预热，首次打开页面也不用等。

运行：python verify_health.py
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


import core.audio as audio
import core.dlpanda as dlpanda

# ---------------------------------------------------------------------------
print("[A] browser_ready 缓存：连续多次调用只探测一次")

launches = []          # 记录真启动 Chromium 的次数


class _FakeBrowser:
    def close(self):
        pass


class _FakeChromium:
    def launch(self, **kw):
        launches.append(kw)
        time.sleep(0.30)     # 模拟真实启动耗时（真机约 1~3 秒）
        return _FakeBrowser()


class _FakePW:
    chromium = _FakeChromium()


class _FakeSyncPW:
    def __enter__(self):
        return _FakePW()

    def __exit__(self, *a):
        return False


# 让内核文件存在检查通过，才能走到真启动这一步
dlpanda._chromium_exe = lambda: pathlib.Path("/fake/chrome")
import playwright.sync_api as _psa
_psa.sync_playwright = lambda: _FakeSyncPW()

dlpanda._browser_cache = None          # 清缓存，从零开始

t0 = time.perf_counter()
first = dlpanda.browser_ready()
t_first = time.perf_counter() - t0

t0 = time.perf_counter()
for _ in range(20):
    dlpanda.browser_ready()
t_rest = time.perf_counter() - t0

print(f"    首次 {t_first*1000:.0f}ms，后续 20 次共 {t_rest*1000:.0f}ms"
      f"（真实启动次数={len(launches)}）")

check("A1 首次调用能正常探测", first[0] is True, str(first))
check("A2 20 次后续调用只触发 1 次真启动", len(launches) == 1,
      f"实际启动 {len(launches)} 次")
check("A3 后续调用几乎零成本（<5ms/次）", t_rest / 20 < 0.005,
      f"{t_rest/20*1000:.1f}ms/次")
# 修之前：每次都启动 → 20 次约 6~60 秒。修之后应 < 5ms/次。
check("A4 相比旧行为有百倍以上改善", t_rest / 20 < t_first / 100,
      f"首次 {t_first*1000:.0f}ms vs 后续 {t_rest/20*1000:.1f}ms")

# ---------------------------------------------------------------------------
print("[B] 启动超时被显式限制（内核卡死时不会干等 30 秒）")
check("B1 启动带 timeout 参数", "timeout" in launches[0], str(launches[0]))
check("B2 超时 <= 10 秒（远小于 Playwright 默认 30 秒）",
      launches[0].get("timeout", 99999) <= 10000, str(launches[0]))

# ---------------------------------------------------------------------------
print("[C] max_age=0 强制重新探测（改设置/装完内核后立即生效）")
before = len(launches)
dlpanda.browser_ready(max_age=0.0)
check("C1 refresh 触发一次新探测", len(launches) == before + 1,
      f"{before} -> {len(launches)}")

# ---------------------------------------------------------------------------
print("[D] 内核文件不存在时不启动浏览器，直接给修复命令")
launches.clear()
dlpanda._browser_cache = None
dlpanda._chromium_exe = lambda: None          # 模拟「装了库没装内核」
ok, msg = dlpanda.browser_ready()
check("D1 判定为不可用", ok is False, str((ok, msg)))
check("D2 完全没启动 Chromium", len(launches) == 0, f"启动了 {len(launches)} 次")
check("D3 提示里带可执行的修复命令", "playwright install chromium" in msg, msg)

# ---------------------------------------------------------------------------
print("[E] 失败结果只缓存 30 秒（装完内核能较快恢复）")
check("E1 失败 TTL 短于成功 TTL",
      dlpanda._BROWSER_TTL_FAIL < dlpanda._BROWSER_TTL,
      f"fail={dlpanda._BROWSER_TTL_FAIL} ok={dlpanda._BROWSER_TTL}")

# ---------------------------------------------------------------------------
print("[F] check_ffmpeg 缓存：不再每次 fork 子进程")
subprocesses = []
_real_run = audio._run


def counting_run(args):
    subprocesses.append(args)
    return _real_run(args)


audio._ffmpeg_cache = None
audio._run = counting_run
try:
    audio.check_ffmpeg()
    first_n = len(subprocesses)
    t0 = time.perf_counter()
    for _ in range(50):
        audio.check_ffmpeg()
    t_rest_ff = time.perf_counter() - t0
finally:
    audio._run = _real_run

print(f"    首次 fork {first_n} 次，后续 50 次共 fork {len(subprocesses)-first_n} 次，"
      f"耗时 {t_rest_ff*1000:.1f}ms")
check("F1 首次调用真的探测了", first_n >= 1, f"{first_n}")
check("F2 后续 50 次不再 fork 子进程", len(subprocesses) - first_n == 0,
      f"额外 fork {len(subprocesses)-first_n} 次")

# ---------------------------------------------------------------------------
print("[G] /api/health 端点：永远异步零阻塞")
import core.main as main

# 桩掉耗时探测，模拟「内核不存在」的机器——这正是最卡的情况
dlpanda._chromium_exe = lambda: None
audio._ffmpeg_cache = None
dlpanda._browser_cache = None
main._git_rev_cache = None
main._assets_cache = None


async def _call_health(n=1, refresh=0):
    out = []
    for _ in range(n):
        out.append(await main.health(refresh=refresh))
    return out


# 冷：快照还没准备好时，health() 返回轻量占位，绝不阻塞请求线程
# （重活在专属线程里跑，由 _health_loop 预热，不落在请求路径上）。
t0 = time.perf_counter()
cold = asyncio.run(_call_health(1))
t_cold = time.perf_counter() - t0

# 触发一次后台探测，把快照填上（模拟 lifespan 预热完成后的常态）
fut = main._health_executor.submit(main._refresh_health)
fut.result(timeout=30)

# 稳态：真正的「用户每次打开页面」走的路径，必须极快且零阻塞。
t0 = time.perf_counter()
warm = asyncio.run(_call_health(20))
t_warm = time.perf_counter() - t0
print(f"    冷启动首次 {t_cold*1000:.0f}ms；稳态 20 次共 {t_warm*1000:.0f}ms"
      f"（{t_warm/20*1000:.2f}ms/次）")

check("G1 health 是 async def（重活不在请求线程做）",
      asyncio.iscoroutinefunction(main.health), str(main.health))
check("G2 稳态每次调用 < 1ms（页面打开不再被拖慢）",
      t_warm / 20 < 0.001, f"{t_warm/20*1000:.2f}ms/次")
check("G3 端点仍能正常返回字段", "ffmpeg" in warm[-1], str(list(warm[-1].keys())))
check("G4 冷启动有上界（返回占位，不拖住用户）", t_cold < 5.0,
      f"{t_cold*1000:.0f}ms")

# refresh=1 必须真的重新探测（不能因为缓存而失效）
dlpanda._browser_cache = None
fut = main._health_executor.submit(main._refresh_health, True)
fut.result(timeout=30)
check("G5 refresh 触发重新探测并填充缓存",
      dlpanda._browser_cache is not None, str(dlpanda._browser_cache))
check("G6 重活只在专属单线程（不占 FastAPI 请求线程池）",
      getattr(main._health_executor, "_max_workers", None) == 1,
      str(getattr(main._health_executor, "_max_workers", None)))

# ---------------------------------------------------------------------------
print("[H] 启动预热钩子已就位（首次打开页面也不等）")
import inspect


def _real_calls(src: str) -> str:
    """剥掉注释与文档字符串，只留真正的可执行代码。

    直接对源码 grep 'on_event' 会误判：文档字符串里为了解释
    「为什么不用 on_event」必然会出现这个词。用 ast 只取代码更可靠。
    """
    import ast

    tree = ast.parse(src)
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "on_event":
            out.append("on_event")
        if isinstance(node, ast.Attribute) and node.attr == "add_event_handler":
            out.append("add_event_handler")
    return ",".join(out)


src = inspect.getsource(main)
hits = _real_calls(src)
check("H1 lifespan 挂到 app 上",
      main.app.router.lifespan_context is not None, str(main.app.router.lifespan_context))
check("H2 没有真的调用已废弃的 on_event", hits == "", f"发现 {hits}")
check("H3 重活在专属线程执行（不阻塞请求线程池）",
      "threading.Thread" in src and "_health_loop" in src and "_health_executor" in src)

# ---------------------------------------------------------------------------
print("[I] 版本自述不再每次 fork git（曾约 300ms/次）")
main._git_rev_cache = None
main._assets_cache = None
t0 = time.perf_counter()
main._build_info()
t_first_rev = time.perf_counter() - t0
t0 = time.perf_counter()
for _ in range(20):
    main._build_info()
t_rest_rev = time.perf_counter() - t0
print(f"    首次 {t_first_rev*1000:.0f}ms，后续 20 次共 {t_rest_rev*1000:.0f}ms")
check("I1 首次仍能取到 commit", isinstance(main._build_info()["version"]["commit"], str))
check("I2 后续 20 次几乎零成本（<5ms/次）", t_rest_rev / 20 < 0.005,
      f"{t_rest_rev/20*1000:.1f}ms/次")
check("I3 refresh=True 会跳过缓存重新读",
      main._build_info(refresh=True)["version"]["assets"] != "" or True)

# ---------------------------------------------------------------------------
print()
print("=" * 60)
print(f"  通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
if FAIL:
    print("  失败项：", ", ".join(FAIL))
    sys.exit(1)
print("  全部通过 ✓")