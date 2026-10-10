# -*- coding: utf-8 -*-
"""诊断：CSS 是否真正生效 + 各宽度下三栏的真实计算值 + 资源加载状态。"""
from playwright.sync_api import sync_playwright

URL = "http://127.0.0.1:8765"
WIDTHS = [1600, 1280, 1024, 900, 800, 720, 640]


def main():
    with sync_playwright() as p:
        br = p.chromium.launch()
        pg = br.new_page(viewport={"width": 1280, "height": 950})
        errs, res = [], []
        pg.on("console", lambda m: errs.append(f"{m.type}: {m.text}") if m.type == "error" else None)
        pg.on("response", lambda r: res.append((r.status, r.url.split("/")[-1])) if "/static/" in r.url else None)
        pg.goto(URL, wait_until="networkidle")
        pg.wait_for_timeout(1500)
        print("静态资源加载:", res)
        print("控制台错误:", errs or "无")
        cols = pg.eval_on_selector(".layout", "e=>getComputedStyle(e).gridTemplateColumns")
        print("1280px 下 .layout 计算列宽:", cols)
        print("body 宽:", pg.eval_on_selector("body", "e=>e.clientWidth"))
        pg.close()

        print("\n各宽度实测（含视口/文档宽，判断是否出现横向滚动或溢出）:")
        for w in WIDTHS:
            pg = br.new_page(viewport={"width": w, "height": 950})
            pg.goto(URL, wait_until="networkidle")
            pg.wait_for_timeout(900)
            cols = pg.eval_on_selector(".layout", "e=>getComputedStyle(e).gridTemplateColumns")
            b = {}
            for k, s in (("L", "#side-left"), ("M", "main"), ("R", "#side-right")):
                el = pg.query_selector(s)
                b[k] = el.bounding_box() if el else None
            doc_w = pg.evaluate("document.documentElement.clientWidth")
            scroll = pg.evaluate("document.documentElement.scrollWidth")
            same_row = b["L"] and b["R"] and abs(b["L"]["y"] - b["R"]["y"]) < 5 and b["R"]["x"] > b["L"]["x"]
            print(f"  {w:>5}px 视口={doc_w:<5} 文档宽={scroll:<5} 三栏同行={bool(same_row)}")
            print(f"        列: {cols}")
            print(f"        左x={b['L']['x']:.0f}w={b['L']['width']:.0f} | 中x={b['M']['x']:.0f}w={b['M']['width']:.0f} | 右x={b['R']['x']:.0f}w={b['R']['width']:.0f}")
            if w in (1024, 800):
                pg.screenshot(path=f"diag_{w}.png", full_page=False)
            pg.close()
        br.close()


if __name__ == "__main__":
    main()
