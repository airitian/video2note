# -*- coding: utf-8 -*-
"""定位 Chromium「启动即崩溃」（Target ... has been closed）的真实原因。

为什么需要这个脚本
------------------
playwright 抛的这句报错是**结果**，不是原因。它把「缺系统库」「内核与库
版本对不上」「/dev/shm 太小」「架构不匹配」全都汇总成同一句
`Target page, context or browser has been closed`，真正的原因写在浏览器
进程的 stderr 里，playwright 默认不打出来。

所以光看应用层报错永远定位不到。这里做三件事：
  1. 报出 playwright 库版本——它决定了需要哪个 revision 的内核
  2. 列出实际落盘的内核可执行文件——确认装的是完整包还是残缺包
  3. 真启动一次并把完整异常栈打出来——里面有 Chromium 自己的 stderr

用法（务必用「跑服务那个解释器」）：
    # 先查服务用的解释器
    readlink -f /proc/$(pgrep -f 'run[.]py' | head -1)/exe
    # 再用它跑本脚本
    /usr/bin/python3.11 verify_chromium.py

退出码：0 = Chromium 能正常启动；1 = 起不来（此时看输出里的原因）
"""

import glob
import os
import shutil
import subprocess
import sys

# Chromium 依赖的共享库 -> Debian/Ubuntu 包名。
# 缺这些库时浏览器会以 exitCode=127 静默退出，playwright 只报
# 「Target ... has been closed」，用户看不出是缺库。直接给出包名省去查证。
_LIB_TO_PKG = {
    "libnspr4.so": "libnspr4",
    "libnss3.so": "libnss3",
    "libnssutil3.so": "libnss3",
    "libsmime3.so": "libnss3",
    "libatk-1.0.so.0": "libatk1.0-0",
    "libatk-bridge-2.0.so.0": "libatk-bridge2.0-0",
    "libcups.so.2": "libcups2",
    "libXcomposite.so.1": "libxcomposite1",
    "libXdamage.so.1": "libxdamage1",
    "libXfixes.so.3": "libxfixes3",
    "libXrandr.so.2": "libxrandr2",
    "libgbm.so.1": "libgbm1",
    "libxkbcommon.so.0": "libxkbcommon0",
    "libpango-1.0.so.0": "libpango-1.0-0",
    "libcairo.so.2": "libcairo2",
    "libasound.so.2": "libasound2",
    "libatspi.so.0": "libatspi2.0-0",
    "libdrm.so.2": "libdrm2",
}


def _exe() -> str:
    return sys.executable or "python3"


def _lib_version() -> str:
    """playwright 库版本。决定内核 revision，版本对不上就起不来。"""
    try:
        from importlib.metadata import version
        return version("playwright")
    except Exception:
        try:
            import playwright
            return getattr(playwright, "__version__", "未知")
        except Exception:
            return "未安装"


def _browsers_path() -> str:
    """playwright 内核根目录。

    不能写死 ~/.cache/ms-playwright：
    - Linux   ~/.cache/ms-playwright（默认，也是报错里出现的那个）
    - macOS   ~/Library/Caches/ms-playwright
    - Windows %USERPROFILE%\\AppData\\Local\\ms-playwright
    优先读 PLAYWRIGHT_BROWSERS_PATH——设了它就以它为准。
    """
    env = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if env and env != "0":
        return env
    home = os.path.expanduser("~")
    if sys.platform == "darwin":
        return os.path.join(home, "Library", "Caches", "ms-playwright")
    if os.name == "nt":
        return os.path.join(home, "AppData", "Local", "ms-playwright")
    return os.path.join(home, ".cache", "ms-playwright")


def _kernels() -> list[tuple[str, list[str]]]:
    """列出已落盘的内核及其可执行文件。

    常见异常：`chromium-XXXX` 目录在，但里面没有可执行文件——
    装到一半断网、或磁盘写满都会留下这种空壳。
    """
    base = _browsers_path()
    out = []
    # 可执行文件名随平台/版本变化，必须都覆盖：
    #   chrome（完整版，Linux/macOS/Windows）
    #   chrome.exe
    #   chrome-headless-shell  ← headless 模式下 playwright 实际启动的就是它
    #   headless_shell（旧版本命名）
    # 漏掉 chrome-headless-shell 会把「装好了」误判成「目录在但没有可执行文件」。
    names = ("chrome", "chrome.exe", "chrome-headless-shell",
             "chrome-headless-shell.exe", "headless_shell")
    for d in sorted(glob.glob(os.path.join(base, "chromium*"))):
        exe = []
        for n in names:
            exe += glob.glob(os.path.join(d, "**", n), recursive=True)
        out.append((os.path.basename(d), sorted(set(exe))))
    return out


def _expected_revision() -> str | None:
    """playwright 库期望的 chromium revision号。

    不同版本的 playwright 要求不同 revision 的内核。目录名带 revision，
    对不上就起不来。从库的 browsers.json 里读，读不到返回 None。
    """
    import json
    try:
        import playwright
        pkg = os.path.dirname(playwright.__file__)
        # browsers.json 位置随版本变动（playwright/driver/package/…），
        # 这里递归找，避免写死路径后又失效。
        hits = glob.glob(os.path.join(pkg, "**", "browsers.json"),
                         recursive=True)
        for p in hits[:3]:
            try:
                with open(p, encoding="utf-8") as f:
                    data = json.load(f)
                for b in data.get("browsers", []):
                    if b.get("name") == "chromium":
                        rev = str(b.get("revision", ""))
                        if "-" in rev:          # 形如 "1234-abc123"
                            rev = rev.split("-")[0]
                        if rev:
                            return rev
            except Exception:
                continue
    except Exception:
        pass
    return None


def _launch_target() -> str | None:
    """headless 模式下 playwright 实际启动的那个可执行文件路径。

    必须查这个而不是完整版 chromium：playwright 1.49+ 在 headless 下
    默认走 `chromium_headless_shell-XXXX`，两者的依赖不完全相同。
    只查完整版可能漏报——「完整版依赖齐全，但 shell 起不来」。
    """
    for name, exes in _kernels():
        if not exes:
            continue
        if "headless_shell" in name:
            return exes[0]        #优先 headless shell
    for _name, exes in _kernels():
        if exes:
            return exes[0]
    return None


def _missing_libs() -> list[str]:
    """用 ldd 找出缺失的共享库——Linux 上「启动即崩」最常见的原因。

    ldd 是 Linux 工具，其他平台直接跳过（那边的失败原因形态不同）。
    """
    if not shutil.which("ldd"):
        return []
    target = _launch_target()
    if not target:
        return []
    try:
        r = subprocess.run(["ldd", target], capture_output=True,
                           text=True, timeout=30)
        return [l.strip() for l in r.stdout.splitlines() if "not found" in l]
    except Exception:
        return []


def main() -> int:
    print("=" * 64)
    print("Chromium 启动诊断")
    print("=" * 64)
    print(f"解释器      : {_exe()}")
    print(f"playwright 库: {_lib_version()}")
    print()

    print("--- 1. 已落盘的内核 ---")
    kernels = _kernels()
    if not kernels:
        print("  （没有任何内核目录——需要执行 "
              f"{_exe()} -m playwright install chromium）")
        return 1
    for name, exe in kernels:
        print(f"  {name}: {exe[0] if exe else '❌ 目录在但没有可执行文件（安装不完整）'}")
    print()

    print("--- 2. 缺失的共享库（ldd）---")
    missing = _missing_libs()
    if missing:
        print(f"  ❌ 缺以下库，浏览器必定启动失败（检查的是 {_launch_target()}）：")
        for m in missing:
            print(f"     {m}")
        print()
        # 把 so 名映射成 apt 包名，用户可直接复制安装，不必自己查
        pkgs = sorted({_LIB_TO_PKG.get(m.split("=>")[0].strip(), "")
                       for m in missing} - {""})
        if pkgs:
            print("  对应的软件包（Debian/Ubuntu，可直接执行）：")
            print(f"    apt-get update && apt-get install -y {' '.join(pkgs)}")
            print()
        print(f"  或让 playwright 自己装依赖："
              f"{_exe()} -m playwright install --with-deps chromium")
        print()
    else:
        print("  ✅ 无缺失库")
    print()

    print("--- 3. 内核版本是否匹配库的期望 ---")
    # 库的版本决定它要哪个 revision 的内核。目录在、版本号却对不上，
    # 是「目录看着齐了但浏览器起不来」的头号原因。
    want = _expected_revision()
    have = [name.split("-")[-1] for name, _ in kernels]
    if want is None:
        print("  （无法确定库期望的 revision）")
    elif want in have:
        print(f"  ✅ 匹配（库需要 chromium-{want}）")
    else:
        print(f"  ❌ 不匹配：库需要 chromium-{want}，实际装了 {have}")
        print(f"     修复：{_exe()} -m playwright install --force chromium")
    print()

    print("--- 4. 真启动一次（完整报错）---")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(f"  ❌ 未安装 playwright 库：{_exe()} -m pip install playwright")
        return 1
    try:
        with sync_playwright() as p:
            b = p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled",
                      "--no-sandbox", "--disable-dev-shm-usage"])
            print(f"  ✅ 启动成功 {b.version}")
            b.close()
        return 0
    except Exception as e:
        print("  ❌ 完整异常（真正的原因在这里面）：")
        for line in str(e).splitlines():
            print(f"     {line}")
        print()
        print("  按可能性排序：")
        print(f"    ①缺系统库 → {_exe()} -m playwright install --with-deps chromium")
        print(f"    ②内核与库版本不匹配 → {_exe()} -m playwright install --force chromium")
        print("    ③内存不足 → 容器加内存，或确认 --disable-dev-shm-usage 已生效")
        return 1


if __name__ == "__main__":
    sys.exit(main())