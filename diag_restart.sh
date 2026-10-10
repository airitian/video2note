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
echo "⚠ 若「git 仓库根」不是 /opt/video2note，那 git pull 更新的不是服务在跑的那份代码。"
echo
echo "真正的项目目录（按优先级探测）："
for d in /opt/video2note "$HOME/video2note" /root/video2note /app /workspace; do
  [ -f "$d/core/main.py" ] && echo "  ✅ 找到: $d"
done
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
  for c in python3 python /opt/video2note/.venv/bin/python "$HOME/video2note/.venv/bin/python"; do
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
for d in /opt/video2note "$HOME/video2note" /root/video2note; do
  if [ -f "$d/run.sh" ]; then echo "--- $d/run.sh ---"; head -30 "$d/run.sh"; fi
  if [ -f "$d/start.sh" ]; then echo "--- $d/start.sh ---"; head -30 "$d/start.sh"; fi
done
echo
echo "========== 诊断结束 =========="
echo "把上面全部输出发回，我据此给你一条可直接执行的启动命令。"
echo "（不要先 kill 进程——杀错会导致服务彻底起不来。）"
echo
echo "只想装Chromium 内核的话，直接跑这一条："
echo "  \$(ps -eo args | grep -m1 '[r]un\.py' | awk '{print \$1}') -m playwright install chromium"
echo "  （用 ps 取服务真实的解释器，比猜 python3 可靠）"