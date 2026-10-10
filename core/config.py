"""配置加载（三层来源，后者覆盖前者）：

    内置默认值  <-  data/settings.json  <-  data/secrets.json  <-  环境变量 V2N_*

密钥类配置单独存放在 secrets.json，且**永不通过接口回传**：
前端只能知道「配没配、配在哪」，拿不到内容本身。settings.json 里如果
残留了旧密钥，会在首次读取时自动搬进 secrets.json 并抹掉。
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _resolve_data_dir() -> Path:
    """数据目录优先级：
    1. 环境变量 V2N_DATA_DIR（本地开发 / 自定义挂载）
    2. /mnt/workspace（若存在且可写，用于容器持久化，重启可保留数据）
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
# 敏感配置独立成文件：便于单独 chmod 600、单独加进 .gitignore、
# 单独复制到服务器而不把普通配置一起带走。
SECRETS_FILE = Path(
    (os.getenv("V2N_SECRETS_FILE") or "").strip() or (DATA_DIR / "secrets.json")
).expanduser()

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
    "chunk_seconds": "180",
    # 首片单独取短，让第一句话尽快出现（秒）。ASR 耗时与音频长度近似成正比，
    # 180 秒的片要等 30~60 秒才出第一句；15 秒则约 3~6 秒。
    # 调大 = 首句慢一点但省调用次数；调小 = 首句更快但分片更多。
    "first_chunk_seconds": "15",
    "max_concurrency": "4",
    "cookie_text": "",                       # 抖音 Cookie 文本，自动落盘成 cookies.txt
    "cookie_text_bili": "",                  # B站 Cookie 文本，落盘成 cookies_bili.txt
    "cookie_browser": "",
    "cookie_file": "",
    "proxy": "",
    "ffmpeg_path": "",
    # 解析接口的浏览器抓包模板（dlpanda 等）。含 Cookie，等价于会话凭证，
    # 所以归入敏感项，只落 secrets.json。
    "resolver_curl": "",
}

# 敏感配置：只落在 secrets.json，永不经接口下发。
# Cookie 文本同样算敏感——它就是登录态，回显到浏览器等于把账号会话交出去。
SECRET_KEYS = {
    "asr_api_key", "llm_api_key", "cookie_text", "cookie_text_bili",
    "resolver_curl",
}

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
    # ---- 平台登录态（最高频，且解析模板紧随其后，改一处就能接着改下一处）----
    "cookie_text": "【抖音】Cookie 文本 —— 直接粘贴一整段 Cookie。"
                   "浏览器 F12 → Network → 任意请求 → Request Headers → 复制 Cookie 整段粘进来即可"
                   "（形如 a=1; b=2），程序会自动转成 cookies.txt；"
                   "也接受 Netscape cookies.txt 全文。抖音视频需要登录态时必填",
    "cookie_text_bili": "【B站】Cookie 文本 —— 与上面的抖音 Cookie 分开填写。"
                        "B站登录后在 F12 → Network → 点任意请求 → Request Headers → 复制 Cookie 整段。"
                        "务必包含登录会话凭证（F12 里能看到的三项），否则风控会返回 412。"
                        "程序自动转成 cookies_bili.txt，域名固定为 .bilibili.com",
    "resolver_curl": "抖音解析接口的请求模板（可选）",
    # ---- 模型与生成 ----
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
    # ---- 性能 ----
    "chunk_seconds": "长音频切片时长（秒），短视频无需调整",
    "first_chunk_seconds": "首个分片的时长（秒）。越小，第一句话出现得越快；"
                           "越大越省 ASR 调用次数。默认 15",
    "max_concurrency": "转写并发数",
    # ---- 其他 ----
    "cookie_browser": "【方式2】从浏览器读取 —— 只填浏览器名，不要粘贴任何 Cookie 内容。"
                      "可用值：chrome / edge / firefox / brave / chromium / opera / safari / vivaldi / whale。"
                      "程序会调用 browser-cookie 库直接读本机该浏览器的 Cookie（需先在浏览器登录）",
    "cookie_file": "【方式3】Cookie 文件路径 —— 本机已有的 Netscape 格式 cookies.txt 的绝对路径，"
                   "例如 D:\\cookies.txt。一般用不到，前两种任选其一即可",
    "proxy": "代理地址，如 http://127.0.0.1:7890",
    "ffmpeg_path": "ffmpeg 可执行文件路径，留空则使用 PATH 中的 ffmpeg",
}

# 设置面板的分组与顺序。设面板从长表单里找出某一项很费力——
#尤其 resolver_curl 这种「不配置也能跑、但配了能救场」的可选项，
# 排在最后等于事实上不可见。这里显式定序，前端按它渲染。
#
# 每一项: (分组标题, [该组内的配置键...])
SETTINGS_GROUPS: list[tuple[str, list[str]]] = [
    ("平台登录态", ["cookie_text", "cookie_text_bili", "resolver_curl"]),
    ("模型与生成", ["asr_language", "llm_max_tokens", "llm_temperature"]),
    ("性能", ["first_chunk_seconds", "chunk_seconds", "max_concurrency"]),
]


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


def _read_json(path: Path) -> dict:
    """读JSON 文件；不存在或损坏都按空处理，不让配置问题拖垮整个服务"""
    try:
        if path.exists():
            data = json.loads(path.read_text("utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def _write_json(path: Path, data: dict, private: bool = False) -> None:
    """写 JSON 文件。private=True 时把权限收紧到仅属主可读写（Windows 上忽略）"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    if private:
        try:
            os.chmod(path, 0o600)
        except Exception:
            pass


def load_settings() -> dict[str, str]:
    """读取顺序：内置默认值 <- settings.json <- secrets.json <- 环境变量

    环境变量优先级最高，是为了让部署平台（云服务器 / 容器等）用环境变量托管密钥：
    即使文件里存着旧密钥，也会被环境变量覆盖，避免线上用了过期凭据。

    secrets.json 覆盖 settings.json：密钥永远以独立文件为准，
    这样即使用户误把密钥提交进settings.json，也会被这里的正确值覆盖。
    """
    s = dict(DEFAULTS)
    s.update(_read_json(SETTINGS_FILE))
    s.update(_read_json(SECRETS_FILE))
    for k in DEFAULTS:
        for name in _env_names(k):
            v = os.getenv(name)
            if v:
                s[k] = v
                break
    return s


def migrate_secrets() -> bool:
    """把 settings.json 里的敏感项搬到 secrets.json，并从 settings.json 移除。

    升级旧版本时自动执行一次：用户的 Token / Cookie 不会因为这次改动而丢失，
    但从此不再留在会被读回浏览器的 settings.json 里。
    """
    stored = _read_json(SETTINGS_FILE)
    moving = {k: v for k, v in stored.items() if k in SECRET_KEYS and v}
    if not moving:
        return False
    secrets = _read_json(SECRETS_FILE)
    for k, v in moving.items():
        secrets.setdefault(k, v)
    _write_json(SECRETS_FILE, secrets, private=True)
    for k in moving:
        stored.pop(k, None)
    _write_json(SETTINGS_FILE, stored)
    return True


def _env_names(key: str) -> tuple[str, ...]:
    """某个配置项可接受的环境变量名（按优先级排列）。
    密钥类额外兼容常见的 Token 变量名。"""
    base = "V2N_" + key.upper()
    alias = {
        "asr_api_key": ("MOARK_API_TOKEN", "GITEE_AI_API_TOKEN", "ASR_API_KEY"),
        "llm_api_key": ("PATEWAY_API_KEY", "ANTHROPIC_API_KEY", "LLM_API_KEY"),
        "llm_protocol": ("LLM_PROTOCOL",),
    }
    return (base,) + alias.get(key, ())


def save_settings(patch: dict, clear_secrets: set[str] | None = None) -> dict[str, str]:
    """保存配置。敏感项写入 secrets.json（0600），其余写 settings.json

    clear_secrets 里列出的敏感项会被显式清空（值为空字符串）。
    之所以要有这个参数：敏感项不再回传，前端无法靠「回传值为空」判断用户
    是否想清空——那会把「没改」和「想清空」混为一谈，导致打开设置就把密钥抹掉。
    """
    ensure_dirs()
    clear_secrets = clear_secrets or set()
    cur = load_settings()
    normal: dict[str, str] = {}
    secret: dict[str, str] = {}

    for k, v in patch.items():
        if k not in DEFAULTS or v is None:
            continue
        val = str(v).strip()
        if k in SECRET_KEYS:
            # 空值不覆盖已有密钥：前端不持有密钥，「清空」必须走 clear_secrets
            if val:
                secret[k] = val
        else:
            normal[k] = val

    for k in clear_secrets:
        if k in SECRET_KEYS:
            secret[k] = ""

    # 普通配置：整份重写，剔除任何敏感项残留
    stored = {k: v for k, v in _read_json(SETTINGS_FILE).items() if k not in SECRET_KEYS}
    stored.update(normal)
    _write_json(SETTINGS_FILE, stored)

    # 敏感配置：独立文件 + 0600
    secrets = {k: v for k, v in _read_json(SECRETS_FILE).items() if k in SECRET_KEYS}
    for k, v in secret.items():
        if v:
            secrets[k] = v
        else:
            secrets.pop(k, None)
    if secret or secrets:
        _write_json(SECRETS_FILE, secrets, private=True)

    cur.update(normal)
    cur.update({k: v for k, v in secret.items() if v})
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
    """给前端用的配置。

    敏感项（Token / Cookie）一律返回空字符串——不是打码，是**完全不发**。
    早前这里返回过 `sk-1***...***abcd` 这样的片段，但「前4 后4」对短密钥几乎等于
    明文（44位里泄露 8 位），而且浏览器里F12 一开就全在，所以直接切断。

    前端要判断「配没配」，用返回的 `_configured`；
    要改值，去编辑 secrets.json 或设环境变量。
    """
    s = load_settings()
    out = dict(s)

    from_env = {}
    for k in DEFAULTS:
        for name in _env_names(k):
            if os.getenv(name):
                from_env[k] = name
                break

    for k in SECRET_KEYS:
        out[k] = ""

    out["_configured"] = {
        "asr": bool(s.get("asr_api_key")),
        "llm": bool(s.get("llm_api_key") or s.get("asr_api_key")),
        "cookie_douyin": bool(s.get("cookie_text")),
        "cookie_bilibili": bool(s.get("cookie_text_bili")),
        "resolver_curl": bool(s.get("resolver_curl")),
    }
    out["_from_env"] = from_env
    # 密钥落盘位置，页面据此提示用户去哪里改
    out["_secrets_file"] = str(SECRETS_FILE)
    return out


# ============================ 抖音失败原因提示 ============================
# 原本随 core/douyin.py 一起维护，该文件已随 pyktok 兜底通道移除；
# 这两段文案是实测踩坑的结论，对用户排障有用，因此保留在此。
DOUYIN_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36")

# 抖音的失败原因有两种，解法完全不同，不能混为一谈：
#
#   missing  没配 Cookie   -> 去设置页粘贴 Cookie
#   blocked  IP 被风控     -> 配代理或换网络（换 Cookie 没用！）
#
# ★ 踩过的坑：曾经用 `aweme/v1/web/user/profile/self` 的 status_code=8
#   当作「登录态作废」的判据 —— **这个判据是错的**，已被对照实验推翻：
#   无 Cookie / 伪造 sessionid / 随机垃圾串 打这个接口，返回值**完全相同**
#   都是 status_code=8。说明 8 只表示「这个接口需要签名参数」，与登录态无关。
#   后来用真实 Chromium 验证，Cookie 登录态完全有效，
#   真正卡住的是 IP 被风控（滑块验证码）。
#
# 所以这里**不猜测 Cookie 是否过期**，只如实区分「没配 Cookie」和「被风控」。
# 后者靠页面特征判定（验证码中间页 / JS 挑战页），不靠接口返回码。
COOKIE_MISSING_HINT = (
    "未配置抖音 Cookie，无法解析。\n\n"
    "实测确认：未配置 Cookie 时，抖音分享页返回的是一段 JS 虚拟机挑战脚本"
    "（`_$jsvmprt`），里面没有任何作品数据，yt-dlp 也会报"
    "\"Fresh cookies (not necessarily logged in) are needed\"（这句措辞有误导，"
    "并不是\"放久了的 Cookie\"问题）。\n\n"
    "解决办法：在「⚙️ 设置」页的「抖音 Cookie 文本」框粘贴浏览器里的抖音 Cookie，"
    "保存后立即生效（F12 → Network → 任意 douyin.com 请求 → "
    "Request Headers → 复制整段 Cookie）。\n\n"
    "注意：抖音与B站 Cookie 必须分开填，混填会因域名不匹配全部失效。"
)

COOKIE_BLOCKED_HINT = (
    "请求被抖音风控拦截（IP 层面），**不是 Cookie 的问题**。\n\n"
    "实测现象：用真实 Chromium 加载已登录的 Cookie 打开作品页，"
    "停在「验证码中间页」，要求拖动滑块完成验证。\n\n"
    "关键点：这种情况**重新获取 Cookie 也解决不了** —— "
    "拦在前面的是 IP 风险识别，与登录态无关。\n\n"
    "解决办法（按推荐顺序）：\n"
    "1. 在「⚙️ 设置」页的**代理**栏填一个可用代理（最直接）\n"
    "2. 换网络环境（如手机热点）\n"
    "3. 关掉可能劫持流量的代理软件后重试\n\n"
    "抖音对云服务器 IP 的风控尤其严格，家用宽带一般不触发。"
)

# 兼容旧引用
DOUYIN_COOKIE_HINT = COOKIE_MISSING_HINT

# 抖音风控拦截的页面特征
_DOUYIN_BLOCK_MARKERS = ("验证码中间页", "请完成下列验证后继续",
                         "captcha", "_$jsvmprt", "__ac_signature")


def cookie_header() -> str:
    """把配置里的 cookie_text 拼成请求头用的 Cookie 串

    Cookie 里常混有中文（如 `SEARCH_RESULT_LIST_TYPE={"keyword":"搜索词"}`），
    而 HTTP 头只能按 latin-1 编码，直接发送会抛
    `'latin-1' codec can't encode characters in position N`。
    这里对名/值统一做 ASCII 化处理，与落盘逻辑保持一致。
    """
    s = load_settings()
    text = (s.get("cookie_text") or "").strip()
    if not text:
        return ""
    # 去掉我们为落盘而加的 `# domain=xxx` 提示行
    text = re.sub(r"^#\s*domain\s*=\s*\S+[ \t]*\n?", "", text, flags=re.I | re.M)
    parts = []
    for chunk in re.split(r"[;\n]", text):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        k, _, v = chunk.partition("=")
        k = _sanitize_cookie_component(k.strip())
        if not k:
            continue
        parts.append(f"{k}={_sanitize_cookie_component(v.strip())}")
    return "; ".join(parts)


def cookie_state() -> str:
    """探测抖音请求的受阻原因：ok / missing / blocked / unknown

    只区分「没配 Cookie」与「被风控」，**不猜测 Cookie 是否过期**——
    公开接口在缺签名参数时一律返回 status_code=8，无论 Cookie 是否有效
    （已用对照实验验证），拿它判断登录态必然误报。
    """
    ck = cookie_header()
    if not ck:
        return "missing"
    try:
        import requests

        r = requests.get(
            "https://www.iesdouyin.com/share/video/1/",
            headers={"User-Agent": DOUYIN_UA, "Cookie": ck,
                     "Referer": "https://www.iesdouyin.com/"},
            timeout=(8, 20),
        )
        html = r.text or ""
    except Exception:
        return "unknown"
    low = html.lower()
    if any(m in low or m in html for m in _DOUYIN_BLOCK_MARKERS):
        return "blocked"
    return "ok"


def cookie_hint() -> str:
    """按实际受阻原因返回提示

    两种情况解法相反：missing 要去填 Cookie，blocked 要换 IP 或加代理。
    弄反会让用户白折腾 —— 之前就发生过「明明配了 Cookie 却被告知没配」。
    """
    return COOKIE_BLOCKED_HINT if cookie_state() == "blocked" else COOKIE_MISSING_HINT


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
