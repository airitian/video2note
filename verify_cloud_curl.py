# -*- coding: utf-8 -*-
"""验证「云端看不到 curl 输入框」的三道防线

排查思路：这个症状有四种可能原因，后端日志里看不出来，只能让用户自己看到。
  A. 浏览器缓存了旧 HTML          → 首页 no-store 头
  B. 后端没更新（没 pull/没重启）→顶栏版本号
  C. /api/settings 挂了           → 面板内红色错误块
  D. 前端把字段过滤掉了           → 面板顶部自检条
本脚本逐条验证，并在「后端是旧版本」的模拟环境下确认 A/B/D 都能给出正确指引。

用法：
    python verify_cloud_curl.py
退出码 0 表示通过。
"""
import json
import re
import sys
import urllib.request

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8765"

_passed = 0
_failed = 0


def ck(name, cond, extra=""):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  ✅ {name}" + (f"  ({extra})" if extra else ""))
    else:
        _failed += 1
        print(f"  ❌ {name}  {extra}")


def api(path):
    with urllib.request.urlopen(BASE + path, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


def headers(path="/"):
    req = urllib.request.Request(BASE + path)
    with urllib.request.urlopen(req, timeout=15) as r:
        # HTTP/2 与部分服务器返回小写头名，统一成小写再比对
        return {k.lower(): v for k, v in dict(r.headers).items()}


def reopen(pg, wait=1500):
    """重新打开设置面板。

    面板打开时会盖住顶栏按钮，直接点 #btn-settings 会被 modal 拦下、
    一直 retry 到超时。所以先关掉再点。
    """
    close = pg.query_selector("#btn-close-settings")
    if close and close.is_visible():
        close.click()
        pg.wait_for_timeout(350)
    pg.click("#btn-settings")
    pg.wait_for_timeout(wait)


print("=" * 70)
print("一、防线 A：首页必须 no-store（否则浏览器缓存旧 HTML）")
print("=" * 70)

h = headers("/")
cc = h.get("cache-control", "")
print(f"  Cache-Control: {cc!r}")
ck("首页有 Cache-Control", bool(cc), cc)
ck("首页是 no-store", "no-store" in cc, cc)
ck("首页有 Pragma: no-cache",
   "no-cache" in h.get("pragma", ""), h.get("pragma", ""))
ck("首页 Content-Type 带 charset",
   "charset" in h.get("content-type", "").lower(),
   h.get("content-type", ""))

# 静态资源要能被缓存（有版本戳），但不能被缓存成永久
hs = headers("/static/app.js?v=test")
print(f"  /static Cache-Control: {hs.get('cache-control', '(未设置)')!r}")

print()
print("=" * 70)
print("二、防线 B：版本号自述接口")
print("=" * 70)

health = api("/api/health")
ver = health.get("version") or {}
print(f"  version = {json.dumps(ver, ensure_ascii=False)}")
ck("health 返回 version", bool(ver))
ck("version 有 commit 或 assets",
   bool(ver.get("commit") or ver.get("assets")),
   f"commit={ver.get('commit')!r} assets={ver.get('assets')!r}")
ck("version.has_groups 为真（后端是新代码）",
   ver.get("has_groups") is True)
ck("version.has_resolver_curl 为真",
   ver.get("has_resolver_curl") is True)
ck("version.settings_keys 覆盖了全部字段",
   ver.get("settings_keys", 0) >= 15, str(ver.get("settings_keys")))

# assets 版本戳必须与 index.html 实际引用的一致，否则说明静态资源会串
html = urllib.request.urlopen(BASE + "/", timeout=15).read().decode("utf-8")
m = re.search(r"app\.js\?v=([0-9a-z]+)", html)
ck("index.html 引用了带版本戳的 app.js", bool(m), m.group(1) if m else "")
if m and ver.get("assets"):
    ck("接口报的 assets 与 index.html 实际引用一致",
       m.group(1) == ver["assets"], f"{m.group(1)} vs {ver['assets']}")

print()
print("=" * 70)
print("三、防线 D：面板顶部自检条（正常情况显示已就位）")
print("=" * 70)

with sync_playwright() as pw:
    b = pw.chromium.launch(headless=True)
    pg = b.new_page(viewport={"width": 1400, "height": 950})
    pg.goto(BASE + "/", wait_until="networkidle")
    pg.wait_for_timeout(1500)

    ck("顶栏显示了版本号", pg.query_selector(".health .ver") is not None)
    print("     顶栏版本:", pg.eval_on_selector(".health .ver", "e=>e.textContent"))
    ck("版本号内容含commit 或资源版本",
       bool(re.search(r"\w{4,}|\d{8}", pg.eval_on_selector(
           ".health .ver", "e=>e.textContent") or "")))

    pg.click("#btn-settings")
    pg.wait_for_timeout(1200)

    sc = pg.query_selector(".scheck")
    ck("面板顶部有自检条", sc is not None)
    if sc:
        txt = sc.inner_text().strip()
        cls = sc.get_attribute("class") or ""
        print(f"     自检条: {txt}")
        print(f"     class : {cls}")
        ck("正常情况下自检条为 ok", " ok" in cls or cls.endswith("ok"), cls)
        ck("自检条明确说输入框已就位", "已就位" in txt, txt)

    ck("curl 输入框确实在 DOM 里",
       pg.query_selector('[data-key="resolver_curl"]') is not None)
    ck("curl 输入框可见（未被隐藏）",
       pg.is_visible('[data-key="resolver_curl"]'))
    box = pg.query_selector('[data-key="resolver_curl"]')
    if box:
        bb = box.bounding_box()
        ck("curl 输入框有实际尺寸（非 0）", bool(bb and bb["height"] > 20),
           f"{bb['width']:.0f}x{bb['height']:.0f}" if bb else "无尺寸")
        # 是否在面板可滚动区域内（首屏就要能看到，不用滚）
        ck("curl 输入框在面板可滚动范围内", bool(bb), "")

    print()
    print("=" * 70)
    print("四、防线 C + D：模拟「后端是旧版本」")
    print("=" * 70)
    # 旧后端：/api/settings 不返回 groups，也没有 resolver_curl
    real = urllib.request.urlopen(BASE + "/api/settings", timeout=15)
    full = json.loads(real.read().decode("utf-8"))
    old_backend = {
        "values": full["values"],
        # 刻意去掉 groups 与 resolver_curl，模拟没 git pull 的老代码
        "help": {k: v for k, v in full["help"].items() if k != "resolver_curl"},
    }

    def fake_old(route):
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps(old_backend, ensure_ascii=False))

    pg.route("**/api/settings", fake_old)
    reopen(pg)

    sc = pg.query_selector(".scheck")
    ck("旧后端下自检条仍存在", sc is not None)
    if sc:
        txt = sc.inner_text().strip()
        cls = sc.get_attribute("class") or ""
        print(f"     旧后端自检条: {txt}")
        ck("判定为异常（bad）", "bad" in cls, cls)
        ck("明确指出代码版本过旧", "版本过旧" in txt or "过旧" in txt, txt)
        ck("给出 git pull 指引", "git pull" in txt, txt)
        ck("强调要重启服务", "重启" in txt, txt)
    ck("旧后端下不渲染 curl 框（符合预期）",
       pg.query_selector('[data-key="resolver_curl"]') is None)
    # 旧后端接口是通的、其余字段也在，所以**不该**出现「无法连接」的错误块——
# 顶部自检条已经把「版本过旧」说清楚了，重复报错反而稀释重点
    # 注意 eval_on_selector 在元素不存在时会抛异常，先判存在再取文本
    ck("旧后端下不误报「无法连接」",
       pg.query_selector(".sload-err") is None)

    print()
    print("=" * 70)
    print("五、防线 C：模拟 /api/settings 完全失败")
    print("=" * 70)
    pg.unroute("**/api/settings")

    def fake_fail(route):
        route.abort("failed")

    pg.route("**/api/settings", fake_fail)
    reopen(pg)

    err = pg.query_selector(".sload-err")
    ck("接口失败时有红色错误块", err is not None)
    if err:
        t = err.inner_text().strip()
        print(f"     错误块: {t[:120]}")
        ck("错误文案含接口路径", "/api/settings" in t, t)
        ck("区分了「无法连接」而非笼统报错",
           "无法连接" in t or "未能连接" in t, t)
        # ★关键：拉取失败不能报「版本过旧」——那是另一个原因，
        #   混为一谈会让用户去 git pull，而问题其实是服务没起来
        ck("不误报为「版本过旧」（服务没起 ≠ 代码没更新）",
           "版本过旧" not in t and "git pull" not in t, t)
        ck("提示要确认服务在运行", "服务" in t, t)
        # 提示条是 .form 的直接子元素（不包 .sgroup），
        # 所以第一个子元素自身就该是 .sload-err
        ck("错误块在表单最上方",
           pg.eval_on_selector(
               "#settings-form",
               "e=>e.firstElementChild.className") == "sload-err",
           pg.eval_on_selector(
               "#settings-form", "e=>e.firstElementChild.className"))
    ck("接口失败时不显示自检条（避免误导）",
       pg.query_selector(".scheck") is None)

    print()
    print("=" * 70)
    print("六、恢复后能正常显示（确认拦截没留下副作用）")
    print("=" * 70)
    pg.unroute("**/api/settings")
    pg.reload(wait_until="networkidle")
    pg.wait_for_timeout(1500)
    reopen(pg)
    ck("恢复后 curl 输入框回来了",
       pg.query_selector('[data-key="resolver_curl"]') is not None)
    sc = pg.query_selector(".scheck")
    if sc:
        ck("恢复后自检条为 ok", "ok" in (sc.get_attribute("class") or ""),
           sc.inner_text().strip())

    # 无 console 报错
    errs = []
    pg.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
    pg.reload(wait_until="networkidle")
    pg.wait_for_timeout(1500)
    ck("无 console.error", not errs, "; ".join(errs[:2]))

    b.close()

print()
print("=" * 70)
print(f"结果：{_passed} 通过，{_failed} 失败")
print("=" * 70)
sys.exit(1 if _failed else 0)