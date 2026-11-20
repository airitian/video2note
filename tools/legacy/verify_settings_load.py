# -*- coding: utf-8 -*-
"""诊断「设置加载失败：无法连接后端 /api/settings」

这个提示极具误导性：它的真实触发条件不是「连不上后端」，
而是「设置面板一个字段都没渲染出来」。两者是完全不同的故障，
但 86aeccc 及更早版本把它们混成了一句话，导致排查方向被带偏。

本脚本对着真实服务跑一遍，把结论直接打出来。
用法：
    python verify_settings_load.py [地址]
    python verify_settings_load.py http://127.0.0.1:8765
退出码 0 = 后端侧一切正常，问题在浏览器/前端；1 = 后端侧有问题。
"""
import json
import re
import sys
import urllib.error
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765").rstrip("/")

# 抄自 static/app.js 的 HIDDEN_SETTINGS：这些后端支持、但面板不渲染。
# 判定「字段是否齐全」时必须先扣掉它们，否则会把正常状态误判成缺字段。
HIDDEN = {
    "asr_base_url", "asr_api_key", "asr_model",
    "llm_protocol", "llm_base_url", "llm_api_key", "llm_model",
    "cookie_browser", "cookie_file", "proxy", "ffmpeg_path",
}

_ok = 0
_bad = 0


def check(cond, name, detail=""):
    global _ok, _bad
    if cond:
        _ok += 1
        print(f"  [OK] {name}")
    else:
        _bad += 1
        print(f"  [!!] {name}")
        if detail:
            for line in str(detail).splitlines():
                print(f"       {line}")
    return cond


def get(path):
    """返回 (状态码, 响应体文本, 响应头)。不抛异常——404/502 也要拿到才能判断。"""
    req = urllib.request.Request(BASE + path)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read().decode("utf-8", "replace"), r.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace"), e.headers
    except Exception as e:
        return 0, f"{type(e).__name__}: {e}", _EmptyHeaders()


class _EmptyHeaders(dict):
    """请求根本没拿到响应时的占位头，避免下游 .get() 抛 AttributeError。"""

    def get(self, *_a, **_k):
        return None


def header(headers, name):
    """大小写不敏感地取响应头。

    http.client 的 HTTPMessage 本身不区分大小写，但一旦降级成普通 dict
    （例如异常分支里塞了 _EmptyHeaders），就必须自己兜住。
    直接用headers.get("Cache-Control") 会漏掉小写返回的 cache-control，
    从而把「缓存头正常」误判成「没设缓存头」——方向完全错了。
    """
    if not headers:
        return None
    try:
        val = headers.get(name)
        if val is not None:
            return val
    except Exception:
        pass
    want = name.lower()
    for key, val in dict(headers).items():
        if str(key).lower() == want:
            return val
    return None


print("=" * 64)
print(f" 诊断目标：{BASE}")
print("=" * 64)

# ---------------------------------------------------------------- 1. 服务存活
print("\n[1] 服务是否活着（首页能打开 ≠ 接口正常，但要分开确认）")
code, body, hdrs = get("/")
check(code == 200, f"GET / 返回 {code}", body[:200])

# ---------------------------------------------------------------- 2. 健康检查
print("\n[2] /api/health —— 版本自述靠它，没有它就分不清新旧代码")
code, body, hdrs = get("/api/health")
health = {}
if check(code == 200, f"GET /api/health 返回 {code}", body[:300]):
    try:
        health = json.loads(body)
    except Exception as e:
        print(f"       健康信息不是合法 JSON：{e}")

ver = health.get("version") or {}
if ver:
    print(f"       运行中的代码：{ver.get('commit')}")
    print(f"       静态资源版本：{ver.get('assets')}")
    print(f"       settings 字段数：{ver.get('settings_keys')}")
    print(f"       含 resolver_curl：{ver.get('has_resolver_curl')}")
    check(bool(ver.get("commit")), "后端会自报版本（老版本没有这段）",
          "如果这里没有 commit 字段，说明跑的是 63f7a8e 之前的代码，\n"
          "       那这些提示文案本身就已经过时了。")
else:
    print("       ⚠️ 无版本信息：后端代码偏旧，无法自证版本")

# ---------------------------------------------------------------- 3. 设置接口
print("\n[3] /api/settings —— 这才是那句提示真正指向的请求")
code, body, hdrs = get("/api/settings")
if not check(code == 200, f"GET /api/settings 返回 {code}", body[:300]):
    print("\n       ★ 接口本身就不通。这时提示语里的「连不上后端」是字面意思：")
    print("         · 服务没起来        → systemctl status video2note")
    print("         · 起了但没重启到新代码 → systemctl restart video2note")
    print("         · 前面挂了反代但没转发 /api → 检查 nginx 的 location")
else:
    try:
        data = json.loads(body)
    except Exception as e:
        print(f"       ★ 返回的不是 JSON（多半被反代拦截成了错误页）：{e}")
        print(f"       前 200 字符：{body[:200]}")
        data = {}

    help_d = data.get("help") or {}
    values = data.get("values") or {}
    groups = data.get("groups") or []
    visible = [k for k in help_d if k not in HIDDEN]

    check(len(help_d) > 0, f"help 返回 {len(help_d)} 个字段")
    check("resolver_curl" in help_d, "help 里含 resolver_curl（curl 输入框的数据源）")
    check(len(groups) > 0, f"groups 返回 {len(groups)} 个分组",
          "没有 groups 说明后端是 d9dfbab 之前的版本")

    # 关键：前端是静态文件，后端是常驻进程。git pull 只换了磁盘上的文件，
    # 不会让已在运行的进程重新加载 Python 模块——于是出现「前端新、后端旧」。
    # 症状极具迷惑性：页面样式和自检条都是新的，但字段列表是旧的。
    if not groups:
        print("\n       ★★ 后端没有返回 groups —— 前后端版本不一致 ★★")
        print("       这是「curl 输入框不见了」最常见的真实原因：")
        print("         git pull 更新了 static/ 下的文件（前端立刻变新），")
        print("         但 uvicorn 进程仍在跑旧的 Python 模块（后端还是旧版）。")
        print("       证据：新前端才有自检条，旧后端没有 groups / resolver_curl。")
        print("       修复：重启服务让后端加载新代码（见文末）。")
    if "resolver_curl" not in help_d:
        print("\n       ★★ 后端没有 resolver_curl 字段 —— 无法渲染 curl 输入框 ★★")
        print(f"       当前 help 字段：{sorted(help_d)}")
        print("       后端跑的是 d9dfbab 之前的旧代码。git pull 不会让运行中的")
        print("       进程重新加载 Python 模块，必须重启服务。")

    check(len(visible) > 0, f"扣掉隐藏项后仍有 {len(visible)} 个可渲染字段")

    # 前端只有在「一个 block 都没渲染出来」时才会弹那句提示。
    # 这里完整复现前端的渲染逻辑，把结论坐实。
    known = {k for g in groups for k in (g.get("keys") or [])}
    from_groups = [k for g in groups for k in (g.get("keys") or [])
                   if k in help_d and k not in HIDDEN]
    rest = [k for k in help_d if k not in known and k not in HIDDEN]
    blocks = len([g for g in groups
                  if [k for k in (g.get("keys") or [])
                      if k in help_d and k not in HIDDEN]]) + (1 if rest else 0)

    print(f"\n       前端渲染推演：{blocks} 个分组块，可渲染字段 {visible}")
    check(blocks > 0,
          "按前端逻辑能渲染出内容 —— 不会触发那句提示",
          "blocks=0 才会触发。网络明明正常却弹提示，说明浏览器拿到的\n"
          "       不是这份响应（多半是缓存里的旧 app.js）。")

# ---------------------------------------------------------------- 4. 缓存头
print("\n[4] 缓存头 —— 决定浏览器会不会一直用旧页面")
code, body, hdrs = get("/")
cache = header(hdrs, "Cache-Control") or "(无)"
print(f"       首页 Cache-Control：{cache}")
if "no-store" in cache or "no-cache" in cache:
    print("       ✓ 已禁止缓存，刷新即可拿到新页面")
else:
    print("       ✗ 无缓存头：浏览器会启发式缓存 HTML —— 这就是「代码更新了")
    print("         但页面还是旧的」的根因。请升级到 63f7a8e 之后的版本。")

# ---------------------------------------------------------------- 5. 前端版本戳
print("\n[5] 前端资源版本戳 —— 确认浏览器加载的是哪份 app.js")
m = re.search(r'app\.js\?v=([0-9a-zA-Z]+)', body)
if m:
    served = m.group(1)
    print(f"       首页引用：app.js?v={served}")
    hv = ver.get("assets")
    if hv:
        check(served == hv, f"与后端自述一致（{hv}）",
              f"首页引用 {served}，后端自述 {hv} —— 两者不一致说明中间有缓存。")
else:
    print("       ⚠️ 首页里没找到 app.js?v= 版本戳")

# ---------------------------------------------------------------- 汇总
print("\n" + "=" * 64)
if _bad == 0:
    print(f" 结论：后端侧全部正常（{_ok} 项通过）")
    print("=" * 64)
    print("""
 那句「无法连接后端 /api/settings」不是后端真的连不上——
 后端此刻是通的，字段也齐全。问题在浏览器这一侧，按顺序试：

   1. 强制刷新（绕过缓存，这是最常见的原因）
        Ctrl+Shift+R （Windows/Linux）
        Cmd+Shift+R  （Mac）
      或打开 DevTools → Network 勾选 Disable cache 后刷新

   2. 确认加载的是新代码
      看首页右上角的版本号，与 /api/health 返回的 commit 比对。
      若不一致 → F12 → Application → Clear site data，清干净再刷新。

   3. 若必须走反代（nginx 等），确认 /api/ 有转发规则
      少了这条，首页能开、/api/settings 却 502/404，
      前端就会把这两种情况都显示成同一句「连不上后端」。
""".strip())
    sys.exit(0)

print(f" 结论：发现 {_bad} 项异常（{_ok} 项通过）")
print("=" * 64)
print("""
 后端代码是旧的——这正是 curl 输入框消失的原因。

 为什么会这样：git pull 只更新了磁盘上的文件。
 static/ 是静态文件，浏览器刷新就拿到新的；
 但 uvicorn 进程里的 Python 模块是启动时载入的，
 不重启就一直跑旧代码。于是前端新、后端旧，字段对不上。

 按顺序执行：

   1. 确认磁盘上的代码已是最新
        cd <项目目录> && git pull && git log --oneline -1

   2. 重启服务（关键，这一步才会让后端加载新代码）
        sudo systemctl restart video2note

   3. 确认没有旧进程还占着端口
        sudo ss -lntp | grep 8765
      若看到陌生 PID 占用该端口，说明有僵尸进程：
        sudo pkill -f "run.py" && sudo systemctl restart video2note

   4. 刷新页面（Ctrl+Shift+R），面板顶部应显示
      「✅ 解析请求模板输入框已就位」
""".strip())
sys.exit(1)