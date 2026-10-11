"""P0-5 新行为验证：下载完成即备份 + 本地清理后从云端回显

隔离策略：MEDIA_DIR 指向临时目录、store.save / list_tasks 换成假实现、
QA.upload_file / QA.stat_size 全部 stub，任何用例都不发真实网络请求。

最受关注的三条：
  · 备份只上传，**绝不删本地**（删文件是 cleanup 的职责）
  · 备份失败/异常不能让任务失败（它是加分项）
  · 本地没了但云端有，/api/media 必须还能播
"""
import ast
import sys
import tempfile
import types
from pathlib import Path

sys.path.insert(0, ".")
ok = fail = 0


def check(name, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  PASS  {name}" + (f"  ({detail})" if detail else ""))
    else:
        fail += 1
        print(f"  FAIL  {name}" + (f"  ({detail})" if detail else ""))


import core.config as CFG          # noqa: E402
from core import qiniu_archive as QA  # noqa: E402
from core import store as ST          # noqa: E402

WORK = Path(tempfile.mkdtemp(prefix="v2n_cloud_"))
(WORK / "media").mkdir(parents=True, exist_ok=True)
CFG.MEDIA_DIR = WORK / "media"
ST.save = lambda t: None

GOOD = {"ak": "ak", "sk": "sk", "bucket": "b",
        "domain": "https://cdn.test", "prefix": "video2note/"}


class FakeTask:
    def __init__(self, tid, meta=None, status="transcribed"):
        self.id = tid
        self.meta = meta or {}
        self.status = status
        self.logs = []

    def log(self, level, msg):
        self.logs.append((level, msg))


def mk_source(tid, name, nbytes=4096):
    d = CFG.MEDIA_DIR / tid
    d.mkdir(parents=True, exist_ok=True)
    f = d / name
    f.write_bytes(b"x" * nbytes)
    return f


print("=" * 66)
print("A. 下载完成即备份：只上传，不删本地")
print("=" * 66)
uploaded = {}
QA.upload_file = lambda local, key, c, *_a, **_k: (
    uploaded.setdefault(key, Path(local).stat().st_size), True, "已上传")[1:]
QA.stat_size = lambda key, c: uploaded.get(key)
QA.cfg = lambda: GOOD

t = FakeTask("abc123456789")
f = mk_source(t.id, "source.mp4", 4096)
# 必须同时atur存在 audio 之类其它文件，验证只挑 source.*
mk_source(t.id, "audio.mp3", 512)
r = QA.backup_source(t)
print(f"  上传记录: {uploaded}")
check("源视频已上传", any(k.endswith("source.mp4") for k in uploaded), str(list(uploaded)))
check("本地文件仍在（备份阶段绝不删）", f.exists(), f"{f} 存在={f.exists()}")
check("非 source 文件没被传", not any("audio.mp3" in k for k in uploaded))
check("meta 已记录归档信息", bool((t.meta.get("archived") or {}).get("source.mp4")),
      str(t.meta.get("archived")))
check("回显 URL 拼出来了",
      (t.meta["archived"]["source.mp4"]["url"]
       == "https://cdn.test/video2note/abc123456789/source.mp4"),
      t.meta["archived"]["source.mp4"]["url"])
check("任务有备份日志", any("备份" in m for _, m in t.logs), str(t.logs))

print("\n== 幂等：已备份过且大小没变，不重复上传 ==")
before = len(uploaded)
before_keys = set(uploaded)
QA.backup_source(t)
check("未产生新上传", set(uploaded) == before_keys,
      f"{len(before_keys)} -> {len(uploaded)}")

print("\n== 云端大小对不上时必须报警而不是写 meta ==")
t2 = FakeTask("def987654321")
mk_source(t2.id, "source.mp4", 4096)
QA.stat_size = lambda key, c: 123          # 半截对象
r2 = QA.backup_source(t2)
check("大小不一致不记归档", not (t2.meta.get("archived") or {}).get("source.mp4"),
      str(t2.meta.get("archived")))
check("给出不一致原因", "不一致" in (r2.get("skipped") or ""), str(r2["skipped"]))
QA.stat_size = lambda key, c: uploaded.get(key)

print("\n== 未配置密钥：完全不动、也不静默丢数据 ==")
saved = GOOD
try:
    QA.cfg = lambda: {"ak": "", "sk": "", "bucket": "b", "domain": "", "prefix": ""}
    before = set(uploaded)
    r3 = QA.backup_source(t2)
    check("未配置时跳过", r3.get("state") == "skip", str(r3))
    check("未配置时不上传", set(uploaded) == before)
finally:
    QA.cfg = lambda: saved

print("\n== 上传失败：本地必须完好，且不抛异常 ==")
t3 = FakeTask("bad000000001")
f3 = mk_source(t3.id, "source.mp4", 2048)
QA.upload_file = lambda *a, **k: (False, "上传失败 HTTP 599")
try:
    r4 = QA.backup_source(t3)
    check("返回 skipped 而不抛异常", "上传失败" in (r4.get("skipped") or ""),
          str(r4.get("skipped")))
except Exception as e:
    check("返回 skipped 而不抛异常", False, str(e))
check("失败后本地文件仍在", f3.exists() and f3.stat().st_size == 2048)
QA.upload_file = lambda local, key, c, *_a, **_k: (
    uploaded.setdefault(key, Path(local).stat().st_size), True, "已上传")[1:]

print("\n" + "=" * 66)
print("B. 异步调度：不阻塞、不抛错")
print("=" * 66)
import time  # noqa: E402
calls = []
QA.backup_source = lambda task, **k: (calls.append(task.id),
                                      {"state": "ok"})[1]
QA.configured = lambda c=None: (True, "")
cfg_backup = CFG.load_settings  # noqa: F841
from core import pipeline as PL  # noqa: E402

PL.load_settings = lambda: {"qiniu_backup_on_finish": "1"}
PL._schedule_backup(t)
time.sleep(0.4)
check("开关打开时触发备份", t.id in calls, str(calls))

calls.clear()
PL.load_settings = lambda: {"qiniu_backup_on_finish": "0"}
PL._schedule_backup(t)
time.sleep(0.3)
check("开关关闭时不触发", not calls, str(calls))

# 备份过程本身炸了也不能冒泡到任务
PL.load_settings = lambda: {"qiniu_backup_on_finish": "1"}


def _boom(task, **k):
    raise RuntimeError("模拟备份异常")


QA.backup_source = _boom
try:
    PL._schedule_backup(t)
    time.sleep(0.4)
    check("备份内部异常不冒泡", True, "未被抛出")
except Exception as e:
    check("备份内部异常不冒泡", False, str(e))

# 钩子必须在 story.save 之后（此时文件才真的落盘完整）
print("\n== 钩子位置：必须在 store.save 之后调用 ==")
src = (CFG.ROOT / "core" / "pipeline.py").read_text("utf-8")
tree = ast.parse(src)
fn = next(n for n in tree.body
          if isinstance(n, ast.FunctionDef) and n.name == "_transcribe_body")
calls_in = [(i, n) for i, n in enumerate(ast.walk(fn))
            if isinstance(n, ast.Call)]
save_idx = next(i for i, n in calls_in
                if getattr(n.func, "attr", "") == "save")
hook_idx = next((i for i, n in calls_in
                 if getattr(n.func, "id", "") == "_schedule_backup"), None)
check("_transcribe_body 里确实调用了备份钩子", hook_idx is not None)
check("钩子排在 store.save 之后（文件已落盘）",
      hook_idx is not None and hook_idx > save_idx,
      f"save #{save_idx} hook #{hook_idx}")

print("\n" + "=" * 66)
print("C. 本地清理后从云端回显（真实 HTTP 栈）")
print("=" * 66)
from fastapi.testclient import TestClient  # noqa: E402

# ★必须换一个从头到尾都没在本机落过盘的 tid：
# core.main 是在 CFG.MEDIA_DIR 被改之后才 import 的，它的 MEDIA_DIR 指向临时目录，
# 若沿用 A 段那个 tid，本地文件还在（4096 字节），请求会走本地分支而不是云端，
# 测出来「能播」其实是本地那份——本节要验的恰好是「本地没了」的场景。
_REAL_STORE_GET = ST.get
TID = "c1e412345678"
_ARCHIVED_URL = f"https://cdn.test/video2note/{TID}/source.mp4"
ST.get = lambda tid: (FakeTask(
    TID, {"archived": {"source.mp4": {"url": _ARCHIVED_URL, "size": 4096}}})
    if tid == TID else None)

real_settings = CFG.load_settings
try:
    from core.main import _cloud_playback  # noqa: E402
    import core.main as MN  # noqa: E402

    # ★必须改 main 命名空间里的那个 load_settings：
    # main 是 import 时就绑定了真实函数，只给 CFG 换值在这里不起作用，
    # 结果「改了代理开关却仍是 302」，看起来像功能坏了其实测法不对。
    MN.load_settings = lambda: {"qiniu_proxy": "0"}

    with TestClient(MN.app) as c:
        r = c.get(f"/api/media/{TID}/source.mp4", follow_redirects=False)
        check("默认返回 302 跳转（浏览器直连 CDN）", r.status_code == 302,
              f"HTTP {r.status_code}")
        check("跳转目标就是云端地址", r.headers.get("location") == _ARCHIVED_URL,
              str(r.headers.get("location")))

        r = c.get(f"/api/media/{TID}/nope.mp4", follow_redirects=False)
        check("云端也没有的文件 404", r.status_code == 404, f"HTTP {r.status_code}")

        r = c.get("/api/media/notavalidtid/source.mp4", follow_redirects=False)
        check("非法 tid 被白名单挡住", r.status_code == 404, f"HTTP {r.status_code}")

    # ---- 代理模式 ----
    MN.load_settings = lambda: {"qiniu_proxy": "1"}
    body = b"0123456789" * 100  # 1000 字节


    class _FakeResp:
        def __init__(self, rng_start, rng_end):
            self.code = 206 if rng_start is not None else 200
            if rng_start is None:
                self._range = (0, len(body) - 1)
            else:
                self._range = (rng_start, min(rng_end, len(body) - 1))
            self.headers = {
                "Content-Type": "video/mp4",
                "Accept-Ranges": "bytes",
                "Content-Range": f"bytes {self._range[0]}-{self._range[1]}/{len(body)}",
                "Content-Length": str(self._range[1] - self._range[0] + 1),
            }
            self.status_code = self.code
            self.closed = False

        def iter_content(self, n=65536):
            s, e = self._range
            yield body[s:e + 1]

        def close(self):
            self.closed = True


    seen = {}


    def fake_open(url, start=None, end=None, timeout=(8, 30)):
        seen["url"] = url
        seen["start"] = start
        seen["end"] = end
        return _FakeResp(start, end if end is not None else len(body) - 1)


    QA.open_remote = fake_open
    with TestClient(MN.app) as c:
        r = c.get(f"/api/media/{TID}/source.mp4", follow_redirects=False)
        check("代理模式返回 200 且能取到内容", r.status_code == 200 and len(r.content) == len(body),
              f"HTTP {r.status_code} bytes={len(r.content)}")
        check("内容字节正确", r.content == body)
        check("代理时打的是云端地址", seen.get("url") == _ARCHIVED_URL, str(seen.get("url")))

        r = c.get(f"/api/media/{TID}/source.mp4", headers={"Range": "bytes=100-199"}, follow_redirects=False)
        check("带 Range 返回 206", r.status_code == 206, f"HTTP {r.status_code}")
        check("Content-Range 正确",
              r.headers.get("content-range") == f"bytes 100-199/{len(body)}",
              str(r.headers.get("content-range")))
        check("只返回了请求的 100 字节", len(r.content) == 100, str(len(r.content)))
        check("Range 透传给了云端", seen.get("start") == 100 and seen.get("end") == 199,
              str(seen))
        ctype = r.headers.get("content-type", "")
        check("Content-Type 只有一份", ctype.count(",") == 0 and "video/mp4" in ctype, ctype)
finally:
    CFG.load_settings = real_settings
    ST.get = _REAL_STORE_GET

print("\n" + "=" * 66)
print("D. 待同步提示与手动补传（pending / sync）")
print("=" * 66)
QA.cfg = lambda: GOOD
QA.configured = lambda c=None: (True, "")
QA.stat_size = lambda key, c: uploaded.get(key)
QA.upload_file = lambda local, key, c, *_a, **_k: (
    uploaded.setdefault(key, Path(local).stat().st_size), True, "已上传")[1:]

t5 = FakeTask("a1b2c3d4e5f6")
mk_source(t5.id, "source.mp4", 1024)
check("没备份过的文件计入待同步", QA.pending(t5) == 1, str(QA.pending(t5)))
t5.meta = {"archived": {"source.mp4": {"url": "https://cdn.test/x", "size": 1024}}}
check("已备份的不计入", QA.pending(t5) == 0, str(QA.pending(t5)))

# 转写优先：先等一会儿再传，不能和刚下载完的密集读写抢资源
QA._BACKUP_DELAY = 0.3
calls = []
QA.backup_source = lambda task, **k: (calls.append(task.id), {"state": "ok"})[1]
QA.schedule_backup(t5, priority="delayed")
time.sleep(0.05)
check("延迟模式下不在下载刚结束时立刻开传", not calls, str(calls))
time.sleep(0.6)
check("延迟结束后确实补上了", calls == [t5.id], str(calls))

calls.clear()
QA.schedule_backup(t5, priority="instant")
time.sleep(0.3)
check("instant 模式立刻传", calls == [t5.id], str(calls))

# 自动推断：清理闹钟开着 -> 必须尽快，否则磁盘随时可能被清
calls.clear()
QA.configured = lambda c=None: (True, "")
QA._cleanup_enabled = lambda: True
QA.schedule_backup(t5)
time.sleep(0.05)
check("清理开启时自动走 instant", calls == [t5.id], str(calls))
calls.clear()
QA._cleanup_enabled = lambda: False
QA.schedule_backup(t5)
time.sleep(0.05)
check("清理关闭时自动走 delayed（不抢转写带宽）", not calls, str(calls))
time.sleep(0.5)

# ---- /api/archive/sync ----
# ★补传测试必须用「确实没备份过」的任务：t5 在上面已被标记成已归档，
# 用它测会得到 started=0，看着像接口坏了，其实是数据状态不对。
t6 = FakeTask("f0e1d2c3b4a5")
mk_source(t6.id, "source.mp4", 2048)
check("新任务待同步数为 1", QA.pending(t6) == 1, str(QA.pending(t6)))

# 先存真身：这两个是模块级函数，测试结束必须原样还回去，
# 直接 del 会把真实实现一起删掉。
_REAL_LIST = ST.list_tasks
ST.get = lambda tid: (t6 if tid == t6.id else (t5 if tid == t5.id else None))
ST.list_tasks = lambda: [t6, t5]
sched = []
QA.schedule_backup = lambda task, **k: sched.append((task.id, k.get("priority")))
try:
    with TestClient(MN.app) as c:
        r = c.post("/api/archive/sync", params={"tid": t6.id})
        check("指定任务补传：已排入队列", r.status_code == 200 and r.json().get("started") == 1,
              f"HTTP {r.status_code} {r.text[:120]}")
        check("补传按 instant 排程", sched and sched[0][1] == "instant", str(sched))

        r = c.post("/api/archive/sync")
        check("不指定 tid 时补全量", r.status_code == 200 and r.json().get("started") >= 1,
              f"HTTP {r.status_code} {r.text[:120]}")

        r = c.post("/api/archive/sync", params={"tid": "notavalidtid"})
        check("非法 tid 被挡下", r.status_code == 404, f"HTTP {r.status_code}")

        r = c.get("/api/archive/status")
        check("状态接口带出待同步数", r.status_code == 200
              and "pending_count" in r.json(), f"HTTP {r.status_code}")

    QA.configured = lambda c=None: (False, "未配置七牛云 AccessKey / SecretKey")
    with TestClient(MN.app) as c:
        r = c.post("/api/archive/sync")
        check("未配置密钥时拒绝补传（不乱传）", r.status_code == 400,
              f"HTTP {r.status_code} {r.text[:120]}")
finally:
    ST.get = _REAL_STORE_GET
    ST.list_tasks = _REAL_LIST

print(f"\n{'=' * 66}\n结果：{ok} 通过 / {fail} 失败\n{'=' * 66}")
sys.exit(1 if fail else 0)
