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
# ---------- 前置校验（在源仓库做，报错信息更准确）----------
# Docker 类型创空间只认根目录的 Dockerfile；缺文件就别推了，否则线上直接构建失败
if [ ! -f "Dockerfile" ]; then
  echo "❌ 缺少 Dockerfile，Docker 类型创空间无法构建"
  exit 1
fi
# 平台强制 7860，EXOSE 别的端口会导致健康检查一直失败
if ! grep -qE '^[[:space:]]*EXPOSE[[:space:]]+7860' Dockerfile; then
  echo "❌ Dockerfile 未 EXPOSE 7860。ModelScope 规定服务必须监听 0.0.0.0:7860"
  exit 1
fi
# 8080 被平台占用
if grep -qE '^[[:space:]]*EXPOSE[[:space:]]+8080([[:space:]]|$)' Dockerfile; then
  echo "❌ Dockerfile EXPOSE 了 8080，该端口被平台占用，会导致启动失败"
  exit 1
fi
# 密钥绝不能进仓库
if git ls-files | grep -Eiq '(^|/)(\.env|cookies(_bili)?\.txt)$|\.log$'; then
  echo "❌ 仓库里存在被跟踪的密钥/Cookie/日志文件，请先加入 .gitignore"
  exit 1
fi
# 超过 100MB 的文件必须走 Git LFS，否则平台直接拒绝
big=""
while IFS= read -r f; do
  sz=$(git cat-file -s "$(git rev-parse ":$f" 2>/dev/null)" 2>/dev/null || echo 0)
  if [ "${sz:-0}" -gt 104857600 ]; then
    big="$big $f"
  fi
done < <(git ls-files)
if [ -n "$big" ]; then
  echo "❌ 以下文件超过平台 100MB 限制，必须改用 Git LFS：$big"
  exit 1
fi
echo "✅ Dockerfile 校验通过（EXPOSE 7860，无敏感文件，无超大文件）"

# ---------- 覆盖代码（保留创空间侧 .git 与运行数据）----------
echo "→ 同步文件..."
# 用 git 跟踪的文件列表为准，避免带上 data/、日志等本地运行数据。
# 从索引（git show :路径）取内容而不是直接 cp 工作区文件：工作区可能是 CRLF，
# 而 ModelScope 的敏感扫描对 CRLF 文件会误判并回滚提交。
git ls-files -z | while IFS= read -r -d '' f; do
  case "$f" in
    *.mp4|*.mp3|*.wav|*.png|*.jpg|*.ico|*.woff2) continue ;;
  esac
  mkdir -p "$WORK/studio/$(dirname "$f")"
  git show ":$f" > "$WORK/studio/$f"
done

cd "$WORK/studio"
# 临时克隆目录里没有全局身份，提交会失败；只在本仓库生效，不改用户全局配置
git config user.email "sync@video2note.local"
git config user.name "video2note sync"

# 删除本地有、但已被 git 跟踪清单移除的文件
git add -A

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
echo "   平台会自动重新构建镜像。Docker 类型首次构建约 3~5 分钟，"
echo "   带 Chromium 时可能到 8~12 分钟，可在创空间「日志」页查看进度。"