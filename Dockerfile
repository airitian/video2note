# ==============================================================================
# 通用部署镜像（Docker / docker compose / 任意云主机）
#
# 设计取舍：
#   1. 服务监听 0.0.0.0:8765（与 run.py 默认值一致）。要换端口改 SERVER_PORT。
#   2. 数据默认落在 /data；用 -v 挂到宿主机即可跨重建保留任务与媒体。
#   3. 抖音解析走 Selenium + 系统 Chromium（chromium / chromium-driver
#      由 apt 安装，版本成对，不再下载 Playwright 内核）。
#   4. Python 3.14 与线上运行环境对齐；依赖以 requirements.txt 精确锁定为准。
# ==============================================================================

# ---------- 构建阶段：只装 Python 依赖，便于分层缓存 ----------
FROM python:3.14-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_ROOT_USER_ACTION=ignore

WORKDIR /build
COPY requirements.txt .

RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip \
 && /opt/venv/bin/pip install -r requirements.txt

# ---------- 运行阶段 ----------
FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    V2N_DATA_DIR=/data \
    SERVER_HOST=0.0.0.0 \
    SERVER_PORT=8765

COPY --from=builder /opt/venv /opt/venv

# ffmpeg/ffprobe：合并音视频、抽音频、探测时长（core/audio.py）
# chromium + chromium-driver：抖音 Selenium 通道的浏览器与驱动（版本成对）
# curl：HEALTHCHECK 与排障；ca-certificates：HTTPS 接口；tzdata：日志时间戳
RUN apt-get update && apt-get install --no-install-recommends -y \
        ffmpeg \
        chromium \
        chromium-driver \
        ca-certificates \
        tzdata \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . /app

RUN mkdir -p /data

EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8765/api/health >/dev/null || exit 1

CMD ["sh", "-c", "exec /opt/venv/bin/python -u run.py --host \"${SERVER_HOST}\" --port \"${SERVER_PORT}\""]
