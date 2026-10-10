"""任务存储：内存索引 + 磁盘 JSON 持久化"""
from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import TASK_DIR, ensure_dirs

_lock = threading.RLock()
_tasks: dict[str, "Task"] = {}


@dataclass
class Task:
    id: str
    url: str = ""
    platform: str = ""
    status: str = "pending"          # pending/running/transcribed/done/failed/canceled
    stage: str = "queued"            # downloading/extracting/slicing/transcribing/polishing/queued
    #失败时所处的阶段：决定重试从哪一步继续（polishing -> 只重跑整理，其余 -> 重跑转写）
    failed_stage: str = ""
    percent: int = 0
    spercent: int = 0                # 当前阶段内部百分比（给前端分阶段进度条）
    message: str = ""
    error: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    meta: dict[str, Any] = field(default_factory=dict)
    segments: list[dict] = field(default_factory=list)
    transcript: str = ""
    note: str = ""
    note_sig: str = ""                # 成稿幂等键（原文+风格+模型+温度的签名）
    note_info: dict = field(default_factory=dict)   # 成稿元信息：风格/模型/字数/时间
    # 按风格缓存的多份成稿：{"general": {"note":..., "sig":..., "info":{...}}, ...}
    # 用于「换风格重写」时复用已生成过的风格，避免反复调用 LLM
    variants: dict = field(default_factory=dict)
    options: dict = field(default_factory=dict)
    events: list[dict] = field(default_factory=list)
    _cancel: bool = False
    # 上次发布文字稿的单调时刻，用于 _publish_segments 的最小间隔节流
    _pub_at: float = 0.0

    def log(self, level: str, text: str) -> None:
        if self.events and self.events[-1]["text"] == text and self.events[-1]["level"] == level:
            return
        self.events.append({"t": time.time(), "level": level, "text": text})
        if len(self.events) > 500:
            self.events = self.events[-500:]
        self.updated_at = time.time()

    def to_dict(self, with_events: bool = True) -> dict:
        d = {
            "id": self.id,
            "url": self.url,
            "platform": self.platform,
            "status": self.status,
            "stage": self.stage,
            "failed_stage": self.failed_stage,
            "percent": self.percent,
            "spercent": self.spercent,
            "message": self.message,
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "meta": self.meta,
            "options": self.options,
            "segments": self.segments,
            "transcript": self.transcript,
            "note": self.note,
            "note_sig": self.note_sig,
            "note_info": self.note_info,
            "variants": self.variants,
        }
        if with_events:
            d["events"] = self.events
        return d


def _path(tid: str) -> Path:
    return TASK_DIR / f"{tid}.json"


def create(url: str, platform: str, options: dict) -> Task:
    ensure_dirs()
    tid = uuid.uuid4().hex[:12]
    t = Task(id=tid, url=url, platform=platform, options=options)
    with _lock:
        _tasks[tid] = t
    return t


def get(tid: str) -> Task | None:
    with _lock:
        t = _tasks.get(tid)
    if t is None and _path(tid).exists():
        try:
            data = json.loads(_path(tid).read_text("utf-8"))
            t = Task(**{k: v for k, v in data.items() if k in Task.__dataclass_fields__})
            with _lock:
                _tasks[tid] = t
        except Exception:
            return None
    return t


def list_tasks() -> list[Task]:
    ensure_dirs()
    out: dict[str, Task] = {}
    for f in TASK_DIR.glob("*.json"):
        tid = f.stem
        t = get(tid)
        if t:
            out[tid] = t
    with _lock:
        out.update(_tasks)
    return sorted(out.values(), key=lambda x: x.created_at, reverse=True)


def save(t: Task) -> None:
    ensure_dirs()
    _path(t.id).write_text(json.dumps(t.to_dict(with_events=False), ensure_ascii=False), encoding="utf-8")


def request_cancel(tid: str) -> bool:
    t = get(tid)
    if not t:
        return False
    t._cancel = True
    t.status = "canceled"
    return True


def remove(tid: str) -> bool:
    with _lock:
        _tasks.pop(tid, None)
    p = _path(tid)
    if p.exists():
        p.unlink()
        return True
    return False
