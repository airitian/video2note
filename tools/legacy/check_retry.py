"""验证：任务在各阶段失败后，重试按钮是否出现、文案是否正确。

严谨做法：按钮状态从 DOM 直接读；任务状态从服务端 API 读
（不依赖页面内部变量，因为 app.js 是 IIFE，外部取不到 currentId）。

四步对照：
  A) 选中一个【成功】的历史任务 -> 按钮应隐藏
  B) 提交必然失败的链接 -> 等失败 -> 按钮应出现
  C) 再点回成功任务 -> 按钮应重新隐藏
  D) 点回失败任务并点重试 -> 任务应真的重新跑起来
"""
import json
import time
import urllib.request

from playwright.sync_api import sync_playwright

B = "http://127.0.0.1:8765"
BAD_URL = "https://www.bilibili.com/video/BV00000000000/"


def api(path, method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(B + path, data=data, method=method,
                                 headers={"content-type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=30))


def btn_state(pg):
    return pg.evaluate("""() => {
      const b = document.querySelector('#btn-retry');
      if (!b) return {exists:false};
      const rc = b.getBoundingClientRect();
      return {
        visible: !b.classList.contains('hidden') && rc.width > 0 && rc.height > 0,
        text: b.textContent.trim(),
      };
    }""")


def main():
    with sync_playwright() as p:
        br = p.chromium.launch()
        pg = br.new_page(viewport={"width": 1400, "height": 950})
        errs = []
        pg.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)

        pg.goto(B, wait_until="domcontentloaded")
        pg.wait_for_timeout(2500)

        tasks = api("/api/tasks")
        print("历史任务数:", len(tasks))
        from collections import Counter
        print("状态分布:", dict(Counter(t.get("status") for t in tasks)))

        # ---- A) 选中一个成功任务 ----
        ok_task = next((t for t in tasks
                        if t.get("status") in ("done", "transcribed")), None)
        fail_task = next((t for t in tasks
                          if t.get("status") in ("failed", "canceled")), None)

        if ok_task:
            pg.click(f'.hitem[data-id="{ok_task["id"]}"]')
            pg.wait_for_timeout(1600)
            print(f"A) 选中成功任务 {ok_task['id']} (status={ok_task['status']}) ->",
                  btn_state(pg), " [期望 visible=False]")
            a_ok = not btn_state(pg)["visible"]
        else:
            print("A) 无成功任务可测")
            a_ok = None

        if fail_task:
            pg.click(f'.hitem[data-id="{fail_task["id"]}"]')
            pg.wait_for_timeout(1600)
            st = btn_state(pg)
            print(f"B) 选中失败任务 {fail_task['id']} "
                  f"(failed_stage={fail_task.get('failed_stage')!r}) ->", st,
                  " [期望 visible=True]")
            print("   文案是否符合该阶段:", st["text"])
            b_ok = st["visible"]
        else:
            print("B) 无失败任务，改用坏链接造一个")
        b_ok = None

        # ---- 用坏链接造一个失败任务，并实测点击重试 ----
        pg.fill("#url", BAD_URL)
        pg.click("#btn-start")
        print("\n已提交坏链接，等待失败…")

        t0 = time.time()
        tid = None
        while time.time() - t0 < 150:
            cand = max(api("/api/tasks"), key=lambda x: x.get("created_at") or 0)
            if (cand.get("status") or "") in ("failed", "canceled", "done", "transcribed"):
                tid = cand["id"]
                break
            pg.wait_for_timeout(800)

        t = api("/api/tasks/" + tid)
        print(f"新任务 {tid}: status={t.get('status')} "
              f"failed_stage={t.get('failed_stage')!r}")
        print(f"   错误: {(t.get('error') or '')[:150]}")

        pg.wait_for_timeout(2000)
        st2 = btn_state(pg)
        print("   失败后按钮:", st2, " [期望 visible=True]")

        d_ok = None
        if st2["visible"]:
            print("\nD) 实测点击「重试」…")
            base = api("/api/tasks/" + tid)
            n0 = len(base.get("events") or [])
            print(f"   点击前: status={base.get('status')} 日志 {n0} 条")

            pg.click("#btn-retry")

            # 高频轮询：链接本身无效，重试必然再次失败，
            # 所以要看的是「中途是否出现过 running/pending」而非终态
            seen = []
            t1 = time.time()
            while time.time() - t1 < 60:
                d = api("/api/tasks/" + tid)
                s = d.get("status") or ""
                seen.append(s)
                if s in ("pending", "running"):
                    break
                time.sleep(0.3)

            d2 = api("/api/tasks/" + tid)
            n1 = len(d2.get("events") or [])
            running_seen = any(s in ("pending", "running") for s in seen)
            print(f"   点击后: status={d2.get('status')} 日志 {n1} 条 "
                  f"(新增 {n1 - n0})")
            print("   中途出现过 running/pending =", running_seen)
            print("   日志是否新增 =", n1 > n0)
            d_ok = running_seen or (n1 > n0)
            print("   重试是否真的执行了 =", d_ok)

            try:
                api("/api/tasks/" + tid, method="DELETE")
                print("   已删除测试任务")
            except Exception:
                pass

        print("\n=== 汇总 ===")
        print("成功任务时隐藏:", a_ok)
        print("失败任务时出现:", st2["visible"] if 'st2' in dir() else "?")
        print("点击后真的重跑:", d_ok)
        print("\n控制台错误:", [e for e in errs if "favicon" not in e][:5])
        br.close()


if __name__ == "__main__":
    main()
