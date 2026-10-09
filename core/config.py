"""配置加载：内置默认值 <- 环境变量 V2N_* <- data/settings.json（后者优先）"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _resolve_data_dir() -> Path:
    """数据目录优先级：
    1. 环境变量 V2N_DATA_DIR（本地开发 / 自定义挂载）
    2. ModelScope 持久化目录 /mnt/workspace（创空间重启可保留数据）
    3. 项目内 data/（兜底）
    """
    custom = (os.getenv("V2N_DATA_DIR") or "").strip()
    if custom:
        return Path(custom).expanduser()

    persistent = Path("/mnt/workspace")
    if persistent.exists() and os.access(persistent, os.W_OK):
        return persistent / "video2note"

    return ROOT / "data"


DATA_DIR = _resolve_data_dir()
TASK_DIR = DATA_DIR / "tasks"
MEDIA_DIR = DATA_DIR / "media"
SETTINGS_FILE = DATA_DIR / "settings.json"

DEFAULTS: dict[str, str] = {
    # ---- 语音识别（模力方舟 / Gitee AI，OpenAI 兼容）----
    "asr_base_url": "https://api.moark.com/v1",
    "asr_api_key": "",
    "asr_model": "SenseVoiceSmall",
    "asr_language": "zh",
    # ---- 大模型整理 ----
    "llm_protocol": "anthropic",                 # anthropic（/v1/messages）| openai（/v1/chat/completions）
    "llm_base_url": "https://api.pateway.ai/v1",
    "llm_api_key": "",
    "llm_model": "deepseek-v4.1-flash",
    "llm_max_tokens": "16384",   # 推理模型会先输出 thinking，额度太小会导致正文被截断
    "llm_temperature": "0.3",
    # ---- 工程参数 ----
    "chunk_seconds": "600",
    "max_concurrency": "4",
    "cookie_text": "",                       # 抖音 Cookie 文本，自动落盘成 cookies.txt
    "cookie_text_bili": "",                  # B站 Cookie 文本，落盘成 cookies_bili.txt
    "cookie_browser": "",
    "cookie_file": "",
    "proxy": "",
    "ffmpeg_path": "",
}

SECRET_KEYS = {"asr_api_key", "llm_api_key"}

# ASR 可用模型（实测于 /v1/models），UI 下拉使用
ASR_MODELS = [
    "SenseVoiceSmall",
    "whisper-large-v3-turbo",
    "whisper-large-v3",
    "whisper-large",
    "whisper-base",
    "Fun-ASR-Nano-2512",
    "GLM-ASR",
    "TeleASR-MultiDialect",
]

HELP: dict[str, str] = {
    "asr_base_url": "ASR 接口地址（OpenAI 兼容，结尾的 /v1）",
    "asr_api_key": "模力方舟 API Token",
    "asr_model": "ASR 模型名，如 SenseVoiceSmall / whisper-large-v3",
    "asr_language": "识别语言代码，如 zh / en / yue",
    "llm_protocol": "LLM 协议：anthropic（/v1/messages + x-api-key）或 openai（/v1/chat/completions）",
    "llm_base_url": "LLM 接口地址（不含 /messages 或 /chat/completions）",
    "llm_api_key": "LLM API Key（留空则复用 ASR 的 Key）",
    "llm_model": "LLM 模型名",
    "llm_max_tokens": "单次生成最大 token 数（Anthropic 协议必填）",
    "llm_temperature": "生成温度 0-1",
    "chunk_seconds": "长音频切片时长（秒），短视频无需调整",
    "max_concurrency": "转写并发数",
    "cookie_text": "【抖音】Cookie 文本 —— 直接粘贴一整段 Cookie。"
                   "浏览器 F12 → Network → 任意请求 → Request Headers → 复制 Cookie 整段粘进来即可"
                   "（形如 a=1; b=2），程序会自动转成 cookies.txt；"
                   "也接受 Netscape cookies.txt 全文。抖音视频需要登录态时必填",
    "cookie_text_bili": "【B站】Cookie 文本 —— 与上面的抖音 Cookie 分开填写。"
                        "B站登录后在 F12 → Network → 点任意请求 → Request Headers → 复制 Cookie 整段。"
                        "务必包含登录会话凭证（F12 里能看到的三项），否则风控会返回 412。"
                        "程序自动转成 cookies_bili.txt，域名固定为 .bilibili.com",
    "cookie_browser": "【方式2】从浏览器读取 —— 只填浏览器名，不要粘贴任何 Cookie 内容。"
                      "可用值：chrome / edge / firefox / brave / chromium / opera / safari / vivaldi / whale。"
                      "程序会调用 browser-cookie 库直接读本机该浏览器的 Cookie（需先在浏览器登录）",
    "cookie_file": "【方式3】Cookie 文件路径 —— 本机已有的 Netscape 格式 cookies.txt 的绝对路径，"
                   "例如 D:\\cookies.txt。一般用不到，前两种任选其一即可",
    "proxy": "代理地址，如 http://127.0.0.1:7890",
    "ffmpeg_path": "ffmpeg 可执行文件路径，留空则使用 PATH 中的 ffmpeg",
}


def ensure_dirs() -> None:
    for d in (DATA_DIR, TASK_DIR, MEDIA_DIR):
        d.mkdir(parents=True, exist_ok=True)


def load_dotenv_once() -> None:
    """加载项目根目录 .env（若存在），不覆盖已有环境变量"""
    f = ROOT / ".env"
    if not f.exists():
        return
    try:
        for line in f.read_text("utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except Exception:
        pass


load_dotenv_once()


def load_settings() -> dict[str, str]:
    """读取顺序：内置默认值 <- data/settings.json <- 环境变量（环境变量最高优先级）

    环境变量优先级最高，是为了让部署平台（ModelScope 创空间等）用环境变量托管密钥：
    即使settings.json 里存着旧密钥，也会被环境变量覆盖，避免线上用了过期凭据。
    """
    s = dict(DEFAULTS)
    if SETTINGS_FILE.exists():
        try:
            stored = json.loads(SETTINGS_FILE.read_text("utf-8"))
            if isinstance(stored, dict):
                s.update(stored)
        except Exception:
            pass
    for k in DEFAULTS:
        for name in _env_names(k):
            v = os.getenv(name)
            if v:
                s[k] = v
                break
    return s


def _env_names(key: str) -> tuple[str, ...]:
    """某个配置项可接受的环境变量名（按优先级排列）。
    密钥类额外兼容 ModelScope 创空间常见的 Token 变量名。"""
    base = "V2N_" + key.upper()
    alias = {
        "asr_api_key": ("MOARK_API_TOKEN", "GITEE_AI_API_TOKEN", "ASR_API_KEY", "MODELSCOPE_API_TOKEN"),
        "llm_api_key": ("PATEWAY_API_KEY", "ANTHROPIC_API_KEY", "LLM_API_KEY"),
        "llm_protocol": ("LLM_PROTOCOL",),
    }
    return (base,) + alias.get(key, ())


def save_settings(patch: dict) -> dict[str, str]:
    ensure_dirs()
    cur = load_settings()
    for k, v in patch.items():
        if k in DEFAULTS and v is not None:
            cur[k] = str(v).strip()
    SETTINGS_FILE.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")
    return cur


COOKIE_FILE = DATA_DIR / "cookies.txt"

# browser-cookie 库支持的浏览器名（用于校验「从浏览器读取」框是否被误填）
BROWSER_CHOICES = ["chrome", "edge", "firefox", "brave", "chromium",
                   "opera", "safari", "vivaldi", "whale"]

_BROWSER_ALIAS = {
    "google chrome": "chrome", "谷歌浏览器": "chrome",
    "microsoft edge": "edge", "微软浏览器": "edge",
    "firefox": "firefox", "火狐": "firefox",
    "chrome.exe": "chrome", "msedge": "edge", "iexplore": "edge",
}


def normalize_browser(value: str) -> tuple[str, str]:
    """校验「从浏览器读取」框。

    返回 (规范化后的浏览器名, 错误提示)。空字符串合法；非浏览器名会给出提示，
    避免用户把 Cookie 文本粘到这个只该填 chrome/edge 的框里。
    """
    raw = (value or "").strip()
    if not raw:
        return "", ""
    low = raw.lower().strip()
    if low in BROWSER_CHOICES:
        return low, ""
    if low in _BROWSER_ALIAS:
        return _BROWSER_ALIAS[low], ""
    if "=" in raw or "\t" in raw or len(raw) > 60:
        return "", ("这里只填浏览器名（如 chrome / edge / firefox），不能粘贴 Cookie 内容。"
                    "Cookie 请粘贴到上方的「Cookie 文本」框。")
    return "", (f"不支持的浏览器：{raw[:30]}。可用值：{' / '.join(BROWSER_CHOICES[:6])} 等")

# Netscape cookies.txt 的数据行：domain  flag  path  secure  expiry  name  value
_NS_RE = re.compile(r"^[.#\w.-]+\t(?:TRUE|FALSE)\t", re.I)
_DOMAIN_HINT = re.compile(r"([\w.-]*(?:douyin|iesdouyin|bilibili|b23|tiktok)[\w.-]*)", re.I)
# 请求头串里这些不是 cookie，转换时跳过
_NOT_COOKIE = {"domain", "path", "expires", "max-age", "secure", "httponly", "samesite", "priority"}
_FAR_FUTURE = "2147483647"


def _sanitize_cookie_component(value: str) -> str:
    """把 Cookie 的名/值压成可安全放进 HTTP 头的ASCII。

    HTTP 头只能按 latin-1 编码，而抖音 Cookie 里常混有中文：
    `SEARCH_RESULT_LIST_TYPE={"keyword":"搜索词"}`、备注类字段、表情等。
    这些字符一旦进到请求头，yt-dlp 会在发请求前直接抛
    `'latin-1' codec can't encode characters in position N`。

    处理方式：优先整体百分号编码（语义正确、浏览器也能还原）；
    只有当值里本来就有 `%XX` 时才退让为逐字符编码，避免双重编码把
    已正确的 `%7B%22keyword%22` 变成 `%257B`。
    分隔符 `;` `,` 必须先剔除，否则会破坏 Cookie 的字段边界。
    """
    if not value:
        return value
    # 去掉会破坏字段结构的分隔符与空白
    cleaned = value.replace(";", "%3B").replace(",", "%2C")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    try:
        cleaned.encode("ascii")
        return cleaned
    except UnicodeEncodeError:
        pass
    # 已含百分号编码 -> 说明多半是 URL 编码过的 JSON，逐字符安全编码
    if re.search(r"%[0-9A-Fa-f]{2}", cleaned):
        return "".join(
            ch if ord(ch) < 128 else "".join(f"%{b:02X}" for b in ch.encode("utf-8"))
            for ch in cleaned
        )
    # 纯明文含非 ASCII -> 整体编码
    from urllib.parse import quote

    return quote(cleaned, safe="%!$&'()*+,-./:;=?@_~")


def normalize_cookie_text(text: str, default_domain: str = "") -> str:
    """把粘贴进来的 Cookie 文本规整成 Netscape cookies.txt 内容。

    支持三种输入：
    1. Netscape cookies.txt 全文（有表头或 tab 分隔行）→ 原样保留，只补齐表头
    2. 请求头串 `a=1; b=2` 或每行 `a=1` → 自动转成 Netscape 行
    3. 首行可写 `# domain=.douyin.com` 指定作用域，否则用 default_domain，
       都没给才从文本里猜，最后兜底 .douyin.com

    default_domain 用来把各平台 Cookie 钉到自己的域名上：B站的登录态落在
    .douyin.com 下是无效的，不能靠猜。

    Cookie 名与值都会被压成纯 ASCII（含中文时做百分号编码），
    否则请求头阶段的 latin-1 编码会失败。
    """
    raw = (text or "").replace("\r\n", "\n").strip()
    if not raw:
        return ""

    domain = ""
    m = re.search(r"^#\s*domain\s*=\s*(\S+)", raw, re.I | re.M)
    if m:
        domain = m.group(1)
        raw = re.sub(r"^#\s*domain\s*=\s*\S+[ \t]*\n?", "", raw, count=1, flags=re.I)
    if not domain:
        domain = (default_domain or "").strip()
    if not domain:
        dm = _DOMAIN_HINT.search(raw)
        domain = dm.group(1) if dm else ".douyin.com"
    domain = domain if domain.startswith(".") else "." + domain.lstrip(".")

    lines = [l for l in raw.split("\n") if l.strip()]
    if any(_NS_RE.match(l) for l in lines):
        body = [l for l in lines if _NS_RE.match(l)]
        return "# Netscape HTTP Cookie File\n" + "\n".join(body) + "\n"

    pairs: list[tuple[str, str]] = []
    for part in re.split(r"[;\n]", "\n".join(lines)):
        part = part.strip()
        if "=" not in part:
            continue
        k, _, v = part.partition("=")
        k = k.strip()
        if not k or k.lower().lstrip(".") in _NOT_COOKIE:
            continue
        pairs.append((_sanitize_cookie_component(k), _sanitize_cookie_component(v.strip())))
    pairs = [(k, v) for k, v in pairs if k]
    if not pairs:
        return ""

    rows = ["# Netscape HTTP Cookie File"]
    rows += [f"{domain}\tTRUE\t/\tFALSE\t{_FAR_FUTURE}\t{k}\t{v}" for k, v in pairs]
    return "\n".join(rows) + "\n"


# 平台 -> (配置项名, cookies.txt 文件名, 默认域名)
COOKIE_CHANNELS: dict[str, tuple[str, str, str]] = {
    "douyin": ("cookie_text", "cookies.txt", ".douyin.com"),
    "bilibili": ("cookie_text_bili", "cookies_bili.txt", ".bilibili.com"),
}


def _remove_cookie_file(path: Path) -> None:
    try:
        if path.exists():
            path.unlink()
    except Exception:
        pass


def ensure_cookie_file(platform: str = "douyin") -> str:
    """把该平台配置里的 Cookie 文本落盘为 cookies*.txt，返回路径；无内容返回 ''

    文本被清空时必须删掉已落盘的文件，否则会出现「页面回显为空、下载却仍在用
    旧 Cookie」的不一致——残留文件照样会被 yt-dlp 读走。
    """
    key, fname, domain = COOKIE_CHANNELS.get(platform, COOKIE_CHANNELS["douyin"])
    path = DATA_DIR / fname
    text = (load_settings().get(key) or "").strip()
    if not text:
        _remove_cookie_file(path)
        return ""
    ensure_dirs()
    content = normalize_cookie_text(text, default_domain=domain)
    if not content:
        return ""
    try:
        if path.exists() and path.read_text("utf-8", errors="ignore") == content:
            return str(path)
    except Exception:
        pass
    try:
        path.write_text(content, encoding="utf-8")
    except Exception:
        return ""
    return str(path)


def public_settings() -> dict:
    """给前端用的配置（密钥脱敏）

    额外返回 _from_env：标记哪些配置项由环境变量托管。
    前端据此把密钥输入框置为只读并提示「由平台环境变量管理」，
    避免在页面上误改、也避免把托管的密钥回显出来。
    """
    s = load_settings()
    out = {k: v for k, v in s.items()}
    from_env = {}
    for k in DEFAULTS:
        for name in _env_names(k):
            if os.getenv(name):
                from_env[k] = name
                break
    for k in SECRET_KEYS:
        v = out.get(k, "")
        # 托管在环境变量里的密钥一律不回显，连脱敏片段都不给
        if k in from_env:
            out[k] = ""
        else:
            out[k] = (v[:4] + "*" * max(0, len(v) - 8) + v[-4:]) if len(v) > 8 else ("*" * len(v))
    out["_configured"] = {
        "asr": bool(s.get("asr_api_key")),
        "llm": bool(s.get("llm_api_key") or s.get("asr_api_key")),
    }
    out["_from_env"] = from_env
    return out


def ff_bin(name: str) -> str:
    """返回 ffmpeg / ffprobe 可执行文件路径：
    设置里的路径 -> PATH -> imageio-ffmpeg 自带二进制（仅 ffmpeg）"""
    from shutil import which

    base = (load_settings().get("ffmpeg_path") or "").strip()
    if base:
        p = Path(base)
        if p.exists():
            if name == "ffmpeg":
                return str(p)
            cand = p.with_name(re.sub(r"^ffmpeg", name, p.name, flags=re.I))
            if cand.exists():
                return str(cand)
            return str(p.parent / (name + p.suffix))

    exe = which(name)
    if exe:
        return exe

    if name == "ffmpeg":
        try:
            import imageio_ffmpeg
            return imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            pass
    return name
