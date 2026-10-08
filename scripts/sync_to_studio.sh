#!/usr/bin/env bash
# 一键把本地代码同步到 ModelScope 创空间（Windows Git Bash / macOS / Linux 通用）
#
# 用法：
#   bash scripts/sync_to_studio.sh
#   MODELSCOPE_TOKEN=xxx bash scripts/sync_to_studio.sh
#
# 令牌读取优先级：环境变量 MODELSCOPE_TOKEN > .env 里的 V2N_STUDIO_TOKEN
set -euo pipefail

STUDIO="${MODELSCOPE_STUDIO:-viva25/video_txt}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# ---------- 读取令牌 ----------
TOKEN="${MODELSCOPE_TOKEN:-}"
if [ -z "$TOKEN" ] && [ -f "$ROOT/.env" ]; then
  TOKEN="$(grep -E '^\s*V2N_STUDIO_TOKEN\s*=' "$ROOT/.env" | head -1 \
           | cut -d= -f2- | tr -d '"'"'"' \r\n')"
fi
if [ -z "$TOKEN" ]; then
  echo "❌ 未找到访问令牌。请设置环境变量："
  echo "     export MODELSCOPE_TOKEN=你的访问令牌"
  echo "   或在项目根目录 .env 中写入 V2N_STUDIO_TOKEN=你的访问令牌"
  echo "  获取地址：https://modelscope.cn/my/myaccesstoken"
  exit 1
fi

echo "目标创空间：${STUDIO}"

# ---------- 克隆创空间仓库 ----------
echo "→ 拉取创空间仓库..."
git clone --depth 1 "https://oauth2:${TOKEN}@modelscope.cn/studios/${STUDIO}.git" "$WORK/studio" 2>&1 | sed 's/oauth2:[^@]*@/oauth2:***@/'
cd "$WORK/studio"

# ---------- 覆盖代码（保留创空间侧 .git 与运行数据）----------
echo "→ 同步文件..."
cd "$ROOT"
# 用 git 跟踪的文件列表为准，避免带上 data/、日志等本地运行数据
git ls-files -z | while IFS= read -r -d '' f; do
  mkdir -p "$WORK/studio/$(dirname "$f")"
  cp "$f" "$WORK/studio/$f"
done

cd "$WORK/studio"
# 删除本地有、但已被 git 跟踪清单移除的文件
git add -A

# ---------- 前置校验 ----------
for f in app.py requirements.txt; do
  if [ ! -f "$f" ]; then
    echo "❌ 缺少必需文件 $f，Gradio 创空间无法启动"
    exit 1
  fi
done

if git diff --cached --quiet; then
  echo "✅ 与创空间内容一致，无需推送"
  exit 0
fi

# ---------- 提交并推送 ----------
git commit -q -m "sync: $(date '+%Y-%m-%d %H:%M:%S') 自动同步"
git push -q origin HEAD 2>&1 | sed 's/oauth2:[^@]*@/oauth2:***@/'

echo ""
echo "✅ 已推送到 ModelScope 创空间"
echo "   https://modelscope.cn/studios/${STUDIO}"
echo "   平台会自动重建，实测约 3~8 分钟，可在创空间「日志」页查看进度。"