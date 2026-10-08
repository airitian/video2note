"""ASR 层：模力方舟（Gitee AI）语音识别，OpenAI 兼容 /v1/audio/transcriptions"""
from __future__ import annotations

import re
import time
from pathlib import Path

import requests

from .config import load_settings


class ASRError(RuntimeError):
    pass


class Segment(dict):
    """{'start': float, 'end': float, 'text': str}"""


# SenseVoice 系列会在文本里插入事件/情感标记，这不是说话内容，必须剔除
# 常见取值：🎼背景音乐 👏掌声 😊笑声 😭哭声 😀开心 😡生气 😔悲伤 😰恐惧 🤢厌恶 😮惊讶 😐中性
_EVENT_TOKENS = (
    "\U0001f3bc\U0001f44f\U0001f60a\U0001f62d\U0001f614\U0001f630"
    "\U0001f922\U0001f62e\U0001f600\U0001f602\U0001f60d\U0001f621"
    "\U0001f915\U0001f637\U0001f912\U0001f914\U0001f610\U0001f612\U0001f644"
)

# 部分模型会残留富文本标签，如 <| Speech |> / <|zh|>
_TAG_RE = re.compile(r"<\|[^|>]{1,20}\|>")


def clean_text(text: str) -> str:
    """剔除 ASR 输出中的事件/情感/富文本标记"""
    if not text:
        return ""
    text = _TAG_RE.sub("", text)
    text = "".join(ch for ch in text if ch not in _EVENT_TOKENS)
    return text.strip()


def _settings() -> dict:
    s = load_settings()
    if not s.get("asr_api_key"):
        raise ASRError("未配置 ASR API Key，请在设置中填写模力方舟 Token")
    return s


def transcribe_file(path: str | Path, offset: float = 0.0, duration: float | None = None,
                    retries: int = 3, language: str | None = None,
                    model: str | None = None) -> list[dict]:
    """转写单个音频分片，返回带绝对时间戳的 Segment 列表"""
    s = _settings()
    url = s["asr_base_url"].rstrip("/") + "/audio/transcriptions"
    path = Path(path)
    model_id = (model or s["asr_model"] or "").strip()
    lang = (language if language is not None else s["asr_language"] or "").strip()

    fields = {"model": model_id}
    if lang and lang.lower() != "auto":
        fields["language"] = lang

    last_err = ""
    for attempt in range(retries):
        try:
            with path.open("rb") as fh:
                resp = requests.post(
                    url,
                    headers={"Authorization": f"Bearer {s['asr_api_key']}"},
                    data=fields,
                    files={"file": (path.name, fh, "audio/mpeg")},
                    timeout=(30, 900),
                )
            if resp.status_code >= 400:
                last_err = f"HTTP {resp.status_code}: {resp.text[:300]}"
                if resp.status_code in (401, 403):
                    raise ASRError(f"ASR 鉴权失败（请检查 Token）：{last_err}")
                if resp.status_code == 404:
                    raise ASRError(f"ASR 接口或模型不存在：{last_err}")
                time.sleep(min(2 ** attempt, 8))
                continue
            data = resp.json()
            return _to_segments(data, offset, duration)
        except ASRError:
            raise
        except Exception as e:
            last_err = str(e)
            time.sleep(2 ** attempt)
    raise ASRError(f"ASR 调用失败（已重试 {retries} 次）：{last_err}")


def _to_segments(data, offset: float, duration: float | None) -> list[dict]:
    if isinstance(data, str):
        data = {"text": data}
    if not isinstance(data, dict):
        raise ASRError(f"ASR 返回格式无法解析：{str(data)[:200]}")

    raw_segs = data.get("segments") or data.get("sentences") or data.get("utterances") or []
    text = clean_text(data.get("text") or data.get("transcription") or data.get("result") or "")

    if isinstance(raw_segs, list) and raw_segs and isinstance(raw_segs[0], dict) and "start" in raw_segs[0]:
        out = []
        for r in raw_segs:
            t = clean_text(r.get("text") or r.get("transcription") or "")
            if not t:
                continue
            out.append(Segment(
                start=float(r.get("start", 0)) + offset,
                end=float(r.get("end", r.get("start", 0))) + offset,
                text=t,
            ))
        if out:
            return out

    if not text:
        text = clean_text(" ".join(str(r.get("text", "")) for r in raw_segs if isinstance(r, dict)))
    if not text:
        return []
    return _even_segments(text, offset, duration or 0.0)


def _even_segments(text: str, offset: float, duration: float) -> list[dict]:
    """接口不返回时间戳时，按句长在该分片时长内按比例估算，保证时间轴可用"""
    parts = [p for p in re.split(r"(?<=[。！？!?；;])", text) if p.strip()]
    if not parts:
        parts = [text]
    total = sum(len(p) for p in parts) or 1
    if duration <= 0:
        duration = max(len(text) / 5.0, 5.0)
    segs, cur = [], offset
    for p in parts:
        d = duration * len(p) / total
        segs.append(Segment(start=round(cur, 2), end=round(cur + d, 2), text=p.strip()))
        cur += d
    return segs


def segments_to_text(segments: list[dict]) -> str:
    return "\n".join(s.get("text", "") for s in segments).strip()
