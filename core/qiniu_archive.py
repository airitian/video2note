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

# 归档/清理目标：任务的全部媒体文件。旧实现只管 source.*（上传任务的
# 源文件），链接任务的大头 video.mp4 / audio.mp3 永远不在范围内、
# 永不释放——磁盘不闭环的主因。现在统一覆盖全部媒体产物；
# atrack.*/vtrack.* 含下载时的 *.info.json 元数据文件。
_SOURCE_GLOBS = ("source.*", "video.mp4", "video_h264.mp4", "audio.mp3",
                 "atrack.*", "vtrack.*")

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
    """任务目录内的全部媒体文件（归档、清理、状态统计共用同一口径）。"""
    out = []
    for pat in _SOURCE_GLOBS:
        out.extend(p for p in media_dir.glob(pat) if p.is_file())
    return sorted(set(out))


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
        # 已记过且云端大小仍对得上，就不重复传（幂等，省流量）。
        # 归属记录认 key（新）或 url（旧数据兼容）任一存在即可。
        if isinstance(prev, dict) and int(prev.get("size") or 0) == size \
                and (prev.get("key") or prev.get("url")):
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
        # meta 只存对象 key，不存任何 URL：签名 URL 会过期、公开 URL
        # 可由 key + domain 现算，落盘存 URL 只会留下长期有效的凭证。
        info = {"key": key, "size": size, "at": int(time.time())}
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
        # 持久化只存 key（url 仅用于本次响应/日志，不进 meta）
        archived_map[f.name] = {"key": key, "size": size, "at": int(time.time())}

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


def domain_private_url(info: dict, c: dict | None = None,
                         expires: int | None = None) -> str | None:
    """按 key + 当前配置域名现生成七牛原生私有下载 URL（不走 S3）。

    空间每 25 天轮换一次、测试域名随空间走：域名始终新鲜可用，
    这条通道是播放首选；S3 预签名只作域名未配置时的兜底。
    """
    c = c or cfg()
    if not c["domain"]:
        return None
    key = s3_key_from(info)
    if not key:
        return None
    if expires is None:
        expires = _url_expires()
    try:
        return _auth(c).private_download_url(
            public_url(key, c["domain"]), expires=expires)
    except Exception:
        return None


def remote_info(t, name: str) -> dict | None:
    """该文件在云端的归档信息（key / size + 按需现生成的 url），没有则 None。

    meta 里只存 key：播放用的 URL 在这里现算——优先空间域名私有链接
    （七牛原生签名，不走 S3），域名未配置时回退 S3 预签名，再回退旧数据
    里存过的 url（仅作兼容，新数据不再写）。
    """
    m = ((t.meta or {}).get("archived") or {}).get(name)
    if not isinstance(m, dict):
        return None
    key = s3_key_from(m)
    if not key:
        return None
    url = domain_private_url(m) or s3_presign_url(m) or (m.get("url") or "") \
        or public_url(key, cfg()["domain"])
    if not url.startswith("http"):
        return None
    return {"key": key, "size": m.get("size"), "url": url}


# ============================ 本地 TTL 清理（不依赖七牛） ============================

def media_ttl_days() -> int:
    """本地媒体保留天数，环境变量 V2N_MEDIA_TTL_DAYS，默认 7；<=0 关闭。"""
    import os
    raw = (os.getenv("V2N_MEDIA_TTL_DAYS") or "").strip()
    if not raw:
        return 7
    try:
        return int(float(raw))
    except ValueError:
        return 7


def local_ttl_cleanup(ttl_days: int | None = None, *,
                      dry_run: bool = False) -> dict:
    """纯本地清理：终态任务的媒体超过 TTL 一律删除（不依赖七牛配置）。

    旧形态下未配七牛 = 一个字节都不删，磁盘只涨不跌。这里与云端归档
    并行：归档管「有云端副本才删」，TTL 管「太老了本地直接删」——
    任务记录与成稿文本（在 TASK_DIR 的 JSON 里）保留，只删 MEDIA_DIR
    下的媒体文件。删前校验每个文件的真实路径必须落在 media 目录内。
    """
    from .config import MEDIA_DIR
    from . import store

    if ttl_days is None:
        ttl_days = media_ttl_days()
    summary = {"ttl_days": ttl_days, "dry_run": dry_run, "scanned": 0,
               "matched": 0, "removed": 0, "freed": 0, "reason": ""}
    if ttl_days <= 0:
        summary["reason"] = "V2N_MEDIA_TTL_DAYS <= 0，本地 TTL 清理已关闭"
        return summary
    media_root = MEDIA_DIR.resolve()
    cutoff = time.time() - ttl_days * 86400
    final_ok = {"done", "transcribed", "failed", "canceled"}
    for t in store.list_tasks():
        summary["scanned"] += 1
        if t.status not in final_ok:
            continue
        last = float(getattr(t, "updated_at", 0) or 0)
        if last <= 0 or last > cutoff:
            continue
        mdir = MEDIA_DIR / t.id
        if not mdir.is_dir():
            continue
        files = _source_files(mdir)
        if not files:
            continue
        summary["matched"] += 1
        for f in files:
            try:
                rp = f.resolve()
            except Exception:
                continue
            if media_root not in rp.parents:
                continue    # 路径不在 media 内：绝不删（防符号链接逃逸）
            try:
                size = f.stat().st_size
            except OSError:
                continue
            if not dry_run:
                try:
                    f.unlink()
                except OSError:
                    continue
            summary["removed"] += 1
            summary["freed"] += size
    return summary


# 七牛 S3 兼容服务的区域与域名。测试域名（*.clouddn.com）会被七牛 30 天回收，
# 回收后域名解析到 127.0.0.1，所有历史归档视频都播不了；S3 域名是独立的存储
# API 入口，不受测试域名回收影响，必须带签名访问（七牛不支持匿名）。
# 该 bucket 实测位于华南 cn-south-1；如迁移区域，改设置里的 qiniu_s3_region。
S3_REGION_DEFAULT = "cn-south-1"


def _url_expires() -> int:
    """签名 URL 有效期（秒），环境变量 V2N_QINIU_URL_EXPIRES，默认 3600。

    签名 URL 是 bearer 凭证：谁拿到谁能下。旧值动辄 2 小时（播放）、
    成稿更是 10 年——一律收短到默认 1 小时，播放拖动会重新请求刷新。
    """
    import os
    raw = (os.getenv("V2N_QINIU_URL_EXPIRES") or "").strip()
    try:
        v = int(float(raw)) if raw else 3600
    except ValueError:
        v = 3600
    return max(60, min(v, 86400))


S3_EXPIRES = 3600  # 兼容旧引用；实际取值以 _url_expires() 为准


def _s3_client(c: dict):
    """按需创建 boto3 S3 客户端（七牛 S3 兼容，path-style）。"""
    import boto3
    from botocore.config import Config
    from .config import load_settings
    region = (str(load_settings().get("qiniu_s3_region") or "").strip()
              or S3_REGION_DEFAULT)
    return boto3.client(
        "s3",
        aws_access_key_id=c["ak"],
        aws_secret_access_key=c["sk"],
        region_name=region,
        endpoint_url=f"https://s3.{region}.qiniucs.com",
        config=Config(signature_version="s3v4",
                      s3={"addressing_style": "path"},
                      connect_timeout=8, read_timeout=15,
                      retries={"max_attempts": 2}),
    )


def s3_key_from(info: dict) -> str | None:
    """从归档记录推断云端 object key。

    归档 url 形如 https://<域名>/video2note/<tid>/<name>，其中第一段是上传时
    的 prefix（qiniu_prefix）。取路径去掉开头斜杠即为 key。老记录若直接带
    key 字段则优先用 key。
    """
    k = (info.get("key") or "").strip()
    if k:
        return k.lstrip("/")
    url = (info.get("url") or "").strip()
    if not url.startswith("http"):
        return None
    from urllib.parse import urlparse, unquote
    path = unquote(urlparse(url).path).lstrip("/")
    return path or None


def s3_presign_url(info: dict, c: dict | None = None,
                   expires: int | None = None) -> str | None:
    """把归档记录转成 S3 预签名 GET 地址；失败返回 None（调用方回退旧逻辑）。

    旧 CDN 测试域名被回收后已不可用，这条通道用独立的 S3 API 域名 + AK/SK 签名，
    浏览器/服务端都能凭预签名地址读取对象，且原生支持 HTTP Range 拖进度。
    """
    c = c or cfg()
    if not c["ak"] or not c["sk"] or not c["bucket"]:
        return None
    key = s3_key_from(info)
    if not key:
        return None
    if expires is None:
        expires = _url_expires()
    try:
        client = _s3_client(c)
        return client.generate_presigned_url(
            "get_object",
            Params={"Bucket": c["bucket"], "Key": key},
            ExpiresIn=expires,
        )
    except Exception:
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
        # 优先空间域名私有链接（不走 S3）：空间每 25 天轮换，域名始终新鲜；
        # 域名未配置时才回退 S3 预签名
        du = domain_private_url(m)
        if du:
            return du
        s3 = s3_presign_url(m)
        if s3:
            return s3
        u = (m.get("url") or "").strip()
        if u.startswith("http"):
            return u
        # 新数据只存 key：用公开域名现拼（公开桶可直读，私有桶走上面的预签名）
        key = s3_key_from(m)
        if key:
            pub = public_url(key, cfg()["domain"])
            if pub.startswith("http"):
                return pub
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
        # meta 只存对象 key，绝不持久化签名 URL：旧实现生成 10 年有效的
        # 私有下载 URL 写进任务 meta，而任务详情接口曾可未授权读取，
        # 等于成稿的长期下载凭证对访客可见。需要 URL 时按 key 现生成
        # （_url_expires 短时效），成稿正文本身也在任务 JSON 里可直接读。
        from . import store
        t.meta = dict(t.meta or {})
        notes = dict(t.meta.get("qiniu_notes") or {})
        notes[style] = {"key": key, "size": size, "at": int(_t.time())}
        t.meta["qiniu_notes"] = notes
        store.save(t)
        try:
            t.log("info", "note uploaded to qiniu: %s" % key)
        except Exception:
            pass
        return key
    except Exception as e:
        try:
            t.log("warn", "note upload exception: %s" % e)
        except Exception:
            pass
        return None
