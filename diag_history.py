# -*- coding: utf-8 -*-
"""诊断：点击左侧历史每一条，检查 视频/文字稿/AI文稿 是否回显。"""
import json
import urllib.request

from playwright.sync_api import sync_playwright

B = "http://127.0.0.1:8765"


def main():
    tasks = json.load(urllib.request.urlopen(B + "/api/tasks", timeout=30))
    with_note = [t for t in tasks if (t.get("note") or "").strip()]
    print(f"共 {len(tasks)} 条历史，后端带 note 的有 {len(with_note)} 条")
    for t in with_note[:5]:
        print(f"   id={t['id']} status={t.get('status')} note长度={len(t['note'])} "
              f"note_info={t.get('note_info')}")

    with sync_playwright() as p:
        br = p.chromium.launch()
        pg = br.new_page(viewport={"width": 1600, "height": 1000})
        pg.goto(B, wait_until="networkidle")
        pg.wait_for_timeout(2500)

        ids = pg.eval_on_selector_all("#history-list .hitem", "els=>els.map(e=>e.dataset.id)")
        print(f"\n前端历史条目 {len(ids)} 条，逐条点击检查回显:")
        bad = 0
        for tid in ids[:8]:
            pg.click(f'#history-list .hitem[data-id="{tid}"]')
            pg.wait_for_timeout(1800)
            st = pg.evaluate("""() => {
                const nb = document.querySelector('#note-body');
                const meta = document.querySelector('#note-meta');
                const lines = document.querySelectorAll('#lines .line').length;
                const vid = !!document.querySelector('#video-host video');
                const txt = (nb.innerText || '').trim();
                return {note: txt.length, empty: !!nb.querySelector('.empty'),
                        meta: (meta.innerText||'').trim(), lines: lines, video: vid};
            }""")
            flag = ""
            if st["empty"]:
                flag = "  <-- 文稿区为空(未回显)"
                bad += 1
            print(f"   {tid}: note字数={st['note']:<6} 时间戳行={st['lines']:<4} "
                  f"视频={st['video']} 元信息='{st['meta']}'{flag}")
        br.close()
    print(f"\n未回显条数: {bad}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
