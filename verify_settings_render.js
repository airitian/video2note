/**
 * 真实执行 app.js 里的 field()，验证「敏感项 → 控件」的渲染结果。
 *
 * 为什么是 Node 而不是肉眼读代码：之前一轮改动把状态提示写在了多行文本分支里，
 * 看代码完全合理，跑起来才发现单行输入的敏感项一行状态都没有。
 * 这类问题只能靠把函数抠出来真跑一遍。
 */
const fs = require("fs");
const path = require("path");

const SRC = fs.readFileSync(path.join(__dirname, "static", "app.js"), "utf8");

let ok = 0, fail = 0;
function check(name, cond, detail) {
  if (cond) { ok++; console.log("  PASS  " + name + (detail ? "  (" + detail + ")" : "")); }
  else { fail++; console.log("  FAIL  " + name + (detail ? "  (" + detail + ")" : "")); }
}

// ---- 从源码里抠出需要的片段（不复制代码，避免和真实实现脱节）----
function extract(re, label) {
  const m = SRC.match(re);
  if (!m) throw new Error("源码里没找到 " + label + "，可能已改名");
  return m[0];
}

const esc = Function(extract(/function esc\(s\) \{[^}]*\}[^}]*\}/, "esc") + "; return esc;")();
// 大括号配平抠函数体：field() 内部有三元嵌套，正则够不到结尾
function extractFn(name) {
  const start = SRC.indexOf("function " + name + "(");
  if (start < 0) throw new Error("没找到 function " + name);
  let depth = 0, i = SRC.indexOf("{", start);
  const begin = i;
  for (; i < SRC.length; i++) {
    if (SRC[i] === "{") depth++;
    else if (SRC[i] === "}") { depth--; if (depth === 0) return SRC.slice(start, i + 1); }
  }
  throw new Error("大括号没配平: " + name);
}
const fieldSrc = extractFn("field");
// SECRET_UI 同样取自源码：签名的具体内容变了这里立刻失败，
// 保证测的是线上那份而不是我手抄的一份副本。
const SECRET_UI = Function(SRC.match(/const SECRET_UI = \{[^}]*\};/)[0]
  + " return SECRET_UI;")();
const field = Function(
  "esc", "SECRET_UI",
  fieldSrc + "; return field;"
)(esc, SECRET_UI);

const full = ["cookie_text", "cookie_text_bili", "resolver_curl"];
const areas = ["cookie_text", "cookie_text_bili", "resolver_curl"];
const rows = { cookie_text: 4, cookie_text_bili: 4, resolver_curl: 9 };
const PH = { qiniu_ak: "AK 占位提示", qiniu_sk: "SK 占位提示" };
const HELP = { qiniu_ak: "七牛云 AccessKey（密钥内容不回显）",
               qiniu_sk: "七牛云 SecretKey（密钥内容不回显）" };

console.log("\n== 单行敏感项也必须带状态行与清除按钮 ==");
const both = { _configured: { qiniu_ak: true, qiniu_sk: true, qiniu: true } };
const hAk = field("qiniu_ak", { qiniu_ak: "" }, HELP, both._configured,
                  full, areas, PH, rows);
check("AK 渲染为输入框", /<input data-key="qiniu_ak"/.test(hAk), hAk.slice(0, 60) + "…");
check("AK 显示「已配置」", /✅ 已配置/.test(hAk));
check("AK 有清除按钮", /data-clear="qiniu_ak"/.test(hAk));
check("AK 不回显真实值", !/value="[^"]/.test(hAk), "value 必须为空");

console.log("\n== 只填一半密钥时提示必须说清楚（而不是显示已配置）==");
const half = { _configured: { qiniu_ak: true, qiniu_sk: false, qiniu: false } };
const hHalf = field("qiniu_ak", { qiniu_ak: "" }, HELP, half._configured,
                    full, areas, PH, rows);
check("提示密钥不完整", /密钥不完整/.test(hHalf));
check("同时仍如实显示 AK 自身已配置", /✅ 已配置/.test(hHalf));
const hHalfSk = field("qiniu_sk", { qiniu_sk: "" }, HELP, half._configured,
                      full, areas, PH, rows);
check("SK 缺的那个框提示「未配置」而非笼统告警", /未配置/.test(hHalfSk));
check("SK 框同样附带成对告警", /密钥不完整/.test(hHalfSk));
check("成对告警用告警色", /secret-state warn/.test(hHalfSk));
const hNone = field("qiniu_sk", { qiniu_sk: "" }, HELP,
                    { _configured: { qiniu_sk: false, qiniu: false } },
                    full, areas, PH, rows);
check("SK 未配置时提示粘贴", /未配置/.test(hNone));
check("SK 未配置时不给清除按钮", !/data-clear="qiniu_sk"/.test(hNone));

console.log("\n== 既有三项行为不变（防止改坏老功能）==");
const old = { _configured: { cookie_douyin: true, resolver_curl: false } };
const hCookie = field("cookie_text", { cookie_text: "" }, HELP, old._configured,
                      full, areas, PH, rows);
check("抖音 Cookie 仍是多行文本框", /<textarea data-key="cookie_text"/.test(hCookie));
check("抖音 Cookie 状态行为已配置", /✅ 已配置/.test(hCookie));
check("抖音 Cookie 有清除按钮", /data-clear="cookie_text"/.test(hCookie));
const hCurl = field("resolver_curl", { resolver_curl: "" }, HELP, old._configured,
                    full, areas, PH, rows);
check("curl 模板仍有校验按钮", /data-parse-curl="1"/.test(hCurl));
check("curl 未配置时提示粘贴", /未配置/.test(hCurl));

console.log("\n== 普通项不受影响 ==");
const hPlain = field("qiniu_bucket", { qiniu_bucket: "my-bucket" }, HELP, {},
                     full, areas, PH, rows);
check("普通项回显真实值", /value="my-bucket"/.test(hPlain));
check("普通项不带状态行", !/secret-state/.test(hPlain));
check("普通项不带清除按钮", !/data-clear/.test(hPlain));

console.log("\n== 转义不被绕过（XSS）==");
const evil = field("qiniu_bucket", { qiniu_bucket: '"><script>alert(1)</script>' },
                   HELP, {}, full, areas, PH, rows);
check("尖括号被转义", !/<script>/.test(evil) && /&lt;script&gt;/.test(evil));

console.log("\n总计: PASS " + ok + " / FAIL " + fail);
process.exit(fail ? 1 : 0);
