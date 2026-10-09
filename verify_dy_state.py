# -*- coding: utf-8 -*-
"""cookie_state() / cookie_hint() 分流验证：4 种登录态必须给出对应文案"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from core import douyin

fails = []
def ck(name, cond, extra=""):
    print(f"[{'OK' if cond else 'FAIL'}] {name}" + (f"  {extra}" if extra else ""))
    if not cond:
        fails.append(name)

print("=" * 60)
print("一、cookie_state 四种状态")
orig_header = douyin._cookie_header
orig_get = None

import requests

class FakeResp:
    def __init__(self, payload, ok=True):
        self._p = payload; self.ok = ok
    def json(self):
        if self.ok is False:
            raise ValueError("not json")
        return self._p

def with_state(ck_text, payload, raise_exc=False, bad_json=False):
    """注入：指定 Cookie 与接口返回"""
    douyin._cookie_header = lambda: ck_text
    if raise_exc:
        requests.get = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("网络不通"))
    elif bad_json:
        requests.get = lambda *a, **k: FakeResp(None, ok=False)
    else:
        requests.get = lambda *a, **k: FakeResp(payload)
    return douyin.cookie_state()

ck("无 Cookie -> missing", with_state("", {}) == "missing")
ck("status_code=0 -> ok", with_state("a=b", {"status_code": 0}) == "ok")
ck("status_code=8 -> expired", with_state("a=b", {"status_code": 8}) == "expired")
ck("其他状态码 -> unknown", with_state("a=b", {"status_code": 999}) == "unknown")
ck("接口异常 -> unknown", with_state("a=b", {}, raise_exc=True) == "unknown")
ck("返回非 JSON -> unknown", with_state("a=b", None, bad_json=True) == "unknown")

print("\n" + "=" * 60)
print("二、cookie_hint 文案分流")

# 每个用例都要显式重置 Cookie 状态——上一组用例把 _cookie_header 改成了空串，
# 不重置的话这里全部会走missing 分支（第一版测试就是这么假通过的）。
douyin._cookie_header = lambda: "sessionid=x; ttwid=y"
requests.get = lambda *a, **k: FakeResp({"status_code": 8})
h = douyin.cookie_hint()
ck("已失效 -> 提示重配", "Cookie 已失效" in h, f"({len(h)}字符)")
ck("已失效文案不含'未配置'", "未配置抖音 Cookie" not in h)
ck("已失效文案讲清判据", "status_code=8" in h)

requests.get = lambda *a, **k: FakeResp({"status_code": 0})
ck("登录态正常 -> 不给失效提示", "已失效" not in douyin.cookie_hint())

douyin._cookie_header = lambda: "sessionid=x"
requests.get = lambda *a, **k: FakeResp({"status_code": 999})
ck("状态未知 -> 退回缺失提示", "未配置抖音 Cookie" in douyin.cookie_hint())

print("\n" + "=" * 60)
print("三、兼容与文案质量")
ck("DOUYIN_COOKIE_HINT 仍存在", hasattr(douyin, "DOUYIN_COOKIE_HINT"))
ck("旧引用仍可用", douyin.DOUYIN_COOKIE_HINT is douyin.COOKIE_MISSING_HINT)
ck("失效文案不含具体 Cookie 值", "839f263e" not in douyin.COOKIE_EXPIRED_HINT)
ck("缺失文案不含具体 Cookie 值", "839f263e" not in douyin.COOKIE_MISSING_HINT)

# 恢复真实实现
douyin._cookie_header = orig_header

print("\n" + "=" * 60)
print("四、真实网络下判定当前 Cookie")
import requests as _rq
_orig_get = _rq.get
_rq.get = _orig_get           # 清掉注入
try:
    print("  cookie_state() =", douyin.cookie_state())
except Exception as e:
    print("  探测异常:", type(e).__name__)

print("\n" + "=" * 60)
print(f"失败项: {len(fails)}")
for f in fails:
    print("  -", f)
print("=== 全部通过 ===" if not fails else "=== 有失败 ===")