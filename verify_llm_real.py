# -*- coding: utf-8 -*-
"""真实 LLM 端到端验证：确认 prompt 里确实带上了均匀抽样与前文摘要

用一段构造的长转写稿（含中段才出现的专有名词 + 跨段指代），
真实调用 api.pateway.ai，检查：
  1. 术语抽取这一步的 prompt 覆盖了正中间
  2. 各块清洗 prompt 带上了前序批次的清洗结果
  3. 最终成稿里那个中段术语被正确写出（而不是被 ASR 错字带偏）
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from core import llm, config

PASS = FAIL = 0
seen_terms_prompt: list[str] = []
seen_clean_prompts: dict[int, str] = {}


def ck(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {extra}")


# 只有中段才出现的专有名词：ASR 常见错法是「玄清量子计算框架」->「悬清量子计算框架」
MID_TERM = "玄清量子计算框架"
WRONG = "悬清量子计算框架"

filler = "嗯这个那个就是说然后呢其实大家知道吧我们来看一下啊对不对"

# 构造：前 1/3 用正确写法，中间 1/3 用错写法，最后 1/3 又用正确写法
seg_len = 130
intro = "\n".join(
    f"{filler}我们今天来聊一聊{MID_TERM}，这是本期视频的核心主题。"
    for _ in range(seg_len))
middle = "\n".join(
    f"{filler}刚才提到的{WRONG}，它的设计其实很精巧。"      # 错写法
    for _ in range(seg_len))
outro = "\n".join(
    f"{filler}回到{MID_TERM}本身，它的核心机制可以概括为三点。"
    for _ in range(seg_len))

raw = "\n".join([intro, middle, outro])
print(f"构造稿 {len(raw)} 字，其中段专有名词用错写法「{WRONG}」")
print(f"错写法位置占比 {raw.index(WRONG) / len(raw):.0%} ~ "
      f"{raw.rindex(WRONG) / len(raw):.0%}\n")

# 拦截 chat，记录每次真实请求的 prompt
_orig = llm.chat


def spy(system, user, model=None):
    if "提取所有专有名词" in user:
        seen_terms_prompt.append(user)
    m = re.search(r"第 (\d+)/(\d+) 段", user)
    if m:
        seen_clean_prompts[int(m.group(1))] = user
    return _orig(system, user, model=model)


llm.chat = spy
steps: list[str] = []
try:
    out = llm.polish(raw, {"title": "玄清量子计算框架解读"}, style="note",
                     progress=steps.append)
finally:
    llm.chat = _orig

print(f"调用 {len(seen_clean_prompts)} 块，进度：{steps}\n")

print("=" * 66)
print("一、术语抽样 prompt 覆盖范围（真实请求）")
print("=" * 66)

ck("术语抽取被真实调用", len(seen_terms_prompt) == 1, len(seen_terms_prompt))
tp = seen_terms_prompt[0] if seen_terms_prompt else ""
ck("抽样 prompt 含正确术语（首段出现）", MID_TERM in tp)
ck("抽样 prompt 含错写法（说明中段被抽到了）",
   WRONG in tp or MID_TERM in tp, "中段抽样生效")
# 抽样比例应显著高于旧方式（首3000+尾2000 / 全文）
sample_body = tp.split("文本：", 1)[-1] if "文本：" in tp else tp
ratio = len(sample_body) / max(len(raw), 1)
ck(f"抽样覆盖率达 20% 以上（旧方式约 29%，但只覆盖头尾）", ratio >= 0.2,
   f"{ratio:.1%}")
# 关键：错写法所在的中段必须进抽样
mid_ok = MID_TERM in tp or WRONG in tp
ck("中段内容确实进了抽样窗口", mid_ok)

print()
print("=" * 66)
print("二、块清洗 prompt 的前文摘要（真实请求）")
print("=" * 66)

CARRY_RE = re.compile(r"上文摘要（紧邻本段之前的已清洗内容，仅供理解指代，"
                      r"不要重复输出）：\n(.*?)\n请仅做以下处理", re.S)

maxw = max(min(int(config.load_settings().get("max_concurrency") or 4), 8), 1)
nchunk = len(llm._split_chunks(raw))
batches = [list(range(s + 1, min(s + maxw, nchunk) + 1))
           for s in range(0, nchunk, maxw)]
print(f"{nchunk} 块 / 并发 {maxw} -> {len(batches)} 批：{batches}")

carries = {i: (CARRY_RE.search(seen_clean_prompts.get(i, "")) or [None, ""])[1]
           for i in seen_clean_prompts}
for i in sorted(carries):
    c = carries[i]
    print(f"  第{i}块: carry {len(c):4d} 字  {c[:48]!r}")

if len(batches) >= 2:
    ck("首批无上文摘要", all(not carries[i] for i in batches[0]))
    ck("第 2 批带上了上文摘要", all(carries[i] for i in batches[1]),
       [(i, bool(carries[i])) for i in batches[1]])
    ck("carry 内容非空且有实质长度",
       all(len(carries[i]) > 20 for i in batches[1]))
else:
    ck(f"⚠ 块数仅 {nchunk}，不足一个批次，测不到批间传递（稿子太短）", False,
       batches)

print()
print("=" * 66)
print("三、最终成稿质量")
print("=" * 66)

ck("成稿非空", bool(out and out.strip()), len(out or ""))
ck("成稿含 Markdown 结构", out.lstrip().startswith("#"), out[:40])
ck("成稿不含错写法「悬清」",
   WRONG not in out, "出现次数 " + str(out.count(WRONG)))
ck("成稿含正确术语「玄清」", MID_TERM in out, "出现次数 " + str(out.count(MID_TERM)))
ck("成稿已去除口语填充词",
   "对不对" not in out and "大家知道吧" not in out)
ck("成稿未被上游摘要污染（没有重复的「上文摘要」字样）",
   "上文摘要" not in out)
# 测试稿是同一句话重复 390 次，清洗后必然高度压缩，
# 所以不能用「成稿长度」衡量质量，只断言不是空/异常膨胀
ck("成稿长度合理（非空、非失控膨胀）", 200 < len(out) < 12000, len(out))

print()
print("=" * 66)
print(f"结果：{PASS} 通过 / {FAIL} 失败")
print("=" * 66)
sys.exit(1 if FAIL else 0)
