# -*- coding: utf-8 -*-
"""依赖完整性检查：requirements.txt 里声明的包，当前环境是否都能 import。

用途：新机器部署前先跑一遍，避免「装完启动才报 ModuleNotFoundError」。
"""
import importlib.metadata as md
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REQ = ROOT / "requirements.txt"

# 包名 -> import 名（不一致的才需要显式映射）
IMPORT_NAME = {
    "python-multipart": "multipart",
    "python-dotenv": "dotenv",
    "yt-dlp": "yt_dlp",
    "imageio-ffmpeg": "imageio_ffmpeg",
    "curl_cffi": "curl_cffi",
}

req_text = REQ.read_text(encoding="utf-8")
pkgs = re.findall(r"^([A-Za-z0-9_.\-]+)\s*[>=~!\[]", req_text, re.M)
print(f"requirements.txt 声明 {len(pkgs)} 个包\n")

miss_ver, miss_imp = [], []
for p in pkgs:
    try:
        ver = md.version(p)
    except Exception:
        miss_ver.append(p)
        print(f"  [未安装] {p}")
        continue

    mod = IMPORT_NAME.get(p, p.replace("-", "_"))
    try:
        __import__(mod)
        print(f"  [OK]     {p}=={ver}  (import {mod})")
    except Exception as e:
        miss_imp.append((p, e))
        print(f"  [装但导不进] {p}=={ver} -> {type(e).__name__}: {e}")

print("\n" + "=" * 56)
print(f"声明={len(pkgs)}  未安装={len(miss_ver)}  导入失败={len(miss_imp)}")
if miss_ver or miss_imp:
    print("建议：pip install -r requirements.txt")
    sys.exit(1)
print("依赖完整")
