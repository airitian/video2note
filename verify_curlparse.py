# -*- coding: utf-8 -*-
"""curl 解析器验证：主用例是用户真实提供的抓包"""
import sys

sys.path.insert(0, ".")

from core import curlparse

PASS = FAIL = 0


def ck(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {extra}")


REAL = r"""curl --url 'https://dlpanda.com/zh-CN' \
  -H 'accept: text/html' \
  -H 'accept-language: zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7,zh-TW;q=0.6' \
  -H 'cache-control: no-cache' \
  -H 'content-type: multipart/form-data; boundary=----WebKitFormBoundaryLDY4ut2moxK2sVD9' \
  -b 'current_locale=zh-CN; x-hng=lang=zh-CN&domain=dlpanda.com; cf_clearance=SAMPLE_CLEARANCE_VALUE_DO_NOT_COMMIT_0000000000000000; __gads=ID=9c5631c65b3e2060' \
  -H 'dnt: 1' \
  -H 'origin: https://dlpanda.com' \
  -H 'pragma: no-cache' \
  -H 'priority: u=1, i' \
  -H 'sec-ch-ua: "Google Chrome";v="155", "Chromium";v="155", "Not(A:Brand";v="24"' \
  -H 'user-agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36' \
  -H 'x-requested-with: XMLHttpRequest' \
  --data-raw $'------WebKitFormBoundaryLDY4ut2moxK2sVD9\r\nContent-Disposition: form-data; name="_token"\r\n\r\nSAMPLE_CSRF_TOKEN_40_CHARS_LONG_0000000000\r\n------WebKitFormBoundaryLDY4ut2moxK2sVD9\r\nContent-Disposition: form-data; name="url"\r\n\r\n0.02 NjC:/ :6pm e@O.kc 02/13 AI 编程接单变现 https://v.douyin.com/EXAMPLE0000/ 复制此链接\r\n------WebKitFormBoundaryLDY4ut2moxK2sVD9\r\nContent-Disposition: form-data; name="t0ken"\r\n\r\nSAMPLE_TOKEN12\r\n------WebKitFormBoundaryLDY4ut2moxK2sVD9--\r\n'"""

print("=" * 64)
print("一、脱敏后的真实抓包结构（原样保留，仅凭据替换为占位符）")
print("=" * 64)

p = curlparse.parse_curl(REAL)
ck("URL 正确", p["url"] == "https://dlpanda.com/zh-CN", p["url"])
ck("方法为 POST", p["method"] == "POST", p["method"])
ck("_token 已提取", p["form"].get("_token") == "SAMPLE_CSRF_TOKEN_40_CHARS_LONG_0000000000",
   p["form"].get("_token"))
ck("t0ken 已提取", p["form"].get("t0ken") == "SAMPLE_TOKEN12", p["form"].get("t0ken"))
ck("url 字段已提取", "v.douyin.com/EXAMPLE0000" in p["form"].get("url", ""),
   p["form"].get("url", "")[:60])
ck("Cookie 已提取到独立字段", "cf_clearance" in p["cookies"],
   f'{p["cookies"][:40]}...')
ck("Cookie 不混进 headers", "cookie" not in {k.lower() for k in p["headers"]})
ck("accept-language 保留", "zh-CN" in p["headers"].get("accept-language", ""))
ck("x-requested-with 保留", p["headers"].get("x-requested-with") == "XMLHttpRequest")
ck("body 未污染 form 值", "\r\n" not in p["form"].get("_token", ""))
ck("无 Content-Disposition 残留",
   "Content-Disposition" not in " ".join(p["form"].values()))
ck("无 boundary 残留",
   not any("WebKitFormBoundary" in v for v in p["form"].values()))

print()
print("  头部保留情况：")
for k in sorted(p["headers"]):
    print(f"    {k}")

print()
print("=" * 64)
print("二、浏览器自带头必须被丢弃")
print("=" * 64)

for k in ("cookie", "user-agent", "origin", "referer", "accept",
          "sec-ch-ua", "sec-fetch-mode", "content-length"):
    ck(f"已丢弃 {k}", k not in {x.lower() for x in p["headers"]})

print()
print("=" * 64)
print("三、概要：区分动态/固定字段")
print("=" * 64)

s = curlparse.summarize(p)
ck("动态字段含 _token", "_token" in s["dynamic"], s["dynamic"])
ck("动态字段含 t0ken", "t0ken" in s["dynamic"], s["dynamic"])
ck("url 字段单列而非混入 dynamic", s["has_url_field"] is True)
ck("url 不在 dynamic 里", "url" not in s["dynamic"], s["dynamic"])
ck("url 不在 static 里", "url" not in s["static"], s["static"])
ck("summary 报 cookie 存在", s["cookie_present"] is True)
ck("summary 统计 cookie 条数", s["cookie_count"] >= 3, s["cookie_count"])

print()
print("=" * 64)
print("四、其他 curl 写法")
print("=" * 64)

one = ('curl "https://dlpanda.com/zh-CN/douyin" -X POST '
       '-H "x-requested-with: XMLHttpRequest" -H "accept: text/html" '
       '--data-raw "_token=abc123&url=https%3A%2F%2Fv.douyin.com%2Fxyz%2F&t0ken=ttt"')
p2 = curlparse.parse_curl(one)
ck("单行双引号: URL", p2["url"] == "https://dlpanda.com/zh-CN/douyin", p2["url"])
ck("单行: _token", p2["form"].get("_token") == "abc123", p2["form"])
ck("单行: t0ken", p2["form"].get("t0ken") == "ttt", p2["form"])

get_only = 'curl -X GET "https://example.com/a?b=1" -H "accept: */*"'
p3 = curlparse.parse_curl(get_only)
ck("纯 GET 保持 GET", p3["method"] == "GET", p3["method"])
ck("GET 也能识别 URL", p3["url"].startswith("https://example.com"))

print()
print("=" * 64)
print("五、异常输入")
print("=" * 64)

for name, bad in (("空内容", ""), ("空白", "   \n  "), ("无 URL", "curl -X POST -d 'a=1'")):
    try:
        curlparse.parse_curl(bad)
        ck(f"{name} 应报错", False)
    except ValueError as e:
        ck(f"{name} -> 明确提示", "粘贴" in str(e) or "请求地址" in str(e), str(e))

print()
print("=" * 64)
print(f"结果：{PASS} 通过 / {FAIL} 失败")
print("=" * 64)
sys.exit(1 if FAIL else 0)