# FIX_REPORT — video2note 全量问题修复报告（2026-11-21）

- 分支：`fix/20261121`，基线提交 `36a74e1`（与线上运行容器 /app 代码对齐，未改动、未重写）。
- 范围：仅本地仓库修改；未连服务器、未推送 GitHub、未部署。
- 验证环境：本机 Python 3.12.3 + 与线上 pip freeze 对齐的依赖集（代码兼容 3.12/3.14）。

## 提交列表（36a74e1..HEAD）

| 提交 | 内容 |
|---|---|
| 84d6944 | fix(auth)：鉴权覆盖面 + 密钥存储地基（问题 1/7/10d） |
| 5aa832b | fix(ssrf)：SSRF 防护与 TLS 校验（问题 4） |
| b875ed7 | fix(robustness)：配置钳制与 ffmpeg 总超时（问题 5） |
| 5ae6ae1 | fix(state)：状态机断裂（问题 6） |
| b535896 | fix(disk)：磁盘闭环 + 七牛签名收口（问题 3/7） |
| 319ef02 | fix(deps)：依赖锁定与 Dockerfile 重写（问题 2） |
| b630c53 | feat(obs+api)：结构化日志、health 磁盘字段、批量创建与转写编辑（问题 9/10） |
| e875106 | chore(hygiene)：仓库卫生 + pytest 套件 + CI + 配置文档对齐（问题 8/9） |
| 1656841 | fix(frontend)：前端 bug 与产品补齐（问题 7/10） |

## 逐项说明

### 1. 鉴权（P0）✅
- 改了什么：`_access_guard` 重写——任务列表/详情/导出/SSE events/`/api/media/*`/DELETE/归档触发接口在「已配置令牌」时必须带有效令牌（任一角色，media 支持 cookie）；`/api/settings`、`/api/admin/*` 保持管理员；`/api/health`、`/api/auth/state`、`/api/auth/login` 保持公开。`config.is_admin` 在未配置任何令牌时返回 False（删除 dev 全开放与「首个令牌即管理员」后门，add_token 本就会把首个令牌设为 admin 角色）。`run.py` 启动守卫：非回环绑定 + 零令牌 → 打印明确错误并以退出码 2 拒绝启动；回环零令牌保留开发模式并打印警告。创建类接口保留原门禁语义（402 兑换码流程是产品设计，未改）。
- 涉及文件：core/main.py、core/config.py、core/store.py（owner 字段）、run.py。
- 如何验证：pytest TestClient 用例（无令牌列表 401 / 有令牌 200 / cookie 通道 200 / 详情·媒体·删除守卫 / 设置管理员）；子进程实测 `run.py --host 0.0.0.0` 无令牌 exit=2 且输出拒绝原因；`--host 127.0.0.1` 无令牌可启动且 /api/tasks 返回 []。
- 兼容性：前端 api() 本就靠 /api/auth/state 判断、401 时弹登录框并自动重试，流程未动；TestClient 来源（host=testclient）在守卫里按回环等价处理，仅影响测试。

### 2. 依赖与 Dockerfile（P0）✅
- 改了什么：requirements.txt 改为与线上 pip freeze 对齐的精确锁定（fastapi 0.143.1、uvicorn 0.54.0、selenium 4.51.0、yt-dlp 2026.8.19 等），含 selenium、不含 playwright（抖音通道实测为 Selenium + 系统 Chromium，Playwright 是死引用）。Dockerfile 重写：python:3.14-slim、apt 装 ffmpeg/chromium/chromium-driver/curl/ca-certificates/tzdata、pip 装锁定版 requirements，保留 HEALTHCHECK 与 CMD 语义（SERVER_HOST/SERVER_PORT、/data 卷、8765）。Dockerfile.cn 同结构换清华镜像源。/api/health 浏览器字段改名 browser/browser_msg，playwright/* 保留同值别名兼容旧前端与监控；dlpanda 删除 ms-playwright 内核探测、报错文案改 apt 口径；deploy_debian.sh 自检改 Selenium + 系统 chromium。
- 删除 Dockerfile.alpine_cached 的说明：该文件 `FROM postgres:16-alpine`，是借数据库基础镜像拼凑应用镜像的非常规做法——正是线上出现 8.13 GB `docker commit` 镜像、依赖无法复现的成因。规范 Dockerfile 已覆盖其全部用途，故删除不保留。
- 涉及文件：requirements.txt、Dockerfile、Dockerfile.cn、Dockerfile.alpine_cached（删）、core/main.py、core/dlpanda.py、scripts/deploy_debian.sh。
- 如何验证：py_compile 通过；本机按锁定版本装出测试环境并跑通全部 pytest 与真实启动冒烟（/api/health 返回 browser/playwright 双字段）。Docker 镜像未在本机实际 build（本机无 Docker），Dockerfile 为静态复核。

### 3. 磁盘闭环（P0/P1）✅
- 改了什么：qiniu_archive 的归档/清理范围从 `source.*` 扩展到全部媒体（video.mp4、video_h264.mp4、audio.mp3、atrack.*、vtrack.*）。新增 `local_ttl_cleanup()`：不依赖七牛，终态任务（done/transcribed/failed/canceled）媒体超 `V2N_MEDIA_TTL_DAYS`（默认 7）删除媒体、保留任务记录与成稿，删前校验真实路径在 media 目录内；挂到 `_archive_loop`（启动首跑 + 每日）。上传限额 `V2N_MAX_UPLOAD_MB`（默认 500）：upload 与 upload-raw 均做 Content-Length 预检 + 流式计数，超限 413 并删半成品与任务记录。
- 涉及文件：core/qiniu_archive.py、core/main.py。
- 如何验证：pytest TTL 用例（30 天前终态任务媒体被清且记录保留、运行中任务不
...[truncated 5872 chars]