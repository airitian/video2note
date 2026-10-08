# 视频转笔记 Video2Note（抖音 / B站 → 结构化笔记）

粘贴抖音 / B站链接或直接上传音视频，自动完成：**下载完整视频 → 语音转写 → AI 去口语化 / 纠错 / 排版**，输出带时间轴的转写稿与可直接使用的 Markdown 笔记。

```
URL / 文件 → yt-dlp(视频轨+音频轨) → ffmpeg(合并 mp4 / 抽 16k 音频 / 静音点切片)
    → 在线 ASR(带时间戳) → LLM(map-reduce 整理) → 视频预览 + 笔记 + 导出 md/srt
```

## 两个版本怎么区分

仓库里有**两个前端**，业务内核（`core/`）完全共用，只换 UI 层：

| |🖥️ 本地版（FastAPI + 自绘前端） | ☁️ Gradio 版（ModelScope 创空间） |
|---|---|---|
| 入口文件 | **`run.py`** | **`app.py`** |
| 启动命令 | `python run.py --port 8765` | `python app.py`（默认 `0.0.0.0:7860`） |
| 技术栈 | FastAPI + 原生 HTML/CSS/JS（`static/`） | Gradio 组件库 |
| 界面外观 | 自绘，深色卡片式布局 | Gradio 默认外观 |
| 端口 | 8765（可改） | **7860 固定**（ModelScope 硬性要求） |
| 默认地址 | http://127.0.0.1:8765 | http://127.0.0.1:7860 |
| 适合场景 | 本机日常使用，界面更清爽 | 部署到创空间 / 分享给他人 |
| 前端文件 | `static/index.html`、`app.js`、`style.css` | 无独立前端文件，全部写在 `app.py` |

**判别口诀**：看到 `run.py` → 本地版；看到 `app.py` → Gradio 版。
两者可同时运行，互不冲突（端口不同），共用同一个 `data/` 目录与配置。

> 想只用其中一个？删掉另一个入口文件即可，`core/` 不受影响。

## 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2A. 本地版（自绘界面，推荐日常使用）
python run.py --port 8765        # 打开 http://127.0.0.1:8765

# 2B. Gradio 版（部署到 ModelScope 创空间时用这个）
python app.py                    # 打开 http://127.0.0.1:7860

# 3. 在页面右上角「设置」中填入 API Token，回到「转写工作台」粘贴链接即可
```

> 两个版本功能一致（转写、换风格重写、历史回显、多风格缓存都支持）。
> ModelScope 创空间只能识别 `app.py`，因此**部署必须用 Gradio 版**。

## 前置条件

| 依赖 | 说明 |
|---|---|
| Python 3.10+ | 必需 |
| ffmpeg / ffprobe | 必需。缺失时执行 `pip install imageio-ffmpeg` 兜底；也可在设置里填完整路径 |
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
# 方式一：环境变量（推荐用于 ModelScope Secrets）
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
- **两个前端共用同一套内核**：🖥️ 本地版 `run.py`（FastAPI + 自绘界面，8765）与 ☁️ Gradio 版 `app.py`（创空间，7860）
- **下载完整视频**（最高 1080P）并在页面内直接播放
- 长音频自动按**静音点**切片并并发转写（默认 10 分钟一片），避免句子被硬切
- ASR 不返回时间戳时按句长比例估算时间轴，保证字幕可用
- LLM 三种风格：结构化笔记 / 公众号文章 / 仅清洗润色；长文本走 **map-reduce**
  （先抽专有名词表 → 分块并行清洗 → 合并结构化）
- 转写完成后可**多次重跑 AI 整理、随时换风格**，不必重新转写
- 转写失败后重跑会**复用已下载的媒体文件**，不再重复下载
- 历史任务里记录的模型若已下线，会自动回退到当前配置里的默认模型
- 历史记录回读：视频、稿件、导出随时找回
- 导出 Markdown / TXT / SRT 字幕 / 转写原文 / JSON（后端接口保留，界面入口已按精简需求隐藏）
- 内置磁盘维护：一键清理 N 天前的媒体文件

## 界面结构

两个版本的主界面结构一致（工作台 + 历史记录 + 设置）。

☁️ Gradio 版页签：

| 页签 | 内容 |
|---|---|
| 🎧 转写工作台 | 输入来源（链接/上传）、成稿风格；右侧视频预览与状态日志；下方转写文字稿 + AI 整理稿 |
| 🗂 历史记录 | 任务表格（选中行 → 载入）、删除、刷新 |
| ⚙️ 设置 | 环境自检、密钥与模型、切片与并发、Cookie、磁盘维护 |

🖥️ 本地版（自绘界面）：

| 区域 | 内容 |
|---|---|
| 输入区 | 链接拖拽/粘贴 + 文件上传 + 成稿风格按钮组 |
| 工作区 | 左侧视频预览、右侧转写文字稿（播放时自动高亮滚动） |
| 文稿区 | AI 整理稿 + 风格切换按钮（多风格缓存，已生成的风格不再重复调用模型） |
| 右侧栏 | 历史记录（点击直接回显全部内容）+ 日志 |

> 两个版本都已按精简需求隐藏参数设置与导出下载入口（后端接口仍保留）。

## 平台注意事项

**B站**：支持良好。1080P 以上清晰度需要登录，在设置里填 Cookie。

**抖音**：默认仍然走 yt-dlp（带 Cookie 时实测可正常解析与下载），**失败才回退 pyktok**。

下载优先级：

```
yt-dlp  →（仅当链接是抖音且 yt-dlp 报错时）→ pyktok 兜底
```

只有 yt-dlp 抛错且平台判定为抖音时，才会启动 `core/douyin.py`：Playwright 起 Chromium 打开抖音首页
并注入 Cookie，由 pyktok 的 `generate_x_bogus()` 在页面上下文里算出 `a_bogus` / `X-Bogus` 签名，
再由本进程请求 `aweme/detail` 拿播放直链；其内部还会回退到移动端分享页抓取（`_scrape_mobile`）。
两者都失败时，错误信息会同时列出 yt-dlp 与 pyktok 的失败原因。

因此**正常使用不需要装 Playwright 内核**；只有 yt-dlp 抓不到抖音时才需要：

```bash
pip install pyktok
python -m playwright install chromium

# 国内网络慢，用镜像
PLAYWRIGHT_DOWNLOAD_HOST=https://registry.npmmirror.com/-/binary/playwright \
  python -m playwright install chromium
```

排查要点：

1. 确认 Cookie 里含 `s_v_web_id`、`ttwid`、`sessionid`
2. 确认内核已装：`python -c "import os,playwright; print(os.path.exists(playwright.sync_api.sync_playwright().start().chromium.executable_path))"`
3. 注意 `pyktok` 内部默认导航 `tiktok.com`（国内不可达），代码里已用 `starting_url` 指向抖音

### Cookie 配置（抖音基本必填）

设置里有三个相关输入框，**任选其一即可**，程序按下面的优先级取用：

| 优先级 | 配置项 | 这个框该填什么 |
| --- | --- | --- |
| 1 | **Cookie 文本**（推荐） | 浏览器 F12 → Network → 点任意请求 → Request Headers → 复制整段 `Cookie` 值粘进来。形如 `a=1; b=2` 的请求头串会自动转成 `data/cookies.txt`；也接受 Netscape `cookies.txt` 全文 |
| 2 | **Cookie 文件路径** | 本机已有的 Netscape 格式 `cookies.txt` 绝对路径，例如 `D:\cookies.txt`。一般用不到 |
| 3 | **从浏览器读取** | **只填浏览器名**：chrome / edge / firefox / brave / chromium / opera / safari / vivaldi / whale。程序调`browser-cookie` 库直接读本机该浏览器的 Cookie（需先在浏览器登录）|

> ⚠️「从浏览器读取」框只接受浏览器名。往里粘 Cookie 内容会被校验拦下并提示，
> 这是为了避免把两种 Cookie 来源搞混。

抖音与B站共用同一份 Cookie 配置，作用域按 `cookies.txt` 里的 domain 区分。`cookie_text` 还支持首行
写 `# domain=.douyin.com` 手动指定作用域，不写则从文本里推断，兜底 `.douyin.com`。

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
├── app.py                  # ☁️ 【Gradio 版入口】ModelScope 创空间用，固定 7860
├── run.py                  # 🖥️ 【本地版入口】FastAPI + static/ 自绘界面，默认 8765
├── requirements.txt        # ⭐ 依赖列表
├── README.md
├── .env.example            # 本地环境变量样例
├── .gitignore
├── core/                   # 业务内核（两个版本共用，与入口解耦）
│   ├── config.py           配置：默认值 <- 环境变量 V2N_* <- settings.json
│   ├── downloader.py       yt-dlp 优先取流下载，抖音失败回退 pyktok、文案中提取 URL
│   ├── douyin.py           抖音专用兜底：pyktok 生成 a_bogus 签名后请求 aweme/detail
│   ├── audio.py            ffmpeg 合并/抽音频/转码、静音点切片、时长探测
│   ├── asr.py              在线语音识别（/v1/audio/transcriptions）+ 事件标记清洗
│   ├── llm.py              LLM 整理：anthropic/openai 双协议、map-reduce、按风格缓存
│   ├── store.py            任务存储（内存索引 + data/tasks/*.json）
│   ├── exporters.py        导出 md / txt / srt / raw / json（两个版本已隐藏入口）
│   ├── pipeline.py         流水线编排，同时提供线程池版与同步回调版
│   └── main.py             FastAPI 接口（🖥️ 仅本地版使用，含 SSE 实时进度）
├── static/                 # 🖥️ 仅本地版使用的前端：index.html / app.js / style.css
└── data/                   运行时生成：tasks / media / export / settings.json
```

## 部署到 ModelScope 创空间

>创空间只认 `app.py`，因此**必须使用 ☁️ Gradio 版**（本地版 `run.py` 不参与部署）。

1. 创空间类型选 **Gradio**，SDK 随意，Python ≥ 3.10
2. 把本目录内容推到创空间仓库，根目录必须有 `app.py` 与 `requirements.txt`
3. 在「设置 → Secrets」里添加 `V2N_ASR_API_KEY`（可选 `V2N_LLM_API_KEY`）
4. 平台会自动安装依赖并在 **7860** 端口拉起服务

### 密钥托管：优先用环境变量，不要在页面上填

**推荐做法**：在创空间「设置 → Secrets」里配好环境变量，页面上的密钥框会自动变为**只读**，
不会回显密钥内容，也不用担心有人误改或误存。

配置优先级：**内置默认值 < `data/settings.json` < 环境变量**（环境变量最高）。

这条顺序很重要——即使 `settings.json` 里残留了旧密钥，也会被环境变量覆盖，
不会出现「换了 Secret 但线上还在用旧密钥」的情况。

| 配置项 | 环境变量名（推荐） | 说明 |
|---|---|---|
| ASR 密钥 | `V2N_ASR_API_KEY`（别名 `MOARK_API_TOKEN`） | 模力方舟语音识别 |
| LLM 密钥 | `V2N_LLM_API_KEY`（留空则复用 ASR） | 大模型整理 |
| 语音识别语言 | `V2N_ASR_LANGUAGE` | 默认 `zh` |
| 抖音 Cookie | `V2N_COOKIE_FILE` 或页面「Cookie 文本」 | 抖音解析需要 |

页面上的密钥框在检测到环境变量后会显示「已由环境变量 XXX 托管，页面不可修改」。

###跨平台说明（本地 Windows / 线上 Linux）

项目代码本身**不含Windows 专用代码**，`core/` 全目录扫描无 `winreg`、`os.startfile`、
`ctypes.windll`，ffmpeg 通过 `shutil.which()` 走 `PATH`，两套系统通用。
部署 workflow 里也加了平台兼容检查，检测到 Windows 专用 API 会直接让流水线失败。

两处需要注意的系统差异：

| 项 | Windows 本地 |创空间 Linux |
|---|---|---|
| 数据目录 | `项目/data/` | `/mnt/workspace/video2note`（自动识别，可持久化） |
| Playwright 内核 | 本机已装 | **未预装**，抖音兜底不可用 |

关于抖音：主链路是 **yt-dlp**（带 Cookie 时 B站与抖音都能解析），**pyktok 只是兜底**。
创空间未装 Chromium 时，兜底会给出明确提示并跳过，不影响yt-dlp 主链路。
若确实要在创空间启用抖音兜底，需在启动脚本里执行
`python -m playwright install --with-deps chromium`（2 核 8G 容器内存偏紧，Chromium 会占用较多资源）。

### 推送到 GitHub

```bash
git init
git add .
git commit -m "feat: 视频转笔记初版，双入口（本地版 run.py + Gradio 版 app.py）"
git branch -M main
git remote add origin https://github.com/<你的用户名>/video2note.git
git push -u origin main
```

`data/`（含 tasks / media / cookies.txt / settings.json）与 `*.log` 已在 `.gitignore` 中排除，
不会把密钥和 Cookie 提交上去。首次推送需要在 GitHub 上创建一个空仓库（不要勾选 README）。

### 手动同步到创空间

创空间仓库：`https://modelscope.cn/studios/viva25/video_txt.git`，
访问令牌用 [ModelScope 访问令牌](https://modelscope.cn/my/myaccesstoken)。

**方式一：命令行推送（推荐）**

在**创空间仓库目录**里直接覆盖文件（无需合并历史，最省事）：

```bash
# 1. 先拉取创空间仓库
git clone https://oauth2:<你的访问令牌>@modelscope.cn/studios/viva25/video_txt.git ms_studio
cd ms_studio

# 2. 把本地项目文件复制进来（排除运行数据与 Git 配置）
#    Windows PowerShell：
#    robocopy ..\video2note . /E /XD .git data .github
#    macOS / Linux：
#    rsync -av --exclude='.git' --exclude='data' --exclude='__pycache__' ../video2note/ ./

# 3. 提交并推送
git add -A
git commit -m "sync: 更新为 GitHub 版本" || echo "无变化"
git push
```

> 令牌直接写在 clone URL 里最省事，但会留在 shell 历史中。
> 更稳妥的做法是先 clone 不带令牌，再执行 `git remote set-url origin https://oauth2:<令牌>@...`，
> 推送完把 remote 改回不带令牌的地址。

**方式二：网页上传**

到创空间「代码 → 上传文件」，或直接把 GitHub 仓库里的文件拖进去覆盖。

> 关键点：创空间的默认分支是 **`master`**，而 GitHub 仓库用的是 `main`。
> 推送时无需合并历史，直接把文件覆盖后 `git add -A && git commit && git push` 即可。

推送后 ModelScope 会**自动重建**（实测约 3~8 分钟），不需要额外触发部署。
可在创空间「日志」页查看启动输出。

### 自动部署（GitHub Actions）

仓库里带了 `.github/workflows/deploy-modelscope.yml`：向 GitHub 的 `main` 分支推送后，
自动把代码同步到创空间并触发重建。也可以在 Actions 页手动点「Run workflow」。

**一次性配置**：GitHub 仓库 → Settings → Secrets and variables → Actions → New repository secret

| Secret | 值 |
|---|---|
| `MODELSCOPE_API_KEY` | ModelScope 访问令牌（[获取](https://modelscope.cn/my/myaccesstoken)） |

workflow 里的目标创空间在文件顶部的 `env`（`STUDIO_OWNER` / `STUDIO_NAME` / `MODELSCOPE_HOST`）改。

几个设计上的取舍：

- **推送前先校验**：缺 `app.py` / `requirements.txt` 直接中止；若仓库里出现被跟踪的
  `data/`、`.env`、`cookies.txt`、`*.log`，也会中止，防止密钥被推到创空间。
- **不做 merge，用 orphan 分支强推**：创空间是部署目标而非代码源，
  合并历史只会在文件冲突时卡死；以本地为准覆盖更可靠。
- **内容一致就跳过推送**：与创空间 `master` 对比无差异时直接结束，省掉一次无意义的重建。
- **令牌脱敏**：所有 git/curl 命令的输出都会把 `oauth2:xxx@` 替换成 `oauth2:***@`。

> 也可以用本地脚本手动同步（不依赖 Actions）：
> `export MODELSCOPE_TOKEN=你的令牌 && bash scripts/sync_to_studio.sh`
> 两条路都可用，互不干扰。

数据落地优先级：`V2N_DATA_DIR` → `/mnt/workspace/video2note`（创空间持久化目录）→ `项目/data`。
创空间重启会保留 `/mnt/workspace`，但迁移或重命名会丢失，重要产物请及时导出。

## 接口（FastAPI 版）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/tasks` | 创建任务 `{url, style}` |
| POST | `/api/tasks/upload` | 上传本地文件 |
| GET | `/api/tasks` | 任务列表 |
| GET | `/api/tasks/{id}` | 任务详情 |
| GET | `/api/tasks/{id}/events` | SSE 实时进度与日志 |
| POST | `/api/tasks/{id}/polish` | 触发 AI 整理 |
| GET | `/api/media/{id}/video.mp4` | 视频流（支持 Range） |
| GET | `/api/tasks/{id}/export?fmt=md\|txt\|srt\|raw\|json` | 导出 |
| GET | `/api/health` | ffmpeg / yt-dlp 就绪检查 |

Gradio 版同名能力由 `app.py` 内的 9 个端点提供（见 `/gradio_api/info`）。

## 成本参考（1 小时视频）

下载 1-3 分钟 → 转写 1-5 分钟 → 整理 30-60 秒，合计约 3-10 分钟；模型调用费用约 ¥1-3。

## 常见问题

| 问题 | 处理 |
|---|---|
| `Address already in use` | 7860 被占用：换 `SERVER_PORT`，或结束占用进程 |
| `ModuleNotFoundError: No module named 'core'` | 在项目根目录执行 `python app.py`，不要直接 `python core/...` |
| 页面无法访问 | 确认监听 `0.0.0.0`（`SERVER_HOST` 默认已是） |
| 视频很慢/显存不足 | 减小 `chunk_seconds`、调低并发，或提高创空间资源规格 |
| 中文乱码 | 代码统一 UTF-8 读写，导出时用 Chrome/Edge 打开 .md |

## 合规提示

- 仅用于个人学习与研究，请勿分发下载的原始音视频
- Cookie 与 API Key 属敏感信息，只保存在本地/创空间 Secrets，不要提交到版本库
