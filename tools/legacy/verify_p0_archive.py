"""P0-5 媒体归档验证（安全铁律）

隔离策略：把 MEDIA_DIR 指向临时目录、store.save 换成空操作、
store.list_tasks 返回假任务，全程不碰真实 data/。

最受关注的一条：即便 ls 显示上传「成功」，只要云端 stat 回来的大小与本地
不一致，本地文件必须一个字节都不能少。
"""
import sys
import types
import tempfile
import shutil
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

WORK = Path(tempfile.mkdtemp(prefix="v2n_arch_"))
(WORK / "media").mkdir(parents=True, exist_ok=True)

_real_media = CFG.MEDIA_DIR
_real_save = ST.save
_real_list = ST.list_tasks
CFG.MEDIA_DIR = WORK / "media"
ST.save = lambda t: None

GOOD = {"ak": "k", "sk": "s", "bucket": "b",
        "domain": "https://cdn.test", "prefix": "video2note/"}

# ★ 一律先 stub 掉外部调用，避免任何用例误发真实网络请求。
# 具体用例按需覆盖这两个函数的返回值。
QA.upload_file = lambda local, key, c, *a, **k: (True, "已上传")
QA.stat_size = lambda key, c: None       # 默认：云端查不到对象


def set_remote(fn):
    """替换云端 stat 的返回"""
    QA.stat_size = fn


def make_task(tid, fname, size, status="done", age_days=400, meta=None):
    import time
    mdir = CFG.MEDIA_DIR / tid
    mdir.mkdir(parents=True, exist_ok=True)
    f = mdir / fname
    f.write_bytes(b"x" * size)
    return types.SimpleNamespace(
        id=tid, meta=dict(meta or {}), status=status,
        updated_at=time.time() - age_days * 86400,
        title=f"t-{tid}",
    )


print("=" * 68)
print("P0-5  归档前必须已安全落地云端（未成功 -> 绝不删本地）")
print("=" * 68)

# ---- A. 未配置密钥：一个字节都不许动 ----
t = make_task("aaaaaaaaaaaa", "source.mp4", 1024)
r = QA.archive_task(t, c={"ak": "", "sk": "", "bucket": "", "domain": "", "prefix": ""})
check("未配置时跳过归档", "未配置" in r["skipped"], r["skipped"])
check("未配置时文件仍在", (CFG.MEDIA_DIR / "aaaaaaaaaaaa" / "source.mp4").is_file())
check("未配置时 freed=0", r["freed"] == 0)

# 缺 bucket / domain 同样要挡住
r = QA.archive_task(t, c={"ak": "k", "sk": "s", "bucket": "", "domain": "d", "prefix": ""})
check("缺 bucket 被挡", "bucket" in r["skipped"], r["skipped"])
r = QA.archive_task(t, c={"ak": "k", "sk": "s", "bucket": "b", "domain": "", "prefix": ""})
check("缺 domain 被挡", "domain" in r["skipped"], r["skipped"])

# ---- B. 云端大小不一致 —— 最关键的一条 ----
SIZE = 2048
t = make_task("bbbbbbbbbbbb", "source.mp4", SIZE)
set_remote(lambda key, c: SIZE - 1)      # 云端比本地少 1 字节
r = QA.archive_task(t, c=GOOD)
check("云端大小不一致 -> 保留本地", not (CFG.MEDIA_DIR / "bbbbbbbbbbbb" / "source.mp4").is_file() is False,
      r["skipped"])
check("...本地文件确实还在", (CFG.MEDIA_DIR / "bbbbbbbbbbbb" / "source.mp4").is_file())
check("...freed=0", r["freed"] == 0)

# 云端查不到对象（None）同样不许删
t = make_task("cccccccccccc", "source.mp4", SIZE)
set_remote(lambda key, c: None)
r = QA.archive_task(t, c=GOOD)
check("云端查不到对象 -> 保留本地",
      (CFG.MEDIA_DIR / "cccccccccccc" / "source.mp4").is_file() and r["freed"] == 0,
      r["skipped"])

# ---- C. 上传直接失败 ----
t = make_task("dddddddddddd", "source.mp4", SIZE)
set_remote(lambda key, c: None)
QA.upload_file = lambda local, key, c, *a, **k: (False, "上传失败 HTTP 599")
r = QA.archive_task(t, c=GOOD)
check("上传失败 -> 保留本地",
      (CFG.MEDIA_DIR / "dddddddddddd" / "source.mp4").is_file() and r["freed"] == 0,
      r["skipped"])

# ---- D. 一切正常：校验通过才删 ----
t = make_task("eeeeeeeeeeee", "source.mp4", SIZE)
QA.upload_file = lambda local, key, c, *a, **k: (True, "已上传")
set_remote(lambda key, c: SIZE)          # 完全一致
r = QA.archive_task(t, c=GOOD)
check("云端一致 -> 删除本地并释放", r["freed"] == SIZE,
      f"freed={r['freed']}")
check("...本地文件已删",
      not (CFG.MEDIA_DIR / "eeeeeeeeeeee" / "source.mp4").is_file())
check("...meta 记录云端 URL",
      "archived" in (t.meta or {}) and
      "source.mp4" in t.meta["archived"], str(list((t.meta.get("archived") or {}).keys())))
u = (t.meta.get("archived") or {}).get("source.mp4", {}).get("url", "")
check("...URL 指向配置的域名", u.startswith(GOOD["domain"] + "/"), u)
check("...URL 含任务目录避免覆盖", "/eeeeeeeeeeee/" in u, u)

# 幂等：云端已有且一致 -> 不重复上传也能完成（省流量）
calls = {"n": 0}
def _count_upload(local, key, c, *a, **k):
    calls["n"] += 1
    return (True, "ok")
QA.upload_file = _count_upload
t2 = make_task("ffffffffffff", "source.mp4", SIZE)
r = QA.archive_task(t2, c=GOOD)
check("云端已存在时不重复上传", calls["n"] == 0, f"调用上传 {calls['n']} 次")
check("...仍然按已归档处理", r["freed"] == SIZE)

# ---- E. dry_run 绝不落盘修改 ----
t = make_task("111111111111", "source.mp4", SIZE)
r = QA.archive_task(t, dry_run=True, c=GOOD)
check("dry_run 不删本地", (CFG.MEDIA_DIR / "111111111111" / "source.mp4").is_file())
check("dry_run freed=0", r["freed"] == 0)
check("dry_run 仍给出预览结果", bool(r["archived"]))

print()
print("=" * 68)
print("cleanup() 的时间与状态门槛")
print("=" * 68)

QA.upload_file = lambda local, key, c, *a, **k: (True, "已上传")
set_remote(lambda key, c: SIZE)

old_done = make_task("222222222222", "source.mp4", SIZE, "done", age_days=400)
recent = make_task("333333333333", "source.mp4", SIZE, "done", age_days=5)
running = make_task("444444444444", "source.mp4", SIZE, "running", age_days=400)
ST.list_tasks = lambda: [old_done, recent, running]
REAL_CFG = QA.cfg
QA.cfg = lambda: GOOD          # cleanup 内部调 cfg()，这里让它认为已配置

s = QA.cleanup(days=30, dry_run=True)
check("dry_run 不删任何文件",
      all((CFG.MEDIA_DIR / x / "source.mp4").is_file()
          for x in ["222222222222", "333333333333", "444444444444"]))
check("超过 30 天的已完成任务被选中", s["matched"] == 1, f"matched={s['matched']}")

s2 = QA.cleanup(days=0)
check("days<=0 关闭自动归档", s2["reason"] != "" and s2["freed"] == 0, s2["reason"])

r3 = QA.cleanup(days=30, dry_run=False)
check("真实清理只处理冷任务",
      not (CFG.MEDIA_DIR / "222222222222" / "source.mp4").is_file()
      and (CFG.MEDIA_DIR / "333333333333" / "source.mp4").is_file()
      and (CFG.MEDIA_DIR / "444444444444" / "source.mp4").is_file())

QA.cfg = lambda: {"ak": "", "sk": "", "bucket": "", "domain": "", "prefix": ""}
s4 = QA.cleanup(days=30)
check("cleanup 未配置时给出原因且不动文件", s4["reason"] != "" and s4["freed"] == 0,
      s4["reason"])
QA.cfg = REAL_CFG

print()
print("=" * 68)
print("归档后的预览重定向")
print("=" * 68)

t1 = types.SimpleNamespace(id="x", meta={"archived": {
    "source.mp4": {"url": "https://cdn.test/x/source.mp4", "size": 1, "at": 0}}})
check("resolve_missing 返回云端 URL",
      QA.resolve_missing(t1, "source.mp4") == "https://cdn.test/x/source.mp4")
check("未归档的文件返回 None", QA.resolve_missing(t1, "audio.mp3") is None)
check("空 meta 不报错", QA.resolve_missing(types.SimpleNamespace(id="y", meta=None), "a.mp4") is None)

print()
print("=" * 68)
print(f"结果：{ok} 通过 / {fail} 失败")
print("=" * 68)

CFG.MEDIA_DIR = _real_media
ST.save = _real_save
ST.list_tasks = _real_list
shutil.rmtree(WORK, ignore_errors=True)
sys.exit(1 if fail else 0)
