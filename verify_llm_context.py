# -*- coding: utf-8 -*-
"""术语抽样 + 块间前文摘要 的验证（不调用真实 LLM，用 mock 拦截 prompt）

验三件事：
  1. _sample_terms 覆盖全文（正中间的专有名词必须能被抽到）
  2. 分批推进时，第 2 批及以后的 prompt 里带上了上一批的清洗结果
  3. 第 1 批的 prompt 不带 prev（没有上文就不该有占位噪音）
  4. CLEAN_TMPL 的占位符齐全，format 不炸
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from core import llm

PASS = FAIL = 0


def ck(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {extra}")


print("=" * 66)
print("=" * 66)
print("一、术语抽样：均匀覆盖全文各处")
print("=" * 66)

# 造长稿，每段带唯一编号，用来确认抽样落在不同位置
n = 500
long_text = "".join(f"第{i}段内容，讨论分布式系统的各种话题，包括一致性、可用性与容错。\n"
                    for i in range(n))
ck("测试稿够长（>8000）", len(long_text) > llm.TERMS_TOTAL_CHARS, len(long_text))

s = llm._sample_terms(long_text)
segs = [x for x in s.split("\n...\n") if x.strip()]
ck("抽样结果非空", bool(s.strip()))
ck(f"取到 {llm.TERMS_SAMPLES} 段左右", len(segs) >= llm.TERMS_SAMPLES - 1, len(segs))

nums = [int(re.search(r"第(\d+)段", seg).group(1)) for seg in segs
        if re.search(r"第(\d+)段", seg)]
ck("每段都能定位回原文", len(nums) == len(segs), f"{len(nums)}/{len(segs)}")
ck("各段位置互不相同（真均匀，非重复取同一处）",
   len(set(nums)) == len(nums), sorted(set(nums)))
ck("覆盖开头（首段编号接近 0）", min(nums) < n * 0.1, min(nums))
ck("覆盖结尾（末段编号接近末尾）", max(nums) > n * 0.85, max(nums))
ck("覆盖正中间（约 40%-60% 区间有段）",
   any(n * 0.35 <= x <= n * 0.65 for x in nums), sorted(nums))

if len(nums) >= 3:
    gaps = [b - a for a, b in zip(sorted(nums), sorted(nums)[1:])]
    avg = sum(gaps) / len(gaps)
    ck("段间距大致均匀（极差不超过均值的 40%）",
       max(gaps) - min(gaps) <= avg * 0.4, f"gaps={gaps} avg={avg:.0f}")

# 对照：旧的「首 3000 + 尾 2000」覆盖不到正中间
old = long_text[:3000] + "\n...\n" + long_text[-2000:]
old_nums = [int(m.group(1)) for m in re.finditer(r"第(\d+)段", old)]
old_mid = [x for x in old_nums if n * 0.4 <= x <= n * 0.6]
ck("对照：旧方式覆盖不到正中间（证明改动有效）", not old_mid, old_mid)

short = "很短的一段话。"
ck("短于阈值时原样返回", llm._sample_terms(short) == short)

ck("抽样总长接近但不超预算",
   len(s) <= llm.TERMS_TOTAL_CHARS * 1.3, f"实际 {len(s)}")
ratio = sum(len(x) for x in segs) / len(long_text)
ck("覆盖率达到可用水位（>= 25%）", ratio >= 0.25, f"{ratio:.1%}")

print("二、_carry_block 前文摘要块")
print("=" * 66)

ck("空列表 -> 空串", llm._carry_block([]) == "")
ck("全空白的列表 -> 空串", llm._carry_block(["", "   "]) == "")

blk = llm._carry_block(["前面已经清洗好的内容abc"])
ck("非空时带标记", "上文摘要" in blk)
ck("非空时带原文尾部", "前面已经清洗好的内容abc" in blk)

long_done = ["X" * 3000]
blk2 = llm._carry_block(long_done)
ck(f"超长时截断到 {llm.CARRY_CHARS} 上下", len(blk2) < llm.CARRY_CHARS + 120, len(blk2))
ck("截断时保留最新内容（尾部）", "X" in blk2)
ck("截断时带省略号提示", "…" in blk2)

# 保留最近的一段，而不是最早的那段
blk3 = llm._carry_block(["第一段AAA", "最后一段ZZZ"])
ck("多段时优先携带最近的尾部", "ZZZ" in blk3)
ck("过老的段落在截断后可被丢弃", len(blk3) < len("第一段AAA") + 200, len(blk3))

print()
print("=" * 66)
print("三、prompt 占位符完整（format 不炸）")
print("=" * 66)

for name in ("CLEAN_TMPL", "TERMS_TMPL", "NOTE_TMPL", "ARTICLE_TMPL"):
    tpl = getattr(llm, name)
    fields = {f for f in re.findall(r"\{(\w+)\}", tpl)}
    ck(f"{name} 占位符可识别", bool(fields) or name == "X", sorted(fields))

ck("CLEAN_TMPL 含 prev 占位符", "{prev}" in llm.CLEAN_TMPL)
ck("CLEAN_TMPL 仍含 chunk/terms/title/idx/total",
   all(f"{{{f}}}" in llm.CLEAN_TMPL for f in ("chunk", "terms", "title", "idx", "total")))

rendered = llm.CLEAN_TMPL.format(title="T", idx=1, total=3, terms="无",
                                  prev="\n上文摘要：AAA\n", chunk="CCC")
ck("format 后无残留占位符", "{" not in rendered and "}" not in rendered)
ck("渲染结果含 prev 内容", "AAA" in rendered)
ck("渲染结果含 chunk 内容", "CCC" in rendered)
ck("首块 prev 为空时也能渲染",
   "{" not in llm.CLEAN_TMPL.format(title="T", idx=1, total=3, terms="无",
                                     prev="", chunk="C"))

print()
print("=" * 66)
print("四、polish 分批推进：第 2 批要带上第 1 批的结果")
print("=" * 66)

# 必须造出「超过一个批次」的块数，否则测不到批间传递。
# 并发度默认 4，所以块数要 > 4。
para = "这是用于验证分批推进机制的测试段落，内容需要足够长才能触发 map 阶段。" * 6
raw = "\n".join(para + f"（段落编号{i}）" for i in range(140))
ck("测试稿超过 SHORT_LIMIT", len(raw) > llm.SHORT_LIMIT, len(raw))

chunks = llm._split_chunks(raw)
maxw = max(min(int(llm.load_settings().get("max_concurrency") or 4), 8), 1)
# 统一用 1-based 块序号（与 prompt 里的 idx 一致），避免和 captured 的键错位
batches = [list(range(s + 1, min(s + maxw, len(chunks)) + 1))
           for s in range(0, len(chunks), maxw)]
ck("已切成多块", len(chunks) >= 2, len(chunks))
ck(f"块数超过一个批次（{len(chunks)} 块 / {len(batches)} 批）", len(batches) >= 2, batches)

captured: dict[int, str] = {}
MARK = "ZZMARKZZ"          # 只出现在清洗结果里，绝不会与块序号混淆


def fake_chat(system, user, model=None):
    m = re.search(r"第 (\d+)/(\d+) 段", user)
    if m:
        idx = int(m.group(1))
        captured[idx] = user
        return f"清洗产出{MARK}{idx}"
    return "OpenAI, Anthropic"


CARRY_RE = re.compile(r"上文摘要（紧邻本段之前的已清洗内容，仅供理解指代，"
                      r"不要重复输出）：\n(.*?)\n请仅做以下处理", re.S)

orig_chat = llm.chat
llm.chat = fake_chat
try:
    out = llm.polish(raw, {"title": "测试视频"}, style="clean")
finally:
    llm.chat = orig_chat

ck("polish 返回非空", bool(out and out.strip()))
ck("所有块都被处理", len(captured) == len(chunks), f"{len(captured)}/{len(chunks)}")
ck("块序号不重复", len(captured) == len(set(captured)))


def carry_of(i: int) -> str:
    m = CARRY_RE.search(captured.get(i, ""))
    return m.group(1) if m else ""


# 首批：没有上文，不该出现 carry 标记
first_batch = batches[0]
ck("首批各块都不含上文摘要（本来就没有上文）",
   all(not carry_of(i) for i in first_batch), [(i, bool(carry_of(i))) for i in first_batch])

# 后续批次：必须带上前序批次的清洗结果
if len(batches) >= 2:
    second = batches[1]
    ck("第 2 批带上了上文摘要", all(carry_of(i) for i in second),
       [(i, bool(carry_of(i))) for i in second])
    prev_ids = [str(i) for i in first_batch]
    ck("第 2 批的上文摘要确实来自第 1 批的清洗结果",
       all(any(f"{MARK}{p}" in carry_of(i) for p in prev_ids) for i in second))
    ck("第 2 批未携带更后面批次的产出（时序正确）",
       all(not any(f"{MARK}{j}" in carry_of(i)
                   for b in batches[2:] for j in b) for i in second))

if len(batches) >= 3:
    third = batches[2]
    ck("第 3 批带上了第 2 批的产出",
       all(any(f"{MARK}{p}" in carry_of(i) for p in batches[1]) for i in third))
    ck("carry 长度受控（不超 CARRY_CHARS + 余量）",
       all(len(carry_of(i)) <= llm.CARRY_CHARS + 60 for i in captured
           if carry_of(i)),
       max((len(carry_of(i)) for i in captured), default=0))

last = batches[-1][-1]
ck("prompt 明确要求不要重复上文内容", "不要重复输出" in captured[last])

print("五、并发度：批内仍并行，不是全串行")
print("=" * 66)

import time

order: list[int] = []


def slow_chat(system, user, model=None):
    m = re.search(r"第 (\d+)/(\d+) 段", user)
    if m:
        idx = int(m.group(1))
        order.append(("start", idx, time.time()))
        time.sleep(0.25)
        order.append(("end", idx, time.time()))
        return f"第{idx}段"
    return "terms"


llm.chat = slow_chat
try:
    llm.polish(raw, {"title": "并发测试"}, style="clean")
finally:
    llm.chat = orig_chat

starts = [t for k, _, t in order if k == "start"]
ck("确实发起了并行请求", len(starts) >= 2, len(starts))
if len(starts) >= 2:
    # 前 maxw 个请求的启动时间应高度接近（同一批内）
    first_batch = sorted(starts)[:min(4, len(starts))]
    spread = max(first_batch) - min(first_batch)
    ck("同批请求几乎同时发出（真并发而非排队）", spread < 0.2, f"时间差 {spread:.3f}s")

print()
print("=" * 66)
print(f"结果：{PASS} 通过 / {FAIL} 失败")
print("=" * 66)
sys.exit(1 if FAIL else 0)
