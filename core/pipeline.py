"""流水线编排：下载/接收文件 -> 抽音频 -> 切片 -> ASR 转写 ->（按需触发）LLM 整理

对外暴露的驱动方式：
- submit() / submit_polish()：线程池异步版，给 FastAPI 用（SSE 推送状态与日志）
"""
from __future__ import annotations

import hashlib
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import asr, audio, downloader, llm, store
from .config import MEDIA_DIR, load_settings

_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="v2n")

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


def _publish_segments(t: store.Task, segments: list[dict]) -> None:
    """把已转写出的分片结果排序后写到任务上并落盘，供前端实时预览。

    segments 是乱序累积的（并发返回），必须按 start 排序，否则预览里
    句子会跳来跳去。transcript 同步刷新，这样刷新页面也能看到中间结果。
    """
    segs = sorted((s for s in segments if s), key=lambda x: x.get("start", 0))
    t.segments = segs
    t.transcript = asr.segments_to_text(segs)
    store.save(t)          # 落盘，刷新页面也能看到已转出的部分


def _check_cancel(t: store.Task) -> None:
    if t._cancel:
        raise InterruptedError("任务已取消")


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

    t.meta = meta
    if meta.get("mux_warning"):
        t.log("warn", f"音视频合并失败，当前为无声视频：{meta['mux_warning'][:200]}")
    t.log("info", f"获取媒体：{meta['title']}（{int(meta.get('duration') or 0)} 秒）")
    store.save(t)

    _check_cancel(t)
    if not src:
        _set(t, "extracting", 1.0, "音频提取完成", emit=emit)

    _set(t, "slicing", 0.0, "音频标准化与切片", emit=emit)
    try:
        chunk_seconds = int(s.get("chunk_seconds") or 180)
    except ValueError:
        chunk_seconds = 180
    chunks = audio.prepare_chunks(Path(meta["audio_path"]), workdir / "chunks", chunk_seconds)
    t.log("info", f"音频处理完成，共 {len(chunks)} 个分片")

    _check_cancel(t)
    _set(t, "transcribing", 0.0, f"开始语音转写（{len(chunks)} 个分片）", emit=emit)
    lang = t.options.get("language") or "auto"
    model = t.options.get("asr_model") or None
    maxw = min(int(s.get("max_concurrency") or 4), 8)

    def do_chunk(i: int) -> list[dict]:
        c = chunks[i]
        return asr.transcribe_file(c["path"], c["offset"], c["duration"],
                                   language=lang, model=model)

    segments: list[dict] = []
    if len(chunks) == 1:
        segments = do_chunk(0)
        _publish_segments(t, segments)
        _set(t, "transcribing", 1.0, "转写完成", emit=emit)
    else:
        done = 0
        lock = threading.Lock()
        with ThreadPoolExecutor(max_workers=max(maxw, 1)) as ex:
            # 必须按「谁先完成」取结果：若按提交顺序等，第 1 片卡住时
            # 后面先跑完的分片没法提前推送，界面就一直空着
            futs = [ex.submit(do_chunk, i) for i in range(len(chunks))]
            for f in as_completed(futs):
                segs = f.result()
                with lock:
                    done += 1
                    segments.extend(segs)
                    # 每完成一个分片就落一次盘，用户能边转写边看到文字，
                    # 而不是干等全部跑完才一次性出现
                    _publish_segments(t, segments)
                    _set(t, "transcribing", done / len(chunks),
                         f"转写进度 {done}/{len(chunks)}", emit=emit)

    segments.sort(key=lambda x: x.get("start", 0))
    t.segments = segments
    t.transcript = asr.segments_to_text(segments)
    if not t.transcript:
        raise RuntimeError("语音转写结果为空，请确认音频有效或更换 ASR 模型")
    t.log("info", f"转写完成，共 {len(segments)} 句、{len(t.transcript)} 字")

    # 原文变了（重新转写 / 换 ASR 模型）时旧成稿作废，避免界面上出现与原文不一致的稿子
    if (t.variants or t.note) and (t.note_info or {}).get("src_hash") != src_hash(t):
        t.note = ""
        t.note_sig = ""
        t.note_info = {}
        t.variants = {}
        t.log("info", "转写原文已更新，原文稿（含各风格缓存）作废，请重新生成")

    t.stage = "transcribed"
    t.status = "transcribed"
    t.spercent = 100
    t.percent = RANGES["transcribing"][1]
    t.error = ""          # 清掉上一轮的失败信息，避免重试成功后界面仍显示旧错误
    t.message = "转写完成，可点击「AI 整理文字」生成成稿"
    store.save(t)


# ============================ AI 整理幂等性 ============================
# 同一任务的整理互斥锁：并发/连点请求排队，后到的会命中幂等直接复用结果
_polish_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


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


def polish_signature(t: store.Task, style: str) -> str:
    """当前输入对应的成稿幂等键"""
    return llm.signature(t.transcript, style, t.options.get("llm_model") or None)


def polish_needed(t: store.Task, style: str, force: bool = False) -> tuple[bool, str]:
    """是否需要真的调用 LLM。返回 (需要整理, 原因说明)。

    命中幂等（该风格已有成稿 + 输入签名一致）时返回 False，调用方应直接复用旧稿，
    既不重复花钱也不覆盖已有结果。换风格则视为新输入，会重新生成一份。
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
    return False, (f"「{info.get('style', style)}」风格已于{info.get('at', '上次')}生成过"
                   f"（{info.get('chars', 0)} 字），直接复用，无需重复调用")


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
    if not t.transcript:
        raise RuntimeError("没有可整理的转写文本，请先完成转写")

    need, reason = polish_needed(t, style, force)
    if not need:
        _apply_variant(t, style)
        _sync_variant_meta(t)
        _mark_idempotent_hit(t, reason)
        return

    t.options["style"] = style
    t.message = "AI 整理中"
    store.save(t)

    def prog(msg: str) -> None:
        _set(t, "polishing", 0.3, msg, emit=emit)

    model = t.options.get("llm_model") or None
    text = llm.polish(t.transcript, t.meta or {}, style, prog, model=model)
    t.log("info", f"整理完成，输出 {len(text)} 字")

    # 按风格存档：不同风格互不覆盖，来回切换可直接复用
    info = {
        "style": style,
        "model": llm.resolve_model(model),
        "chars": len(text),
        "at": datetime.now().strftime("%m-%d %H:%M"),
        "src_hash": src_hash(t),
    }
    t.variants = t.variants or {}
    t.variants[style] = {"note": text, "sig": polish_signature(t, style), "info": info}
    t.note = text
    t.note_sig = t.variants[style]["sig"]
    t.note_info = dict(info)
    # 必须在 note_info 赋值之后再同步，否则 styles 字段写不进去
    _sync_variant_meta(t)

    t.stage = "done"
    t.status = "done"
    t.spercent = 100
    t.percent = 100
    t.error = ""
    t.message = "完成"
    store.save(t)


# ============================ 对外接口 ============================

def retry(t: store.Task) -> str:
    """从失败处重试，返回本次要执行的动作（transcribe / polish）。

    分两类：
    - 失败在polishing（AI 整理）：转写原文还在，只重跑 LLM，不重新下载和转写
    - 其他阶段（下载/抽音频/切片/转写）：重跑整条流程。
      _transcribe_body 开头会检测本地已有媒体并复用，所以不会重复下载。

    canceled 也允许重试：用户主动取消后通常就是想再跑一次。
    """
    if t.status == "running":
        return "busy"

    t._cancel = False
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
    t.status = "running"
    store.save(t)
    _executor.submit(lambda: _run_and_catch(t, lambda: _transcribe_body(t)))


def submit_polish(t: store.Task, style: str, force: bool = False) -> None:
    """转写完成后的 AI 整理，独立触发、可换风格重跑。

    幂等：输入未变化且已有成稿时不提交任务，直接复用旧稿（force=True 可强制重跑）。
    """
    # 加锁只保护「判定 + 置为 running」这一段，不阻塞后续执行
    with _task_lock(t.id):
        need, reason = polish_needed(t, style, force)
        if not need:
            # 命中缓存：把该风格的稿切回展示位
            _apply_variant(t, style)
            _sync_variant_meta(t)
            _mark_idempotent_hit(t, reason)
            return
        if t.status == "running":
            t.log("warn", "任务正在处理中，忽略本次整理请求")
            store.save(t)
            return
        t.status = "running"
        t.error = ""
        store.save(t)
    _executor.submit(lambda: _catch_polish(t, style, force))


def _catch_polish(t: store.Task, style: str, force: bool = False) -> bool:
    try:
        _polish_body(t, style, force=force)
        return True
    except InterruptedError as e:
        t.status = "canceled"
        t.error = str(e)
        t.log("warn", str(e))
        store.save(t)
        return False
    except Exception as e:
        # 失败回退到「已转写」，允许重试
        t.status = "transcribed"
        t.stage = "transcribed"
        t.spercent = 100
        t.percent = RANGES["transcribing"][1]
        t.error = str(e)
        t.message = "AI 整理失败，可重试"
        t.log("error", str(e))
        store.save(t)
        return False


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


def _existing_media(t: store.Task) -> dict | None:
    """该任务是否已在本地落盘？返回可复用的 meta，否则 None。

    典型场景：下载成功但转写因缺 Key / 网络抖动失败，重跑时不必重新下载。
    """
    meta = t.meta or {}
    audio = meta.get("audio_path")
    if not audio or not Path(audio).is_file():
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


def _cleanup_chunks(workdir: Path) -> None:
    try:
        shutil.rmtree(workdir / "chunks", ignore_errors=True)
    except Exception:
        pass
