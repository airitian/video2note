# -*- coding: utf-8 -*-
"""实测：转写进行中，文字稿区是否逐次增量显示。"""
import time

from playwright.sync_api import sync_playwright

B = "http://127.0.0.1:8765"
URL = "https://www.bilibili.com/video/BV1ALeE6yEsw/?share_source=copy_web"


def main():
    with sync_playwright() as p:
        br = p.chromium.launch()
        pg = br.new_page(viewport={"width": 1600, "height": 1000})
        pg.goto(B, wait_until="domcontentloaded")
        pg.wait_for_timeout(2500)

        pg.fill("#url", URL)
        pg.click("#btn-start")
        pg.wait_for_timeout(2000)  # 等提交返回、前端切到新任务，避免读到上一条的残留状态

        seen = []
        t0 = time.time()
        while time.time() - t0 < 300:
            s = pg.evaluate("""() => {
                const lines = document.querySelectorAll('#lines .line:not(.pending-line)').length;
                const pend  = !!document.querySelector('#lines .pending-line');
                const st = document.querySelector('#ws-status');
                const rows = [...document.querySelectorAll('.prow')]
                    .filter(e=>!e.classList.contains('hidden'))
                    .map(e=>e.dataset.stage+':'+e.querySelector('.ppct').textContent);
                return {lines, pend, status:(st?st.innerText:'').trim(), rows};
            }""")
            key = (s["lines"], s["pend"], s["status"], str(s["rows"]))
            if not seen or seen[-1][0] != key:
                seen.append((key, round(time.time() - t0, 1)))
            if s["status"].startswith("转写完成") or s["status"].startswith("已完成"):
                break
            pg.wait_for_timeout(400)
        br.close()

    print("时间序列（行数为 文字稿区已出现的句子数）:")
    for (lines, pend, status, rows), ts in seen:
        print(f"  {ts:>6}s  句子={lines:<4} 识别中占位={pend}  状态={status:<18} {rows}")
    transcribing = [ts for (lines, pend, status, rows), ts in seen if "转写" in status]
    print("\n转写阶段采样点数:", len(transcribing))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
