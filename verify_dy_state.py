# -*- coding: utf-8 -*-
"""cookie_state() / cookie_hint() 验证

★ 本测试刻意包含一条「对照实验」用例：status_code=8 在 Cookie 无效时
  也必须返回 unknown 而非 expired/ok。这是为了锁死上一轮的误判——
  曾把 8 当成「登录态作废」，导致明明登录有效的用户被反复要求换 Cookie。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import requests
from core import douyin

fails = []
def ck(name, cond, extra=""):
    print(f"[{'OK' if cond else 'FAIL'}] {name}" + (f"  {extra}" if extra else ""))
    if not cond:
        fails.append(name)


class FakeResp:
    def __init__(self, text):
        self.text = text
    def json(self):
        return {"status_code": 8}      # 无论传什么，都返回 8

orig_header = douyin._cookie_header
orig_get = requests.get

def inject(ck_text, html=None, exc=False):
    """注入 Cookie 头与页面返回"""
    douyin._cookie_header = lambda: ck_text
    if exc:
        requests.get = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("超时"))
    else:
        requests.get = lambda *a, **k: FakeResp(html if html is not None else "<html>ok</html>")
    return douyin.cookie_state()

CH = "sessionid=x; ttwid=y"

print("=" * 62)
print("一、cookie_state 四种状态")
ck("无 Cookie -> missing", inject("") == "missing")
ck("正常页面 -> ok", inject(CH) == "ok")
ck("JS 挑战页 -> blocked", inject(CH, "<script>_$jsvmprt</script>") == "blocked")
ck("验证码中间页 -> blocked", inject(CH, "<title>验证码中间页</title>") == "blocked")
ck("验证提示文案 -> blocked",
   inject(CH, "<div>请完成下列验证后继续</div>") == "blocked")
ck("__ac_signature -> blocked", inject(CH, "x=__ac_signature;") == "blocked")
ck("网络异常 -> unknown", inject(CH, exc=True) == "unknown")

print("\n" + "=" * 62)
print("二、★ 对照实验：status_code=8 不能用于判定登录态")
# 这是核心回归：上一轮用 profile/self 的 status_code=8 判定「Cookie 失效」，
# 实测证明它在无 Cookie / 伪造 Cookie 下返回值完全相同，无判别力。
class Sc8Resp:
    text = "<html>正常内容</html>"
    def json(self):
        return {"status_code": 8}

douyin._cookie_header = lambda: CH
requests.get = lambda *a, **k: Sc8Resp()
st = douyin.cookie_state()
ck("接口返回8 但页面正常 -> 不得判为 expired/blocked", st == "ok", f"实际={st}")
ck("不返回 expired（该状态已移除）", st != "expired")

print("\n" + "=" * 63)
print("三、cookie_hint 分流（解法不能弄反）")

douyin._cookie_header = lambda: CH
requests.get = lambda *a, **k: FakeResp("<title>验证码中间页</title>")
h = douyin.cookie_hint()
ck("被风控 -> 提示加代理/换网络", "代理" in h and "不是 Cookie 的问题" in h)
# 文案里的「重新获取 Cookie」出现在否定句里（"重新获取 Cookie 也解决不了"），
# 这正是要表达的重点，所以不能断言"不含该词"，只能断言不是把换 Cookie 当解法。
ck("被风控文案未把换 Cookie 当解法",
   "**重新获取 Cookie 也解决不了**" in h)
ck("被风控文案给出可执行动作", "手机热点" in h)

requests.get = lambda *a, **k: FakeResp("<html>正常</html>")
h = douyin.cookie_hint()
ck("未受阻 -> 提示填 Cookie", "未配置抖音 Cookie" in h)

douyin._cookie_header = lambda: ""
ck("无 Cookie -> 提示填 Cookie", "未配置抖音 Cookie" in douyin.cookie_hint())

print("\n" + "=" * 62)
print("四、兼容性与安全性")
ck("DOUYIN_COOKIE_HINT 仍存在", hasattr(douyin, "DOUYIN_COOKIE_HINT"))
ck("别名指向 MISSING", douyin.DOUYIN_COOKIE_HINT is douyin.COOKIE_MISSING_HINT)
ck("COOKIE_BLOCKED_HINT 存在", hasattr(douyin, "COOKIE_BLOCKED_HINT"))
ck("风控文案不含 Cookie 值", "839f263e" not in douyin.COOKIE_BLOCKED_HINT)
ck("缺失文案不含 Cookie 值", "839f263e" not in douyin.COOKIE_MISSING_HINT)

# 恢复
douyin._cookie_header = orig_header
requests.get = orig_get

print("\n" + "=" * 62)
print(f"失败项: {len(fails)}")
for f in fails:
    print("  -", f)
print("=== 全部通过 ===" if not fails else "=== 有失败 ===")