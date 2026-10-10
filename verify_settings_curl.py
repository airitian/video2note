# -*- coding: utf-8 -*-
"""验证设置面板里resolver_curl 输入框是否真的渲染出来。

背景：用户反馈「抖音解析接口的请求模板」这项在设置面板里看不到。
配置层、接口层、前端渲染逻辑三处都查过没问题，所以必须以浏览器实际
渲染结果为准—— DOM 里有没有这个 field、textarea 是否可见、按钮在不在。
"""
import sys
import time

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
        pg = b.new_page(viewport={"width": 1600, "height": 1100})
        pg.goto(URL)
        pg.wait_for_timeout(2000)

        print("=" * 66)
        print("一、打开设置面板")
        print("=" * 66)
        pg.click("#btn-settings")
        pg.wait_for_selector("#settings-modal:not(.hidden)", timeout=5000)
        pg.wait_for_timeout(1200)
        pg.screenshot(path="ui_settings_1_open.png", full_page=True)

        # 所有渲染出来的字段
        fields = pg.eval_on_selector_all(
            "#settings-form .field",
            """els=>els.map(e=>{
                 const lab=e.querySelector('label');
                 const ctl=e.querySelector('[data-key]');
                 return {label: lab?lab.textContent:'',
                         key: ctl?(ctl.dataset.key||''):''};
               })""",
        )
        print("  面板内共 %d 个字段：" % len(fields))
        for f in fields:
            print(f"     - [{f['key']}] {f['label'][:44]}")

        print()
        print("=" * 66)
        print("二、resolver_curl 输入框")
        print("=" * 66)
        ck("面板已打开", not pg.query_selector("#settings-modal.hidden"))
        ck("渲染出了 resolver_curl 字段",
           any(f["key"] == "resolver_curl" for f in fields),
           [f["key"] for f in fields])

        ta = pg.query_selector('#settings-form [data-key="resolver_curl"]')
        ck("textarea 存在", ta is not None)
        if ta:
            ck("textarea 是 textarea 标签",
               ta.evaluate("e=>e.tagName") == "TEXTAREA",
               ta.evaluate("e=>e.tagName"))
            ck("textarea 在视口内可见",
               ta.is_visible())
            ck("textarea 有 rows（不是压扁的一行）",
               int(ta.get_attribute("rows") or 0) >= 6,
               ta.get_attribute("rows"))
            box = ta.bounding_box()
            ck("textarea 高度足够（>=100px，能粘整段 curl）",
               (box or {}).get("height", 0) >= 100,
               (box or {}).get("height"))
            ck("textarea 宽度撑满（不是窄条）",
               (box or {}).get("width", 0) >= 500,
               (box or {}).get("width"))

            ph = ta.get_attribute("placeholder") or ""
            print()
            print("  placeholder 全文：")
            for line in [ph[i:i + 44] for i in range(0, len(ph), 44)]:
                print("     ", line)
            ck("placeholder 含操作指引 F12", "F12" in ph)
            ck("placeholder 含『复制为 cURL』", "cURL" in ph)
            ck("placeholder 含留空说明", "留空" in ph)

        print()
        print("=" * 66)
        print("三、配套按钮与视觉标记")
        print("=" * 66)
        ck("有『清除』按钮", pg.query_selector('[data-clear="resolver_curl"]') is not None)
        ck("有『校验这段 curl』按钮",
           pg.query_selector('[data-parse-curl]') is not None)
        ck("有醒目标记 data-mark=curl",
           pg.query_selector('.field[data-mark="curl"]') is not None)
        st = pg.eval_on_selector(
            '#settings-form .field[data-mark="curl"] .secret-state',
            "e=>e?e.textContent:''")
        print("  状态提示:", st)
        ck("显示了配置状态提示", bool(st.strip()))

        print()
        print("=" * 66)
        print("三之二、分组与顺序（用户找不到的根因）")
        print("=" * 66)
        groups = pg.eval_on_selector_all(
            ".sgroup",
            """els=>els.map(e=>({
                 title: (e.querySelector('.sgroup-t')||{}).textContent || '',
                 keys: Array.from(e.querySelectorAll('[data-key]'))
                         .map(x=>x.dataset.key)
               }))""")
        print("  共 %d 个分组：" % len(groups))
        for g in groups:
            print(f"     ▸ {g['title']}: {g['keys']}")
        ck("渲染出了分组", len(groups) >= 3, len(groups))
        ck("首个分组是『平台登录态』",
           groups and groups[0]["title"] == "平台登录态",
           groups[0]["title"] if groups else None)
        ck("resolver_curl 在首个分组内",
           groups and "resolver_curl" in groups[0]["keys"],
           groups[0]["keys"] if groups else None)

        # 关键：它必须在首屏可见，不能要滚动才找得到
        if ta:
            vis = pg.evaluate(
                """() => {
                     const t = document.querySelector(
                         '#settings-form [data-key="resolver_curl"]');
                     if (!t) return null;
                     const r = t.getBoundingClientRect();
                     return {top: Math.round(r.top), bottom: Math.round(r.bottom),
                             vh: window.innerHeight,
                             onscreen: r.top < window.innerHeight && r.bottom > 0};
                   }""")
            print(f"  位置: top={vis['top']} bottom={vis['bottom']} 视口高={vis['vh']}")
            ck("打开设置面板即在首屏可见（无需滚动）", vis["onscreen"], vis)
            ck("位置靠前（top < 600px，说明排到了前部）", vis["top"] < 600, vis["top"])
            ta.scroll_into_view_if_needed()
            pg.wait_for_timeout(400)
            pg.screenshot(path="ui_settings_2_curl.png", full_page=True)

        print()
        print("=" * 66)
        print("四、校验按钮可用性（填一段合法 curl）")
        print("=" * 66)
        if ta:
            ta.fill("curl 'https://dlpanda.com/zh-CN' -X POST "
                    "-H 'x-requested-with: XMLHttpRequest' "
                    "--data-raw '_token=abc123&url=https%3A%2F%2Fv.douyin.com%2Fxyz%2F&t0ken=ttt'")
            pg.click("[data-parse-curl]")
            pg.wait_for_timeout(1500)
            msg = pg.eval_on_selector("#curl-check", "e=>e.textContent") \
                if pg.query_selector("#curl-check") else ""
            print("  校验结果:", msg)
            ck("校验按钮有反馈", bool(msg.strip()))
            ck("校验通过（✓ 开头）", msg.strip().startswith("✓"), msg)

            # 非法输入应报错
            ta.fill("这不是 curl 命令")
            pg.click("[data-parse-curl]")
            pg.wait_for_timeout(1500)
            msg2 = pg.eval_on_selector("#curl-check", "e=>e.textContent") \
                if pg.query_selector("#curl-check") else ""
            print("  非法输入结果:", msg2)
            ck("非法输入报错（✗ 开头）", msg2.strip().startswith("✗"), msg2)
            pg.screenshot(path="ui_settings_3_checked.png", full_page=True)

        b.close()

    print()
    print("=" * 66)
    print(f"结果：{PASS} 通过 / {FAIL} 失败")
    print("=" * 66)
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()