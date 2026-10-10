# -*- coding: utf-8 -*-
"""验收：按钮在链接输入框右侧同行；说明文案在下方单独一行。"""
from playwright.sync_api import sync_playwright

B = "http://127.0.0.1:8765"


def main():
    with sync_playwright() as p:
        br = p.chromium.launch()
        for w in (1400, 1024, 860):
            pg = br.new_page(viewport={"width": w, "height": 950})
            pg.goto(B, wait_until="domcontentloaded")  # SSE 长连接会让 networkidle 永不达成
            pg.wait_for_timeout(3000)
            r = pg.evaluate("""() => {
              const ta = document.querySelector('textarea#url').getBoundingClientRect();
              const bs = document.querySelector('#btn-start').getBoundingClientRect();
              const h  = document.querySelector('.opts-hint').getBoundingClientRect();
              return {ta_r: Math.round(ta.right), ta_y: Math.round(ta.y), ta_h: Math.round(ta.height),
                      b_x: Math.round(bs.x), b_y: Math.round(bs.y), b_h: Math.round(bs.height),
                      h_y: Math.round(h.y), h_x: Math.round(h.x),
                      docW: document.documentElement.scrollWidth,
                      winW: document.documentElement.clientWidth};
            }""")
            right = r["b_x"] >= r["ta_r"] - 2
            same_row = abs(r["b_y"] - r["ta_y"]) < 5
            hint_below = r["h_y"] >= r["b_y"] + r["b_h"] - 2
            print(f"{w}px 按钮右侧={right} 与输入框同行={same_row} 按钮高={r['b_h']} "
                  f"文案在按钮下方={hint_below} 横向溢出={r['docW'] > r['winW']}")
            pg.close()
        br.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
