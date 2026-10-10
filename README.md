# 视频转笔记 Video2Note（抖音 / B站 → 结构化笔记）

粘贴抖音 / B站链接或直接上传音视频，自动完成：**下载完整视频 → 语音转写 → AI 去口语化 / 纠错 / 排版**，输出带时间轴的转写稿与可直接使用的 Markdown 笔记。

```
URL / 文件 → 解析下载(视频轨+音频轨) → ffmpeg(合并 mp4 / 抽 16k 音频 / 静音点切片)
    → 在线 ASR(带时间戳) → LLM(map-reduce 整理) → 视频预览 + 笔记 + 导出 md/srt
```

## 快速开始

```bash
# 0. 确认解释器名。容器/多数 Linux 上只有 python3，没有 python，
#    写 python 会直接 command not found。下文统一用 python3。
command -v python3 || command -v python

# 1. 安装依赖
python3 -m python3 -m pip install -r requirements.txt

# 2. 装浏览器内核（抖音解析需要，只跑 B站可跳过）
python3 -m playwright install --with-deps chromium

# 3. 启动服务
python3 run.py --port 8765         # 打开 http://127.0.0.1:8765

# 4. 在页面右上角「设置」中填入 API Token，回到「转写工作台」粘贴链接即可
```

> 若用虚拟环境（`.venv`），把上面的 `python3` 换成 `.venv/bin/python`。
> **关键是所有命令都得用同一个解释器**——用另一个 python 装内核，
> 服务照样报 `Executable doesn't exist`。
>
> 不确定跑服务的是哪个解释器？先查出来再装：
>
> ```bash
> ps -eo pid,args | grep '[r]un\.py'   # 第一个字段就是解释器绝对路径
> ```

> 对外提供服务时加 `--host 0.0.0.0`；在反代后面再加 `--root-path /xxx`。

## 前置条件

### 先确认项目路径

部署路径**不固定**，取决于怎么部署的：

| 部署方式 | 典型路径 |
|---|---|
| 本仓库的 Dockerfile | `/app` |
| 托管平台 / 工作区挂载 | `/workspace` |
| 手动 systemd 部署（`scripts/deploy_debian.sh`） | `/opt/video2note` |

所以别照抄路径，先查出来：

```bash
ps -eo pid,args | grep '[r]un\.py'          # 服务在哪跑
ls -d /app /workspace /opt/video2note 2>/dev/null
python3 -c "import core, os; print(os.path.dirname(core.__file__))" 2>/dev/null
```

下文所有命令都在**项目根目录**（含 `run.py` 和 `core/` 的那层）执行。

| 依赖 | 说明 |
|---|---|
| Python 3.10+ | 必需 |
| ffmpeg / ffprobe | 必需。缺失时执行 `python3 -m pip install imageio-ffmpeg` 兜底；也可在设置里填完整路径 |
| yt-dlp | 必需，平台改版频繁，遇到解析失败先升级 |
| API Token | ASR 用模力方舟 Token；LLM 用各自的 Key（留空自动复用 ASR 的） |

## 接入的服务

### 语音识别（默认）

模力方舟 / Gitee AI，OpenAI 兼容的 `/v1/audio/transcriptions`。

| 项 | 值 |
|---|---|
| 接口地址 | `https://api.moark.com/v1`（`https://ai.gitee.com/v1` 亦可） |
| 鉴权 | `Authorization: Bearer <Token>` |
| 默认模型 | `SenseVoiceSmall` |
| 返回体 | `{"text": "...", "language": "zh"}` |

实测 `response_format`（json / verbose_json / srt / vtt）**均被忽略，不返回时间戳**，因此项目按句长在分片时长内比例估算时间轴，保证字幕与回溯可用。

可选模型（实测取自 `/v1/models`）：

```
SenseVoiceSmall  whisper-large-v3-turbo  whisper-large-v3  whisper-large
whisper-base     Fun-ASR-Nano-2512       GLM-ASR           TeleASR-MultiDialect
```

> SenseVoice 会在文本里插入事件/情感标记（🎼背景音乐、👏掌声、😊笑声、😭哭声等），
> `core/asr.py` 的 `clean_text()` 已统一剔除，避免污染后续整理。

### 大模型整理（默认）

支持两种协议，设置页切换即可：

| 协议 | 请求 | 鉴权 | 适用场景 |
|---|---|---|---|
| `anthropic`（默认） | `POST {base}/messages` | header `x-api-key` | api.pateway.ai 等 Anthropic 风格网关 |
| `openai` | `POST {base}/chat/completions` | header `Authorization: Bearer` | 模力方舟 / DeepSeek 官方等 OpenAI 兼容服务 |

Anthropic 协议要点（已实现）：

- `system` 作为**顶层字段**，不放在 messages 里
- `max_tokens` 必填，由 `llm_max_tokens` 控制，默认 8192
- 响应体 `content` 是**块数组**，需只取 `type == "text"` 的块（`thinking` / `tool_use` 已自动忽略）

## 配置方式（三选一，优先级从低到高）

```bash
# 方式一：环境变量（推荐用于服务器 / CI，容器平台 Secrets 同样适用）
export V2N_ASR_API_KEY=你的Token          # 别名 MOARK_API_TOKEN 亦可
export V2N_ASR_BASE_URL=https://api.moark.com/v1
export V2N_ASR_MODEL=SenseVoiceSmall

export V2N_LLM_PROTOCOL=anthropic         # anthropic | openai
export V2N_LLM_BASE_URL=https://api.pateway.ai/v1
export V2N_LLM_API_KEY=你的Key            # 别名 PATEWAY_API_KEY 亦可
export V2N_LLM_MODEL=deepseek-v4.1-flash
export V2N_LLM_MAX_TOKENS=8192

# 方式二：项目根目录 .env（见 .env.example）
cp .env.example .env

# 方式三：页面「⚙️ 设置」页签填写，落到 data/settings.json
```

## 功能

- 双入口：**粘贴链接**（支持整段分享文案，自动抠出 URL）/ **上传本地音视频**
- **下载完整视频**（最高 1080P）并在页面内直接播放
- 长音频自动按**静音点**切片并**并发**转写（默认 180 秒一片），避免句子被硬切；
  各片完成即增量推送，文字稿**边转边出**，不用等全部跑完
- ASR 不返回时间戳时按句长比例估算时间轴，保证字幕可用
- LLM 三种风格：结构化笔记 / 公众号文章 / 仅清洗润色；长文本走 **map-reduce**
  （先抽专有名词表 → 分块并行清洗 → 合并结构化）
- **跨段上下文不丢**：专有名词表从全文均匀抽 5 段（首段贴头、末段贴尾），
  正中间才出现的术语也能抽到；分块清洗按批推进，批内并行、批间把上一批的
  清洗结果尾部带进下一批，解决「他刚才提到的那个方案」这类指代断裂
- 转写完成后可**多次重跑 AI 整理、随时换风格**，不必重新转写
- **字数实时可见**：文字稿面板标题行显示当前字数（转写中随增量上涨），
  AI 文稿标题行显示该风格成稿字数并附原文字数对照；换风格时字数跟着切换，
  不会串到别的风格上。字数按不计空格口径统计，与 Word 字符数一致
- 转写失败后重跑会**复用已下载的媒体文件**，不再重复下载
- 历史任务里记录的模型若已下线，会自动回退到当前配置里的默认模型
- 历史记录回读：视频、稿件、导出随时找回
- 导出 Markdown / TXT / SRT 字幕 / 转写原文 / JSON（后端接口保留，界面入口已按精简需求隐藏）
- 内置磁盘维护：一键清理 N 天前的媒体文件

## 界面结构

三栏布局：左侧历史记录 / 中间主工作区 / 右侧处理日志。

| 区域 | 内容 |
|---|---|
| 输入区 | 链接输入框（右侧「开始转写」按钮）+ 文件上传 + 成稿风格按钮组 |
| 工作区 | 左侧视频预览、右侧转写文字稿（播放时自动高亮滚动） |
| 文稿区 | AI 整理稿 + 风格切换按钮（多风格缓存，已生成的风格不再重复调用模型） |
| 左栏 | 历史记录，点击直接回显视频 / 文字稿 / AI 整理稿 |
| 右栏 | 处理日志，自动滚到底 |

> 设置面板分三组：**平台登录态**（抖音 Cookie、B站 Cookie、抖音解析请求模板）、
> 模型与生成、性能。接口地址、协议、模型名、Token、代理、ffmpeg 路径属于部署期参数，
> 不在面板里展示（后端接口仍支持，可改 `data/secrets.json` 或用 `V2N_*` 环境变量）。

## 平台注意事项

**B站**：支持良好。1080P 以上清晰度需要登录，在设置里填 Cookie。

**抖音**：**优先走解析接口（dlpanda）**，这是目前唯一在云服务器 IP 上实测能拿到视频的路径。

下载优先级：

```
抖音：dlpanda 接口 → yt-dlp
B站：yt-dlp
```

> 原第三级 pyktok 兜底已移除 —— PyPI 上的 `pyktok` 实为海外版 TikTokApi
> （硬编码 tiktok.com），对抖音无效，属死代码。

### 为什么抖音要改用接口

抖音对**服务器 IP** 的风控比家用宽带严得多，实测四层都被挡：

| 尝试 | 结果 |
|---|---|
| yt-dlp 直接抓分享页 | 返回 `_$jsvmprt` JS 虚拟机挑战脚本，报 `Fresh cookies are needed` |
| curl_cffi 伪装 TLS 指纹（7 种） | 全部返回一模一样的 72914 字节挑战页 |
| 真实浏览器 + 有效登录 Cookie | 停在「验证码中间页」，业务代码根本没加载 |
| 补 `uifid` 参数 | 报错从 `Uifid Not Found` 变成 `Signature Not Found` |
| 伪造 `a_bogus` 签名 | 被判 `Sign Invalid` |

关键在最后两步：`uifid` 补上后错误精确变成「只差签名」，而服务端**确实在校验签名的真实性**。
抖音的 `a_bogus` 必须由它自己的 JS 代码产出，而风控拦在业务代码执行之前 —— 拿不到代码就产不出签名。

所以本地硬啃签名这条路走不通，正确做法是**把解析交给已经解决该问题的一方**，
我们只负责拿它返回的 CDN 直链并下载。

### dlpanda 通道的工作方式

协议为实测所得（`core/dlpanda.py` 里有完整说明）：

1. 抖音走**独立路由** `/zh-CN/douyin`，不是首页
2. POST multipart：`_token`（CSRF）+ `url` + `t0ken`（固定值）
3. 响应是 **HTML**（不是 JSON），地址在 `data-download-url` 属性里
4. 结果状态看 `data-state`：`success` / `unsupported` / `private` / `rate_limited` / …

架构上有个**必须遵守的分工**：

- **解析**必须走真实浏览器。实测纯 HTTP（curl_cffi 带完整浏览器指纹 + 含 `cf_clearance`
  的全套 Cookie）依然被 Cloudflare 403 —— CF 的 clearance 与浏览器指纹绑定，脚本伪造不了。
- **下载**不需要浏览器。返回的是抖音官方 CDN（`*.zjcdn.com`），
  带 `access-control-allow-origin: *` 且支持 Range（实测返回 206），普通 HTTP 直接下。

> 不要把几十 MB 的视频塞进浏览器上下文下载 —— 实测这样会因默认 30 秒超时直接失败。
> 程序只让浏览器负责解析那一步（秒级），下载全程轻量 HTTP，并支持断点续传。

依赖：

```bash
# 必需：解析（浏览器过 CF）+ 下载（TLS 指纹）
python3 -m pip install curl_cffi playwright
python3 -m playwright install --with-deps chromium

# 国内网络慢，用镜像
PLAYWRIGHT_DOWNLOAD_HOST=https://registry.npmmirror.com/-/binary/playwright \
  python3 -m playwright install chromium
```

健康检查接口会分别报告两项，服务器上排查很方便：

```bash
curl -s http://127.0.0.1:8765/api/health
# {"ffmpeg":true,"yt_dlp":true,"douyin_api":true,"playwright":true}
#                  ↑ 抖音通道    ↑ 浏览器内核
```

`playwright` 这一项会**真的启动一次 Chromium** 来判定，不是只看 `import`——
因为「装了库没装内核」时 `import playwright` 依然成功，
但一跑任务就会报 `BrowserType.launch: Executable doesn't exist at .../ms-playwright/...`。
判定为不可用时，`playwright_msg` 字段里会直接给出修复命令。

缺任何一项都会**自动降级**到 yt-dlp，不影响 B站。

> 想对比排查接口通道，可用 `downloader.download_media(..., prefer_api=False)` 强制跳过。

### 自定义解析请求模板（可选）

设置页有一项**「抖音解析请求模板」**。绝大多数情况**留空即可**，它只在
站点改了字段名时你需要介入。

填法：浏览器打开解析接口页 → F12 → Network → 随便点一次解析请求 →
右键「复制」→「复制为 cURL」，整段粘进输入框，点「校验这段 curl」确认。

程序会自动做三件事：

| 部分 | 处理方式 |
|---|---|
| `_token` / `t0ken` / `csrf_token` | **替换为页面实时值**，不信任模板里的旧值 |
| `url` | 替换为你在首页提交的链接 |
| Cookie | **注入浏览器会话**（不是脚本裸发） |
| 其余自定义头 | 原样保留 |

保存时会先校验格式，粘错立刻报错，不会拖到下载时才失败。

> **为什么要保留 Cookie 而不是直接裸发请求？**
> 实测 `curl_cffi` 带完整浏览器指纹 + 含 `cf_clearance` 的全套 Cookie，
> 依然被 Cloudflare 403 —— clearance 与浏览器指纹绑定，脚本伪造不了。
> 所以这些头是用来**驱动真实浏览器**的，不是给 Python 用的。

模板本身含 Cookie，等价于会话凭证，因此与 Token 一起存在
`data/secrets.json`（权限 600），接口只回「配没配」不回内容。

### 常见报错

**`'latin-1' codec can't encode characters in position N: ordinal not in range(256)`**

Cookie 里混进了非 ASCII 字符（最常见是中文搜索词，比如
`SEARCH_RESULT_LIST_TYPE={"keyword":"中文"}`），而 HTTP 请求头只能按 latin-1 编码，
yt-dlp 在**发出请求之前**就会抛这个错。

程序已自动处理：Cookie 名与值会统一压成纯 ASCII（含中文时做百分号编码，
且不会对已有的 `%XX` 二次编码）。若仍看到此报错，说明 `data/cookies.txt` 是旧版本生成的，
删掉它后在设置页重新保存一次 Cookie 即可重建。

**`请求被抖音风控拦截` / `Signature Not Found`**

这是 **IP 层面的风控**，与登录态无关，换 Cookie 解决不了。按顺序试：

1. 在「⚙️ 设置」页的**代理**栏填一个可用代理（最直接）
2. 换网络环境（例如手机热点）
3. 确认解析接口可用：健康检查里 `douyin_api` 与 `playwright` 都应为 `true`

### Cookie 配置（抖音基本必填）

设置里有三个相关输入框，**任选其一即可**，程序按下面的优先级取用：

| 优先级 | 配置项 | 这个框该填什么 |
| --- | --- | --- |
| 1 | **Cookie 文本**（推荐） | 浏览器 F12 → Network → 点任意请求 → Request Headers → 复制整段 `Cookie` 值粘进来。形如 `a=1; b=2` 的请求头串会自动转成 `data/cookies.txt`；也接受 Netscape `cookies.txt` 全文 |
| 2 | **Cookie 文件路径** | 本机已有的 Netscape 格式 `cookies.txt` 绝对路径，例如 `D:\cookies.txt`。一般用不到 |
| 3 | **从浏览器读取** | **只填浏览器名**：chrome / edge / firefox / brave / chromium / opera / safari / vivaldi / whale。程序调`browser-cookie` 库直接读本机该浏览器的 Cookie（需先在浏览器登录）|

> ⚠️「从浏览器读取」框只接受浏览器名。往里粘 Cookie 内容会被校验拦下并提示，
> 这是为了避免把两种 Cookie 来源搞混。

抖音与 B站的 Cookie **必须分开填**，两者域名不同，混在一起会因作用域不匹配而双双失效：

| 平台 | 页面输入框 | 配置项 | 落盘文件 | 固定域名 |
|---|---|---|---|---|
| 抖音 | 抖音 Cookie 文本 | `cookie_text` | `data/cookies.txt` | `.douyin.com` |
| B站 | B站 Cookie 文本 | `cookie_text_bili` | `data/cookies_bili.txt` | `.bilibili.com` |

下载时会按链接自动选对应通道；B站若未单独配置，会回退读抖音通道以兼容早期配置。

- 清空某个平台的 Cookie 框并保存，对应的 `cookies*.txt` 会一并删除，不会出现
  「页面回显为空、下载却仍在用旧 Cookie」的情况。
- B站 Cookie 至少要含登录会话凭证（登录后 F12 里能看到的三项），否则大概率返回 412。
- 仍支持首行写 `# domain=.xxx.com` 手动指定作用域，覆盖默认域名。

## 视频预览为什么只有声音

浏览器（Chrome / Edge / Firefox）普遍放不了 HEVC（hev1 / hvc1），播出来就是黑屏加声音。
程序做了两层处理：

1. **选流时避开 HEVC**：优先挑 H.264 的流，B站默认给的 hevc 高清流会被跳过
2. **下载后兜底转码**：若最终文件仍是 HEVC，自动转码成 H.264（`libx264 veryfast`），
   断点续跑和本地上传的文件也会顺带修一次

1080p 转码大约需要视频时长的 1/6（17 分钟素材实测 164 秒），期间进度停在「转换」阶段。

## 目录结构

```
video2note/
├── run.py                  # 启动入口：FastAPI + static/ 自绘界面，默认 127.0.0.1:8765
├── requirements.txt        # ⭐ 依赖列表
├── Dockerfile              # 通用部署镜像（监听 0.0.0.0:8765）
├── .dockerignore           # 排除 data/ 媒体/缓存，减小构建上下文
├── README.md
├── .env.example            # 环境变量样例
├── .gitignore
├── .gitattributes          # 强制仓库内一律 LF
├── core/                   # 业务内核
│   ├── config.py           配置：默认值 <- 环境变量 V2N_* <- settings.json
│   ├── downloader.py       下载调度：抖音走 dlpanda 接口，其余走 yt-dlp
│   ├── dlpanda.py          抖音解析通道：浏览器过 CF + CDN 直链下载
│   ├── curlparse.py        解析「粘贴 curl」得到自定义请求模板
│   ├── audio.py            ffmpeg 合并/抽音频/转码、静音点切片、时长探测
│   ├── asr.py              在线语音识别（/v1/audio/transcriptions）+ 事件标记清洗
│   ├── llm.py              LLM 整理：anthropic/openai 双协议、map-reduce、术语抽样、块间上文摘要、按风格缓存
│   ├── store.py            任务存储（内存索引 + data/tasks/*.json）
│   ├── exporters.py        导出 md / txt / srt / raw / json（入口已隐藏）
│   ├── pipeline.py         流水线编排（线程池异步版）
│   └── main.py             FastAPI 接口，含 SSE 实时进度与日志
├── static/                 前端：index.html / app.js / style.css
├── verify_*.py             回归脚本（改完代码跑一遍）：
│   verify_dlpanda_prio.py    两级下载调度：接口优先、失败降级、错误文案
│   verify_dy_state.py        Cookie 状态判定与提示分流
│   verify_mux.py              ffmpeg 合并（-c copy 优先，必要时转 AAC）
│   verify_llm_context.py      术语抽样与块间上文摘要（mock LLM，不花钱）
│   verify_llm_real.py         同上，真实调用 LLM 验证成稿质量
│   verify_realtime.py         转写结果是否增量推送
│   verify_wordcount.py        文字稿/成稿字数展示与多风格不串号
│   verify_settings_curl.py    设置面板分组顺序与 curl 输入框可用性
│   verify_cloud_settings.py   设置接口失败时是否显式报错而非空白
│   verify_cloud_curl.py       云端排障四道防线：缓存头/版本自述/接口失败/自检条
│   verify_history_note.py     历史记录回显成稿
│   verify_ui.py               端到端 UI 冒烟
├── scripts/
│   ├── deploy_debian.sh              # Debian/Ubuntu 一键部署
│   ├── verify_dockerfile.py          # Dockerfile 静态校验（无需 Docker 环境）
│   └── verify_dockerfile.selftest.py # 校验器自测（故意改坏确认能拦住）
└── data/                   运行时生成：tasks / media / export / settings.json / secrets.json
```

> 校验类脚本用托管虚拟环境的解释器跑（依赖装在
> `C:\Users\24838\.workbuddy\binaries\python\envs\default`）。

## 部署

### 方式一：直接跑（最简单）

```bash
python3 -m pip install -r requirements.txt
python3 -m playwright install --with-deps chromium   # 抖音解析需要，只跑 B站可跳过

python3 run.py --host 0.0.0.0 --port 8765
```

无 systemd 的容器环境用 `nohup` 常驻：

```bash
nohup python3 -u run.py --host 0.0.0.0 --port 8765 > server.log 2>&1 &
```

### 方式二：Docker

```bash
docker build -t video2note .
docker run -d --name v2n -p 8765:8765 -v /srv/video2note:/data video2note
```

- 数据目录：`V2N_DATA_DIR`（镜像内默认 `/data`），挂卷后跨重建保留任务与媒体
- 端口：`SERVER_HOST` / `SERVER_PORT`（默认 `0.0.0.0` / `8765`）
- 不解析抖音可瘦身：`docker build --build-arg INSTALL_PLAYWRIGHT=0 .`

Debian / Ubuntu 主机也可用 `bash scripts/deploy_debian.sh`。

### 密钥配置

**推荐用环境变量**（页面上的密钥框会自动变为只读，不回显内容）：

| 配置项 | 环境变量名 | 说明 |
|---|---|---|
| ASR 密钥 | `V2N_ASR_API_KEY`（别名 `MOARK_API_TOKEN`） | 模力方舟语音识别 |
| LLM 密钥 | `V2N_LLM_API_KEY`（留空则复用 ASR） | 大模型整理 |
| 抖音 Cookie | `V2N_COOKIE_TEXT` | 抖音解析需要 |
| B站 Cookie | `V2N_COOKIE_TEXT_BILI` | 含登录态凭证，缓解 412 |

优先级：**内置默认值 < `data/settings.json` < `data/secrets.json` < 环境变量**（环境变量最高）。

Token 和 Cookie 单独存在 `data/secrets.json`（写入时自动 `chmod 600`，Windows 上忽略），
不在 `settings.json` 里，接口也只回「已配置 / 未配置」。
用 `V2N_SECRETS_FILE` 可把它放到数据目录之外。

### nginx 反代示例

```nginx
location /v2n/ {
    proxy_pass http://127.0.0.1:8765/;
    proxy_http_version 1.1;
    proxy_set_header Connection "";
    proxy_buffering off;          # SSE 必须关缓冲，否则进度不动
}
```

此时启动加 `--root-path /v2n`。

## 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/tasks` | 创建任务 `{url, style}` |
| POST | `/api/tasks/upload-raw` | 上传本地文件（裸流直传） |
| GET | `/api/tasks` | 任务列表 |
| GET | `/api/tasks/{id}` | 任务详情 |
| GET | `/api/tasks/{id}/events` | SSE 实时进度与日志 |
| POST | `/api/tasks/{id}/polish` | 触发 AI 整理 |
| POST | `/api/tasks/{id}/retry` | 从失败处重试 |
| GET | `/api/media/{id}/video.mp4` | 视频流（支持 Range） |
| GET | `/api/tasks/{id}/export?fmt=md\|txt\|srt\|raw\|json` | 导出 |
| GET | `/api/health` | ffmpeg / yt-dlp / 抖音通道 / 浏览器内核就绪检查 |

## 成本参考（1 小时视频）

下载 1-3 分钟 → 转写 1-5 分钟 → 整理 30-60 秒，合计约 3-10 分钟；模型调用费用约 ¥1-3。

## 常见问题

| 问题 | 处理 |
|---|---|
| `Address already in use` | 8765 被占用：换 `--port`，或结束占用进程 |
| `ModuleNotFoundError: No module named 'core'` | 在项目根目录执行 `python3 run.py`，不要直接 `python3 core/...` |
| 页面无法访问 | 确认监听 `0.0.0.0`（用 `--host 0.0.0.0`） |
| 视频很慢/内存不足 | 减小 `chunk_seconds`、调低并发，或提高机器资源规格 |
| 中文乱码 | 代码统一 UTF-8 读写，导出时用 Chrome/Edge 打开 .md |
| 页面能开但样式/接口 404 | 反代前缀问题：启动时加 `--root-path /你的前缀` |
| 重启后历史记录没了 | 数据目录被清理；用 `V2N_DATA_DIR` 指定持久化路径，并挂卷 |
| 抖音一直解析失败 | IP 风控（与 Cookie 无关）：在设置页填代理，或换网络环境 |
| **抖音解析报 `Executable doesn't exist`** | `python3 -m pip install playwright` 只装 Python 包，浏览器内核是另一套。执行 `python3 -m playwright install chromium`（容器里可能还需 `--with-deps`）。健康检查的 `playwright` 字段会真启动一次 Chromium，此项为 false 时 `playwright_msg` 直接给出该命令 |
| 抖音报验证码 / 滑块拦截 | 服务器 IP 被抖音风控，**换 Cookie 无效**。按序尝试：设置页填代理 → 换网络（手机热点）→ 关闭本机代理软件 |
| **设置面板是空的 / 少了某几项** | 多半是「前端新、后端旧」：`git pull` 只更新磁盘文件，运行中的 Python 进程不会重新加载，**必须重启服务**。先看设置面板顶部的自检条，它会直接说明是哪种情况；重启方式见下方「按部署方式重启」 |
| **curl 输入框不显示** | 同上。自检条显示「后端未返回 resolver_curl」即属此类，重启后应变为「✅ 已就位」 |
| 提示「无法连接后端 /api/settings」 | 若设置面板里其他字段能正常显示，说明后端其实是通的、只是没渲染出内容；空面板才可能是服务没起或反代拦了 `/api/*` |
| **改了代码但页面没变化** | 先看页面右上角的版本号（形如 `v63f7a8e · 20261011q`）。它对不上就是浏览器拿的旧页面——首页已设`no-store`，正常情况下刷新即生效，若仍不变请用 Ctrl+F5 |
| 提示「无法连接后端 /api/settings」 | 服务本身没起来，或反代把 `/api/*` 拦了。先看面板里其他字段是否正常显示——若正常，说明后端是通的、只是没渲染出内容 |

### 按部署方式重启

更新代码后**必须重启**，否则新代码不生效。原因是 `git pull` 只改磁盘上的文件，
而 Python 模块是在进程启动时载入的——不重启，跑的还是旧代码。

先确认代码到位：

```bash
cd <项目目录> && git pull && git log --oneline -1
```

然后按你的部署方式选一条：

**systemd（有 systemctl）：**

```bash
sudo systemctl restart video2note
```

**容器 / 无 systemd（`nohup` 常驻，最常见）：**

```bash
pkill -f "run.py"
nohup python3 -u run.py --host 0.0.0.0 --port 8765 > server.log 2>&1 &
```

**Docker：**

```bash
docker restart v2n
```

> `sudo: command not found` 说明当前不是 root、且环境里没装 sudo。
> 先用 `whoami` 确认身份：若已是 root，去掉 `sudo` 直接执行即可。
> 容器里通常也没有 systemd，用上面的 `nohup` 方式。
> 不确定服务是怎么起的，先跑 `bash diag_restart.sh` 看看进程与监听情况。

## 合规提示

- 仅用于个人学习与研究，请勿分发下载的原始音视频
- Cookie 与 API Key 属敏感信息，只保存在 `data/secrets.json` 或环境变量，不要提交到版本库
