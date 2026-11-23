"""流水线编排：下载/接收文件 -> 抽音频 -> 切片 -> ASR 转写 ->（按需触发）LLM 整理

对外暴露的驱动方式：
- submit() / submit_polish()：线程池异步版，给 FastAPI 用（SSE 推送状态与日志）
"""
from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

# 文稿/质检的展示时间固定按北京时间（容器运行在 UTC，直接 datetime.now 会差 8 小时）
_CST = timezone(timedelta(hours=8))


def _now_str() -> str:
    return datetime.now(_CST).strftime("%m-%d %H:%M")

from typing import Callable

from . import asr, audio, downloader, llm, store
from .config import MEDIA_DIR, TASK_DIR, get_int_clamped, load_settings

def _max_workers() -> int:
    """.env 的 MAX_WORKERS（或 V2N_MAX_WORKERS）真正接到线程池大小。

    旧代码把 max_workers=2 写死，.env.example 里的 MAX_WORKERS 是摆设。
    钳制 [1,8]：这台机器只有 2 核，池开太大只会互相抢 CPU/内存。
    """
    raw = (os.getenv("MAX_WORKERS") or os.getenv("V2N_MAX_WORKERS") or "").strip()
    try:
        v = int(float(raw)) if raw else 2
    except ValueError:
        v = 2
    return max(1, min(v, 8))


_executor = ThreadPoolExecutor(max_workers=_max_workers(),
                               thread_name_prefix="v2n")

# 阶段 -> 整体进度区间
RANGES = {
    "downloading": (2, 40),
    "extracting": (40, 52),
    "slicing": (52, 60),
    "transcribing": (60, 90),
    "polishing": (90, 99),
}

# 视为纯音频、无需抽轨的后缀
AUDIO_EXT = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma"}

EmitFn = Callable[[str, float, str], None] | None


# ============================ 统一状态上报 ============================

def _set(t: store.Task, stage: str, pct: float, msg: str,
         level: str = "info", emit: EmitFn = None) -> None:
    lo, hi = RANGES.get(stage, (0, 100))
    p = max(0.0, min(pct, 1.0))
    t.stage = stage
    t.spercent = int(p * 100)
    t.percent = int(lo + (hi - lo) * p)
    t.message = msg
    t.log(level, msg)
    store.save(t)
    if emit:
        try:
            emit(stage, p, msg)
        except Exception:
            pass


def _publish_segments(t: store.Task, segments: list[dict], force: bool = False) -> None:
    """把已转写出的分片结果排序后写到任务上并落盘，供前端实时预览。

    segments 是乱序累积的（并发返回），必须按 start 排序，否则预览里
    句子会跳来跳去。transcript 同步刷新，这样刷新页面也能看到中间结果。

    force=False 时按最小间隔节流（默认 0.4 秒）：
    首片压到 15 秒后片数会明显增多，每片都全量写盘 + 全量推送是 O(n²)。
    而 SSE 本身 0.8 秒才推一次，推得再密界面也看不出来，
    只会把磁盘和网络写满、反而更卡。
    force=True 用于收尾，确保最后一批一定落盘（否则末尾几句会丢）。

    ★内存更新无条件执行（#2）：旧代码把 `t.segments = segs` 也放在节流分支
    里，节流窗口内（0.4 秒）新完成的片不会写进内存，SSE 每 0.8 秒读到的
    是旧内容，界面要等下一次 publish 才看到 —— 表现就是「字出来了但慢半拍」。
    现在内存始终是最新的，只有落盘才受节流。

    ★去重（#3）：同一时间点 + 同一文本只保留一条。并发返回的多个分片时间本不
    重叠，但静音点切分偶发边界重合、或某分片被重复提交时会出现重复句，
    宁可丢一句重复的，也不能让界面出现两句一模一样的字。
    """
    segs = sorted((s for s in segments if s), key=lambda x: x.get("start", 0))
    dedup: dict[tuple, dict] = {}
    for s in segs:
        key = (round(float(s.get("start", 0)) * 100) / 100, s.get("text") or "")
        if key not in dedup:
            dedup[key] = s
    segs = list(dedup.values())
    # 内存里始终反映最新进度：SSE 每 0.8 秒读一次，不能因为节流而滞后
    t.segments = segs
    t.transcript = asr.segments_to_text(segs)
    now = time.monotonic()
    if not force and now - t._pub_at < 0.4:
        return
    t._pub_at = now
    store.save(t)          # 落盘，刷新页面也能看到已转出的部分


def _check_cancel(t: store.Task) -> None:
    if t._cancel:
        raise InterruptedError("任务已取消")


def _check_alive(t: store.Task) -> None:
    """删除墓碑检查：任务已被删除时立即中止，不再继续落盘/推进。

    与 store.save 的墓碑拦截互为双保险：save 拦截保证记录不复活，
    这里保证 worker 不再白白下载/转写一个用户已经删掉的任务。
    """
    if store.is_deleted(t.id):
        raise InterruptedError("任务已被删除")


def _run_and_catch(t: store.Task, body: Callable[[], None]) -> bool:
    """统一的异常收敛：线程版与同步版共用一套终态语义"""
    try:
        body()
        return True
    except InterruptedError as e:
        t.status = "canceled"
        t.error = str(e)
        t.log("warn", str(e))
        store.save(t)
        return False
    except Exception as e:
        t.status = "failed"
        # 记下失败发生在哪个阶段：重试据此决定是只重跑 AI 整理，还是整个转写流程
        t.failed_stage = t.stage
        t.error = str(e)
        t.message = "处理失败"
        t.log("error", str(e))
        store.save(t)
        return False
    finally:
        _cleanup_chunks(MEDIA_DIR / t.id)


# ============================ 核心业务过程 ============================

def _transcribe_body(t: store.Task, emit: EmitFn = None) -> None:
    """获取媒体 -> 抽音频 -> 切片 -> 并发转写。异常直接向上抛"""
    workdir = MEDIA_DIR / t.id
    s = load_settings()

    src = t.options.get("source_file")
    reused = _existing_media(t) if not src else None
    if src:
        _set(t, "extracting", 0.0, "处理上传的文件", emit=emit)
        meta = _meta_from_file(Path(src), workdir)
        _ensure_playable_video(t, meta, emit)
    elif reused:
        # 之前已下载过（例如上次因缺 Key 在转写环节失败），直接复用，避免重复下载
        _set(t, "extracting", 1.0, "复用已下载的媒体文件", emit=emit)
        meta = reused
        t.log("info", "检测到本地已有该任务的媒体文件，跳过下载")
        # 历史任务可能是 HEVC（浏览器放不出画面），顺手修一次
        _ensure_playable_video(t, meta, emit)
        _ensure_faststart(t, meta)
    else:
        _check_cancel(t)
        _set(t, "downloading", 0.0, "正在解析并下载视频", emit=emit)

        def prog(pct: float, msg: str) -> None:
            step = int(pct // 10) * 10
            if pct < 90:
                _set(t, "downloading", pct / 90.0, f"{msg} {step}%", emit=emit)
            else:
                _set(t, "extracting", (pct - 90) / 10.0, msg, emit=emit)

        meta = downloader.download_media(t.url, workdir, progress=prog)
        # 直下的单文件可能 moov 索引在文件尾：补一遍 faststart 重封装，
        # 保证浏览器拿到开头就能起播，不必先把文件拉完
        _ensure_faststart(t, meta)

    _check_alive(t)          # 下载期间被删除：到此为止，不再往下跑
    t.meta = meta
    if meta.get("mux_warning"):
        t.log("warn", f"音视频合并失败，当前为无声视频：{meta['mux_warning'][:200]}")
    t.log("info", f"获取媒体：{meta['title']}（{int(meta.get('duration') or 0)} 秒）")
    _ensure_cover(t, meta)
    store.save(t)

    # 媒体落盘即触发七牛备份（后台异步上传，不拖慢转写主流程）：
    # 播放可以尽早走云端，不必等生成文稿后才备份。
    # _polish_body 末尾的调用保留作兜底（备份模块自身幂等，重复触发无妨）。
    _schedule_backup(t)

    _check_cancel(t)
    if not src:
        _set(t, "extracting", 1.0, "音频提取完成", emit=emit)

    _set(t, "slicing", 0.0, "音频标准化与切片", emit=emit)
    # 数值一律走钳制读取：chunk_seconds=0 曾能让切片原地打转、
    # max_concurrency 非数字曾让每个新任务抛 ValueError 全站失败。
    chunk_seconds = get_int_clamped(s, "chunk_seconds", 180, 10, 600)
    # 首片单独取短，让第一句话尽快出现。ASR 耗时与音频长度近似成正比，
    # 180 秒的片要等 30~60 秒才出第一句；首片压到 15 秒则约 3~6 秒。
    # 代价是多几次调用，但切片与转写并行后总时长反而更短。
    first_seconds = get_int_clamped(s, "first_chunk_seconds", 15, 5, 600)
    first_seconds = min(first_seconds, chunk_seconds)

    _check_cancel(t)
    _set(t, "transcribing", 0.0, "开始语音转写", emit=emit)
    lang = t.options.get("language") or "auto"
    model = t.options.get("asr_model") or None
    maxw = get_int_clamped(s, "max_concurrency", 4, 1, 8)

    segments: list[dict] = []
    done = 0
    lock = threading.Lock()
    total_hint = {"n": 0}
    finished = threading.Event()

    def do_chunk(c: dict) -> list[dict]:
        return asr.transcribe_file(c["path"], c["offset"], c["duration"],
                                   language=lang, model=model)

    def on_first_chunk(c: dict) -> None:
        total_hint["n"] = max(total_hint["n"], 1)
        t.log("info", f"首片就绪（{int(c['duration'])} 秒），开始转写")

    def on_chunk(i: int, c: dict, n: int) -> None:
        nonlocal done
        total_hint["n"] = max(total_hint["n"], n)
        with lock:
            done += 1
            # 每完成一个分片就落一次盘并推送，用户能边转写边看到文字，
            # 而不是干等全部跑完才一次性出现
            _publish_segments(t, segments)
            cur = done
        _set(t, "transcribing", min(0.99, cur / max(total_hint["n"], 1)),
             f"转写进度 {cur}/{total_hint['n']}", emit=emit)

    run_err: list = []

    def run() -> None:
        """切片线程：边切边把分片塞进队列，供转写线程池即时消费"""
        q: queue.Queue = queue.Queue(maxsize=max(maxw * 2, 4))
        ex = ThreadPoolExecutor(max_workers=max(maxw, 1))
        pending: list = []
        failed_chunks: list = []
        pump_err: list = []          # pump 线程异常由此带回主线程
        pump_done = threading.Event()
        try:
            def pump() -> None:
                # ★pump_done 必须在 finally 置位，不能只在正常路径结尾。
                # iter_chunks 是生成器，随时可能抛异常（音频损坏、ffprobe
                # 读不到时长、ffmpeg 失败、磁盘满）——此时若 pump_done 没置位，
                # 而 finished 要等 run() 返回才 set、run() 又正卡在
                # consume_th.join()，两边互等：consume 每 0.5s 空转一圈永不退出，
                # 任务永久死锁（线程池只有 2 个 worker，两次就把全站堵死）。
                try:
                    # 有界队列 = 背压：切片跑太快时会被挡住，不会堆满磁盘
                    for i, c in enumerate(audio.iter_chunks(
                            Path(meta["audio_path"]), workdir / "chunks",
                            chunk_seconds, first_seconds=first_seconds)):
                        while True:
                            try:
                                q.put((i, c), timeout=0.5)
                                break
                            except queue.Full:
                                if finished.is_set():
                                    return
                except BaseException as e:      # noqa: BLE001 - 跨线程传递
                    pump_err.append(e)
                finally:
                    pump_done.set()   # 无论正常还是异常，都要通知 consume 收尾

            def consume() -> None:
                while True:
                    try:
                        i, c = q.get(timeout=0.5)
                    except queue.Empty:
                        if finished.is_set():
                            return
                        # ★正常流程下 pump 已结束且队列已空时必须退出，否则
                        # run() 会永远卡在 consume_th.join()，转写线程死锁、
                        # segments 永远写不出、任务卡在「转写 0%」（SSE 和刷新
                        # 都拿不到文字）。这是确定性死锁——之前所有新任务都中招。
                        if pump_done.is_set() and q.empty():
                            return
                        continue
                    fut = ex.submit(do_chunk, c)
                    fut._idx = i          # 记下分片下标，失败时好定位
                    pending.append(fut)
                    if i == 0:
                        on_first_chunk(c)
                    total_hint["n"] = max(total_hint["n"], i + 1)

            pump_th = threading.Thread(target=pump, daemon=True)
            consume_th = threading.Thread(target=consume, daemon=True)
            pump_th.start()
            consume_th.start()
            pump_th.join()
            consume_th.join()
            # 队列已空，等所有在途请求返回；按完成顺序取，先跑完的先推送。
            # ★单片失败不再拖垮整个任务（#6）：跳过分片并记一条警告，
            # 其余分片照常拼接；只有全部失败时整体才失败。ASR 内部已重试 3 次，
            # 到这里还失败基本是接口/网络/Token 问题，单点失败不该让整段音频白转。
            for f in as_completed(pending):
                try:
                    res = f.result()
                except BaseException as e:      # noqa: BLE001 - 跨线程收集异常
                    idx = getattr(f, "_idx", -1)
                    failed_chunks.append(idx)
                    t.log("warn", f"分片 {idx} 转写失败，已跳过：{e}")
                    continue
                segments.extend(res)
                on_chunk(-1, {"duration": 0}, total_hint["n"])
        finally:
            # ★无论成功还是异常都要关线程池，否则 ex 泄漏、worker 线程常驻（#7）。
            # 旧代码把 ex.shutdown 写在正常路径末尾，run() 中途抛异常时就被跳过。
            ex.shutdown(wait=True)
        # ★切片失败要报真实原因。否则队列为空 -> segments 为空，会落到
        # 下面「转写结果为空」那种无信息量的报错，用户根本看不出其实是
        # 音频损坏 / ffmpeg 异常 / 磁盘满。
        if pump_err and not segments:
            raise pump_err[0]
        if pump_err:
            t.log("warn", f"切片未能全部完成，成稿可能缺失内容：{pump_err[0]}")
        if failed_chunks and not segments:
            raise RuntimeError(
                f"全部 {len(failed_chunks)} 个分片转写失败，请检查 ASR 接口 / 网络 / Token")
        if failed_chunks:
            t.log("warn", f"有 {len(failed_chunks)} 个分片转写失败（已跳过），"
                           f"成稿可能缺失部分内容")

    def runner() -> None:
        # ★异常必须带回主线程：run() 跑在线程里，直接 raise 只会让线程
        # 静默死掉，任务会以「成功但没有任何文字」收场——比报错更难查。
        try:
            run()
        except BaseException as e:      # noqa: BLE001 - 跨线程传递
            run_err.append(e)
            finished.set()

    th = threading.Thread(target=runner, name="v2n-transcribe", daemon=True)
    th.start()
    while th.is_alive():
        th.join(timeout=0.3)
        _check_cancel(t)
        _check_alive(t)
    th.join()
    if run_err:
        raise run_err[0]
    t.log("info", f"分片转写完成，共 {len(segments)} 句")

    segments.sort(key=lambda x: x.get("start", 0))
    # force=True：收尾必须落盘。上面按 0.4 秒节流，最后一批可能正好
    # 被节流掉，那就会重演「转写完成了但末尾几句不见了」。
    _publish_segments(t, segments, force=True)
    if not t.transcript:
        raise RuntimeError("语音转写结果为空，请确认音频有效或更换 ASR 模型")
    t.log("info", f"转写完成，共 {len(segments)} 句、{len(t.transcript)} 字")

    # 原文变了（重新转写 / 换 ASR 模型）时旧成稿作废，避免界面上出现与原文不一致的稿子
    if (t.variants or t.note) and (t.note_info or {}).get("src_hash") != src_hash(t):
        t.note = ""
        t.note_sig = ""
        t.note_info = {}
        t.variants = {}
        t.clean_cache = {}
        t.log("info", "转写原文已更新，原文稿（含各风格缓存与共享清洗稿）作废，请重新生成")

    t.stage = "transcribed"
    t.status = "transcribed"
    t.spercent = 100
    t.percent = RANGES["transcribing"][1]
    t.error = ""          # 清掉上一轮的失败信息，避免重试成功后界面仍显示旧错误
    t.message = "转写完成，正在自动生成全部风格文稿"
    store.save(t)
    # 免费次数计数：每次成功转写 +1（失败不计数）。写入失败不影响主流程。
    try:
        from .config import record_pro_use
        record_pro_use()
    except Exception:
        pass
    # 转写完成默认自动生成全部风格，用户无需再手动点生成：清洗文稿
    # 最先（产出共享清洗产物），其余风格随后只做 compose。
    # 模型不在此处预分配：每次真正生成时由 _polish_body 按生成次数
    # 从轮换名单取下一个（见 pick_generation_model）。
    for _style in AUTO_STYLES:
        try:
            submit_polish(t, _style)
        except Exception:
            t.log("warn", f"自动生成「{_style}」提交失败，可在页面手动点击生成")


# ============================ AI 整理幂等性 ============================
# 同一任务的整理互斥锁：并发/连点请求排队，后到的会命中幂等直接复用结果
_polish_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()

# 正在生成的风格集合（按任务）：不同风格可以同时生成、互不阻塞；
# 同一风格重复提交才忽略。任务的终态由「最后一个结束的风格」收尾。
_polish_running: dict[str, set[str]] = {}
_polish_running_lock = threading.Lock()


def polishing_styles(tid: str) -> list[str]:
    """该任务当前正在生成的风格列表（供接口透出给前端显示状态）"""
    with _polish_running_lock:
        return sorted(_polish_running.get(tid) or ())


def _others_running(tid: str, style: str) -> list[str]:
    with _polish_running_lock:
        return sorted(s for s in (_polish_running.get(tid) or ()) if s != style)


def _polish_register(tid: str, style: str) -> None:
    with _polish_running_lock:
        _polish_running.setdefault(tid, set()).add(style)


def _polish_unregister(tid: str, style: str) -> bool:
    """注销一个在跑风格；返回 True 表示已无任何风格在跑（由调用方收尾状态）"""
    with _polish_running_lock:
        s = _polish_running.get(tid)
        if s:
            s.discard(style)
            if s:
                return False
            _polish_running.pop(tid, None)
        return True


# ========== 模型轮换（按生成次数轮流用不同厂商的旗舰模型） ==========
# 名单在设置项 llm_rotation_models（逗号/换行分隔）。每一次真正执行的
# 文稿生成（任何风格，手动或转写后自动，一视同仁）都取名单中的下一个
# 模型；缓存命中/冷却复用不算生成、不递增。全局计数落盘，重启不乱序；
# 挑模型时逐个探活，不可用的跳过，全灭则回退设置里的默认模型。
_ROTATION_LOCK = threading.Lock()
# 全局整理并发上限：多个视频同时下完时，整理任务最多同时跑 2 个，
# 避免把模型端点打到限流（限流后重试反而更慢）
_POLISH_SEM = threading.Semaphore(2)
# 转写后自动生成顺序：清洗文稿最先（它就是共享清洗产物，跑完后其余
# 风格只剩 compose），其后仍是原有四风格。
AUTO_STYLES = ("cleaned", "general", "digest", "article", "star")


def rotation_models() -> list[str]:
    raw = (load_settings().get("llm_rotation_models") or "")
    models = [m.strip() for m in re.split(r"[,\n]", raw) if m.strip()]
    return list(dict.fromkeys(models))


def _rotation_next_index(n: int) -> int:
    """取当前轮换序号并 +1 落盘（rotation.json 与任务数据同目录）。"""
    path = TASK_DIR.parent / "rotation.json"
    idx = 0
    try:
        idx = int(json.loads(path.read_text("utf-8")).get("idx", 0))
    except Exception:
        idx = 0
    try:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"idx": idx + 1}), "utf-8")
        tmp.replace(path)
    except Exception:
        pass
    return idx % n if n else 0


def _probe_model(model: str) -> bool:
    try:
        llm.chat("你是一个助手", "只回复：OK", model=model, max_retries=0)
        return True
    except Exception:
        return False


def pick_generation_model(t: store.Task) -> str | None:
    """为一次「真正执行的生成」挑模型（按生成次数轮换）。

    - 任务显式指定了模型（options['llm_model']）→ 用它，不递增轮换计数；
    - 否则取轮换名单中的下一个（全局计数落盘 +1），探活跳过不可用的，
      全不可用返回 None（调用方回退设置里的默认模型）。
    只应在确认要真跑 LLM 后调用：缓存命中/冷却复用不经此处、不计数。
    """
    explicit = (t.options or {}).get("llm_model") or ""
    if explicit:
        return explicit
    pool = rotation_models()
    if not pool:
        return None
    with _ROTATION_LOCK:
        start = _rotation_next_index(len(pool))
        for k in range(len(pool)):
            m = pool[(start + k) % len(pool)]
            if _probe_model(m):
                if k:
                    t.log("warn", f"轮换模型 {pool[start]} 不可用，改用 {m}")
                t.log("info", f"本次生成使用轮换模型 {m}（名单第 {start + 1} 位）")
                return m
            t.log("warn", f"轮换模型 {m} 探活失败，尝试下一个")
    t.log("warn", "轮换模型全部不可用，回退到设置中的默认模型")
    return None


# ========== 生成后质检（独立环节，只出报告不改稿） ==========
_QC_RUNNING: set[str] = set()


def qc_running(tid: str) -> bool:
    with _polish_running_lock:
        return tid in _QC_RUNNING


def _qc_stale(t: store.Task) -> bool:
    """已有报告是否覆盖了当前最新成稿（任一风格比报告新 = 报告过期）。"""
    qc = t.qc or {}
    if not qc:
        return True
    at_ts = float(qc.get("at_ts") or 0)
    latest = max((float((v or {}).get("info", {}).get("ts") or 0)
                  for v in (t.variants or {}).values()), default=0)
    return latest > at_ts


def submit_qc(t: store.Task, force: bool = False) -> bool:
    """提交一次质检（异步）。已有质检在跑、或报告未过期且非强制时跳过。"""
    if not any((v or {}).get("note") for v in (t.variants or {}).values()):
        return False
    with _polish_running_lock:
        if t.id in _QC_RUNNING:
            return False
        if not force and not _qc_stale(t):
            return False
        _QC_RUNNING.add(t.id)
    _executor.submit(lambda: _catch_qc(t))
    return True


def _catch_qc(t: store.Task) -> None:
    try:
        generate_qc(t)
    except Exception as e:
        try:
            t.log("warn", f"质检报告生成失败：{e}")
            store.save(t)
        except Exception:
            pass
    finally:
        with _polish_running_lock:
            _QC_RUNNING.discard(t.id)


def _judge_model_for(t: store.Task) -> str:
    """质检裁判：优先用轮换名单里「最近一次生成所用模型」的下一个，
    避免自己评自己。生成模型按生成次数轮换、各风格可能不同，
    以最新一稿（info.ts 最大）的实际模型为准；无成稿记录时回退
    任务显式模型。"""
    gen = ""
    latest = -1.0
    for v in (t.variants or {}).values():
        info = (v or {}).get("info") or {}
        try:
            ts = float(info.get("ts") or 0)
        except (TypeError, ValueError):
            ts = 0.0
        if info.get("model") and ts >= latest:
            latest, gen = ts, info["model"]
    gen = gen or (t.options or {}).get("llm_model") or ""
    pool = rotation_models()
    if gen in pool and len(pool) > 1:
        return pool[(pool.index(gen) + 1) % len(pool)]
    for m in pool:
        if m != gen:
            return m
    return llm.resolve_model(None)


def generate_qc(t: store.Task) -> dict:
    """对任务的全部成稿逐风格做质检，结果存 t.qc 并落盘。"""
    variants = {s: v for s, v in (t.variants or {}).items()
                if (v or {}).get("note")}
    if not variants:
        raise RuntimeError("尚无成稿可质检")
    judge = _judge_model_for(t)
    src_len = max(1, len(t.transcript or ""))
    items: dict[str, dict] = {}
    for style, v in variants.items():
        info = v.get("info") or {}
        note = v["note"]
        review = llm.review_note(t.transcript or "", note, style, judge)
        items[style] = {
            "model": info.get("model") or (t.options or {}).get("llm_model") or "",
            "chars": len(note),
            "ratio": round(len(note) / src_len, 3),
            "seconds": info.get("seconds"),
            "fit": review["fit"],
            "problems": review["problems"],
            "suggestions": review["suggestions"],
        }
    qc = {"at": _now_str(),
          "at_ts": time.time(),
          "judge_model": judge,
          "items": items}
    with _task_lock(t.id):
        t.qc = qc
        store.save(t)
    t.log("info", f"质检报告已生成（裁判模型：{judge}）")
    return qc


def _task_lock(tid: str) -> threading.Lock:
    with _locks_guard:
        lk = _polish_locks.get(tid)
        if lk is None:
            lk = threading.Lock()
            _polish_locks[tid] = lk
        return lk


def src_hash(t: store.Task) -> str:
    """转写原文指纹：判断已有成稿是否仍对应当前原文"""
    return hashlib.sha1((t.transcript or "").encode("utf-8")).hexdigest()[:16]


# ============================ 共享清洗产物 ============================
# 一篇稿子的清洗（术语抽取 + 分块清洗）只跑一次，结果落在 t.clean_cache，
# 五种风格共享：清洗文稿 = 产物本身，整理文稿 = 产物加标题，精简/公众号/
# 面试只在产物之上做一次 compose。清洗签名由转写原文 + 清洗模板版本决定
# （llm.clean_signature），原文被编辑/重转写即失效。
_clean_locks: dict[str, threading.Lock] = {}


def _clean_lock(tid: str) -> threading.Lock:
    with _locks_guard:
        lk = _clean_locks.get(tid)
        if lk is None:
            lk = threading.Lock()
            _clean_locks[tid] = lk
        return lk


def clean_cache_valid(t: store.Task) -> dict | None:
    """任务的共享清洗缓存是否可用（有文本且签名与当前原文匹配）。"""
    cache = t.clean_cache or {}
    if cache.get("text") and cache.get("sig") == llm.clean_signature(t.transcript or ""):
        return cache
    return None


def ensure_clean(t: store.Task, model: str | None = None,
                 prog=None, force: bool = False) -> dict:
    """确保任务有与当前转写原文匹配的共享清洗产物，返回缓存 dict。

    命中（签名一致）零 LLM 调用直接返回；未命中才真跑清洗
    （llm.clean_transcript），产物连同实际所用模型/用时落盘到
    t.clean_cache。同任务并发调用由 per-task 锁串行化：先到者清洗、
    后到者复检命中，避免自动生成多风格并发时把同一份稿子洗两遍。
    force=True 时绕过缓存强制重洗（清洗文稿/整理文稿的「重新生成」）。
    """
    if not force:
        cache = clean_cache_valid(t)
        if cache is not None:
            return cache
    with _clean_lock(t.id):
        if not force:
            cache = clean_cache_valid(t)
            if cache is not None:
                return cache
        _t0 = time.time()
        cleaned, terms = llm.clean_transcript(
            t.transcript, t.meta or {}, prog, model=model)
        cache = {"text": cleaned, "terms": terms,
                 "sig": llm.clean_signature(t.transcript or ""),
                 "model": llm.resolve_model(model),
                 "seconds": round(time.time() - _t0, 1),
                 "ts": time.time()}
        t.clean_cache = cache
        store.save(t)
        t.log("info", f"共享清洗完成：清洗稿 {len(cleaned)} 字"
                      f"（用时 {cache['seconds']}s），后续各风格共享")
        return cache


def apply_cleaned_edit(t: store.Task, text: str) -> dict:
    """保存用户人工编辑后的清洗文本，返回更新后的 cleaned 成稿 info。

    编辑只换 clean_cache 的文本：缓存签名按转写原文计算（不变），
    clean_cache_valid 照常命中，ensure_clean 不会把编辑冲掉；
    cleaned 变体同步为编辑稿并打 edited 标记；下游风格变体一个都不
    动——它们的成稿签名基于清洗文本，编辑后自动不匹配，下次点击
    时按新底稿重新生成。强制重洗 cleaned/general 仍会覆盖编辑
    （与「重新生成」的既有语义一致）。
    调用方（接口层）负责前置校验：文本非空、任务不在运行中、
    已有有效清洗缓存。
    """
    with _task_lock(t.id):
        cache = dict(t.clean_cache or {})
        cache["text"] = text
        cache["edited"] = True
        cache["edited_at"] = _now_str()
        t.clean_cache = cache
        old = (t.variants or {}).get("cleaned") or {}
        info = dict(old.get("info") or {})
        if not info:
            # 清洗缓存有、cleaned 变体还没生成过：按清洗信息新建
            info = {"style": "cleaned",
                    "model": cache.get("model") or "",
                    "seconds": cache.get("seconds") or 0,
                    "at": _now_str(),
                    "ts": cache.get("ts") or time.time(),
                    "src_hash": src_hash(t)}
        info["chars"] = len(text)
        info["ratio"] = round(len(text) / max(1, len(t.transcript or "")), 3)
        info["edited"] = True
        info["edited_at"] = _now_str()
        t.variants = t.variants or {}
        t.variants["cleaned"] = {
            "note": text,
            "sig": llm.clean_signature(t.transcript or ""),
            "info": info}
        if (t.note_info or {}).get("style") == "cleaned":
            # 当前展示的正是清洗文稿：展示位同步为编辑稿
            t.note = text
            t.note_sig = t.variants["cleaned"]["sig"]
            t.note_info = dict(info)
            _sync_variant_meta(t)
        t.log("info", f"清洗文稿已人工编辑（{len(text)} 字），"
                      "下游风格下次生成将按新底稿")
        store.save(t)
        return info


def polish_signature(t: store.Task, style: str) -> str:
    """当前输入对应的成稿幂等键。

    模型不计入签名：模型按生成次数轮换，若计入，每次生成都会被判为
    「输入已变化」、10 分钟冷却形同虚设。
    清洗文稿的签名就是清洗签名本身；其余风格改为基于共享清洗文本
    签名——清洗变了（原文变/重洗），下游成稿签名跟着变，下次点击时
    重新生成。尚无有效清洗缓存时退回基于原文的签名：它与任何
    「基于清洗文本」落盘的签名必然不同，照常触发重新生成（生成时
    会先补清洗再按清洗文本重算签名落盘）。
    """
    if style == "cleaned":
        return llm.clean_signature(t.transcript or "")
    cache = clean_cache_valid(t)
    if cache is not None:
        return llm.signature(cache["text"], style, None)
    return llm.signature(t.transcript, style, None)


# 同一风格两次 LLM 整理之间的最短间隔（秒）。输入没变化时，
# 命中缓存不再永久复用，而是允许「重新生成」，但 10 分钟内重复点只复用旧稿，避免刷额度。
POLISH_COOLDOWN = 10 * 60


def polish_needed(t: store.Task, style: str, force: bool = False) -> tuple[bool, str]:
    """是否需要真的调用 LLM。返回 (需要整理, 原因说明)。

    命中幂等（该风格已有成稿 + 输入签名一致）时的策略：
    - 距上次生成不足 10 分钟 → 复用旧稿（省额度、防连点）；
    - 距上次生成超过 10 分钟 → 允许重新生成一份（同风格、同输入也可重出稿）。
    换风格 / 换模板 / 重转写（签名变化）则无论间隔都直接重新生成。
    （模型按生成次数轮换、不计入签名，见 polish_signature。）
    """
    if force:
        return True, "已强制重新整理"
    cached = (t.variants or {}).get(style) or {}
    if not cached.get("note"):
        if t.note and t.note_info and t.note_info.get("style") == style:
            return True, "缺少风格缓存（旧数据），重新生成"
        return True, f"尚无「{style}」风格成稿"
    if not cached.get("sig"):
        return True, "缺少成稿签名（旧数据），重新生成"
    if cached["sig"] != polish_signature(t, style):
        return True, "风格 / 模型 / 转写原文已变化，重新整理"

    info = cached.get("info") or {}
    style_name = info.get("style", style)
    last_ts = info.get("ts")
    if isinstance(last_ts, (int, float)):
        elapsed = time.time() - float(last_ts)
        if elapsed < POLISH_COOLDOWN:
            remain_min = int((POLISH_COOLDOWN - elapsed + 59) // 60)
            return False, (f"「{style_name}」风格 {remain_min} 分钟内已生成过"
                           f"（{info.get('chars', 0)} 字），同一风格两次生成需间隔 10 分钟，"
                           f"当前直接复用旧稿")
        return True, f"距上次生成「{style_name}」已超过 10 分钟，重新生成"
    # 没有时间戳的旧数据：无法判断生成时间，保守复用
    return False, (f"「{style_name}」风格已生成过（{info.get('chars', 0)} 字），"
                   f"直接复用，无需重复调用")


def _apply_variant(t: store.Task, style: str) -> None:
    """把该风格缓存的成稿设为当前展示稿"""
    cached = (t.variants or {}).get(style) or {}
    t.note = cached.get("note") or ""
    t.note_sig = cached.get("sig") or ""
    t.note_info = cached.get("info") or {}
    if t.note_info:
        t.options["style"] = t.note_info.get("style", style)


def _mark_idempotent_hit(t: store.Task, reason: str) -> None:
    """幂等命中：把缓存稿铺到当前展示位，只补状态与日志"""
    t.stage = "done"
    t.status = "done"
    t.spercent = 100
    t.percent = 100
    t.error = ""
    t.message = reason
    t.log("info", f"幂等命中，跳过 LLM 调用：{reason}")
    store.save(t)


def _apply_cached(t: store.Task, style: str, reason: str) -> None:
    """幂等命中时的统一处理：有其它风格正在生成时不抢任务终态。

    判断时排除 style 自己：submit 时它还没在册，body 里它已在册，
    两种情况用「除自己外还有没有在跑的」衡量都成立。
    """
    _apply_variant(t, style)
    _sync_variant_meta(t)
    if _others_running(t.id, style):
        t.log("info", f"幂等命中，跳过 LLM 调用：{reason}")
        store.save(t)
    else:
        _mark_idempotent_hit(t, reason)


def _sync_variant_meta(t: store.Task) -> None:
    """刷新 note_info 里的 styles 列表，供前端显示「已生成过哪些风格」"""
    styles = sorted(k for k, v in (t.variants or {}).items() if (v or {}).get("note"))
    if not styles:
        return
    t.note_info = dict(t.note_info or {})
    t.note_info["styles"] = styles


def _polish_body(t: store.Task, style: str, emit: EmitFn = None, force: bool = False) -> None:
    """在已有转写文本基础上做 LLM 整理。异常直接向上抛"""
    _check_cancel(t)
    _check_alive(t)
    if not t.transcript:
        raise RuntimeError("没有可整理的转写文本，请先完成转写")

    need, reason = polish_needed(t, style, force)
    if not need:
        _apply_cached(t, style, reason)
        # 命中缓存也算「AI 去重已完成」：补一次备份（幂等，云端已有则跳过），
        # 避免早期漏备份的任务一直卡在「待同步」。
        _schedule_backup(t)
        try:
            from . import qiniu_archive as QA
            QA.upload_note(t, style, (t.variants or {}).get(style, {}).get("note") or t.note or "")
        except Exception:
            pass
        return

    t.options["style"] = style
    t.message = "AI 整理中"
    store.save(t)

    def prog(msg: str) -> None:
        _set(t, "polishing", 0.3, msg, emit=emit)

    # 生成走共享清洗：清洗只跑一次（ensure_clean），各风格在此之上产出。
    # 挑模型（pick_generation_model）只发生在确实要调 LLM 时——清洗已
    # 命中缓存的整理/清洗文稿零调用完成，不消耗轮换名额；清洗未命中
    # 时，清洗用本次挑的模型（它就是本风格内容实际的生产者）。
    _t0 = time.time()
    if style == "cleaned":
        # 清洗文稿 = 共享清洗产物本身；force 时绕过缓存重洗
        hit = None if force else clean_cache_valid(t)
        if hit is not None:
            cache, used_model = hit, hit.get("model") or ""
        else:
            m = pick_generation_model(t)
            cache = ensure_clean(t, model=m, prog=prog, force=force)
            used_model = cache.get("model") or llm.resolve_model(m)
        text = cache["text"]
    elif style == "general":
        # 整理文稿 = 清洗产物加标题：清洗已完成时零额外 LLM 调用
        hit = None if force else clean_cache_valid(t)
        if hit is not None:
            text = llm.compose_from_cleaned(
                hit["text"], t.meta or {}, "general", prog)
            used_model = hit.get("model") or ""
        else:
            m = pick_generation_model(t)
            try:
                cache = ensure_clean(t, model=m, prog=prog, force=force)
                text = llm.compose_from_cleaned(
                    cache["text"], t.meta or {}, "general", prog, model=m)
                used_model = cache.get("model") or llm.resolve_model(m)
            except Exception:
                # 共享清洗失败：回退老路径（自带术语+清洗+成稿），
                # 不能因为共享化让原本能生成的风格生成不了
                t.log("warn", "共享清洗失败，整理文稿回退原有独立整理路径")
                text = llm.polish(t.transcript, t.meta or {}, style, prog,
                                  model=m)
                used_model = llm.resolve_model(m)
    else:
        # 精简/公众号/面试：跳过各自原有的术语+清洗，只剩 compose 一次调用。
        # 只有清洗这一步失败才回退老路径；compose 本身失败与改版前一样
        # 直接失败、可重试（老路径里 compose 失败也是整体失败）。
        m = pick_generation_model(t)
        cache = None
        try:
            cache = ensure_clean(t, model=m, prog=prog)
        except Exception:
            t.log("warn", f"共享清洗失败，「{style}」回退原有独立整理路径")
        if cache is not None:
            text = llm.compose_from_cleaned(
                cache["text"], t.meta or {}, style, prog, model=m)
        else:
            text = llm.polish(t.transcript, t.meta or {}, style, prog,
                              model=m)
        used_model = llm.resolve_model(m)
    _seconds = round(time.time() - _t0, 1)
    t.log("info", f"整理完成，输出 {len(text)} 字（用时 {_seconds}s）")

    # 按风格存档：不同风格互不覆盖，来回切换可直接复用。
    # 写回在任务锁内做：多个风格同时生成时，避免交叉 save 把对方刚写的
    # variant 覆盖掉。终态只由「最后一个结束的风格」写，其余只报进度。
    info = {
        "style": style,
        "model": used_model,
        "chars": len(text),
        "seconds": _seconds,
        "ratio": round(len(text) / max(1, len(t.transcript or "")), 3),
        "at": _now_str(),
        "ts": time.time(),
        "src_hash": src_hash(t),
    }
    with _task_lock(t.id):
        t.variants = t.variants or {}
        t.variants[style] = {"note": text, "sig": polish_signature(t, style), "info": info}
        t.note = text
        t.note_sig = t.variants[style]["sig"]
        t.note_info = dict(info)
        # 必须在 note_info 赋值之后再同步，否则 styles 字段写不进去
        _sync_variant_meta(t)
        if _others_running(t.id, style):
            t.message = f"「{style}」已生成，其余风格生成中"
        else:
            t.stage = "done"
            t.status = "done"
            t.failed_stage = ""
            t.spercent = 100
            t.percent = 100
            t.error = ""
            t.message = "完成"
        store.save(t)

    # 备份兜底：主触发点已提前到媒体落盘时（见 _transcribe_body），
    # 这里再触发一次保证早期漏备份的任务也能补上（备份模块自身幂等）。
    _schedule_backup(t)

    try:
        from . import qiniu_archive as QA
        QA.upload_note(t, style, text)
    except Exception:
        pass


# ============================ 对外接口 ============================

def retry(t: store.Task, force_media: bool = False) -> str:
    """从失败处重试，返回本次要执行的动作（transcribe / polish）。

    分两类：
    - 失败在polishing（AI 整理）：转写原文还在，只重跑 LLM，不重新下载和转写
    - 其他阶段（下载/抽音频/切片/转写）：重跑整条流程。
      _transcribe_body 开头会检测本地已有媒体并复用，所以不会重复下载。

    canceled 也允许重试：用户主动取消后通常就是想再跑一次。
    force_media=True：先清掉本地媒体与 meta，强制重新下载（媒体损坏时用；
    上传任务的源文件就是媒体本身，不清，避免把用户文件删了无处重下）。
    """
    if t.status == "running":
        return "busy"

    t._cancel = False
    if force_media and not (t.options or {}).get("source_file"):
        shutil.rmtree(MEDIA_DIR / t.id, ignore_errors=True)
        t.meta = {}
        t.log("info", "已清除本地媒体，重试将重新下载")
    stage = t.failed_stage or ""

    # 整理阶段失败：原文完好，只补这一段
    if stage == "polishing" and t.transcript:
        style = (t.options or {}).get("style") or "general"
        submit_polish(t, style, force=True)
        return "polish"

    # 其余情况走完整流程。注意要先清掉旧错误，否则界面会同时显示
    # 上一次的失败信息和本次的进度
    t.failed_stage = ""
    t.error = ""
    t.status = "pending"
    t.stage = "queued"
    t.message = "正在重试"
    t.log("info", "从失败处重试：重新执行转写流程"
          + ("（将复用已下载的媒体文件）" if _existing_media(t) else ""))
    store.save(t)
    submit(t)
    return "transcribe"


def submit(t: store.Task) -> None:
    if store.is_deleted(t.id):
        return
    t.status = "running"
    store.save(t)
    _executor.submit(lambda: _run_and_catch(t, lambda: _transcribe_body(t)))


def recover_interrupted() -> int:
    """进程重启后，把上一次运行遗留的「处理中」任务标记为失败。

    执行线程已随旧进程消失，状态却永远停在 running/pending（僵尸任务）。
    危害不只是显示难看：前端 boot 会对每个 running/pending 任务开一条 SSE
    连接 + 一个周期轮询器，多个僵尸同时 follow 时，挂死的 SSE 能把浏览器
    对同域名的并发连接数（HTTP/1.1 下 6 条）占满，/api/health 排不上队，
    顶栏就永远停在「检测中」。

    标成 failed 后：不再被 follow；「重试」按钮出现，且重试会复用已下载
    的媒体文件（_existing_media），不会重复下载。
    返回清理数量（仅用于启动日志）。
    """
    n = 0
    for t in store.list_tasks():
        if t.status not in ("running", "pending"):
            continue
        t.status = "failed"
        # 记下中断发生在哪个阶段：重试据此走智能续跑（只补整理 / 全流程复用媒体）
        t.failed_stage = t.stage if t.stage and t.stage != "queued" else "downloading"
        t.error = "服务重启导致任务中断"
        t.message = "处理失败"
        t.log("warn", "检测到服务重启，上次未完成的任务已标记为失败；"
                      "点「重试」可继续（会复用已下载的媒体文件）")
        store.save(t)
        n += 1
    return n


def submit_polish(t: store.Task, style: str, force: bool = False) -> None:
    """转写完成后的 AI 整理，独立触发、可换风格重跑。

    幂等：输入未变化且已有成稿时不提交任务，直接复用旧稿（force=True 可强制重跑）。
    """
    # 加锁只保护「判定 + 登记」这一段，不阻塞后续执行
    with _task_lock(t.id):
        need, reason = polish_needed(t, style, force)
        if not need:
            # 命中缓存：把该风格的稿切回展示位
            _apply_cached(t, style, reason)
            return
        if style in polishing_styles(t.id):
            t.log("info", f"该风格正在生成中，忽略重复请求")
            store.save(t)
            return
        if t.status == "running" and t.stage != "polishing":
            # 还在下载/转写阶段：原文没定型，不能开整理。
            # 已在 polishing 阶段则放行——不同风格可以同时生成。
            t.log("warn", "任务正在处理中，忽略本次整理请求")
            store.save(t)
            return
        _polish_register(t.id, style)
        t.status = "running"
        t.stage = "polishing"
        t.error = ""
        store.save(t)
    _executor.submit(lambda: _catch_polish(t, style, force))


def _catch_polish(t: store.Task, style: str, force: bool = False) -> bool:
    # 全局整理并发封顶（见 _POLISH_SEM）：排队等待，不拒绝任务
    with _POLISH_SEM:
        return _catch_polish_run(t, style, force)


def _catch_polish_run(t: store.Task, style: str, force: bool = False) -> bool:
    ok = False
    try:
        _polish_body(t, style, force=force)
        ok = True
    except InterruptedError as e:
        with _task_lock(t.id):
            if _polish_unregister(t.id, style):
                t.status = "canceled"
                t.error = str(e)
            t.log("warn", str(e))
            store.save(t)
    except Exception as e:
        # 失败回退，允许重试。★必须记 failed_stage="polishing"：
        # retry() 靠它分流到「只重跑整理」；旧代码漏设，整理失败的任务
        # 既进不了 retry（状态是 transcribed 而非 failed），分流也无从谈起。
        # 还有其它风格在跑时不抢终态，只记日志；最后一个失败的按
        # 「已有成稿→done / 没有→transcribed」收尾。
        with _task_lock(t.id):
            if _polish_unregister(t.id, style):
                has = any((v or {}).get("note") for v in (t.variants or {}).values())
                t.status = "done" if has else "transcribed"
                t.stage = t.status
                t.failed_stage = "polishing"
                t.spercent = 100
                t.percent = 100 if has else RANGES["transcribing"][1]
                t.error = str(e)
                t.message = "AI 整理失败，可重试"
            t.log("error", str(e))
            store.save(t)
    finally:
        # 成功路径在这里注销。若注销后已无在跑风格、而 _polish_body
        # 写终态时还有别的风格在册（对方随后先失败退出），由这里补齐终态。
        if ok:
            with _task_lock(t.id):
                if _polish_unregister(t.id, style) \
                        and t.status == "running" and t.stage == "polishing":
                    has = any((v or {}).get("note") for v in (t.variants or {}).values())
                    t.status = "done" if has else "transcribed"
                    t.stage = t.status
                    if has:
                        t.spercent = 100
                        t.percent = 100
                        t.message = "完成"
                    store.save(t)
            # 全部风格都生成完后，自动补一次生成后质检（旁路，失败不影响成稿）
            if ok and not polishing_styles(t.id):
                submit_qc(t)
    return ok


def _ensure_cover(t: store.Task, meta: dict) -> None:
    """给视频生成封面图 cover.jpg（播放器 poster 回显）。纯音频跳过；
    失败只记 warn，绝不影响转写主流程。"""
    vp = (meta or {}).get("video_path")
    if not vp:
        return
    src = Path(vp)
    dst = src.parent / "cover.jpg"
    if dst.is_file() and dst.stat().st_size > 0:
        meta["cover"] = "cover.jpg"
        return
    try:
        audio.extract_cover(src, dst)
        if dst.is_file() and dst.stat().st_size > 0:
            meta["cover"] = "cover.jpg"
            t.log("info", "已生成视频封面")
    except Exception as e:
        t.log("warn", f"生成视频封面失败（不影响转写）：{e}")


def _ensure_playable_video(t: store.Task, meta: dict, emit: EmitFn = None) -> None:
    """HEVC 等浏览器放不出画面的编码转成 H.264，保证预览看得到画面而不是只听声音"""
    vp = (meta or {}).get("video_path")
    if not vp:
        return
    p = Path(vp)
    if not p.is_file() or not audio.needs_transcode(p):
        return
    _set(t, "extracting", 0.9, "视频为 HEVC，转换为 H.264 以便预览", emit=emit)
    tmp = p.with_name("video_h264.mp4")
    try:
        audio.to_h264(p, tmp)
        if p.exists():
            p.unlink()
        tmp.replace(p)
        meta["video_codec"] = "h264"
        meta["transcoded"] = "h264"
        t.log("info", "已将视频转码为 H.264，浏览器预览可正常显示画面")
    except Exception as e:
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass
        t.log("warn", f"转码为 H.264 失败，预览可能只有声音：{e}")


def _ensure_faststart(t: store.Task, meta: dict) -> None:
    """下载/复用的视频若 moov 索引在文件尾，补一遍 faststart 重封装（不重编码）"""
    vp = (meta or {}).get("video_path")
    if not vp:
        return
    try:
        if audio.ensure_faststart(Path(vp)):
            t.log("info", "视频索引已前置（faststart），起播更快")
    except Exception:
        pass  # 加分项，失败不影响主流程


def _existing_media(t: store.Task) -> dict | None:
    """该任务是否已在本地落盘？返回可复用的 meta，否则 None。

    典型场景：下载成功但转写因缺 Key / 网络抖动失败，重跑时不必重新下载。

    ★复用前必须校验：旧实现只判 is_file()，半截/损坏的 audio.mp3 会被
    无条件复用、直达切片阶段报错，且没有任何「强制重下」出口。
    现在用 ffprobe 校验（文件大小 > 0 且能探到时长 > 0），不过则删掉
    音视频强制重新下载。
    """
    meta = t.meta or {}
    audio_path = meta.get("audio_path")
    if not audio_path or not Path(audio_path).is_file():
        return None
    p = Path(audio_path)
    ok = False
    try:
        ok = p.stat().st_size > 0 and audio.probe_duration(p) > 0
    except Exception:
        ok = False
    if not ok:
        for key in ("audio_path", "video_path"):
            fp = meta.get(key)
            if fp:
                try:
                    Path(fp).unlink(missing_ok=True)
                except Exception:
                    pass
        t.log("warn", "本地已有媒体校验不通过（文件为空或 ffprobe 探不到时长），"
                      "已删除，将重新下载")
        return None
    video = meta.get("video_path") or ""
    if video and not Path(video).is_file():
        meta = dict(meta)
        meta["video_path"] = ""   # 视频已被清理，仅复用音频
    return meta


def _meta_from_file(src: Path, workdir: Path) -> dict:
    """用户上传的本地音/视频文件：统一抽出 16k 单声道 mp3"""
    workdir.mkdir(parents=True, exist_ok=True)
    if not src.exists():
        raise FileNotFoundError("上传的文件不存在，请重新上传")
    video_path = ""
    if src.suffix.lower() in AUDIO_EXT:
        audio.transcode(src, workdir / "audio.mp3")
    else:
        try:
            audio.extract_audio(src, workdir / "audio.mp3")
        except Exception as e:
            raise RuntimeError(f"无法从文件中提取音频：{e}")
        try:
            # 注意：/ 优先级高于 +，必须写成 workdir / f"source{ext}"，
            # 否则会算成 (workdir / "source") + ext —— Path + str 直接抛 TypeError
            video_path = workdir / f"source{src.suffix.lower()}"
            # 上传接口已经把文件存成了 workdir/source<ext>，这里 src 与目标
            # 是同一个文件。copy2 撞上同路径会抛 SameFileError，被下面的
            # except 吞掉后 video_path 变None —— 表现为「上传了视频但预览
            # 区一直显示暂无视频」。所以要先比一下，相同就直接沿用。
            if not (src.exists() and src.resolve() == video_path.resolve()):
                shutil.copy2(src, video_path)
        except Exception:
            # 复制失败不影响转写，只丢视频预览；这里不能带上任务对象（不在作用域内）
            video_path = None
    meta = {
        "video_id": src.stem,
        "title": src.stem or "本地文件",
        "uploader": "本地上传",
        "description": "",
        "duration": audio.probe_duration(workdir / "audio.mp3"),
        "webpage_url": "",
        "thumbnail": "",
        "audio_path": str(workdir / "audio.mp3"),
    }
    if video_path:
        # 必须转 str：meta 会被 json 序列化落盘，Path 不可序列化
        meta["video_path"] = str(video_path)
    return meta


def _schedule_backup(t: store.Task) -> None:
    """AI 去重（整理）成功后触发云端备份。整个函数不允许抛异常——
    备份是加分项，它的失败绝不能影响任务。由 qiniu_backup_on_finish 开关控制。"""
    try:
        if (load_settings().get("qiniu_backup_on_finish") or "1").strip() == "0":
            return
        from . import qiniu_archive as QA
        QA.schedule_backup(t)
    except Exception:
        pass


def _cleanup_chunks(workdir: Path) -> None:
    try:
        shutil.rmtree(workdir / "chunks", ignore_errors=True)
    except Exception:
        pass
