# -*- coding: utf-8 -*-
"""curl 设置项端到端验证：直接调函数层，不依赖常驻服务"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, ".")

PASS = FAIL = 0


def ck(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {extra}")


REAL = open("verify_curlparse.py", encoding="utf-8").read()
REAL = REAL.split('REAL = r"""')[1].split('"""')[0]

# 用独立数据目录，避免污染真实配置
tmp = Path(tempfile.mkdtemp(prefix="v2n_curl_"))
import os

os.environ["V2N_DATA_DIR"] = str(tmp)

from core import config, curlparse

print("=" * 66)
print("一、配置层：resolver_curl 归入敏感项")
print("=" * 66)

ck("DEFAULTS 含 resolver_curl", "resolver_curl" in config.DEFAULTS)
ck("属于 SECRET_KEYS", "resolver_curl" in config.SECRET_KEYS)
ck("HELP 有说明", "resolver_curl" in config.HELP)

print()
print("=" * 66)
print("二、保存 -> 内容不进 settings.json，只进 secrets.json")
print("=" * 66)

config.save_settings({"resolver_curl": REAL})
sf = config.SETTINGS_FILE
sec = config.SECRETS_FILE
ck("settings.json 已生成", sf.exists())
ck("secrets.json 已生成", sec.exists())

s_raw = sf.read_text("utf-8") if sf.exists() else ""
c_raw = sec.read_text("utf-8") if sec.exists() else ""
ck("curl 内容不在 settings.json", "WebKitFormBoundary" not in s_raw)
ck("curl 内容在 secrets.json", "WebKitFormBoundary" in c_raw)
ck("Cookie 不在 settings.json", "cf_clearance" not in s_raw)

print()
print("=" * 66)
print("三、public_settings 不回显内容")
print("=" * 66)

pub = config.public_settings()
ck("resolver_curl 回显为空", pub.get("resolver_curl") == "", repr(pub.get("resolver_curl"))[:40])
cfgd = (pub.get("_configured") or {}).get("resolver_curl")
ck("_configured 标记为已配置", bool(cfgd), cfgd)
ck("无cf_clearance 泄露", "cf_clearance" not in json.dumps(pub, ensure_ascii=False))
ck("无 boundary 泄露", "WebKitFormBoundary" not in json.dumps(pub, ensure_ascii=False))

print()
print("=" * 66)
print("四、加载回来仍可用（模板能被解析）")
print("=" * 66)

back = config.load_settings().get("resolver_curl") or ""
ck("长度一致", len(back) == len(REAL), f"{len(back)}/{len(REAL)}")
try:
    p = curlparse.parse_curl(back)
    ck("重新解析成功", p["url"] == "https://dlpanda.com/zh-CN", p["url"])
    # 不硬编码具体 token 值：样本来自本机 data/settings.json，每次都不一样
    ck("_token 保留在模板里", bool(p["form"].get("_token")),
       p["form"].get("_token"))
    ck("Cookie 保留在模板里", "cf_clearance" in p["cookies"])
except Exception as e:
    ck("重新解析成功", False, str(e))

print()
print("=" * 66)
print("五、留空表示不改（不能等同清空）")
print("=" * 66)

config.save_settings({})
still = config.load_settings().get("resolver_curl") or ""
ck("留空后内容仍在", len(still) == len(REAL), f"{len(still)}")

print()
print("=" * 66)
print("六、显式清除")
print("=" * 66)

config.save_settings({}, clear_secrets={"resolver_curl"})
gone = config.load_settings().get("resolver_curl")
ck("清除后为空", not gone, repr(gone)[:40])
ck("清除后 _configured 为假",
   not (config.public_settings().get("_configured") or {}).get("resolver_curl"))

print()
print("=" * 66)
print(f"结果：{PASS} 通过 / {FAIL} 失败")
print("=" * 66)

import shutil

shutil.rmtree(tmp, ignore_errors=True)
sys.exit(1 if FAIL else 0)