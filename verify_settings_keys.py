"""设置面板「配置项 → UI 控件」链路的自检。

这一类 bug 的共同点是：前后端各自看起来都对，合起来才出错，
而且**不报任何错**——只是某个输入框悄悄消失，或者永远显示「未配置」。

   ① 分组里的键不在 HELP 里        -> 前端按 help[k] 过滤，整项静默消失
   ② 敏感键不在 HELP 里            -> 同上，用户以为没有这个功能
   ③ 敏感键不在前端 SECRET_UI 里   -> 渲染成普通明文输入框，填了也没反馈
   ④ SECRET_UI 指向的 _configured 键不存在 -> 状态永远「未配置」，清不掉也改不了
   ⑤ public_settings 没把敏感值置空 -> 密钥直接下发到浏览器（安全问题）

这五条逐条卡死，任一条破了这套联动就退化成老样子。
"""
import re
import sys

sys.path.insert(0, ".")
ok = fail = 0


def check(name, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  PASS  {name}" + (f"  ({detail})" if detail else ""))
    else:
        fail += 1
        print(f"  FAIL  {name}" + (f"  ({detail})" if detail else ""))


import core.config as CFG  # noqa: E402

APP_JS = (CFG.ROOT / "static" / "app.js").read_text("utf-8")
INDEX_HTML = (CFG.ROOT / "static" / "index.html").read_text("utf-8")

# 从 app.js 里抠出字面量，前端源码就是唯一事实来源，不另维护一份副本
def grab(name: str) -> str:
    """抠出 `const NAME = {...};` 或 `const NAME = [...];` 两种形态的字面量。

    踩过一次坑：先写的正则只认花括号，而 HIDDEN_SETTINGS 是方括号数组，
    结果抠出空串 → hidden_keys 为空 → asr_api_key 被误判成「会以明文框渲染」。
    """
    # 不能用 `([\{\[])(?:.|\n)*?\1` 这种反向引用：\1 要求匹配**同一个字符**，
    # 于是原始在找「[;」而实际结尾是「];」。这里把开合写死成对。
    m = re.search(r"const " + name + r" = (\{[^}]*\}|\[[^\]]*\]);", APP_JS, re.S)
    return m.group(0) if m else ""


secret_ui_src = grab("SECRET_UI")
hidden_src = grab("HIDDEN_SETTINGS")

print("\n== ① 分组里的键必须都在 HELP 里（否则整项不渲染）==")
group_keys = [k for _, keys in CFG.SETTINGS_GROUPS for k in keys]
miss_help = [k for k in group_keys if k not in CFG.HELP]
check("SETTINGS_GROUPS 全部键在 HELP 中存在", not miss_help,
      f"缺标签: {miss_help}" if miss_help else f"{len(group_keys)} 项齐全")

print("\n== ② 敏感键必须有 HELP 标签（否则用户看不到配置入口）==")
miss_secret_help = [k for k in CFG.SECRET_KEYS if k not in CFG.HELP]
check("SECRET_KEYS 全部有标签", not miss_secret_help,
      f"缺标签: {miss_secret_help}" if miss_secret_help
      else f"{len(CFG.SECRET_KEYS)} 项齐全")

print("\n== ③ 敏感键必须登记进前端 SECRET_UI（否则渲染成明文框）==")
front_secret = set(re.findall(r"^\s{4}(\w+):", secret_ui_src, re.M))
# HIDDEN_SETTINGS 里的键前端压根不渲染，不需要 SECRET_UI 保护
hidden_keys = set(re.findall(r'"(\w+)"', hidden_src))
need_secret = {k for k in CFG.SECRET_KEYS if k not in hidden_keys}
leak_plain = sorted(need_secret - front_secret)
check("可见敏感项均已在前端标记为敏感", not leak_plain,
      f"会以明文框渲染: {leak_plain}" if leak_plain
      else f"{len(need_secret)} 项已覆盖")
front_only = sorted(front_secret - need_secret)
check("SECRET_UI 无残留失效映射", not front_only, f"多余: {front_only}" if front_only else "")

print("\n== ④ SECRET_UI 指向的 _configured 键必须真实存在 ==")
ui_targets = sorted(set(re.findall(r':\s*"(\w+)"\s*,', secret_ui_src)))
pub = CFG.public_settings()
configured = pub.get("_configured", {})
miss_cfg = [t for t in ui_targets if t not in configured]
check("_configured 覆盖前端全部引用", not miss_cfg,
      f"缺: {miss_cfg}" if miss_cfg else f"{ui_targets}")

print("\n== ⑤ public_settings 不得下发任何敏感值 ==")
real = CFG.load_settings()
leaked = []
for k in CFG.SECRET_KEYS:
    if pub.get(k, "") != "":
        leaked.append(k)
    # 环境变量带进来的也不能漏出去
check("接口下发中敏感值全为空串", not leaked, f"泄露: {leaked}" if leaked else "")

# 反向验证：_configured 的布尔值要和真实存储一致，否则 UI 显示假的「已配置」
# 这里只查真实配置存在与否，不改动任何文件
print("\n== ⑥ _configured 与真实存储一致（不展示假状态）==")
bad_flag = []
for k, flag in (("cookie_text", "cookie_douyin"),
                ("cookie_text_bili", "cookie_bilibili"),
                ("resolver_curl", "resolver_curl"),
                ("qiniu_ak", "qiniu_ak"),
                ("qiniu_sk", "qiniu_sk")):
    if bool(configured.get(flag)) != bool(real.get(k)):
        bad_flag.append(f"{flag}(真实={bool(real.get(k))}) 接口={bool(configured.get(flag))}")
both_half = bool(real.get("qiniu_ak")) or bool(real.get("qiniu_sk"))
if bool(configured.get("qiniu")) != (bool(real.get("qiniu_ak")) and bool(real.get("qiniu_sk"))):
    bad_flag.append("qiniu(成对判定)")
check("_configured 与落盘值一致", not bad_flag, "; ".join(bad_flag) if bad_flag
      else f"已配置: {[k for k, v in configured.items() if v]}"
           + (f"（AK/SK 仅配其一 → qiniu=False, 正确）"
              if both_half and not configured.get("qiniu") else ""))

print("\n== ⑦ 资源戳一致性（改了前端却不变戳 => 用户永远看到旧文件）==")
stamps = set(re.findall(r"\?v=(\w+)", INDEX_HTML))
css = re.search(r"style\.css\?v=(\w+)", INDEX_HTML)
js = re.search(r"app\.js\?v=(\w+)", INDEX_HTML)
check("css 与 js 使用同一资源戳", bool(css and js and css.group(1) == js.group(1)),
      f"css={css.group(1) if css else None} js={js.group(1) if js else None}")
check("页面只有一处资源戳取值", len(stamps) == 1, f"{stamps}")

print(f"\n总计: PASS {ok} / FAIL {fail}")
sys.exit(1 if fail else 0)
