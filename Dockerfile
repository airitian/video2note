# ==============================================================================
# 通用部署镜像（Docker / docker compose / 任意云主机）
#
# 设计取舍：
#   1. 服务监听 0.0.0.0:8765（与 run.py 默认值一致）。要换端口改 SERVER_PORT。
#   2. 数据默认落在 /data；用 -v 挂到宿主机即可跨重建保留任务与媒体。
#   3. 抖音解析走 Selenium + Chrome for Testing 152.0.7977.82（浏览器与
#      驱动成对下载、版本钉死；Debian 源的新版 Chromium 无头指纹过不了
#      抖音详情接口风控，详见运行阶段注释，不要随意升级浏览器）。
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
# 浏览器：Chrome for Testing 152.0.7977.82（浏览器 + 驱动成对下载、钉死版本）。
# 钉 152 的原因：Debian 源的 Chromium 156 无头指纹过不了抖音详情接口风控
# （2026-11-21 实测：解析 3 次全失败、单轮耗 252s；152 单轮 34s 成功），
# 152.0.7977.82 是旧线上环境长期验证的浏览器/驱动组合，不要随意升级。
# 其余包为 chrome 的运行时库（原由 apt chromium 包传递引入，现显式声明）。
# curl：HEALTHCHECK 与排障；ca-certificates：HTTPS 接口；tzdata：日志时间戳
ARG CFT_VERSION=152.0.7977.82
RUN apt-get update && apt-get install --no-install-recommends -y \
        ffmpeg \
        ca-certificates \
        tzdata \
        curl \
        unzip \
        fonts-liberation \
        libnss3 libnspr4 libatk1.0-0t64 libatk-bridge2.0-0t64 libatspi2.0-0t64 \
        libcups2t64 libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 \
        libxrandr2 libgbm1 libasound2t64 libpango-1.0-0 libpangocairo-1.0-0 \
        libcairo2 libglib2.0-0t64 libdbus-1-3 libexpat1 libfontconfig1 \
        libx11-6 libx11-xcb1 libxcb1 libxext6 libxcursor1 libxrender1 \
        libxss1 libxtst6 libuuid1 \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /opt/chrome-for-testing \
    && (curl -fL --retry 3 -o /tmp/chrome.zip "https://storage.googleapis.com/chrome-for-testing-public/${CFT_VERSION}/linux64/chrome-linux64.zip" \
        || curl -fL --retry 3 -o /tmp/chrome.zip "https://registry.npmmirror.com/-/binary/chrome-for-testing/${CFT_VERSION}/linux64/chrome-linux64.zip") \
    && (curl -fL --retry 3 -o /tmp/chromedriver.zip "https://storage.googleapis.com/chrome-for-testing-public/${CFT_VERSION}/linux64/chromedriver-linux64.zip" \
        || curl -fL --retry 3 -o /tmp/chromedriver.zip "https://registry.npmmirror.com/-/binary/chrome-for-testing/${CFT_VERSION}/linux64/chromedriver-linux64.zip") \
    && unzip -q /tmp/chrome.zip -d /opt/chrome-for-testing \
    && unzip -q /tmp/chromedriver.zip -d /opt/chrome-for-testing \
    && rm -f /tmp/chrome.zip /tmp/chromedriver.zip \
    && ln -s /opt/chrome-for-testing/chrome-linux64 /usr/lib/chromium \
    && ln -s /opt/chrome-for-testing/chromedriver-linux64/chromedriver /usr/bin/chromedriver \
    && chmod +x /opt/chrome-for-testing/chrome-linux64/chrome /opt/chrome-for-testing/chromedriver-linux64/chromedriver \
    && ! ldd /opt/chrome-for-testing/chrome-linux64/chrome | grep "not found" \
    && /usr/lib/chromium/chrome --version \
    && /usr/bin/chromedriver --version

WORKDIR /app
COPY . /app

RUN mkdir -p /data

EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8765/api/health >/dev/null || exit 1

CMD ["sh", "-c", "exec /opt/venv/bin/python -u run.py --host \"${SERVER_HOST}\" --port \"${SERVER_PORT}\""]
