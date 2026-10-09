#!/usr/bin/env bash
# ============================================================
#  video2note · Debian 12 服务器部署脚本
#  环境：2 核 / 7.8G / Python 3.11 / ffmpeg 5.1（已具备）
#  用法：bash deploy_debian.sh
# ============================================================
set -euo pipefail

APP_NAME=video2note
APP_DIR=/opt/$APP_NAME
SERVICE_NAME=video2note
PORT=8765
REPO_URL=https://github.com/airitian/video2note.git
LOG=/var/log/$SERVICE_NAME.log

say()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m  ✅\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m  ⚠️\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m  ❌\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "请用 root 执行：sudo bash $0"
command -v python3 >/dev/null || die "缺少 python3"
command -v ffmpeg  >/dev/null || die "缺少 ffmpeg，请先 apt install ffmpeg"

PYV=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
ok "Python $PYV"
[ "$(printf '%s\n3.10\n' "$PYV" | sort -V | head -1)" = "3.10" ] || die "Python 版本过低（需 ≥ 3.10）"
case "$PYV" in 3.1[0-3]) ;; *) warn "Python $PYV 未验证，建议用 3.11" ;; esac

# ---------- 1. 拉代码 ----------
say "同步代码"
if [ -d "$APP_DIR/.git" ]; then
  cd "$APP_DIR" && git pull --ff-only
else
  [ -e "$APP_DIR" ] && die "$APP_DIR 已存在且不是 git 仓库，请先备份清空"
  git clone "$REPO_URL" "$APP_DIR"
  cd "$APP_DIR"
fi
ok "$(git log --oneline -1)"

# ---------- 2. 依赖 ----------
say "安装依赖"
python3 -m venv "$APP_DIR/.venv" 2>/dev/null || {
  apt-get update -qq && apt-get install -y -qq python3-venv >/dev/null
  python3 -m venv "$APP_DIR/.venv"
}
VPY="$APP_DIR/.venv/bin/python"
"$VPY" -m pip install -q --upgrade pip
"$VPY" -m pip install -q -r requirements.txt
ok "依赖就绪：$("$VPY" -m pip list 2>/dev/null | wc -l) 个包"

# ---------- 3. 数据目录 ----------
say "准备数据目录"
mkdir -p /var/lib/$APP_NAME /var/log
chmod 755 /var/lib/$APP_NAME
ok "/var/lib/$APP_NAME"

# ---------- 4. 密钥（不落库、不进代码） ----------
SECRETS=/etc/$APP_NAME.env
if [ ! -f "$SECRETS" ]; then
  cat > "$SECRETS" <<'EOF'
# video2note 运行配置（权限 600，不要提交到任何仓库）
V2N_ASR_API_KEY=
V2N_LLM_API_KEY=
V2N_ASR_LANGUAGE=zh
V2N_COOKIE_TEXT=
V2N_COOKIE_TEXT_BILI=
EOF
  chmod 600 "$SECRETS"
  warn "已生成 $SECRETS，请填入 ASR/LLM 密钥后再启动"
  warn "  nano $SECRETS"
fi
chmod 600 "$SECRETS"
ok "配置文件 $SECRETS"

# ---------- 5. systemd ----------
say "注册 systemd 服务"
cat > /etc/systemd/system/$SERVICE_NAME.service <<EOF
[Unit]
Description=video2note - 视频转笔记
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$APP_NAME
WorkingDirectory=$APP_DIR
EnvironmentFile=$SECRETS
Environment="V2N_DATA_DIR=/var/lib/$APP_NAME"
Environment="PYTHONUNBUFFERED=1"
ExecStart=$APP_DIR/.venv/bin/python $APP_DIR/run.py --host 0.0.0.0 --port $PORT
Restart=always
RestartSec=5
StandardOutput=append:$LOG
StandardError=append:$LOG

# 资源限制：2 核机器上给转写留出余量
MemoryMax=4G
TasksMax=512

[Install]
WantedBy=multi-user.target
EOF

id -u $APP_NAME >/dev/null 2>&1 || useradd -r -s /usr/sbin/nologin $APP_NAME
chown -R $APP_NAME:$APP_NAME "$APP_DIR" /var/lib/$APP_NAME
systemctl daemon-reload
ok "服务已注册：$SERVICE_NAME"

# ---------- 6. 防火墙 ----------
say "开放端口 $PORT"
if command -v ufw >/dev/null; then
  ufw allow ${PORT}/tcp >/dev/null && ok "ufw 已放行 $PORT" || warn "uffw 放行失败"
else
  warn "未装ufw，如被拦请手动放行 $PORT/tcp"
fi

# ---------- 7. 启动 ----------
say "启动服务"
systemctl enable $SERVICE_NAME >/dev/null
systemctl restart $SERVICE_NAME
sleep 4

if systemctl is-active --quiet $SERVICE_NAME; then
  ok "服务已运行"
else
  warn "启动失败，最近日志："
  tail -25 "$LOG" 2>/dev/null || journalctl -u $SERVICE_NAME -n 25 --no-pager
  exit 1
fi

# ---------- 8. 自检 ----------
say "健康检查"
HEALTH=$(curl -fsS -m 10 "http://127.0.0.1:$PORT/api/health" 2>/dev/null || echo "")
if [ -n "$HEALTH" ]; then
  ok "服务响应正常"
  # 直接把接口结论转成人类可读的判断
  case "$HEALTH" in
    *'"ffmpeg":true'*)ok "ffmpeg 就绪" ;;
    *)                   warn "ffmpeg 不可用 —— 下载合并与抽音频会失败：$HEALTH" ;;
  esac
  case "$HEALTH" in
    *'"yt_dlp":true'*) ok "yt-dlp 就绪" ;;
    *)                 warn "yt-dlp 未安装 —— 下载全部失败：$HEALTH" ;;
  esac
else
  warn "健康检查未通过（服务可能仍在启动），请看 $LOG"
fi

# 抖音兜底（pyktok）需要浏览器内核；没有也不影响 yt-dlp 主链路
if "$VPY" -c "from playwright.sync_api import sync_playwright" >/dev/null 2>&1; then
  ok "Playwright 可用，抖音兜底已启用"
else
  warn "未装 Playwright 内核：抖音仅走 yt-dlp，失败时无兜底"
  warn "  如需兜底：$VPY -m playwright install --with-deps chromium"
fi

# 密钥是否填了（不显示内容，只看非空）
if grep -qE '^V2N_ASR_API_KEY=.+' "$SECRETS"; then
  ok "ASR 密钥已配置"
else
  warn "ASR 密钥为空 —— 转写会失败，请编辑 $SECRETS 后 systemctl restart $SERVICE_NAME"
fi

cat <<EOF

────────────────────────────────────────────────
 部署完成
   本机访问   http://127.0.0.1:$PORT
   外部访问   http://<服务器IP>:$PORT
   查看状态   systemctl status $SERVICE_NAME
   实时日志   tail -f $LOG
   修改配置   nano $SECRETS  && systemctl restart $SERVICE_NAME
────────────────────────────────────────────────
EOF
