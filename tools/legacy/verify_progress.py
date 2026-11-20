# -*- coding: utf-8 -*-
"""验证：进度行只在执行中显示；转写完成等终态下全部隐藏。"""
import json
import time
import urllib.request

from playwright.sync_api import sync_playwright

B = "http://127.0.0.1:8765"
URL = "https://www.bilibili.com/video/BV1ALeE6yEsw/?share_source=copy_web"


def api_get(p):
    return json.load(urllib.request.urlopen(B + p, timeout=30))


def rows_visible(pg):
    return pg.eval_on_selector_all(
        ".prow",
        "els=>els.filter(e=>!e.classList.contains('hidden')).map(e=>e.dataset.stage+':'+e.querySelector('.ppct').textContent)",
    )


def main():
    ok = True
    with sync_playwright() as p:
        br = p.chromium.launch()
        pg = br.new_page(viewport={"width": 1600, "height": 1000})

        # 1) 打开页面，默认选中最近一条已完成/转写完成任务 → 进度行应全部隐藏
        pg.goto(B, wait_until="networkidle")
        pg.wait_for_timeout(2500)
        vis = rows_visible(pg)
        print("1) 打开页面（选中历史终态任务）可见进度行:", vis or "无")
        ok &= not vis

        # 2) 通过页面 UI 提交新任务 → 执行中应依次显示
        pg.fill("#url", URL)
        pg.click("#btn-start")
        pg.wait_for_timeout(4000)
        vis = rows_visible(pg)
        print("2) 执行中可见进度行:", vis)
        ok &= len(vis) >= 1

        # 3) 等到终态 → 进度行应全部收回（id 从当前选中历史项取）
        tid = pg.evaluate("""() => {
            const el = document.querySelector('#history-list .hitem.on');
            return el ? el.dataset.id : '';
        }""")
        assert tid, "页面未选中任何任务"
        t0 = time.time()
        while time.time() - t0 < 420:
            d = api_get("/api/tasks/" + tid)
            if d.get("status") in ("done", "transcribed", "failed"):
                break
            time.sleep(2)
        pg.wait_for_timeout(2500)  # 等 SSE 推送 + 重绘
        vis = rows_visible(pg)
        print(f"3) 任务终态={d.get('status')} 可见进度行:", vis or "无")
        ok &= not vis
        br.close()

    print("结果:", "全部通过" if ok else "有失败项")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
