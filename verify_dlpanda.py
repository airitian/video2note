# -*- coding: utf-8 -*-
"""core/dlpanda.py 解析逻辑验证

数据来源是真实抓取的解析响应 HTML（data/tmp/dlp_dy.html，
由 test_dlpanda5.py 生成），不是构造样本 —— 这样测的是实际结构。
"""
import sys

sys.path.insert(0, ".")

from core import dlpanda

PASS = FAIL = 0


def ck(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {extra}")


print("=" * 64)
print("一、真实响应解析")
print("=" * 64)

# 内嵌真实抓取到的成功响应片段（原样摘自dlpanda 解析结果页，
# 属性里的 & 被转义成 &amp;、协议相对地址等特征都保留）。
# 用真实样本而非构造样本：测的是实际结构，不是我们以为的结构。
REAL_HTML = (
    '<div data-download-state data-state="success" data-can-retry="false">'
    '<div id="result">'
    '<h2 class="mt-2 text-2xl font-black leading-tight tracking-tight">'
    'AI 编程接单变现，仿豆包流式交互实战，小单稳步做大单 #AI编程'
    '</h2>'
    '<video controls class="mb-4" referrerpolicy="no-referrer">'
    '<source src="//v5-hl-mly-ov.zjcdn.com/3bd723620588940b0c84269948b32cf7'
    '/6aca72b1/video/tos/cn/tos-cn-ve-15/okwAUAI8QlQiapqBJIS1S8DiEPkPcQHO'
    'n5ti/?a=6383&amp;ch=26&amp;br=287&amp;mime_type=video_mp4'
    '&amp;dy_q=1791640677&amp;feature_id=37f92ebd2877ae8e7" '
    'type="video/mp4" referrerpolicy="no-referrer">'
    '</video>'
    '<audio controls class="mb-4">'
    '<source src="https://sf6-cdn-tos.douyinstatic.com/obj/ies-music/'
    '7663802245675830079.mp3" type="audio/mpeg">'
    '</audio>'
    '<a href="//v5-hl-mly-ov.zjcdn.com/3bd723620588940b0c84269948b32cf7/x.mp4" '
    'data-download-url="//v5-hl-mly-ov.zjcdn.com/3bd723620588940b0c84269948b32cf7'
    '/x.mp4" target="_blank" rel="nofollow noopener noreferrer">保存视频</a>'
    '<span class="border border-black/20 bg-[#f2f0eb] px-3 py-1 text-xs font-bold'
    ' text-neutral-600">小涛AI大讲堂</span>'
    '<span class="border border-black/20 bg-[#f2f0eb] px-3 py-1 text-xs font-bold'
    ' text-neutral-600">7663801899128343843</span>'
    '</div></div>'
)

r = dlpanda.parse_html(REAL_HTML)
ck("状态为 success", r.extra.get("state") == "success", r.extra)
ck("拿到视频直链", bool(r.video_url))
ck("视频直链是 https", (r.video_url or "").startswith("https://"))
ck("视频直链指向抖音 CDN", "zjcdn.com" in (r.video_url or ""),
   (r.video_url or "")[:80])
ck("协议相对前缀已补全", not (r.video_url or "").startswith("//"))
ck("HTML 实体已解码 (&amp; -> &)", "&amp;" not in (r.video_url or ""),
   (r.video_url or "")[-60:])
ck("拿到音频直链", bool(r.audio_url), r.audio_url)
ck("音频指向 douyinstatic", "douyinstatic" in (r.audio_url or ""))
ck("标题已提取", "AI 编程接单变现" in r.title, r.title[:50])
ck("标题已去 HTML 标签", "<" not in r.title)
ck("作品 ID 已提取", r.aweme_id == "7663801899128343843", r.aweme_id)
ck("作者已提取", r.author == "小涛AI大讲堂", r.author)
ck("优先取 video source 而非 a[href]",
   "/3bd723620588940b0c84269948b32cf7/6aca72b1/" in (r.video_url or ""))

print()
print("=" * 64)
print("二、错误状态分流（每种都必须给出可执行文案）")
print("=" * 64)

for state, must in (
    ("unsupported", "不支持"),
    ("private", "私密"),
    ("rate_limited", "限流"),
    ("security_check", "验证"),
    ("timeout", "超时"),
    ("server_error", "服务端"),
):
    page = f'<div data-download-state data-state="{state}"></div>'
    try:
        dlpanda.parse_html(page)
        ck(f"{state} 应报错", False, "竟然成功了")
    except dlpanda.DlpandaError as e:
        ck(f"{state} -> 有指引文案", must in str(e), str(e))

try:
    dlpanda.parse_html("<div>无 state 属性</div>")
    ck("缺状态应报错", False)
except dlpanda.DlpandaError as e:
    ck("缺状态 -> 明确说明", "未知状态" in str(e), str(e))

print()
print("=" * 64)
print("三、success 但无媒体（站点结构变更）")
print("=" * 64)

page = '<div data-state="success"></div>'
try:
    dlpanda.parse_html(page)
    ck("无媒体应报错", False)
except dlpanda.DlpandaError as e:
    ck("无媒体 -> 提示结构变更", "结构可能已变更" in str(e), str(e))

print()
print("=" * 64)
print("四、协议相对地址补全")
print("=" * 64)

ck("_abs('//x.com/a.mp4')", dlpanda._abs("//x.com/a.mp4") == "https://x.com/a.mp4")
ck("_abs('https://x/a') 保持", dlpanda._abs("https://x/a") == "https://x/a")
ck("_abs 解转义实体",
   dlpanda._abs("//x.com/a?b=1&amp;c=2") == "https://x.com/a?b=1&c=2")

print()
print("=" * 64)
print("五、扩展名推断")
print("=" * 64)

ck(".mp4", dlpanda._ext_for("https://x/a.mp4?k=1") == ".mp4")
ck(".webm", dlpanda._ext_for("https://x/a.WEBM") == ".webm")
ck("默认 mp4", dlpanda._ext_for("https://x/stream") == ".mp4")

print()
print("=" * 64)
print("六、平台路由与可用性")
print("=" * 64)

ck("抖音路由独立", dlpanda.PLATFORM_PATH["douyin"] == "/zh-CN/douyin")
ck("B站 路由存在", "bilibili" in dlpanda.PLATFORM_PATH)
try:
    dlpanda.resolve("x", platform="unknown")
    ck("未知平台应拒绝", False)
except dlpanda.DlpandaError as e:
    ck("未知平台 -> 明确拒绝", "暂不支持" in str(e), str(e))
ck("available() 返回 bool", isinstance(dlpanda.available(), bool))

print()
print("=" * 64)
print(f"结果：{PASS} 通过 / {FAIL} 失败")
print("=" * 64)
sys.exit(1 if FAIL else 0)