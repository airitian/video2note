"""导出：把任务结果渲染成 Markdown / 纯文本 / SRT / 原文 / JSON，并落盘为可下载文件"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .store import Task, TASK_DIR

FORMATS = ("md", "txt", "srt", "raw", "json")

ILLEGAL_NAME = re.compile(r'[\\/:*?"<>|\r\n]+')


def safe_title(title: str | None, fallback: str = "transcript", limit: int = 60) -> str:
    """把视频标题转成安全的文件名"""
    name = ILLEGAL_NAME.sub("_", (title or "").strip()) or fallback
    return name[:limit]


def _srt(segments: list[dict]) -> str:
    def ts(v: float) -> str:
        v = max(0.0, float(v or 0))
        h, m = divmod(int(v), 3600)
        m, s = divmod(m, 60)
        return f"{h:02d}:{m:02d}:{s:02d},{int((v - int(v)) * 1000):03d}"

    out = []
    for i, seg in enumerate(segments, 1):
        out.append(f"{i}\n{ts(seg.get('start', 0))} --> {ts(seg.get('end', 0))}\n{seg.get('text', '')}\n")
    return "\n".join(out)


def plain_from_markdown(md: str) -> str:
    """去掉 Markdown 标记，得到纯文本"""
    md = re.sub(r"^#{1,6}\s*", "", md or "", flags=re.M)
    md = re.sub(r"\*\*(.*?)\*\*", r"\1", md)
    md = re.sub(r"`{1,3}", "", md)
    return md.strip()


def render(t: Task, fmt: str = "md") -> tuple[str, str, str]:
    """返回 (content, filename, mime)"""
    if fmt not in FORMATS:
        fmt = "md"
    title = safe_title(t.meta.get("title"), fallback=t.id)

    if fmt == "md":
        return t.note or "", f"{title}.md", "text/markdown; charset=utf-8"
    if fmt == "txt":
        return plain_from_markdown(t.note or t.transcript or ""), f"{title}.txt", "text/plain; charset=utf-8"
    if fmt == "srt":
        return _srt(t.segments), f"{title}.srt", "application/x-subrip; charset=utf-8"
    if fmt == "raw":
        return t.transcript or "", f"{title}-原文.txt", "text/plain; charset=utf-8"
    return json.dumps(t.to_dict(), ensure_ascii=False, indent=2), f"{title}.json", "application/json; charset=utf-8"


def dump(t: Task, fmt: str = "md", outdir: Path | None = None) -> Path:
    """渲染并写入导出目录，返回可直接提供给下载组件的文件路径。

    目录按任务 ID 分隔（避免任务间同名覆盖），文件名保持可读的「标题.后缀」。
    """
    content, filename, _ = render(t, fmt)
    outdir = outdir or (TASK_DIR.parent / "export")
    sub = outdir / t.id
    sub.mkdir(parents=True, exist_ok=True)
    p = sub / filename
    p.write_text(content, encoding="utf-8")
    return p
