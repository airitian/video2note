# -*- coding: utf-8 -*-
"""验证上传进度条：默认 0%，上传时按已传字节数走，传完停 100%。"""
from playwright.sync_api import sync_playwright

URL = "http://127.0.0.1:8765"
SRC = "data/media/0e39255d8a85/video.mp4"   # 158MB，本地上传也要几秒

with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={"width": 1600, "height": 1000})
    pg.goto(URL)
    pg.wait_for_timeout(2500)

    before = pg.eval_on_selector_all(".prow", "els=>els.map(e=>e.dataset.stage)")
    row = pg.eval_on_selector('[data-stage="upload"]', "e=>!e.classList.contains('hidden')")
    print("提交前 upload 行是否显示:", row, "（应 False）")

    pg.set_input_files("#file", SRC)
    pg.wait_for_timeout(500)
    pg.click("#btn-start")

    seen = []
    tid = None
    for _ in range(240):
        pg.wait_for_timeout(250)
        r = pg.eval_on_selector(
            '[data-stage="upload"]',
            "e=>e?{show:!e.classList.contains('hidden'),pct:e.querySelector('.ppct').textContent}:null",
        )
        if r and r["show"] and (not seen or seen[-1] != r["pct"]):
            seen.append(r["pct"])
        st = pg.eval_on_selector("#ws-status", "e=>e.textContent")
        if "提取" in st or "切片" in st or "转写" in st or "失败" in st:
            break
    print("上传过程观察到的百分比序列:", seen)
    # 158MB 要几秒才传完（服务器写盘反压），等到 100% 或阶段推进为止
    r2 = None
    for _ in range(120):
        pg.wait_for_timeout(500)
        r2 = pg.eval_on_selector(
            '[data-stage="upload"]',
            "e=>({show:!e.classList.contains('hidden'),pct:e.querySelector('.ppct').textContent})",
        )
        st2 = pg.eval_on_selector("#ws-status", "e=>e.textContent")
        if not r2["show"] or r2["pct"] == "100%" or "提取" in st2 or "转写" in st2:
            break
    print("上传完成后的终态:", r2)

    # 清理：删掉刚生成的任务，别让它占着转写额度
    import urllib.request
    import json as _j
    lst = _j.load(urllib.request.urlopen(URL + "/api/tasks", timeout=15))
    newest = max(lst, key=lambda t: t["created_at"])
    tid = newest["id"]
    if newest.get("status") in ("running", "pending"):
        try:
            req = urllib.request.Request(URL + "/api/tasks/%s/cancel" % tid, data=b"{}",
                                         headers={"content-type": "application/json"}, method="POST")
            urllib.request.urlopen(req, timeout=15)
        except Exception as e:
            print("取消失败:", e)
    print("已取消测试任务:", tid[:12], newest.get("status"))
    b.close()
