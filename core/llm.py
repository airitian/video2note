"""LLM 层：负责去口语化 / 纠错 / 排版

支持两种协议，均为 OpenAI 兼容生态的常见形态：
- anthropic：POST {base}/messages，鉴权头 x-api-key（如 api.pateway.ai 的 DeepSeek）
- openai：POST {base}/chat/completions，鉴权头 Authorization: Bearer

两者对上层统一暴露一个函数：chat(system, user, model=None) -> str
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor

import requests

from .config import SETTINGS_FILE, get_int_clamped, load_settings

SHORT_LIMIT = 3000      # 低于此长度一次性完成
CHUNK_LIMIT = 3500      # map 阶段每块字符数

# 管线版本：生成行为（合并方式/温度/质检）变化时递增，纳入缓存签名，
# 避免旧逻辑生成的缓存成稿在新逻辑上线后继续被当作有效结果复用。
PIPELINE_REV = "20261122-v1"

# 分风格温度：忠实类要稳、创作类要放。设置页显式填了温度时以设置页为准
# （见 _style_temperature），这里只提供未显式设置时的默认值。
STYLE_TEMPERATURES = {"general": 0.1, "digest": 0.3, "star": 0.35,
                      "article": 0.65}
# 清洗/术语/质检这类确定性工序统一用低温
WORK_TEMP = 0.1

# 术语抽样：均匀取 5 段。总量随稿长放大（见 _terms_budget）：
# 旧实现固定共 8000 字，三万字的稿子中后段第一次出现的术语抽样命不中，
# 名单还会被截断在 600 字——对应分块的同音错字就修不回来。
TERMS_TOTAL_CHARS = 8000
TERMS_MAX_CHARS = 16000
TERMS_SAMPLES = 5
TERMS_LIST_LIMIT = 1500

# 块间前文摘要：传递多少字上一段的清洗结果尾部
# 解决指代断裂（「他刚才提到的那个方案」里的"他/那个方案"指谁），
# 同时让同一个词在相邻块里的写法保持一致。
CARRY_CHARS = 500

# Anthropic 协议要求必带此头
ANTHROPIC_VERSION = "2023-06-01"


class LLMError(RuntimeError):
    pass


def _explicit_temperature() -> float | None:
    """用户显式设置的温度（环境变量或设置页真填过），没有则 None。

    load_settings() 会用默认值 0.3 兜底，无法区分「默认」与「用户就想要
    0.3」，所以这里直接看原始 settings.json 是否包含该键。
    """
    v = os.environ.get("V2N_LLM_TEMPERATURE")
    if v:
        try:
            return float(v)
        except ValueError:
            return None
    try:
        raw = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        if isinstance(raw, dict) and "llm_temperature" in raw:
            return float(raw["llm_temperature"])
    except Exception:
        pass
    return None


def _style_temperature(style: str) -> float:
    """本次生成实际使用的温度：用户显式设置优先，否则按风格取默认。"""
    explicit = _explicit_temperature()
    if explicit is not None:
        return min(max(explicit, 0.0), 2.0)
    return STYLE_TEMPERATURES.get(style, 0.3)


def _terms_budget(n_chars: int) -> int:
    """术语抽样总字数：短稿维持 8000，长稿按一半长度放大、上限 16000。"""
    return min(TERMS_MAX_CHARS, max(TERMS_TOTAL_CHARS, n_chars // 2))


def _config(model_override: str | None = None,
            temperature: float | None = None) -> dict:
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

    max_tokens = get_int_clamped(s, "llm_max_tokens", 16384, 256, 131072)
    if temperature is not None:
        temp = min(max(float(temperature), 0.0), 2.0)
    else:
        try:
            temp = float(s.get("llm_temperature") or 0.3)
        except ValueError:
            temp = 0.3

    model = (model_override or s.get("llm_model") or "").strip() \
        or "deepseek-ai/DeepSeek-V4.1-Flash-1017"
    return {
        "protocol": protocol,
        "base": base,
        "key": key,
        "model": model,
        "max_tokens": max_tokens,
        "temperature": temp,
        # 个别模型/路由不接受 temperature 参数（如 Kimi K3 经 DashScope
        # 路由：传了直接 400 invalid_parameter_error），这类模型省略该参数
        "omit_temperature": _model_omits_temperature(model),
    }


# 不接受 temperature 参数的模型（按模型名小写子串匹配）
_NO_TEMP_PATTERNS = ("kimi",)


def _model_omits_temperature(model: str) -> bool:
    m = (model or "").lower()
    return any(p in m for p in _NO_TEMP_PATTERNS)


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


# 推理模型（如 DeepSeek-V4.1-Flash）默认会先输出一大段 thinking，
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
    """OpenAI Chat Completions 协议（流式）。

    部分中转（如 newapi 类网关）对推理模型的非流式长请求会缓冲完整响应
    （含 reasoning）直至挂起/超时；改用流式逐 chunk 读取则正常返回。
    流式只取 delta.content（正文），reasoning_content 忽略，对调用方透明。
    """
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
    kwargs = dict(
        model=cfg["model"],
        messages=messages,
        max_tokens=cfg["max_tokens"],
        stream=True,
    )
    if not cfg.get("omit_temperature"):
        kwargs["temperature"] = cfg["temperature"]
    stream = client.chat.completions.create(**kwargs)
    parts: list[str] = []
    for chunk in stream:
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta
        content = getattr(delta, "content", None)
        if content:
            parts.append(content)
    return "".join(parts).strip()


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


def chat(system: str, user: str, max_retries: int = 2, model: str | None = None,
         temperature: float | None = None) -> str:
    """统一入口。

    model 是按任务传入的覆盖值（历史任务可能留存了已下线的旧模型名），
    若该模型报错不存在，自动回退到当前配置里的默认模型。
    temperature 显式传入时覆盖设置页的全局温度（分风格/分工序用）。
    """
    cfg = _config(model, temperature)
    try:
        return _request(cfg, system, user, max_retries)
    except LLMError as e:
        default_model = (load_settings().get("llm_model") or "").strip()
        if model and default_model and default_model != cfg["model"] and _is_model_error(str(e)):
            return _request(_config(None, temperature), system, user, max_retries)
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
GENERAL_TMPL = """请对下面这份语音转写稿做校对整理，只修错、不改写：
1. 修正转写错别字与同音误识（结合上下文判断，专有名词以视频标题为参考）
2. 补全明显缺失的文字（被吞掉的字、残缺的半句话）；没有把握的保留原样或标 [?]
3. 删除纯语气填充与引流话术（嗯、啊、求关注点赞、加微信私信领取等无信息内容）
4. 补上标点，按语义分段
5. 其余内容一字不删：不精简、不重组结构、不改变措辞与语气，不新增任何内容
6. 数字、单位、代码、英文原词保持原样
7. 直接输出整理后的全文：首行 `# {title}`，正文不要加小标题、不要任何解释

视频标题（纠错参考）：{title}

转写稿：
{cleaned}"""

DIGEST_TMPL = """请把下面这份视频转写稿精简成一篇只保留主要内容的文稿。

删除：
1. 无意义的内容：寒暄客套、口头禅、语气填充、与主题无关的闲聊跑题
2. 引流/广告话术：求关注点赞转发、加微信/进群/私信领取、报课与机构课程宣传
3. 废话与重复：同义反复、绕圈子铺垫、说了一半又重说的部分
4. 非重点枝节：对主线没有支撑作用的细枝末节与题外举例

保留（主要内容，一条不落）：
- 核心观点与结论
- 支撑观点的关键事实、数据、案例与操作步骤
- 重要的条件、边界与注意事项

规则：
- 保持原文的叙述顺序与原有表述：保留下来的内容用原文的句子，不要改写成摘要腔、不要换成你自己的概括
- 不新增原文没有的内容，不替作者下原文没有的结论
- 数字、单位、代码、英文原词保持原样；无法判断的转写疑难处保留原样或标 [?]
- 输出 Markdown：首行 `# {title}`，按语义用 `##` 分小节；直接输出正文，不要解释

视频信息（仅作纠错参考，不要写进正文）：
- 标题：{title}
- 作者：{uploader}

转写稿：
{cleaned}"""

CLEAN_TMPL = """下面是视频「{title}」转写稿的第 {idx}/{total} 段原始文本（来自语音识别，可能有同音错字、缺标点、口语冗余）。

已知专有名词：{terms}
{prev}
请仅做以下处理，输出清洗后的正文：
1. 删除口语填充词与重复啰嗦（嗯、啊、那个、就是说、然后呢、对不对、大家知道吧 等），保留全部有信息量的内容
2. 删除引流/广告话术：求关注点赞转发、加微信/进群/私信领取资料、报课报名引导、
   具体课程或机构的推广话术（即便与话题相关也删）；作者自述的经历与数据必须保留
3. 补上缺失标点，按语义分段
4. 结合专有名词表和视频标题修正同音错字、识别错误
5. 数字、单位、代码、英文原词保持原样
6. 严禁新增原文没有的事实、数据、案例或观点；无法判断的地方保留原样或标 [?]
7. 保持原有叙述顺序与人称，不要改变风格
8. 上文摘要仅用于理解指代与统一术语用词，**不要重复输出它的内容**，也不要加「上文提到」之类的衔接语

只输出清洗后的正文，不要任何解释、标题或Markdown标记。

原始文本：
{chunk}"""

TERMS_TMPL = """从下面这段视频转写稿（标题：{title}）中提取所有专有名词、人名、品牌名、产品名、技术术语、英文词。
只输出一行逗号分隔的词语列表，不要任何解释。若没有则输出「无」。

文本：
{sample}"""

# 公众号文章风格专属 system：与全局 SYSTEM 的区别是放开「修辞自由」，
# 只锁死「事实必须来自原文」——否则全局铁律会把钩子/过渡/互动全部压掉，写出干巴巴的缩水版
ARTICLE_SYSTEM = (
    "你是头部微信公众号的主笔，擅长把视频内容改写成读者愿意读完、愿意转发的推文。"
    "铁律：事实、数据、案例、观点必须来自转写稿，一个都不能编；"
    "但标题、开头钩子、过渡句、互动收尾属于修辞，可以自由发挥。"
    "直接输出 Markdown 文章正文，不要任何解释、前言或后缀。"
)

# 公众号爆款文章风格：选题切入 + 爆款标题 + 黄金三段式 + 说人话 + 手机端排版
ARTICLE_TMPL = """下面是一份视频转写稿（已清洗），请基于它改写一篇公众号文章。

视频信息（仅用于选题参考，不要原样写进正文）：
- 标题：{title}
- 作者：{uploader}
- 简介：{description}

转写稿：
{cleaned}

==================== 改写规则（逐条执行） ====================

【一、选题与切入】
- 从读者需求、痛点、好奇心或实用价值出发找切入角度：读者是谁？他正被什么问题困扰？这个视频能帮他什么？
- 避免自嗨：不要罗列"这个视频讲了什么"，要回答"读者为什么要看下去"
- 可参考的高传播切入：痛点提问、反常识结论、真实经历复盘、数字承诺

【二、标题（首行输出，只给 1 个）】
- 用爆款公式：数字+干货 / 反常识或悬念 / 引发共鸣，任选其一或组合
- 20 字以内为佳，具体、有画面感或冲突感；禁止"浅谈XX""XX的几点思考"这类空泛标题
- 格式：首行 `# ` + 标题

【三、结构：黄金三段式】
1. 开头钩子（三选一）：痛点场景（让读者看到自己）/ 反常识金句 / 一句故事悬念。150 字以内，禁止暖场废话
2. 正文分 3-5 个小节，每节一个 `## ` 一级小标题；内容密的小节可用 `### ` 二级小标题再分
3. 每节只解决一个具体问题或讲清一个层次，节与节之间有推进感，不重复
4. 结尾总结升华：回扣开头的钩子，给一句读者能带走的话；也可轻轻抛一个问题引导评论互动

【四、语言风格】
- 说人话：像朋友聊天，短句为主，一句一个意思，允许口语化表达
- 去 AI 味：严禁"首先/其次/再者/综上所述/总而言之/值得一提的是/本文将/不仅…更…/赋能/抓手"这类套话
- 不用排比堆砌、不用空洞形容词、不写"在这个快节奏的时代"式开场
- 段落要短：一两句一段，读起来有呼吸感

【五、排版规范】
- 关键结论、金句、数字用 **加粗**
- 开头金句或实测数据可用 `> ` 引用块突出
- 多用短段落和列表，避免大段密集文字
- Markdown 只用这些元素：`# `（仅标题一次）、`## `、`### `、`> `、`**`、`- `

【六、硬性底线】
- 正文总字数控制在 800-2000 字
- 不得编造原文没有的数据、案例、观点、引用；修辞性过渡可以写，事实性内容必须来自转写稿
- 保留原作者的核心观点与关键案例，可以重组结构、换表述
- 直接输出文章正文，不要任何解释、前后缀或"以下是改写结果"之类的话"""

# 面试话术风格专属 system：突破全局「忠实原文」铁律——允许判断完整性、允许修正不严谨说法，
# 但修正只能靠技术常识、不得引入原文没有的结论；补充只能写通用方法/常见风险/面试追问并标注。
# 风格键沿用 star（历史键名，避免旧任务缓存失效与前端/接口兼容问题），展示名已改为「面试话术」。
INTERVIEW_SYSTEM = (
    "你是一名资深技术面试官兼面试回答编辑，把视频里的技术回答重组为自然、准确、可口述的面试话术。"
    "核心：优先保留作者自己的观点、经验和技术细节，不改成空泛的标准答案。铁律："
    "一、保留原回答的核心观点、技术深度和个人表达；"
    "二、修正绝对化、过度简化、概念边界混淆，只能靠技术常识，不引入原文没有的结论；"
    "三、不凭空编造作者的项目经历、指标结果、工具经验或数据，"
    "    新增内容只作为通用测试维度、常见风险或面试追问提出，并标注 [补充]。"
    "成文用第一人称口语、自然过渡（可用「先说结论/另外/最后」，分层讲透但过渡用人话，不用套话）；"
    "信息顺序「先给结论→再解释原因→最后补测试方法、例子、边界与闭环」；"
    "指标要和验证方法对应、并说明失效场景，不只罗列名词。"
    "直接输出 Markdown 正文，不要任何解释、前言或后缀。"
)

# 面试话术风格：完整性判断 + 准确性修正 + 口语化改写 + 技术准确性检查（固定两段 + 可选短版）
INTERVIEW_TMPL = """下面是一段技术类视频的转写稿（已清洗），视频里是作者针对某个技术问题的口述回答。请以资深技术面试官兼面试回答编辑的身份，把它重组为自然、准确、能在现场说出来的面试话术。

视频信息（仅用于理解背景，不要原样写进正文）：
- 标题：{title}
- 作者：{uploader}
- 简介：{description}

转写稿：
{cleaned}

==================== 处理规则（逐条执行） ====================

【一、先判断完整性】
- 判断原回答是否完整：已经覆盖的关键点、最值得保留的优点、明显缺失或容易被追问的部分
- 只在「内容判断」里用一小段话说清，不要逐句挑错、不要列清单

【二、修正技术表述】
- 重点修正：绝对化、过度简化、概念边界混淆
- 只改「说错/不严谨」的地方，不推翻作者观点和表达；只能靠技术常识，不引入原文没有的结论

【三、调整信息顺序】
- 保留技术深度，但顺序改为：先给结论/判断 → 再解释原因 → 最后补测试方法、例子、边界和闭环

【四、口语化改写】
- 第一人称口语、专业但不背稿：多用「我会先确认……」「这里我会重点看……」「如果出现问题，我会先判断……」
- 句子长短交替，保留必要技术名词，避免连续堆砌术语
- 可用「先说结论」「另外」「最后」做自然路标；可以分层讲解，每层要讲透、层与层分开是必须的，
  但过渡用人话、不用套话
- 名词第一次出现用人话解释一句，能举例就举真例子，重点结论加粗，去掉「首先/其次/综上所述」这类 AI 腔
- 不用夸张总结、营销话术、空泛价值判断、明显 AI 套话

【五、不编造（严守）】
- 不得凭空增加作者的项目经历、指标结果、工具经验或数据
- 需要新增时，只写通用测试维度/常见风险，或作为面试追问提出，并标注 [补充]

【六、技术准确性检查（按题目领域做针对性检查）】
- 不把传统测试和大模型测试简单概括成「对不对」「好不好」：说明预期结果是否固定、输出是否开放、评价是否要多维指标
- 不把模型/检索/生成/应用编排混为一层：区分模型层、数据层、检索层、生成层、应用工程层
- 不把所有链路都当确定性逻辑：OCR、向量化、排序模型、生成模型可能有波动，说明哪些部分能单独验证
- 指标要和验证方法对应：检索可谈 Recall@K / MRR / nDCG，生成可谈正确性、相关性、忠实度、拒答、引用、幻觉；但不只罗列名词，要说清怎么测
- 说明指标/方法的失效场景：规则可能漏掉语义等价，评分模型可能有偏差，人工评审难规模化，单一指标不代表整体质量
- 面向真实产品补边界：权限隔离、数据新鲜度、越权、敏感信息、超时降级、流式中断、成本、长尾时延

【七、输出格式（固定）】
- 首行 `# ` 后是标题：从转写稿里提炼「面试要回答的核心问题」作标题，不要照搬原视频标题（{title} 仅作背景参考）
  - 标题要精简：控制在一句话内（一般不超过 20 字），只点出核心问题，不复述内容、不带引流话术
  - 不能遗漏：若原回答覆盖了多个问题，标题要把主要问题都概括进去（可用顿号/斜杠并排），不能只挑一个、漏掉其余
  - 若原文没明说问题、只是观点陈述，则提炼「作者要回答的核心主题」作标题
- `## 内容判断`：一小段话（见【一】）
- `## 优化后的回答`：完整、连贯的口述稿，通常包含：先确认目标/用户/边界/通过标准 → 按业务链路或风险拆测试重点 → 关键指标说明怎么测 → 补异常/非功能/安全场景 → 最后说明上线门禁、监控、bad case 回流或问题定位闭环
- `## 可选短版`：仅当完整回答超过现场可控长度时，补一版约一分钟的口述版，保留主线、不塞细节
- 结尾保留两三个自然的追问入口（如问题定位方式、指标选择依据、业务权重确定），不要为了显得全面而无限扩展
- 直接输出正文，不要「以下是改写结果」之类的开场白"""

STYLES = {
    "general": GENERAL_TMPL,   # 整理文稿（默认，第一位）：基于原文校对，只修错补缺、一字不删
    "digest": DIGEST_TMPL,     # 精简文稿：删无意义/引流/废话/非重点，只留主要内容
    "article": ARTICLE_TMPL,
    "star": INTERVIEW_TMPL,    # 面试话术：内容判断 + 口语化回答（键沿用历史 star）
}
DEFAULT_STYLE = "general"
# 各风格专属 system prompt；未列出的风格走全局 SYSTEM（忠实原文铁律）
STYLE_SYSTEMS = {
    "article": ARTICLE_SYSTEM,
    "star": INTERVIEW_SYSTEM,
}


# ========== 生成后质检（独立环节，只评不改） ==========
# 质检在全部风格生成完之后单独跑，产出报告供人查看（页头「质检报告」）。
# 它只对照风格要求评价成稿、列问题与修改建议，绝不改写成稿本身。

STYLE_RUBRICS = {
    "general": "整理文稿：基于原文校对——只修错别字、补缺失字、删纯填充词与引流内容，"
               "其余内容与表述一字不删、不改写、不概括；字数应与原文接近（保留率不低于 85%）",
    "digest": "精简文稿：删无意义/引流/废话/非重点内容，只保留主要内容与关键信息；"
              "明显比原文短，但主线与关键事实完整，不能把重点也删掉",
    "article": "公众号文章：改写成适合公众号发布的文章体——有吸引人的标题与小标题、"
               "段落顺畅、可读性强；可适度重组顺序，但事实、数据须与原文一致，不得编造",
    "star": "面试话术：整理成面试问答口语稿——含「内容判断」与「优化后的回答」两个固定板块，"
            "口语化、结构清晰，是能直接开口讲的回答，不是书面摘要",
}

QC_TMPL = """你是文稿质检员。用户对每种风格有明确预期（见【风格要求】）。请对照【风格要求】与【原文】检查【成稿】是否符合预期，只输出一行 JSON，不要任何解释。

输出格式：{{"fit": "符合", "problems": [], "suggestions": []}}
- fit：只能是「符合」「基本符合」「不符合」之一。不符合 = 明显偏离风格定位（如整理文稿大幅删减或被概括、精简文稿几乎照搬全文、公众号文章没有标题结构、面试话术不像口语问答）
- problems：具体问题，最多 5 条，指明哪里不符合风格要求、哪里与原文事实出入
- suggestions：针对问题给出怎么修改，最多 5 条
成稿完全符合时 problems 与 suggestions 留空数组。

【风格要求】
{rubric}

【原文】
{source}

【成稿】
{note}"""


def _parse_review(raw: str) -> dict | None:
    """从质检回复里抠出 JSON；解析不了返回 None。"""
    m = re.search(r"\{.*\}", raw or "", re.DOTALL)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    fit = str(data.get("fit") or "").strip()
    if fit not in ("符合", "基本符合", "不符合"):
        fit = "基本符合" if fit else "未评"
    probs = data.get("problems") or []
    sugs = data.get("suggestions") or []
    return {"fit": fit,
            "problems": [str(x) for x in probs][:5] if isinstance(probs, list) else [],
            "suggestions": [str(x) for x in sugs][:5] if isinstance(sugs, list) else []}


def review_note(source: str, note: str, style: str,
                judge_model: str | None = None) -> dict:
    """生成后质检：对照风格要求评价一篇成稿，返回 {fit, problems, suggestions}。

    质检失败不抛异常，返回「未评」加失败原因——质检是旁路，不能拖垮主流程。
    """
    rubric = STYLE_RUBRICS.get(style, STYLE_RUBRICS["general"])
    try:
        raw = chat(SYSTEM, QC_TMPL.format(rubric=rubric,
                                          source=(source or "")[:16000],
                                          note=(note or "")[:16000]),
                   model=judge_model, temperature=0.2, max_retries=1)
    except Exception as e:
        return {"fit": "未评", "problems": [f"质检调用失败：{e}"[:160]], "suggestions": []}
    parsed = _parse_review(raw)
    if parsed is None:
        return {"fit": "未评", "problems": ["质检回复无法解析"], "suggestions": []}
    return parsed


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


def _sample_terms(text: str, total: int = TERMS_TOTAL_CHARS,
                  parts: int = TERMS_SAMPLES) -> str:
    """从全文均匀取若干段做术语抽样

    为什么不用「首 N 字 + 尾 N 字」：那样只覆盖开头和结尾。一小时视频里
    某个产品名若在第 40 分钟才第一次出现，就永远进不了术语表，后面所有块
    都会把它写错。均匀切分保证全文任何位置的专有名词都有机会被抽到。

    段落边界尽量落在换行/句号上，减少把一个词切成两半的情况。
    """
    if len(text) <= total:
        return text

    per = max(total // parts, 200)
    # 起点按「首段贴头、末段贴尾」均分，而不是 step = len // parts。
    # 后者会让末段停在 len-per 之前，尾部一整块（约 1/parts 的长度）
    # 完全落在抽样之外——而结尾的总结与出镜感谢往往正是术语出处。
    span = len(text) - per
    bounds = [int(i * span / max(parts - 1, 1)) for i in range(parts)]

    out: list[str] = []
    for i, start in enumerate(bounds):
        if i > 0:
            # 起点回退到最近的换行，避免从半个词开始；首段不回退，否则会漏开头
            head = text.rfind("\n", 0, start + 1)
            if head != -1 and start - head <= 80:
                start = head + 1
        end = len(text) if i == parts - 1 else min(start + per, len(text))
        seg = text[start:max(end, start)].strip()
        if seg:
            out.append(seg)
    return "\n...\n".join(out) if out else text[:total]


def _extract_terms(raw: str, title: str, model: str | None = None) -> str:
    sample = _sample_terms(raw, total=_terms_budget(len(raw)))
    try:
        out = chat(SYSTEM, TERMS_TMPL.format(title=title, sample=sample),
                   model=model, temperature=WORK_TEMP)
        return out.replace("\n", " ").strip()[:TERMS_LIST_LIMIT]
    except Exception:
        return "无"


def _carry_block(done: list[str], limit: int = CARRY_CHARS) -> str:
    """生成注入下一块的「前文摘要」

    取已清洗内容的尾部若干字。刻意用**尾部**而不是摘要：中文的指代
    （「他刚才提到的那个方案」）几乎总是回指紧邻上文，尾部信噪比最高；
    而跨段的术语一致性已由全局术语表负责，不必重复携带。
    """
    if not done:
        return ""
    tail = "\n".join(p for p in done if p.strip()).strip()
    if not tail:
        return ""
    if len(tail) > limit:
        tail = "…" + tail[-limit:]
    return (f"\n上文摘要（紧邻本段之前的已清洗内容，仅供理解指代，"
            f"不要重复输出）：\n{tail}\n")


def polish(raw: str, meta: dict, style: str = "general", progress=None,
           model: str | None = None) -> str:
    """短文本一次完成；长文本 map-reduce：术语抽取 -> 分块清洗 -> 合并成稿

    model：按任务覆盖的配置化模型名（对应页面「LLM 模型」输入框）
    """
    title = (meta.get("title") or "未命名视频").strip()
    raw = raw.strip()
    if not raw:
        raise LLMError("转写文本为空，无法整理")
    if progress is None:
        progress = lambda _msg: None  # noqa: E731  后续环节统一直接调用

    if len(raw) <= SHORT_LIMIT:
        tmpl = STYLES.get(style, STYLES[DEFAULT_STYLE])
        temp = _style_temperature(style)
        sys_p = STYLE_SYSTEMS.get(style, SYSTEM)
        note = _strip_fence(chat(sys_p, tmpl.format(
            title=title, uploader=meta.get("uploader", ""),
            description=meta.get("description", "")[:300], cleaned=raw),
            model=model, temperature=temp))
        # 生成内质检已下线（2026-11-22）：质检改为生成后的独立环节出报告，
        # 只评不改——避免「按意见整篇重写」把好稿改没（曾把 4785 字改成 1080 字）
        return note

    progress("抽取专有名词，用于纠错")
    terms = _extract_terms(raw, title, model)

    chunks = _split_chunks(raw)
    total = len(chunks)
    maxw = get_int_clamped(load_settings(), "max_concurrency", 4, 1, 8)

    if progress:
        progress(f"分 {total} 块清洗文本")

    # 分批推进：批内并行（互不依赖，可并发），批与批之间把上文摘要带进下一批。
    # 为什么不全串行——串行上下文最完整，但耗时会翻数倍；
    # 分批在并发度和连贯性之间取平衡，断点只落在相邻两批之间。
    cleaned_parts: list[str] = []
    for start in range(0, total, maxw):
        batch = list(range(start, min(start + maxw, total)))
        prev = _carry_block(cleaned_parts)

        def work(i: int, _prev: str = prev) -> str:
            return chat(SYSTEM, CLEAN_TMPL.format(
                title=title, idx=i + 1, total=total, terms=terms,
                prev=_prev, chunk=chunks[i]), model=model, temperature=WORK_TEMP)

        if progress:
            progress(f"清洗第 {batch[0] + 1}-{batch[-1] + 1} 段 / 共 {total} 段")
        with ThreadPoolExecutor(max_workers=maxw) as ex:
            cleaned_parts.extend(ex.map(work, batch))
    cleaned = "\n\n".join(p for p in cleaned_parts if p.strip())

    if style == "general":
        # 整理文稿：分块清洗的产物本身就是校对稿，拼接后只加一行标题，
        # 不再整篇再生成——避免同一风格下成稿长度大幅漂移、内容被重写
        progress("拼接清洗稿")
        note = f"# {title}\n\n{cleaned}" if title else cleaned
    else:
        progress("合并并结构化排版")
        tmpl = STYLES.get(style, STYLES[DEFAULT_STYLE])
        sys_p = STYLE_SYSTEMS.get(style, SYSTEM)
        note = _strip_fence(chat(sys_p, tmpl.format(
            title=title, uploader=meta.get("uploader", ""),
            description=meta.get("description", "")[:300], cleaned=cleaned),
            model=model, temperature=_style_temperature(style)))
    # 同短稿路径：生成后不再做质检修订，成稿直接交付；质检走独立报告
    return note


# ========== 幂等键 ==========
def resolve_model(model_override: str | None = None) -> str:
    """本次调用实际会用到的模型名（任务覆盖值优先，其次配置）"""
    if (model_override or "").strip():
        return model_override.strip()
    return (load_settings().get("llm_model") or "").strip() or "deepseek-ai/DeepSeek-V4.1-Flash-1017"


def signature(transcript: str, style: str, model: str | None = None) -> str:
    """成稿幂等键：原文 + 风格模板 + 模型 + 温度 全部一致才算「同一输入」。

    任一要素变化（换风格、换模型、重转写、改提示词模板、调温度）签名都会变，
    从而自动触发重新整理，避免拿到过期的旧稿。
    """
    tmpl = STYLES.get(style, STYLES[DEFAULT_STYLE])
    digest = hashlib.sha1(tmpl.encode("utf-8")).hexdigest()[:8]
    temp = str(_style_temperature(style))
    raw = "\n".join((digest, style, resolve_model(model), temp,
                     PIPELINE_REV, transcript or ""))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
