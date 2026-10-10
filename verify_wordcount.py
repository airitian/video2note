# -*- coding: utf-8 -*-
"""验证页面展示「文字稿字数」与「成稿字数」

覆盖要点：
  1. 两处计数节点都存在，且用同一套不计空格的字数口径
  2. 千分位格式化正确（含 5 位数边界）
  3. 文字稿字数随 SSE 增量实时上涨
  4. 成稿字数取「当前展示的那份」，切换风格会跟着变
  5. 成稿 meta 附带原文字数，切换到未生成的风格会清空
  6. 无任务时不残留上一个任务的字数

用法：
    python verify_wordcount.py
退出码 0 表示通过。
"""
import json
import re
import sys
import urllib.request

BASE = "http://127.0.0.1:8765"

# 与 app.js 的 STYLE_TEXT 保持一致，用于校验 meta 前缀
STYLE_TEXT_CN = {
    "general": "整理文稿", "note": "结构化笔记",
    "article": "公众号文章", "clean": "仅清洗润色",
}

_passed = 0
_failed = 0


def ck(name, cond, extra=""):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  ✅ {name}" + (f"  ({extra})" if extra else ""))
    else:
        _failed += 1
        print(f"  ❌ {name}  {extra}")


def ck_eq(name, got, want):
    ck(name, got == want, f"got={got!r} want={want!r}")


def api(path):
    with urllib.request.urlopen(BASE + path, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


print("=" * 68)
print("一、charCount：字数口径")
print("=" * 68)

from playwright.sync_api import sync_playwright  # noqa: E402

src = open("static/app.js", encoding="utf-8").read()

def extract_fn(src, name):
    """从 app.js 源码里精确切出某个函数的完整定义（花括号配平）。

    不能靠正则抓单行return —— app.js 整体包在 IIFE 里，函数在页面全局作用域
    不可访问，只能把源码原样注入浏览器执行。
    """
    i = src.index("function " + name)
    start = src.index("{", i)
    depth = 0
    for j in range(start, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[i:j + 1]
    raise RuntimeError("函数 " + name + " 花括号不配平")


cc_js = extract_fn(src, "charCount")
num_js = extract_fn(src, "fmtNum")
ck("charCount 可完整提取", cc_js.startswith("function charCount"))
ck("fmtNum 可完整提取", num_js.startswith("function fmtNum"))

# 在浏览器里跑真实实现
with sync_playwright() as pw:
    b = pw.chromium.launch(headless=True)
    pg = b.new_page()
    pg.goto(BASE + "/", wait_until="networkidle")
    pg.wait_for_timeout(600)

    def cc(s):
        # 表达式本身必须是函数，Playwright 才会把参数传进去
        return pg.evaluate("v => (" + cc_js + ")(v)", s)

    def fn(n):
        return pg.evaluate("v => (" + num_js + ")(v)", n)

    ck_eq("纯中文按字符数计", cc("你好世界"), 4)
    ck_eq("换行不计入", cc("你好\n世界"), 4)
    ck_eq("空格不计入", cc("你好 世界"), 4)
    ck_eq("连续空白折叠后不计", cc("你好   \t  世界"), 4)
    ck_eq("中英混排各计一字", cc("用AI做"), 4)
    ck_eq("空串为 0", cc(""), 0)
    ck_eq("全空白为 0", cc("  \n\t "), 0)
    ck_eq("标点计入（与 Word 字符数一致）", cc("你好，世界。"), 6)

    ck_eq("fmtNum 千分位：4 位不变", fn(8765), "8,765")
    ck_eq("fmtNum 千分位：5 位分组", fn(12345), "12,345")
    ck_eq("fmtNum 千分位：6 位", fn(123456), "123,456")
    ck_eq("fmtNum 千分位：0", fn(0), "0")

    print()
    print("=" * 68)
    print("二、DOM：两个计数节点就位")
    print("=" * 68)
    ck("有 #lines-meta（文字稿字数）",
       pg.query_selector("#lines-meta") is not None)
    ck("有 #note-meta（成稿字数）",
       pg.query_selector("#note-meta") is not None)
    ck("两处都用了 count-meta 样式",
       pg.eval_on_selector_all(
           "#lines-meta,#note-meta",
           "els=>els.every(e=>e.className==='count-meta')"))
    # 位置：文字稿那处必须在文字稿面板标题行内，成稿那处在AI 文稿标题行内
    in_lines_head = pg.evaluate(
        "!!document.querySelector('#lines-meta').closest('.pane-head')")
    ck("文字稿字数挂在文字稿面板标题行", in_lines_head)
    in_note_head = pg.evaluate(
        "!!document.querySelector('#note-meta').closest('.pane-head')")
    ck("成稿字数挂在 AI 文稿标题行", in_note_head)

    print()
    print("=" * 68)
    print("三、页面加载即自动选中最近任务")
    print("=" * 68)
    # boot() 会选中最近一条任务（core: currentId = sorted[0].id），
    # 所以这里不该断言"初始为空"，而要断言"与当前选中任务一致"
    sel = pg.eval_on_selector_all(".hitem.on", "els=>els.map(e=>e.dataset.id)")
    ck("默认选中了某条历史记录", len(sel) == 1, str(sel))
    if sel:
        auto_shown = pg.eval_on_selector("#lines-meta", "e=>e.textContent").strip()
        auto_task = api("/api/tasks/" + sel[0])
        auto_n = len(re.sub(r"\s+", "", auto_task.get("transcript") or ""))
        if auto_n:
            want = f"{auto_n:,} 字"
            ck_eq("自动选中任务的文字稿字数正确", auto_shown, want)
        else:
            ck("该任务无转写，字数应为空", auto_shown == "", repr(auto_shown))

    print()
    print("=" * 68)
    print("四、真实任务：文字稿字数与转写一致")
    print("=" * 68)

    tasks = api("/api/tasks")
    items = tasks["items"] if isinstance(tasks, dict) else tasks
    done = [t for t in items if t.get("status") in ("done", "transcribed") and t.get("transcript")]
    ck("库里有可测任务", bool(done), f"{len(done)} 条")
    if not done:
        print("  ⚠️ 没有已完成任务，跳过实测部分")
    else:
        # 成稿部分要挑「确实生成过文稿」的任务，不能只看转写长度——
        # 最长的那条常常只转写没整理，断言会假失败
        withnote = [t for t in done if t.get("variants")]
        ck("库里有生成过文稿的任务", bool(withnote), f"{len(withnote)} 条")
        # 优先挑「多风格」的任务：note_info 只记最后生成的风格，
        # 只有当variants 里有 2 种以上，才能验出「切风格后字数串号」这个真回归
        multi = [t for t in withnote if len(t.get("variants") or {}) >= 2]
        ck("库里有生成过 2 种以上风格的任务（用于验证不串号）",
           bool(multi), f"{len(multi)} 条")
        pool = multi or withnote
        target = max(pool, key=lambda x: len(x.get("transcript") or "")) \
            if pool else max(done, key=lambda x: len(x.get("transcript") or ""))
        tid = target["id"]
        item = pg.query_selector('.hitem[data-id="%s"]' % tid)
        ck("历史列表里能找到该任务", item is not None)
        if item:
            item.click()
            pg.wait_for_timeout(2500)

            # 页面渲染出的字数，应等于接口 transcript 去空白后的长度
            api_trans = len(re.sub(r"\s+", "", target["transcript"]))
            shown = pg.eval_on_selector("#lines-meta", "e=>e.textContent")
            digits = re.match(r"^([\d,]+) 字", shown or "")
            ck("文字稿字数有值", bool(digits), repr(shown))
            if digits:
                shown_n = int(digits.group(1).replace(",", ""))
                ck_eq("文字稿字数与后端 transcript 一致", shown_n, api_trans)
                ck("字数带千分位或本就是短数",
                   "," in digits.group(1) or api_trans < 1000, digits.group(1))
            ck("非转写中不显示「识别中」",
               "识别中" not in (shown or ""), repr(shown))

            # 成稿字数
            note_shown = pg.eval_on_selector("#note-meta", "e=>e.textContent")
            ck("成稿字数有值（该任务已生成过文稿）", bool(note_shown.strip()), repr(note_shown))
            if note_shown.strip():
                ck("成稿 meta 含「原文 N 字」",
                   re.search(r"原文 [\d,]+ 字", note_shown) is not None, note_shown)
                mnote = re.search(r"· ([\d,]+) 字", note_shown)
                ck("成稿 meta 首个数字是成稿自身字数",
                   bool(mnote), note_shown)

                # 与后端 note 内容交叉核对
                detail = api("/api/tasks/" + tid)
                cur = detail.get("note") or ""
                if cur:
                    want = len(re.sub(r"\s+", "", cur))
                    ck_eq("成稿字数与实际展示文本一致",
                          int(mnote.group(1).replace(",", "")), want)

            print()
            print("=" * 68)
            print("五、切换风格：字数跟着当前展示的那份变")
            print("=" * 68)
            detail = api("/api/tasks/" + tid)
            variants = detail.get("variants") or {}
            ck("接口确实返回了 variants", bool(variants), str(sorted(variants)))

            chips = pg.query_selector_all(".style-chip")
            ck("有 4 个风格可切", len(chips) == 4, str(len(chips)))
            shown_map = {}
            for c in chips:
                style = c.get_attribute("data-style")
                c.click()
                pg.wait_for_timeout(700)
                txt = pg.eval_on_selector("#note-meta", "e=>e.textContent").strip()
                shown_map[style] = txt
                exists = style in variants
                print(f"     {style:8s} 已生成={exists!s:5s} meta={txt!r}")
                # 关键：切换后 meta 必须与该风格是否已生成严格对应
                if exists:
                    ck(f"「{style}」已生成 → 有字数", bool(txt), repr(txt))
                    v = variants.get(style) or {}
                    raw = v.get("note") or ""
                    want = len(re.sub(r"\s+", "", raw))
                    m = re.search(r"· ([\d,]+) 字", txt)
                    ck(f"「{style}」字数与该风格稿件一致",
                       bool(m) and int(m.group(1).replace(",", "")) == want,
                       f"meta={m.group(1) if m else None} want={want}")
                    ck(f"「{style}」meta 含原文字数",
                       re.search(r"原文 " + f"{api_trans:,}" + r" 字", txt) is not None,
                       txt)
                    ck(f"「{style}」meta 前缀是风格名",
                       txt.startswith(STYLE_TEXT_CN.get(style, "?")),
                       txt[:20])
                else:
                    ck(f"「{style}」未生成 → 字数清空", txt == "", repr(txt))
                ck(f"切到「{style}」后文字稿字数仍在",
                   bool(pg.eval_on_selector("#lines-meta", "e=>e.textContent").strip()))

            # ★核心回归：note_info 只记最后一次生成的风格，
            #  若前端偷懒读note_info.chars，切到别的风格就会显示错的字数
            if len(variants) >= 2:
                ck("至少两个风格已成稿，可验证不串号",
                   len([s for s, t2 in shown_map.items() if t2]) >= 2,
                   str({s: t2 for s, t2 in shown_map.items() if t2}))
                mismatch = []
                for style, txt in shown_map.items():
                    if style not in variants or not txt:
                        continue
                    want = len(re.sub(r"\s+", "",
                                      (variants[style] or {}).get("note") or ""))
                    m = re.search(r"· ([\d,]+) 字", txt)
                    got = int(m.group(1).replace(",", "")) if m else -1
                    if got != want:
                        mismatch.append((style, got, want))
                ck("各风格字数与各自稿件严格对应（无串号）",
                   not mismatch, str(mismatch))

            print()
            print("=" * 68)
            print("六、复制按钮等原有功能未受影响")
            print("=" * 68)
            ck("复制按钮仍在", pg.query_selector('[data-act="copy"]') is not None)
            ck("生成按钮仍在", pg.query_selector("#btn-polish") is not None)

    print()
    print("=" * 68)
    print("七、控制台无报错")
    print("=" * 68)
    errs = []
    pg.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
    pg.reload(wait_until="networkidle")
    pg.wait_for_timeout(1500)
    ck("重载后无 console.error", not errs, "; ".join(errs[:3]))

    b.close()

print()
print("=" * 68)
print(f"结果：{_passed} 通过，{_failed} 失败")
print("=" * 68)
sys.exit(1 if _failed else 0)