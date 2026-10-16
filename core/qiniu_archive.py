"""媒体归档：释放本地磁盘前，先把源视频备份到七牛云对象存储。

背景
----
MEDIA_DIR 只在「用户显式删除任务」时才会释放，长期运行的实例磁盘单调增长
（实测已占 4.3 GB 且还在涨）。而任务完成后，源视频基本只用于回看预览，
属低频访问，很适合冷归档到对象存储，本地空间随即释放。

两个动作在这里是**分开**的，不要合并：

    ① 备份（backup_source）—— 下载/上传文件落盘后立刻异步上传到云端，不动本地文件
    ② 清理（cleanup）      —— 过了保留期才删本地源视频，且必须先确认云端已在

早些时候把它做成「到天数才连上传带删除一把梭」，问题是：临近清理的这段时间
本地那一份是**唯一副本**，此时上传失败就等于没备份；
而且集中上传会把网络带宽在清理那一刻打满。分开之后，
清理时的大概率情况是「云端早已存在」，幂等跳过，真正要传的只剩少量失败重试。

★ 铁律：顺序绝不能反
-------------------
    1. 上传本地文件到对象存储
    2. 用 stat 回查云端对象，**校验大小与本地完全一致**
    3. 两者都对得上，才允许删除本地文件

任何一步失败都保留本地文件不动。宁可多占磁盘，也绝不能出现「云端没备份
成功、本地却已经删掉」的丢数据事故。未配置密钥时本模块完全不生效。

为什么必须回查而不是相信 put_file 的返回值：上传返回成功只代表服务端接受了
请求，不代表对象最终可读（分片上传、最终一致性、网络中断都可能出现半截对象）。
只有用服务端返回的 fsize 与本地 size 比对，才是「云端确实完整存着」的证明。
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

_log = logging.getLogger("uvicorn.error")

# 归档目标文件的后缀（用户选定：只归档源视频 source.*）
_SOURCE_GLOBS = ("source.*",)

_UPLOAD_TOKEN_TTL = 3600
_STAT_RETRY = 3
_STAT_RETRY_SLEEP = 1.0
# 上传并行限制为 2：一个 1080p 源视频几百 MB，同时传太多会把正常请求的带宽挤掉，
# 而备份并不着急——晚几十秒成功也不会丢任何东西（本地那份还在）。
_BACKUP_WORKERS = 2
# 转写优先模式下先等这段时间再开传：下载刚结束的头十几秒正是抽音频、切片的
# 密集读写期，此时让路，避免和磁盘 IO、ASR 带宽抢资源。
_BACKUP_DELAY = 12.0
_executor = None


def _pool():
    global _executor
    if _executor is None:
        from concurrent.futures import ThreadPoolExecutor
        _executor = ThreadPoolExecutor(max_workers=_BACKUP_WORKERS,
                                       thread_name_prefix="qiniu-backup")
    return _executor


def cfg() -> dict:
    """读取七牛配置。load_settings 已合并 secrets.json，敏感值直接可取。"""
    from .config import load_settings
    s = load_settings()
    return {
        "ak": (s.get("qiniu_ak") or "").strip(),
        "sk": (s.get("qiniu_sk") or "").strip(),
        "bucket": (s.get("qiniu_bucket") or "").strip(),
        "domain": (s.get("qiniu_domain") or "").strip().rstrip("/"),
        "prefix": (s.get("qiniu_prefix") or "").strip().lstrip("/"),
    }


def configured(c: dict | None = None) -> tuple[bool, str]:
    """是否具备归档条件。返回 (是否可用, 缺失原因)。"""
    c = c or cfg()
    if not c["ak"] or not c["sk"]:
        return False, "未配置七牛云 AccessKey / SecretKey"
    if not c["bucket"]:
        return False, "未配置 qiniu_bucket"
    if not c["domain"]:
        return False, "未配置 qiniu_domain（下载域名）"
    return True, ""


def object_key(tid: str, name: str, prefix: str = "") -> str:
    """云端对象 key：按任务分目录，避免不同任务的同名文件互相覆盖。"""
    return f"{prefix}{tid}/{name}" if prefix else f"{tid}/{name}"


def public_url(key: str, domain: str) -> str:
    from urllib.parse import quote
    d = (domain or "").strip()
    if d and "://" not in d:
        d = "https://" + d
    return f"{d}/{quote(key)}"


def _auth(c: dict):
    from qiniu import Auth
    return Auth(c["ak"], c["sk"])


def upload_file(local: Path, key: str, c: dict, *_unused, **_kw) -> tuple[bool, str]:
    """上传本地文件。返回 (是否成功, 说明)。"""
    from qiniu import put_file
    token = _auth(c).upload_token(c["bucket"], key, _UPLOAD_TOKEN_TTL)
    ret, info = put_file(token, key, str(local))
    if ret is None or info.status_code != 200:
        return False, f"上传失败 HTTP {getattr(info, 'status_code', '?')}"
    return True, "已上传"


def stat_size(key: str, c: dict) -> int | None:
    """回查云端对象大小。查不到返回 None —— 这是「不准删本地」的信号。"""
    from qiniu import BucketManager
    bm = BucketManager(_auth(c))
    for i in range(_STAT_RETRY):
        ret, info = bm.stat(c["bucket"], key)
        if ret and "fsize" in ret:
            return int(ret["fsize"])
        if i < _STAT_RETRY - 1:
            time.sleep(_STAT_RETRY_SLEEP)
    return None


def _source_files(media_dir: Path) -> list[Path]:
    out = []
    for pat in _SOURCE_GLOBS:
        out.extend(p for p in media_dir.glob(pat) if p.is_file())
    return sorted(out)


def _human(n: int) -> str:
    """日志里给人看的大小。几十 MB 的视频写成 0MB 毫无信息量，保留一位小数。"""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return "?"
    if n < 1024:
        return f"{n}B"
    if n < 1024 * 1024:
        return f"{n / 1024:.0f}KB"
    if n < 1024 * 1024 * 1024:
        return f"{n / 1048576:.1f}MB"
    return f"{n / (1024 * 1048576):.2f}GB"


def _record_archived(t, archived_map: dict) -> None:
    """把已归档映射写回任务 meta —— resolve_missing 回显全靠它。"""
    if not archived_map:
        return
    from . import store
    t.meta = dict(t.meta or {})
    merged = dict(t.meta.get("archived") or {})
    merged.update(archived_map)
    t.meta["archived"] = merged
    store.save(t)


def backup_source(t, *, c: dict | None = None) -> dict:
    """把任务源视频上传到云端，**不删本地文件**。

    与 archive_task 的唯一区别就是最后那一步：这里只是「多一份异地副本」，
    删文件是 cleanup 的职责。下载完就传，本地这一份在此期间始终是完整可用的。
    """
    res: dict = {"tid": t.id, "archived": [], "skipped": "", "state": "idle"}
    c = c or cfg()
    able, why = configured(c)
    if not able:
        res["state"] = "skip"
        res["skipped"] = why
        return res

    from .config import MEDIA_DIR
    mdir = MEDIA_DIR / t.id
    files = _source_files(mdir) if mdir.is_dir() else []
    if not files:
        res["skipped"] = "无 source 源文件"
        return res

    known = dict((t.meta or {}).get("archived") or {})
    fresh: dict = {}
    for f in files:
        size = f.stat().st_size
        if size <= 0:
            continue
        key = object_key(t.id, f.name, c["prefix"])
        prev = known.get(f.name)
        # 已记过且云端大小仍对得上，就不重复传（幂等，省流量）
        if isinstance(prev, dict) and int(prev.get("size") or 0) == size \
                and (prev.get("url") or "").startswith("http"):
            continue
        remote = stat_size(key, c)
        okay = (remote == size)
        if not okay:
            ok_up, msg = upload_file(f, key, c)
            if not ok_up:
                res["skipped"] = f"{f.name} {msg}（本地文件保留）"
                continue
            remote = stat_size(key, c)
            okay = (remote == size)
        if not okay:
            res["skipped"] = (f"{f.name} 云端校验不一致 "
                              f"(本地 {size} / 云端 {remote})，稍后重试")
            continue
        url = public_url(key, c["domain"])
        info = {"url": url, "key": key, "size": size, "at": int(time.time())}
        fresh[f.name] = info
        res["archived"].append(info)
        res["state"] = "ok"
    if fresh:
        _record_archived(t, fresh)
        freed_txt = ", ".join(_human(i["size"]) for i in fresh.values())
        try:
            t.log("info", f"已备份到对象存储（{freed_txt}），本地文件保留至清理期")
        except Exception:
            pass
    return res


def pending(t) -> int:
    """还没成功备份到云端、且本地仍然存在的源视频数量。

    任务列表拿它显示「待同步 N 个」，用户点一下就能手动触发。
    """
    from .config import MEDIA_DIR
    known = dict((t.meta or {}).get("archived") or {})
    mdir = MEDIA_DIR / t.id
    if not mdir.is_dir():
        return 0
    return sum(1 for f in _source_files(mdir)
               if f.stat().st_size > 0 and not known.get(f.name))


def schedule_backup(t, *, priority: str = "") -> None:
    """媒体落盘后调用：后台线程上传，绝不拖慢下载/转写主流程。

    priority 决定多快开始传：
      instant  立刻（清理闹钟开着时的默认行为：磁盘随时可能被清）
      delayed  先等 _BACKUP_DELAY 秒（转写优先时的默认行为，让路给 ASR）

    整个调用必须「不可能抛异常」——它是顺手做的加分项，
    任何问题都不该让任务失败，最多就是这次没备份成功（可以由用户手动重来）。
    """
    try:
        if not configured()[0]:
            return
        mode = (priority or "").strip().lower()
        if mode not in ("instant", "delayed"):
            # 没显式指定时，按「本地会不会被自动清理」推断：
            # 清理任务开着说明磁盘有压力，必须尽快同步；关着就不着急。
            mode = "instant" if _cleanup_enabled() else "delayed"
        pool = _pool()

        def _job():
            try:
                if mode == "delayed":
                    time.sleep(_BACKUP_DELAY)
                backup_source(t)
            except Exception as e:               # 备份失败不影响任务
                try:
                    t.log("warn", f"备份到对象存储失败（本地文件仍在）：{e}")
                except Exception:
                    pass

        pool.submit(_job)
    except Exception:
        pass


def _cleanup_enabled() -> bool:
    try:
        from .config import load_settings
        return int(load_settings().get("qiniu_archive_days") or 0) > 0
    except Exception:
        return False


def _deleted_remote_hint(t) -> list[str]:
    return [k for k, v in ((t.meta or {}).get("archived") or {}).items()
            if isinstance(v, dict) and (v.get("key") or "")]


def archive_task(t, *, dry_run: bool = False, c: dict | None = None
                 ) -> dict:
    """归档单个任务的源视频，成功后删除本地文件。

    返回 dict：{"tid", "archived": [{name,key,url,size}], "freed": 字节数,
               "skipped": 原因} —— 供调用方汇总与日志展示。
    本函数不向外抛异常：清理循环里单个任务的问题不该中断整批处理。
    """
    res: dict = {"tid": t.id, "archived": [], "freed": 0, "skipped": ""}
    c = c or cfg()
    able, why = configured(c)
    if not able:
        res["skipped"] = why
        return res

    from .config import MEDIA_DIR
    mdir = MEDIA_DIR / t.id
    if not mdir.is_dir():
        res["skipped"] = "无本地媒体目录"
        return res

    files = _source_files(mdir)
    if not files:
        res["skipped"] = "无 source 源文件"
        return res

    archived_map = dict((t.meta or {}).get("archived") or {})

    for f in files:
        size = f.stat().st_size
        if size <= 0:
            continue
        key = object_key(t.id, f.name, c["prefix"])

        # 幂等：云端已有同名对象且大小一致，直接走「确认 -> 删本地」流程，
        # 不必重复上传（省流量，也让中断后重跑能续上）。
        remote = stat_size(key, c)
        okay = (remote == size)

        if not okay:
            if dry_run:
                res["archived"].append({"name": f.name, "key": key,
                                        "size": size, "dry_run": True})
                continue
            ok_up, msg = upload_file(f, key, c)
            if not ok_up:
                res["skipped"] = f"{f.name} {msg}（本地文件保留）"
                continue
            remote = stat_size(key, c)
            okay = (remote == size)

        # ★决胜点：云端大小必须与本地完全一致，否则绝不删本地
        if not okay:
            res["skipped"] = (f"{f.name} 云端校验不一致 "
                              f"(本地 {size} / 云端 {remote})，本地文件保留")
            continue

        url = public_url(key, c["domain"])
        archived_map[f.name] = {"url": url, "size": size, "at": int(time.time())}

        if dry_run:
            res["archived"].append({"name": f.name, "key": key, "url": url,
                                    "size": size, "dry_run": True})
            continue

        try:
            f.unlink()
        except OSError as e:
            res["skipped"] = f"{f.name} 删除本地文件失败：{e}"
            continue
        res["archived"].append({"name": f.name, "key": key, "url": url,
                                "size": size})
        res["freed"] += size

    if archived_map and not dry_run:
        t.meta = dict(t.meta or {})
        t.meta["archived"] = archived_map
        from . import store
        store.save(t)
    return res


def cleanup(days: int | None = None, *, dry_run: bool = False,
            limit: int | None = None) -> dict:
    """按天数自动归档：清理「已完成且超过 N 天没动过」的任务的源视频。

    days 取 qiniu_archive_days 配置；<=0 表示关闭自动归档。
    返回汇总 dict。
    """
    from .config import load_settings
    from . import store

    s = load_settings()
    if days is None:
        try:
            days = int(s.get("qiniu_archive_days") or 0)
        except ValueError:
            days = 0
    summary = {"days": days, "dry_run": dry_run, "scanned": 0, "matched": 0,
               "freed": 0, "archived": [], "skipped": [], "reason": ""}

    if days <= 0:
        summary["reason"] = "qiniu_archive_days <= 0，自动归档已关闭"
        return summary

    c = cfg()
    able, why = configured(c)
    if not able:
        # 关键安全行为：没配好就一个字节都不删
        summary["reason"] = f"跳过归档：{why}（本地文件保持不动）"
        return summary

    now = time.time()
    cutoff = now - days * 86400
    # 只有终态任务才动进行中的任务删了会破坏续跑与预览
    final_ok = {"done", "transcribed", "failed", "canceled"}

    for t in store.list_tasks():
        summary["scanned"] += 1
        if t.status not in final_ok:
            continue
        # 用 updated_at（最后活动时间）而不是 created_at：近期重试/整理过的
        # 任务不该被当作「冷数据」清掉。
        last = float(getattr(t, "updated_at", 0) or 0)
        if last <= 0 or last > cutoff:
            continue
        summary["matched"] += 1
        if limit and len(summary["archived"]) >= limit:
            break
        r = archive_task(t, dry_run=dry_run, c=c)
        summary["freed"] += r["freed"]
        if r["archived"]:
            summary["archived"].append({k: v for k, v in r.items()
                                        if k in ("tid", "archived")})
        if r["skipped"]:
            summary["skipped"].append({"tid": t.id, "reason": r["skipped"]})
    return summary


def remote_info(t, name: str) -> dict | None:
    """该文件在云端的归档信息（含 url / size / key），没有则 None。"""
    m = ((t.meta or {}).get("archived") or {}).get(name)
    if isinstance(m, dict) and (m.get("url") or "").startswith("http"):
        return m
    return None


def open_remote(url: str, start: int | None = None, end: int | None = None,
                timeout: tuple = (8, 30)):
    """服务器端代拉取云端的某一段字节（Range 透传，支持拖进度条）。

    302 跳转是默认方案（浏览器直连 CDN，最快），但在这些情况下打不通：
      · 部署机出网被限制 / 与 CDN 不同区域
      · 桶设为私有，仅有 AK 的服务端才能读
    这时就要退回到「服务端拉下来再转给浏览器」，代价是流量过一遍服务器。
    """
    import requests
    headers = {}
    if start is not None or end is not None:
        headers["Range"] = f"bytes={start or 0}-" + ("" if end is None else str(end))
    return requests.get(url, headers=headers, stream=True, timeout=timeout)


def resolve_missing(t, name: str) -> str | None:
    """本地媒体已被归档删除时，给出云端 URL（供 /api/media 302 重定向）。

    前端预览地址保持 /api/media/{tid}/{name} 不变，由后端透明跳转，
    浏览器与 <video src> 都不需要改动。
    """
    m = ((t.meta or {}).get("archived") or {}).get(name)
    if isinstance(m, dict):
        u = (m.get("url") or "").strip()
        if u.startswith("http"):
            return u
    return None


def upload_text(text, key, c):
    from qiniu import put_data
    data = text.encode("utf-8")
    token = _auth(c).upload_token(c["bucket"], key, _UPLOAD_TOKEN_TTL)
    ret, info = put_data(token, key, data)
    if ret is None or info.status_code != 200:
        return False, "upload failed HTTP %s" % getattr(info, "status_code", "?")
    return True, "ok"


def upload_note(t, style, text):
    try:
        import time as _t
        c = cfg()
        able, why = configured(c)
        if not able:
            try:
                t.log("warn", "qiniu not configured, skip note upload: %s" % why)
            except Exception:
                pass
            return None
        if not text or not text.strip():
            return None
        name = "%s.md" % style
        key = object_key(t.id, name, c["prefix"])
        ok, msg = upload_text(text, key, c)
        if not ok:
            try:
                t.log("warn", "note upload failed (%s): %s" % (name, msg))
            except Exception:
                pass
            return None
        size = len(text.encode("utf-8"))
        remote = stat_size(key, c)
        if remote != size:
            try:
                t.log("warn", "note cloud size mismatch (local %s / remote %s), keep url" % (size, remote))
            except Exception:
                pass
        base = public_url(key, c["domain"])
        # 私有空间：直链会 401，生成带下载令牌的签名 URL（长期有效）
        url = _auth(c).private_download_url(base, expires=3600 * 24 * 365 * 10)
        from . import store
        t.meta = dict(t.meta or {})
        notes = dict(t.meta.get("qiniu_notes") or {})
        notes[style] = {"url": url, "key": key, "size": size, "at": int(_t.time())}
        t.meta["qiniu_notes"] = notes
        store.save(t)
        try:
            t.log("info", "note uploaded to qiniu: %s" % url)
        except Exception:
            pass
        return url
    except Exception as e:
        try:
            t.log("warn", "note upload exception: %s" % e)
        except Exception:
            pass
        return None
