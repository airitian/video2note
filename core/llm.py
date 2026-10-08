"""LLM 层：负责去口语化 / 纠错 / 排版

支持两种协议，均为 OpenAI 兼容生态的常见形态：
- anthropic：POST {base}/messages，鉴权头 x-api-key（如 api.pateway.ai 的 DeepSeek）
- openai：POST {base}/chat/completions，鉴权头 Authorization: Bearer

两者对上层统一暴露一个函数：chat(system, user, model=None) -> str
"""
from __future__ import annotations

import hashlib
import re
from concurrent.futures import ThreadPoolExecutor

import requests

from .config import load_settings

SHORT_LIMIT = 3000      # 低于此长度一次性完成
CHUNK_LIMIT = 3500      # map 阶段每块字符数

# Anthropic 协议要求必带此头
ANTHROPIC_VERSION = "2023-06-01"


class LLMError(RuntimeError):
    pass


def _config(model_override: str | None = None) -> dict:
    """汇总一次调用所需的全部配置（每次读取，保证页面改设置后立即生效）"""
    s = load_settings()
    key = (s.get("llm_api_key") or s.get("asr_api_key") or "").strip()
    if not key:
        raise LLMError("未配置 LLM API Key，请在「设置」页签填写（或填写 ASR 密钥以复用）")

    base = (s.get("llm_base_url") or "").strip().rstrip("/")
    if not base:
        raise LLMError("未配置 LLM 接口地址")

    protocol = (s.get("llm_protocol") or "auto").strip().lower()
    if protocol in ("", "auto"):
        # 自动判断：地址以 /messages 结尾视为 Anthropic，其余按 OpenAI 处理
        protocol = "anthropic" if base.endswith("/messages") else "openai"

    try:
        max_tokens = int(float(s.get("llm_max_tokens") or 8192))
    except ValueError:
        max_tokens = 8192
    try:
        temp = float(s.get("llm_temperature") or 0.3)
    except ValueError:
        temp = 0.3

    return {
        "protocol": protocol,
        "base": base,
        "key": key,
        "model": (model_override or s.get("llm_model") or "").strip() or "deepseek-v4.1-flash",
        "max_tokens": max_tokens,
        "temperature": temp,
    }


def _err_message(resp: requests.Response) -> str:
    """把各类错误体收敛成一句可读的话"""
    try:
        data = resp.json()
    except Exception:
        return f"HTTP {resp.status_code}: {resp.text[:200]}"
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            return f"HTTP {resp.status_code}: {err.get('message') or err}"
        if isinstance(err, str):
            return f"HTTP {resp.status_code}: {err}"
        if data.get("message"):
            return f"HTTP {resp.status_code}: {data['message']}"
    return f"HTTP {resp.status_code}: {resp.text[:200]}"


# 推理模型（如 deepseek-v4.1-flash）默认会先输出一大段 thinking，
# 在 max_tokens 有限时会把正文挤没。这里默认关闭思考；若服务端不支持该参数则自动降级。
_THINKING_OFF = True


def _chat_anthropic(cfg: dict, system: str, user: str) -> str:
    """Anthropic Messages 协议：system 独立字段，content 是块数组"""
    global _THINKING_OFF
    base = cfg["base"]
    url = base + ("" if base.endswith("/messages") else "/messages")
    payload = {
        "model": cfg["model"],
        "max_tokens": cfg["max_tokens"],
        "messages": [{"role": "user", "content": user}],
    }
    if system:
        payload["system"] = system
    if cfg["temperature"] > 0:
        payload["temperature"] = cfg["temperature"]
    if _THINKING_OFF:
        payload["thinking"] = {"type": "disabled"}

    headers = {
        "content-type": "application/json",
        "x-api-key": cfg["key"],
        "anthropic-version": ANTHROPIC_VERSION,
    }
    resp = requests.post(url, headers=headers, json=payload, timeout=(30, 600))

    # 服务端不认识 thinking 参数时，去掉后重试一次，并记住本次进程内不再携带
    if resp.status_code >= 400 and _THINKING_OFF and "thinking" in resp.text.lower():
        _THINKING_OFF = False
        payload.pop("thinking", None)
        resp = requests.post(url, headers=headers, json=payload, timeout=(30, 600))

    if resp.status_code >= 400:
        if resp.status_code in (401, 403):
            raise LLMError(f"LLM 鉴权失败（请检查 API Key）：{_err_message(resp)}")
        raise LLMError(f"LLM 调用失败：{_err_message(resp)}")

    data = resp.json()
    blocks = data.get("content")
    if isinstance(blocks, str):
        return blocks.strip()
    if isinstance(blocks, list):
        # 只需 text 块；thinking / tool_use 等块忽略
        text = "\n".join(
            b.get("text", "") for b in blocks
            if isinstance(b, dict) and b.get("type") == "text"
        ).strip()
        if text:
            return text
        # 只回了 thinking：通常是思考过程把 max_tokens 吃光了，给出明确错误而不是空结果
        has_thinking = any(isinstance(b, dict) and b.get("type") == "thinking" for b in blocks)
        if has_thinking:
            if data.get("stop_reason") == "max_tokens":
                raise LLMError("LLM 输出被截断：思考过程占满了额度，请在「设置」中把"
                               "「单次生成最大 token 数」调大（如 32768）")
            raise LLMError("LLM 只返回了思考内容、没有正文，请调大额度或更换模型")
    return ((data.get("text") or data.get("completion") or "") if isinstance(data, dict) else "").strip()


def _chat_openai(cfg: dict, system: str, user: str) -> str:
    """OpenAI Chat Completions 协议"""
    try:
        from openai import OpenAI
    except ImportError:
        raise LLMError("openai 协议需要安装 SDK：pip install openai")

    base = cfg["base"]
    if not base.endswith("/chat/completions"):
        base = base.rstrip("/")
    client = OpenAI(base_url=base, api_key=cfg["key"], timeout=600.0)
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": user})
    r = client.chat.completions.create(
        model=cfg["model"],
        messages=messages,
        temperature=cfg["temperature"],
        max_tokens=cfg["max_tokens"],
    )
    return (r.choices[0].message.content or "").strip()


def _request(cfg: dict, system: str, user: str, max_retries: int) -> str:
    """按 cfg 指定的协议发起请求，带退避重试"""
    fn = _chat_anthropic if cfg["protocol"] == "anthropic" else _chat_openai
    last = ""
    for i in range(max_retries + 1):
        try:
            return fn(cfg, system, user)
        except LLMError as e:
            last = str(e)
            if "鉴权失败" in last:      # 鉴权失败重试无意义
                raise
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
    raise LLMError(f"LLM 调用失败（已重试 {max_retries} 次）：{last}")


def chat(system: str, user: str, max_retries: int = 2, model: str | None = None) -> str:
    """统一入口。

    model 是按任务传入的覆盖值（历史任务可能留存了已下线的旧模型名），
    若该模型报错不存在，自动回退到当前配置里的默认模型。
    """
    cfg = _config(model)
    try:
        return _request(cfg, system, user, max_retries)
    except LLMError as e:
        default_model = (load_settings().get("llm_model") or "").strip()
        if model and default_model and default_model != cfg["model"] and _is_model_error(str(e)):
            return _request(_config(None), system, user, max_retries)
        raise


def _is_model_error(msg: str) -> bool:
    low = msg.lower()
    return any(k in low for k in (
        "does not exist", "do not have access", "not found", "model_not_found",
        "unknown model", "invalid model", "no such model",
    ))


SYSTEM = (
    "你是专业的中文文字编辑，擅长把口语化的语音转写原文整理成便于阅读的书面文稿。"
    "铁律：严格保留原意与全部事实信息，不添加原文没有的内容；"
    "去除口头语、重复、无意义的语气词和废话；去除引流/广告类与主题无关的引导性内容；"
    "修正错别字与明显的转写错误；按语义分段，必要时用 Markdown 小标题组织；"
    "直接输出整理后的 Markdown 正文，不要任何解释、前言或后缀。"
)

# 默认风格：忠实原文的「整理文稿」，不额外提炼摘要/要点板块
GENERAL_TMPL = """你是专业的中文文字编辑。用户提供一段语音转写原文，请整理成便于阅读的文稿：
1) 去除口头语、重复、无意义的语气词和废话；
2) 去除引流/广告类与主题无关的引导性内容；
3) 修正错别字与明显的转写错误；
4) 按语义分段，必要时用 Markdown 小标题组织；
5) 严格保留原意与全部事实信息，不添加原文没有的内容；
6) 直接输出整理后的 Markdown 正文，不要任何解释、前言或后缀。

补充约束：
- 引流/广告包括：求关注点赞转发、加微信/进群/私信领取、评论区置顶链接、报课引导，
  以及具体机构或课程的宣传话术（师资、助教、接单渠道、收益承诺等）；
  作者自述的经历、收入数字、操作步骤不属于广告，必须保留
- 数字、单位、金额、代码、英文原词保持原样
- 保持原有叙述顺序与人称，不要改变作者的观点与语气
- 无法判断的转写疑难处保留原样或标 [?]，不要臆测
- 文首可加一个 `# ` 标题，不要写「以下是整理结果」之类的话
- 下面的视频信息仅用于纠错参考，不要写进正文

视频信息：
- 标题：{title}
- 作者：{uploader}

语音转写原文：
{cleaned}"""

CLEAN_TMPL = """下面是视频「{title}」转写稿的第 {idx}/{total} 段原始文本（来自语音识别，可能有同音错字、缺标点、口语冗余）。

已知专有名词：{terms}

请仅做以下处理，输出清洗后的正文：
1. 删除口语填充词与重复啰嗦（嗯、啊、那个、就是说、然后呢、对不对、大家知道吧 等），保留全部有信息量的内容
2. 删除引流/广告话术：求关注点赞转发、加微信/进群/私信领取资料、报课报名引导、
   具体课程或机构的推广话术（即便与话题相关也删）；作者自述的经历与数据必须保留
3. 补上缺失标点，按语义分段
4. 结合专有名词表和视频标题修正同音错字、识别错误
5. 数字、单位、代码、英文原词保持原样
6. 严禁新增原文没有的事实、数据、案例或观点；无法判断的地方保留原样或标 [?]
7. 保持原有叙述顺序与人称，不要改变风格

只输出清洗后的正文，不要任何解释、标题或Markdown标记。

原始文本：
{chunk}"""

TERMS_TMPL = """从下面这段视频转写稿（标题：{title}）中提取所有专有名词、人名、品牌名、产品名、技术术语、英文词。
只输出一行逗号分隔的词语列表，不要任何解释。若没有则输出「无」。

文本：
{sample}"""

NOTE_TMPL = """请把下面这份已清洗的视频转写稿整理成一篇高质量的结构化中文笔记。

视频信息：
- 标题：{title}
- 作者：{uploader}
- 简介：{description}

输出要求（严格使用 Markdown）：
1. 首行用 `# {title}`
2. `## 一句话摘要` — 2-3 句话说清这个视频讲了什么
3. `## 核心要点` — 5-8 条要点，用 `-` 列表，每条一句话
4. `## 正文整理` — 按内容逻辑分成 3-6 个 `##` 章节，每章用段落和要点讲清楚，层次分明
5. `## 关键术语` — 5-10 个术语，各用一句话解释
6. `## 行动建议` — 可执行的建议用 `-` 列出；没有就写「无」
7. 语言精炼，彻底去除废话与客套；不得编造原文没有的信息
8. 不要输出「以下是整理结果」这类开场白，直接给正文

转写稿：
{cleaned}"""

ARTICLE_TMPL = """请把下面这份已清洗的视频转写稿改写成一篇可直接发布的公众号文章。

视频信息：
- 标题：{title}
- 作者：{uploader}

输出要求（严格使用 Markdown）：
1. 首行 `# ` 后面给一个有吸引力的新标题
2. 开头一段导语（80-150 字），抓住读者
3. 正文用 4-8 个 `##` 小标题分段，段落简短，多用短句，保留原作者的观点与案例
4. 结尾一段总结升华
5. 全文最后用 `## 关键要点` 列出 3-6 条要点
6. 不要编造原文没有的数据或案例；不要写「这篇文章将带你了解」之类的空洞句式

转写稿：
{cleaned}"""

CLEAN_ONLY_TMPL = """这是视频「{title}」的语音转写稿，请做最保守的润色：
1. 只删口语填充词（嗯、啊、那个、就是说）和明显重复
2. 删除与主题无关的引流/广告话术（求关注、点赞、加微信、私信领取 等）
3. 补标点、按语义分段
4. 修正明显的同音错字
5. 不重组结构、不删减信息、不新增内容
6. 输出 Markdown：首行 `# {title}`，之后按语义用 `##` 分小节（保留原讲述顺序）

转写稿：
{raw}"""

STYLES = {
    "general": GENERAL_TMPL,   # 默认：忠实原文的整理文稿
    "note": NOTE_TMPL,
    "article": ARTICLE_TMPL,
    "clean": CLEAN_ONLY_TMPL,
}
DEFAULT_STYLE = "general"


def _strip_fence(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```(?:markdown|md)?\s*\n", "", text)
    text = re.sub(r"\n```\s*$", "", text)
    return text.strip()


def _split_chunks(text: str, limit: int = CHUNK_LIMIT) -> list[str]:
    paras = [p for p in text.split("\n") if p.strip()]
    if not paras:
        return [text] if text.strip() else []
    chunks, cur = [], ""
    for p in paras:
        if len(cur) + len(p) + 1 > limit and cur:
            chunks.append(cur)
            cur = p
        else:
            cur = (cur + "\n" + p).strip()
    if cur:
        chunks.append(cur)
    return chunks


def _extract_terms(raw: str, title: str, model: str | None = None) -> str:
    sample = raw[:3000]
    if len(raw) > 3000:
        sample += "\n...\n" + raw[-2000:]
    try:
        out = chat(SYSTEM, TERMS_TMPL.format(title=title, sample=sample), model=model)
        return out.replace("\n", " ").strip()[:600]
    except Exception:
        return "无"


def polish(raw: str, meta: dict, style: str = "note", progress=None,
           model: str | None = None) -> str:
    """短文本一次完成；长文本 map-reduce：术语抽取 -> 分块清洗 -> 合并成稿

    model：按任务覆盖的配置化模型名（对应页面「LLM 模型」输入框）
    """
    title = (meta.get("title") or "未命名视频").strip()
    raw = raw.strip()
    if not raw:
        raise LLMError("转写文本为空，无法整理")

    if len(raw) <= SHORT_LIMIT:
        tmpl = STYLES.get(style, STYLES[DEFAULT_STYLE])
        if style == "clean":
            return _strip_fence(chat(SYSTEM, tmpl.format(title=title, raw=raw), model=model))
        return _strip_fence(chat(SYSTEM, tmpl.format(
            title=title, uploader=meta.get("uploader", ""),
            description=meta.get("description", "")[:300], cleaned=raw), model=model))

    terms = "无"
    if progress:
        progress("抽取专有名词，用于纠错")
    terms = _extract_terms(raw, title, model)

    chunks = _split_chunks(raw)
    total = len(chunks)

    def work(i: int) -> str:
        return chat(SYSTEM, CLEAN_TMPL.format(
            title=title, idx=i + 1, total=total, terms=terms, chunk=chunks[i]), model=model)

    if progress:
        progress(f"分 {total} 块清洗文本")
    maxw = min(int(load_settings().get("max_concurrency") or 4), 8)
    with ThreadPoolExecutor(max_workers=max(maxw, 1)) as ex:
        cleaned_parts = list(ex.map(work, range(total)))
    cleaned = "\n\n".join(p for p in cleaned_parts if p.strip())

    if progress:
        progress("合并并结构化排版")
    if style == "clean":
        return _strip_fence(chat(SYSTEM, CLEAN_ONLY_TMPL.format(title=title, raw=cleaned),
                                model=model))
    tmpl = STYLES.get(style, STYLES[DEFAULT_STYLE])
    return _strip_fence(chat(SYSTEM, tmpl.format(
        title=title, uploader=meta.get("uploader", ""),
        description=meta.get("description", "")[:300], cleaned=cleaned), model=model))


# ========== 幂等键 ==========
def resolve_model(model_override: str | None = None) -> str:
    """本次调用实际会用到的模型名（任务覆盖值优先，其次配置）"""
    if (model_override or "").strip():
        return model_override.strip()
    return (load_settings().get("llm_model") or "").strip() or "deepseek-v4.1-flash"


def signature(transcript: str, style: str, model: str | None = None) -> str:
    """成稿幂等键：原文 + 风格模板 + 模型 + 温度 全部一致才算「同一输入」。

    任一要素变化（换风格、换模型、重转写、改提示词模板、调温度）签名都会变，
    从而自动触发重新整理，避免拿到过期的旧稿。
    """
    tmpl = STYLES.get(style, STYLES[DEFAULT_STYLE])
    digest = hashlib.sha1(tmpl.encode("utf-8")).hexdigest()[:8]
    try:
        temp = str(float(load_settings().get("llm_temperature") or 0.3))
    except ValueError:
        temp = "0.3"
    raw = "\n".join((digest, style, resolve_model(model), temp, transcript or ""))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
