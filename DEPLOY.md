# video2note 部署文档

抖音 / B站视频转文字服务：链接解析或本地文件上传 → 提取音频 → ASR 转写 → AI 整理成稿。
本文档覆盖 Docker 与裸机两种部署方式、全部配置项、数据备份、升级回滚与运维注意事项。

## 1. 架构一览

- **单体应用**：Python 标准库 HTTP 服务（入口 `run.py`，逻辑在 `core/`），无数据库；
  任务、配置、令牌都是数据目录里的 JSON 文件。
- **运行时依赖**：ffmpeg/ffprobe（音视频处理）、Chrome for Testing（抖音页面解析，
  Selenium 驱动）、yt-dlp（B站/通用下载）。Docker 镜像已全部内置，裸机部署需自行安装。
- **外部服务**（都在设置页或配置文件里配置）：
  - ASR 转写：OpenAI 兼容端点，默认模力方舟 `https://api.moark.com/v1` + `SenseVoiceSmall`；
  - LLM 整理：任意 OpenAI 兼容端点（模型名、地址、Key 均可配）；
  - 七牛云对象存储（可选）：媒体归档与 CDN 播放加速，强烈建议开启（见 §6）。
- **监听**：`0.0.0.0:8765`（环境变量 `SERVER_HOST` / `SERVER_PORT` 可改）。

## 2. 方式一：Docker 部署（推荐）

### 2.1 构建镜像

```bash
git clone https://github.com/airitian/video2note.git
cd video2note
docker build -t video2note:latest .          # 通用
docker build -f Dockerfile.cn -t video2note:latest .   # 国内网络（镜像源加速）
```

> ⚠️ **浏览器版本已钉死，不要改**：镜像内是 Chrome for Testing **152.0.7977.82**
> （浏览器与驱动成对下载）。实测 Debian 源的新版 Chromium（如 156）无头指纹过不了
> 抖音详情接口风控，表现为解析全部失败、单轮空转 4 分钟以上；152 版单轮约 34 秒成功。
> 升级浏览器前必须先做抖音解析 A/B 实测。

### 2.2 准备环境变量文件

```bash
mkdir -p /data/video2note          # 数据目录（任务/媒体/配置都放这，要持久化）
cat > /root/video2note.env <<'EOF'
V2N_ADMIN_TOKEN=改成你的管理员令牌
EOF
chmod 600 /root/video2note.env
```

`V2N_ADMIN_TOKEN` 是管理员引导令牌：带它访问即管理员，可在页面里再签发普通令牌、
管理免费额度门禁。旧变量 `V2N_ACCESS_TOKEN` 会被当作管理员令牌兼容识别。
更多环境变量见 §4.3。

### 2.3 启动

```bash
docker run -d --name video2note --restart unless-stopped \
  -p 8765:8765 \
  --env-file /root/video2note.env \
  -v /data/video2note:/data \
  video2note:latest
```

镜像内已设 `V2N_DATA_DIR=/data`，数据卷务必挂载，否则重建容器丢任务。
宿主机端口按需改（NAT 机器把外部端口映射到 8765 即可）。

### 2.4 验证

```bash
curl -s http://127.0.0.1:8765/api/health
# 期望返回 JSON 且 "browser": true（浏览器就绪）
curl -s -H "Authorization: Bearer <管理员令牌>" http://127.0.0.1:8765/api/tasks
```

然后浏览器打开 `http://<主机>:8765/`，右上角「设置」里填 ASR / LLM 的 API Key
（或直接编辑数据目录里的 `settings.json` / `secrets.json`，见 §4）。

## 3. 方式二：Debian / Ubuntu 裸机部署

```bash
bash scripts/deploy_debian.sh
```

脚本会把代码装到 `/opt/video2note` 并注册 systemd 服务（名 `video2note`，端口 8765）。
前提：Python ≥ 3.10、ffmpeg 已安装；抖音解析还需要 Chrome/Chromium + 对应驱动，
版本要求同 §2.1 的警告。日常管理：

```bash
systemctl status|restart video2note
journalctl -u video2note -f
```

## 4. 配置说明

### 4.1 配置优先级

```
内置默认值  <  settings.json  <  secrets.json  <  环境变量
```

`settings.json` / `secrets.json` 都在**数据目录**里，可在页面「设置」里改，
也可在停机时直接编辑文件。`secrets.json` 只存密钥类字段，文件权限应为 `0600`，
程序写入时也是原子写 + 0600。

### 4.2 主要配置项（settings / secrets）

| 分组 | 键 | 说明 |
|---|---|---|
| ASR | `asr_base_url` / `asr_model` / `asr_language` | 转写端点与模型，默认模力方舟 SenseVoiceSmall、中文 |
| ASR | `asr_api_key`（secrets） | 转写服务 API Key |
| LLM | `llm_base_url` / `llm_model` / `llm_max_tokens` | 整理端点、模型、最大输出（默认 32768） |
| LLM | `llm_api_key`（secrets） | LLM API Key |
| 七牛 | `qiniu_bucket` / `qiniu_domain` | 归档桶与访问域名 |
| 七牛 | `qiniu_ak` / `qiniu_sk`（secrets） | 七牛密钥对，用于上传归档与生成预签名播放地址 |

### 4.3 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `V2N_ADMIN_TOKEN` | 空 | 管理员引导令牌（兼容旧名 `V2N_ACCESS_TOKEN`） |
| `V2N_DATA_DIR` | `./data`（镜像内 `/data`） | 数据目录 |
| `V2N_MEDIA_TTL_DAYS` | 7 | 本地媒体保留天数，超期自动清理（任务记录与成稿保留；已归档七牛的不受影响） |
| `V2N_MAX_UPLOAD_MB` | 500 | 单文件上传上限 |
| `V2N_MAX_WORKERS` | 按 CPU | 并发处理任务数 |
| `V2N_FFMPEG_TIMEOUT` | 内部默认 | ffmpeg 单步超时（秒） |
| `V2N_POLISH_JUDGE` | — | 已废弃（2026-11-22 起生成内质检下线，质检改为生成后独立报告，此变量不再生效） |
| `V2N_LLM_TEMPERATURE` | 空 | 全局温度覆盖；不设则按文稿风格分别取温度 |
| `V2N_QINIU_URL_EXPIRES` | 内部默认 | 七牛预签名播放地址有效期（秒） |
| `V2N_DISK_WARN_GB` | 内部默认 | 数据盘剩余空间告警阈值 |
| `V2N_SECRETS_FILE` | 数据目录下 | secrets.json 路径覆盖 |
| `V2N_ALLOW_PRIVATE_URLS` | 0 | ⚠️ 允许解析内网地址（SSRF 防护），生产保持 0 |
| `V2N_ALLOW_INSECURE_TLS` | 0 | ⚠️ 跳过 TLS 校验，生产保持 0 |
| `SERVER_HOST` / `SERVER_PORT` | `0.0.0.0` / `8765` | 监听地址与端口 |

## 5. 数据与备份

数据目录结构：

```
/data/video2note/
├── tasks/         # 每个任务一个 JSON（元信息、转写、成稿）
├── media/<任务id>/ # source.mp4、音频、cover.jpg 封面等（受 TTL 清理）
├── settings.json  # 非敏感配置
├── secrets.json   # 密钥（0600）
└── tokens.json    # 访问令牌（0600）
```

备份只需打包整个数据目录 + env 文件：

```bash
tar czf video2note-backup-$(date +%F).tar.gz -C /data video2note /root/video2note.env
```

恢复：解包回原路径后按 §2.3 启动即可，任务与成稿都在。

## 6. 运维注意事项

- **播放加速**：任务媒体归档到七牛后，播放器请求会优先 302 到七牛预签名地址走 CDN；
  未归档或归档失败才由本机流式直出。**小带宽/NAT 机器务必开启七牛归档**，
  否则播放速度受本机上行带宽限制（实测某 NAT 机公网出流仅约 65 KB/s，必然卡顿）。
- **封面**：新任务处理时自动抽帧生成 `cover.jpg` 作为播放器封面；
  该功能上线前处理的老任务没有封面文件，属正常现象。
- **升级**：构建新镜像（打新标签）→ 停旧容器并改名保留 → 用新镜像起同名容器 →
  查 `/api/health` 与任务数 → 无误后删除旧容器；**回滚**就是停新容器、
  把旧容器改回原名再启动。升级前先按 §5 备份数据目录。
- **日志**：`docker logs -f video2note`（裸机 `journalctl -u video2note -f`），
  服务输出 JSON 结构化日志。
- **磁盘**：媒体 TTL 默认 7 天自动清理；成稿与任务记录不占大空间。
  数据盘告警阈值见 `V2N_DISK_WARN_GB`。

## 7. 开发与自检

```bash
python -m pytest            # 单元测试（48 项）
python scripts/verify_dockerfile.py   # Dockerfile 静态校验（无需 Docker 环境）
```
