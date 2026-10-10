# -*- coding: utf-8 -*-
"""布局实测：不同窗口宽度下，历史/主区/日志 三栏的实际相对位置。"""
from playwright.sync_api import sync_playwright

URL = "http://127.0.0.1:8765"
WIDTHS = [1600, 1400, 1280, 1180, 1100, 980, 900, 800]


def box(pg, sel):
    b = pg.query_selector(sel)
    if not b:
        return None
    return b.bounding_box()


def main():
    with sync_playwright() as p:
        br = p.chromium.launch()
        for w in WIDTHS:
            pg = br.new_page(viewport={"width": w, "height": 950})
            pg.goto(URL)
            pg.wait_for_timeout(1200)
            left = box(pg, "#side-left")
            mid = box(pg, "main")
            right = box(pg, "#side-right")
            if not (left and mid and right):
                print(f"{w:>5}px  缺元素")
                pg.close()
                continue
            log_right = right["x"] >= mid["x"] + mid["width"] - 2
            log_below = right["y"] >= mid["y"] + mid["height"] - 2
            hist_left = left["x"] + left["width"] <= mid["x"] + 2
            pos = "右侧" if log_right else ("下方" if log_below else "?")
            hl = "左侧" if hist_left else "上方"
            n = pg.eval_on_selector_all("#history-list .hitem", "e=>e.length")
            folded = pg.eval_on_selector("#side-left", "e=>e.classList.contains('folded')")
            print(
                f"{w:>5}px  历史={hl:<3}(默认{'折叠' if folded else '展开'},{n}条)  日志={pos:<3}"
                f"  (左x={left['x']:.0f} 中x={mid['x']:.0f}w={mid['width']:.0f}"
                f" 右x={right['x']:.0f}y={right['y']:.0f} 中底y={mid['y']+mid['height']:.0f})"
            )
            if w in (1280, 1100, 980):
                pg.screenshot(path=f"lay_{w}.png", full_page=False)
            pg.close()
        br.close()


if __name__ == "__main__":
    main()
