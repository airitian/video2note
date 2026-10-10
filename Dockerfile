# ==============================================================================
# 通用部署镜像（Docker / docker compose / 任意云主机）
#
# 设计取舍：
#   1. 服务监听 0.0.0.0:8765（与 run.py 默认值一致）。要换端口改 SERVER_PORT。
#   2. 数据默认落在 /data；用 -v 挂到宿主机即可跨重建保留任务与媒体。
#      镜像内不依赖任何平台专属目录。
#   3. Chromium 用于抖音解析（过 Cloudflare 挑战）。不解析抖音可
#      --build-arg INSTALL_PLAYWRIGHT=0 瘦身约 400MB。
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

# V2N_DATA_DIR 指定数据根目录；PLAYWRIGHT_BROWSERS_PATH 让内核随镜像走。
# 注意：ENV 的反斜杠续行块里不能放注释，Docker 会把 # 之后当变量值。
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright \
    V2N_DATA_DIR=/data \
    SERVER_HOST=0.0.0.0 \
    SERVER_PORT=8765

# venv 必须先落地：下面装 Chromium 的 RUN 要用 /opt/venv/bin/python
COPY --from=builder /opt/venv /opt/venv

# ffmpeg/ffprobe：合并音视频、抽音频、探测时长都依赖它（core/audio.py）
# ca-certificates：访问 ASR / LLM / B站等 HTTPS 接口
# curl：给 HEALTHCHECK 与排障用；tzdata：日志时间戳
RUN apt-get update && apt-get install --no-install-recommends -y \
        ffmpeg \
        ca-certificates \
        tzdata \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Chromium 仅供抖音解析使用（过 Cloudflare 挑战），B站链路不需要。
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

WORKDIR /app
COPY . /app

RUN mkdir -p /data

EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8765/api/health >/dev/null || exit 1

CMD ["sh", "-c", "exec python -u run.py --host \"${SERVER_HOST}\" --port \"${SERVER_PORT}\""]
