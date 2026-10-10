#!/usr/bin/env bash
# 诊断「服务要怎么重启」——sudo 不存在时，先把环境摸清再动手。
#
# 用法：
#     bash diag_restart.sh
#
# 只读，不改任何东西。跑完把输出发回即可。
echo "========== 1. 你是谁 =========="
echo "用户: $(whoami)   UID=$(id -u)"
echo "在 root 下？$( [ "$(id -u)" = 0 ] && echo 是 || echo 否，无 sudo 通常就是因为已在 root)"
echo

echo "========== 2. 你在哪个目录（关键）=========="
echo "当前目录: $(pwd)"
echo "git 仓库根: $(git rev-parse --show-toplevel 2>/dev/null || echo '不在 git 仓库里')"
echo "HEAD: $(git log --oneline -1 2>/dev/null || echo '无')"
echo
echo "⚠ 若「git 仓库根」不在上面列出的项目目录里，那 git pull 更新的不是服务在跑的那份代码。"
echo
echo "真正的项目目录（按优先级探测）："
# 容器里的路径不固定：Dockerfile 用 /app，托管平台常用 /workspace，
# 手动部署常见 /opt/video2note。所以按 run.py 实际位置探测，不写死。
APP_FOUND=""
for d in /workspace /app /opt/video2note "$HOME/video2note" /root/video2note /data; do
  if [ -f "$d/run.py" ] && [ -d "$d/core" ]; then
    echo "  ✅ 找到: $d"
    [ -z "$APP_FOUND" ] && APP_FOUND="$d"
  fi
done
[ -z "$APP_FOUND" ] && echo "  （都没找到。若服务在跑，用第3 节的 ps 输出反推）"
echo "  ⚠ 后续所有 git pull / 重启命令都要在上述目录里执行，路径写错等于没生效。"
echo

echo "========== 3. 服务进程怎么起来的 =========="
echo "--- run.py / uvicorn 进程 ---"
ps -ef 2>/dev/null | grep -E "run\.py|uvicorn|video2note" | grep -v grep || echo "（没查到）"
echo
echo "--- 谁在监听 8765 ---"
if command -v ss >/dev/null 2>&1; then
  ss -lntp 2>/dev/null | grep 8765 || echo "（ss 没查到 8765）"
elif command -v netstat >/dev/null 2>&1; then
  netstat -lntp 2>/dev/null | grep 8765 || echo "（netstat 没查到 8765）"
else
  echo "（ss 和 netstat 都没有，可试: cat /proc/net/tcp | grep 22B5）"
fi
echo

echo "========== 4. 有哪些重启方式可用 =========="
echo "systemctl: $(command -v systemctl >/dev/null 2>&1 && echo '存在' || echo '不存在')"
echo "supervisorctl: $(command -v supervisorctl >/dev/null 2>&1 && echo '存在' || echo '不存在')"
echo "service: $(command -v service >/dev/null 2>&1 && echo '存在' || echo '不存在')"
echo "docker: $(command -v docker >/dev/null 2>&1 && echo '存在' || echo '不存在')"
echo "pm2: $(command -v pm2 >/dev/null 2>&1 && echo '存在' || echo '不存在')"
echo

echo "--- systemd 服务是否存在 ---"
if command -v systemctl >/dev/null 2>&1; then
  systemctl list-unit-files 2>/dev/null | grep -i video2note || echo "（systemd 里没有 video2note 服务）"
fi
echo

echo "--- supervisor 配置 ---"
if command -v supervisorctl >/dev/null 2>&1; then
  supervisorctl status 2>&1 | head -20
  ls /etc/supervisor/conf.d/ 2>/dev/null
fi
echo
echo "========== 5. 该用哪个 Python 解释器 =========="
#关键：装 playwright 内核必须用「跑服务那个解释器」，
# 用别的 python 装到别的环境里去，服务照样报内核缺失。
PY_BIN=""
for pid in $(ps -eo pid,args 2>/dev/null | grep -E "run\.py" | grep -v grep | awk '{print $1}'); do
  exe=$(readlink -f "/proc/$pid/exe" 2>/dev/null)
  [ -n "$exe" ] && { echo "  服务 PID $pid的解释器: $exe"; PY_BIN="$exe"; }
done
if [ -z "$PY_BIN" ]; then
  for c in python3 python "${APP_FOUND:-/nonexistent}/.venv/bin/python"; do
    if command -v "$c" >/dev/null 2>&1 || [ -x "$c" ]; then PY_BIN="$c"; echo "  回退候选: $(command -v "$c" 2>/dev/null || echo "$c")"; break; fi
  done
fi
echo
echo "  ⚠ 容器里通常只有 python3，没有 python。所以写 python 会 command not found。"
echo "    请用下面这条（把解释器换成实际路径）："
echo "      ${PY_BIN:-python3} -m playwright install chromium"
echo
echo "--- playwright 库装在哪个环境 ---"
for c in ${PY_BIN:-python3} python3; do
  command -v "$c" >/dev/null 2>&1 || [ -x "$c" ] || continue
  echo "  $c → $($c -c 'import playwright,sys;print("库在",playwright.__file__)' 2>/dev/null || echo '未安装 playwright')"
done
echo
echo "--- 内核目录（装完应存在 chromium-XXXX）---"
ls -d ~/.cache/ms-playwright/* 2>/dev/null || echo "  （不存在——这就是报 Executable doesn't exist 的原因）"
echo

echo "========== 6. 启动脚本长什么样（决定怎么重启）=========="
for d in ${APP_FOUND:-/nonexistent} /workspace /app; do
  [ -d "$d" ] || continue
  for f in run.sh start.sh entrypoint.sh; do
    [ -f "$d/$f" ] && { echo "--- $d/$f ---"; head -30 "$d/$f"; }
  done
done
echo "--- Docker CMD 决定了默认启动命令 ---"
grep -E "^CMD|^ENTRYPOINT|^WORKDIR" Dockerfile 2>/dev/null | head || echo "（无 Dockerfile）"
echo
echo "========== 诊断结束 =========="
echo "把上面全部输出发回，我据此给你一条可直接执行的启动命令。"
echo "（不要先 kill 进程——杀错会导致服务彻底起不来。）"
echo
echo "只想装Chromium 内核的话，直接跑这一条："
echo "  \$(ps -eo args | grep -m1 '[r]un\.py' | awk '{print \$1}') -m playwright install chromium"
echo "  （用 ps 取服务真实的解释器，比猜 python3 可靠）"