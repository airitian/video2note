# ==============================================================================
# ModelScope 创空间 —— Docker 类型
#
# 平台硬性要求（见 https://modelscope.cn/docs/studios/docker）：
#   1. 仓库根目录必须有本文件；
#   2. 服务必须监听 0.0.0.0:7860（schema 里 port 只能填 7860，8080 被平台占用）；
#   3. 响应头不得携带 Authorization / X-modelscope-* / X-studio-*；
#   4. 数据落在 /mnt/workspace 才会跨重启保留。
#
# 可选开关：
#   docker build --build-arg INSTALL_PLAYWRIGHT=0 .   # 不装 Chromium，镜像小 ~1.3GB
# ==============================================================================

# ---------- 构建阶段：只装 Python 依赖，便于分层缓存 ----------
FROM python:3.11-slim-bookworm AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_ROOT_USER_ACTION=ignore

WORKDIR /build
COPY requirements.txt .

RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip setuptools wheel \
 && /opt/venv/bin/pip install -r requirements.txt

# ---------- 运行阶段 ----------
FROM python:3.11-slim-bookworm

ARG INSTALL_PLAYWRIGHT=1

# 数据与临时文件都放持久化目录，重启不丢；
# SERVER_HOST/SERVER_PORT 固定为平台要求的 0.0.0.0:7860，不要改。
# 注意：ENV 的反斜杠续行块里不能放注释，Docker 会把 # 之后当变量值。
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright \
    V2N_DATA_DIR=/mnt/workspace/video2note \
    GRADIO_TEMP_DIR=/mnt/workspace/video2note/tmp \
    SERVER_HOST=0.0.0.0 \
    SERVER_PORT=7860 \
    DEBUG_MODE=false \
    APP_ENTRY=app

# ffmpeg/ffprobe：合并音视频、抽音频、探测时长都依赖它（core/audio.py）
# ca-certificates：访问模力方舟 / LLM / B站等 HTTPS 接口
# curl：给 HEALTHCHECK 与排障用；tzdata：日志时间戳
RUN apt-get update && apt-get install --no-install-recommends -y \
        ffmpeg \
        ca-certificates \
        tzdata \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Chromium 仅供 pyktok 抖音签名使用，yt-dlp 主链路不需要。
# 想瘦身镜像就构建时传 --build-arg INSTALL_PLAYWRIGHT=0。
RUN if [ "$INSTALL_PLAYWRIGHT" = "1" ]; then \
        apt-get update && apt-get install --no-install-recommends -y \
            libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
            libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 \
            libxrandr2 libgbm1 libpango-1.0-0 libcairo2 libasound2 \
        && rm -rf /var/lib/apt/lists/* \
        && PLAYWRIGHT_DOWNLOAD_HOST=https://registry.npmmirror.com/-/binary/playwright \
           /opt/venv/bin/python -m playwright install chromium ; \
    fi

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY . /app

# 创空间持久化目录：首次构建时平台不会预建
RUN mkdir -p /mnt/workspace/video2note

EXPOSE 7860

HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
    CMD curl -fsS http://127.0.0.1:7860/ >/dev/null || exit 1

# 同一份代码两种 UI，切换只改环境变量：
#   APP_ENTRY=app → Gradio 版（默认，与原创空间行为一致）
#   APP_ENTRY=run → FastAPI + static/ 本地版（端口同样是 7860）
CMD ["sh", "-c", "if [ \"$APP_ENTRY\" = \"run\" ]; then exec python -u run.py --host 0.0.0.0 --port 7860; else exec python -u app.py; fi"]
