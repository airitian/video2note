# -*- coding: utf-8 -*-
"""验证：点历史记录能回显 AI 文稿；切换风格 chip 时文稿跟着切换（不重复调用 LLM）。"""
import time

from playwright.sync_api import sync_playwright

B = "http://127.0.0.1:8765"
URL = "https://www.bilibili.com/video/BV1ALeE6yEsw/?share_source=copy_web"


def wait_polish_ready(pg, timeout=300):
    t = time.time()
    while time.time() - t < timeout:
        if pg.eval_on_selector("#btn-polish", "e=>!e.disabled"):
            return True
        pg.wait_for_timeout(1000)
    return False


def note_sig(pg):
    return pg.evaluate("""() => {
        const nb = document.querySelector('#note-body');
        if (!nb) return {len:0, head:'', empty:false};
        const t = (nb.innerText || '').trim();
        return {len: t.length, head: t.slice(0, 30), empty: !!nb.querySelector('.empty')};
    }""")


def main():
    ok = True
    with sync_playwright() as p:
        br = p.chromium.launch()
        pg = br.new_page(viewport={"width": 1600, "height": 1000})
        pg.goto(B, wait_until="networkidle")
        pg.wait_for_timeout(2000)

        # 1) 提交任务，等到生成按钮可用（转写完成）
        pg.fill("#url", URL)
        pg.click("#btn-start")
        t0 = time.time()
        while time.time() - t0 < 420:
            pg.wait_for_timeout(1500)
            if pg.eval_on_selector("#btn-polish", "e=>!e.disabled"):
                break
        print(f"1) 转写完成，用时 {time.time()-t0:.0f}s，生成按钮可用=True")
        ok &= pg.eval_on_selector("#btn-polish", "e=>!e.disabled")

        # 2) 生成 A 风格（整理文稿）
        pg.click('.style-chip[data-style="general"]')
        assert wait_polish_ready(pg), "生成按钮一直不可用"
        pg.click("#btn-polish")
        a = None
        t1 = time.time()
        while time.time() - t1 < 300:
            pg.wait_for_timeout(1500)
            s = note_sig(pg)
            if not s["empty"] and s["len"] > 100:
                a = s
                break
        print(f"2) A 风格成稿：{a['len']} 字  开头={a['head'][:24]!r}")
        ok &= bool(a)

        # 3) 切到 B 风格并生成
        pg.click('.style-chip[data-style="note"]')
        assert wait_polish_ready(pg), "生成按钮一直不可用(B风格)"
        pg.click("#btn-polish")
        b = None
        t2 = time.time()
        while time.time() - t2 < 300:
            pg.wait_for_timeout(1500)
            s = note_sig(pg)
            if not s["empty"] and s["len"] > 100 and s["head"] != a["head"]:
                b = s
                break
        print(f"3) B 风格成稿：{b['len']} 字  开头={b['head'][:24]!r}")
        ok &= bool(b)

        # 4) 关键：切回 A 风格，应立即回显 A 的内容（不重新生成）
        pg.click('.style-chip[data-style="general"]')
        pg.wait_for_timeout(1200)
        back = note_sig(pg)
        same = back["head"] == a["head"] and back["len"] == a["len"]
        print(f"4) 切回 A 风格：{back['len']} 字  与A一致={same}")
        ok &= same

        # 5) 切到别的历史项再切回来，当前风格的文稿仍在
        ids = pg.eval_on_selector_all("#history-list .hitem", "els=>els.map(e=>e.dataset.id)")
        cur = pg.evaluate("()=>{const e=document.querySelector('#history-list .hitem.on');return e?e.dataset.id:''}")
        other = next((i for i in ids if i != cur), "")
        if other:
            pg.click(f'#history-list .hitem[data-id="{other}"]')
            pg.wait_for_timeout(1500)
            pg.click(f'#history-list .hitem[data-id="{cur}"]')
            pg.wait_for_timeout(2200)
            again = note_sig(pg)
            ok2 = again["head"] == a["head"] and again["len"] == a["len"]
            print(f"5) 切走再切回：{again['len']} 字  与A一致={ok2}")
            ok &= ok2
        br.close()

    print("结果:", "全部通过" if ok else "有失败项")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
