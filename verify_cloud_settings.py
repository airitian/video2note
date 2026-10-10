# -*- coding: utf-8 -*-
"""云机器部署场景的回归测试。

覆盖两类「设置项看不见」的原因：
  A. 字段存在但排在末尾 / 后端较新但前端较旧 —— 由 verify_settings_curl.py 覆盖
  B. /api/settings 拿不到 —— 本文件负责

B 类是最阴的：boot() 里 `try { settingsCache = await api(...) } catch (e) {}`
曾经把错误整个吞掉，settingsCache 保持为空对象，于是 openSettings 渲染出
**一个完全空白、且没有任何提示的面板**。用户看不到 curl 设置，也看不到原因，
只会以为「这个版本没有这个功能」。
"""
import sys

from playwright.sync_api import sync_playwright

URL = "http://127.0.0.1:8765"
PASS = FAIL = 0


def ck(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {extra}")


def main():
    with sync_playwright() as p:
        b = p.chromium.launch()

        # ---------- B：接口失败不能变成空白面板 ----------
        print("=" * 66)
        print("一、/api/settings 失败：必须显式报错，不能是空白面板")
        print("=" * 66)
        pg = b.new_page(viewport={"width": 1600, "height": 1000})
        pg.route("**/api/settings", lambda r: r.abort())
        pg.goto(URL)
        pg.wait_for_timeout(2000)
        pg.click("#btn-settings")
        pg.wait_for_timeout(1000)

        txt = pg.eval_on_selector("#settings-form", "e=>e.textContent.trim()")
        print(f"  表单区文本：{txt[:70]}...")
        ck("面板不是空白（有内容）", bool(txt.strip()), repr(txt[:50]))
        ck("明确告知是设置加载失败",
           "无法连接" in txt or "设置加载失败" in txt, txt[:60])
        ck("给出可操作指引（git pull / 重启）",
           "git pull" in txt or "重启" in txt, txt[:80])

        err_box = pg.query_selector(".sload-err")
        ck("错误提示有独立样式块", err_box is not None)
        if err_box:
            ck("错误提示在视口内可见", err_box.is_visible())
        # 保存按钮在字段缺失时不应误导用户以为能保存
        pg.screenshot(path="ui_cloud_settings_err.png", full_page=True)
        pg.close()

        # ---------- B2：恢复后重试应成功 ----------
        print()
        print("=" * 66)
        print("二、接口恢复后重试：点开面板应能拿到字段")
        print("=" * 66)
        pg2 = b.new_page(viewport={"width": 1600, "height": 1000})
        state = {"fail": True}

        def handler(route):
            if state["fail"]:
                route.abort()
            else:
                route.continue_()

        pg2.route("**/api/settings", handler)
        pg2.goto(URL)
        pg2.wait_for_timeout(1800)
        pg2.click("#btn-settings")
        pg2.wait_for_timeout(800)
        ck("失败时先显示错误", pg2.query_selector(".sload-err") is not None)

        # 后端恢复后再次点开（关掉再打开）
        state["fail"] = False
        pg2.keyboard.press("Escape")
        pg2.wait_for_timeout(300)
        pg2.click("#btn-settings")
        pg2.wait_for_timeout(1500)
        n2 = pg2.eval_on_selector_all("#settings-form .field", "els=>els.length")
        print(f"  恢复后字段数 = {n2}")
        ck("恢复后重新渲染出字段", n2 > 0, n2)
        ck("恢复后 curl 字段出现",
           pg2.query_selector('[data-key="resolver_curl"]') is not None)
        pg2.close()

        # ---------- A：旧后端无 groups 仍要兜底 ----------
        print()
        print("=" * 66)
        print("三、旧后端（不返回 groups）：不能整页空白")
        print("=" * 66)
        pg3 = b.new_page(viewport={"width": 1600, "height": 1000})
        pg3.route("**/api/settings", lambda r: r.fulfill(
            status=200, content_type="application/json",
            body='{"values":{"_configured":{"resolver_curl":true}},'
                 '"help":{"cookie_text":"【抖音】Cookie","resolver_curl":'
                 '"抖音解析接口的请求模板（可选）"}}'))
        pg3.goto(URL)
        pg3.wait_for_timeout(1800)
        pg3.click("#btn-settings")
        pg3.wait_for_timeout(800)
        n3 = pg3.eval_on_selector_all("#settings-form .field", "els=>els.length")
        print(f"  字段数 = {n3}（应为 2）")
        ck("无 groups 时兜底渲染单列", n3 == 2, n3)
        ck("旧后端下 curl 字段仍存在",
           pg3.query_selector('[data-key="resolver_curl"]') is not None)
        pg3.close()

        b.close()

    print()
    print("=" * 66)
    print(f"结果：{PASS} 通过 / {FAIL} 失败")
    print("=" * 66)
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()