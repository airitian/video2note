# -*- coding: utf-8 -*-
"""UI 验证：进度行按需显示 / 下载完即出预览 / 三栏布局。跑完生成截图供人眼确认。"""
import time

from playwright.sync_api import sync_playwright

URL = "http://127.0.0.1:8765"


def main():
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 1600, "height": 1000})
        pg.goto(URL)
        pg.wait_for_timeout(2500)

        def rows():
            return pg.eval_on_selector_all(
                ".prow",
                "els=>els.map(e=>e.dataset.stage+':'+(!e.classList.contains('hidden')?'显示':'隐藏')"
                "+':'+e.querySelector('.ppct').textContent)",
            )

        print("初始（选中最近一条已完成任务）:")
        for r in rows():
            print("   ", r)
        pg.screenshot(path="ui_1_init.png", full_page=True)

        # 三栏是否都在
        sides = pg.eval_on_selector_all(
            "#side-left, #side-right", "els=>els.map(e=>e.id+':'+(e.getBoundingClientRect().left|0))"
        )
        print("侧栏位置:", sides)

        pg.fill("#url", "https://www.bilibili.com/video/BV1ALeE6yEsw/")
        pg.click("#btn-start")
        pg.wait_for_timeout(1500)
        print("刚提交（应该只露出已开始的阶段）:")
        for r in rows():
            print("   ", r)
        pg.screenshot(path="ui_2_start.png", full_page=True)

        t0 = time.time()
        got = False
        for _ in range(60):
            pg.wait_for_timeout(1000)
            got = pg.eval_on_selector("#video-host", "e=>!!e.querySelector('video,audio')")
            if got:
                break
        print("预览区出现媒体耗时: %.0fs" % (time.time() - t0))
        print("此时任务状态:", pg.eval_on_selector("#ws-status", "e=>e.textContent"))
        print("此时进度行:")
        for r in rows():
            print("   ", r)
        pg.screenshot(path="ui_3_video.png", full_page=True)

        # 等转写完，看日志栏是否有内容
        for _ in range(90):
            pg.wait_for_timeout(1000)
            st = pg.eval_on_selector("#ws-status", "e=>e.textContent")
            if "转写完成" in st or "已完成" in st or "失败" in st:
                break
        nlogs = pg.eval_on_selector_all("#ws-logs div", "els=>els.length")
        print("状态:", st, "| 右栏日志条数:", nlogs)
        pg.screenshot(path="ui_4_done.png", full_page=True)
        b.close()


if __name__ == "__main__":
    main()
