/* 验证「复制按钮」修复的核心逻辑：从 static/app.js 用正则抽出 copyText /
 * _copyFallback，在桩环境里跑四条路径。不依赖行号，app.js 后续改动不会误伤。
 *
 * 背景：navigator.clipboard 只在安全上下文（HTTPS/localhost）存在，本站 HTTP
 * 部署时它是 undefined，旧代码裸调 writeText 会静默失败（见 commit 说明）。
 *
 * 用法：node verify_copy_fallback.js
 */
const fs = require("fs");
const path = require("path");

const src = fs.readFileSync(path.join(__dirname, "static", "app.js"), "utf8");
const m = src.match(
  /function _copyFallback\([\s\S]*?\n  \}\n\n  \/\/ 统一返回 Promise<boolean>[^\n]*\n  function copyText\([\s\S]*?\n  \}/
);
if (!m) {
  console.error("FATAL: 没能从 app.js 抽出 copyText/_copyFallback，函数结构可能变了");
  process.exit(1);
}

function makeEnv(withClipboard, clipboardBehavior) {
  const calls = { execCommand: 0, writeText: 0 };
  const fakeTa = {
    value: "", style: {},
    select() {}, setSelectionRange() {}, setAttribute() {},
  };
  const doc = {
    createElement: () => fakeTa,
    body: { appendChild() {}, removeChild() {} },
    execCommand: () => { calls.execCommand++; return true; },
  };
  const nav = withClipboard ? {
    clipboard: {
      writeText: () => {
        calls.writeText++;
        if (clipboardBehavior === "reject") return Promise.reject(new Error("denied"));
        return Promise.resolve();
      },
    },
  } : {};
  const fn = new Function("navigator", "document",
    m[0] + "\nreturn { copyText, _copyFallback };");
  return { api: fn(nav, doc), calls };
}

(async () => {
  let fail = 0;
  const check = (name, ok, extra) => {
    console.log(`${name} -> ${ok ? "PASS" : "FAIL"} ${extra || ""}`);
    if (!ok) fail++;
  };

  // 场景 1：HTTP 部署（线上实际情况）——navigator.clipboard 为 undefined。
  // 旧代码在这里直接 TypeError 静默失败；新代码应走 execCommand 降级并成功。
  let env = makeEnv(false);
  let ok = await env.api.copyText("## 文稿\nhello");
  check("场景1 HTTP无clipboard", ok === true && env.calls.execCommand === 1,
    `copyText=${ok}, execCommand=${env.calls.execCommand}`);

  // 场景 2：HTTPS/localhost——clipboard 可用，直接成功，不触发降级。
  env = makeEnv(true, "ok");
  ok = await env.api.copyText("abc");
  check("场景2 clipboard可用", ok === true && env.calls.writeText === 1 && env.calls.execCommand === 0,
    `copyText=${ok}, writeText=${env.calls.writeText}, execCommand=${env.calls.execCommand}`);

  // 场景 3：clipboard 存在但被拒（未聚焦/权限）——应降级到 execCommand 仍成功。
  env = makeEnv(true, "reject");
  ok = await env.api.copyText("abc");
  check("场景3 clipboard被拒降级", ok === true && env.calls.writeText === 1 && env.calls.execCommand === 1,
    `copyText=${ok}, writeText=${env.calls.writeText}, execCommand=${env.calls.execCommand}`);

  // 场景 4：全挂（execCommand 也抛异常）——必须返回 false 而不是抛出。
  const stub = {
    createElement: () => ({ value: "", style: {}, select() {}, setSelectionRange() {}, setAttribute() {} }),
    body: { appendChild() {}, removeChild() {} },
    execCommand: () => { throw new Error("boom"); },
  };
  const fn2 = new Function("navigator", "document", m[0] + "\nreturn { _copyFallback };");
  const r4 = fn2({}, stub)._copyFallback("x");
  check("场景4 全失败不抛错", r4 === false, `_copyFallback=${r4}`);

  console.log(fail === 0 ? "\nALL PASS" : `\n${fail} SCENARIO(S) FAILED`);
  process.exit(fail === 0 ? 0 : 1);
})();
