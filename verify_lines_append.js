/**
 * 验证文字稿的「增量追加」渲染逻辑。
 *
 * 为什么单独测：首片压到 15 秒后分片数明显增多，如果每次 partial 都重建
 * innerHTML，长稿会频繁重排——滚动条跳动、正在看的那句丢失选中、
 * 用户选中的文字被清掉。改成只追加新句后必须证明：
 *   ① 追加模式下 DOM 节点是复用的（不是全部重建）
 *   ② 乱序插入时能正确退回全量重建（否则句子顺序会错）
 *   ③ 「正在识别后续内容…」始终在末尾
 *   ④ 非转写态（live=false）会移除该提示
 *
 * 用法： node verify_lines_append.js
 */
"use strict";

const fs = require("fs");
const path = require("path");

const OUT = [];
function emit() {
  OUT.push(Array.prototype.slice.call(arguments).join(" "));
}

/* ---------- 极简 DOM 替身：只实现用到的几个操作 ---------- */
class El {
  constructor(tag) {
    this.tagName = (tag || "div").toUpperCase();
    this.children = [];
    this.className = "";
    this._html = "";
    this.textContent = "";
    this.dataset = {};
    this.parent = null;
    this.listeners = {};
    this.scrollTop = 0;
  }
  set className(v) { this._html = v; this._cls = v; }
  get className() { return this._cls || ""; }
  set innerHTML(v) {
    this._html = v;
    this.children = [];
    // 极简解析：把拼接出的多个 <div class="line"> 拆成子节点。
    // 只支持 lineHtml 的输出形态（<div class="line" ...>...</div>），
    // 目的是让 firstChild / querySelectorAll 能拿到真实节点。
    const re = /<div class="line([^>]*)>([\s\S]*?)<\/div>/g;
    let m;
    while ((m = re.exec(v))) {
      const node = new El("div");
      node.className = "line" + (/pending-line/.test(m[1]) ? " pending-line" : "");
      const di = /data-i="(\d+)"/.exec(m[1]);
      if (di) node.dataset.i = di[1];
      node._text = m[2];
      node.parent = this;
      this.children.push(node);
    }
  }
  get innerHTML() {
    if (this.children.length) {
      return this.children.map((c) =>
        '<div class="line" data-i="' + (c.dataset.i || "") + '">' + c._text + "</div>").join("");
    }
    return this._html;
  }
  // 单个节点的文本内容（测试断言用）
  text() { return this._text || ""; }
  get firstChild() { return this.children[0] || null; }
  appendChild(c) {
    c.parent = this;
    this.children.push(c);
    return c;
  }
  insertBefore(node, ref) {
    node.parent = this;
    const i = ref ? this.children.indexOf(ref) : -1;
    if (i < 0) this.children.push(node);
    else this.children.splice(i, 0, node);
    return node;
  }
  remove() {
    if (!this.parent) return;
    const i = this.parent.children.indexOf(this);
    if (i >= 0) this.parent.children.splice(i, 1);
    this.parent = null;
  }
  querySelector(sel) {
    const cls = sel.replace(".", "");
    const walk = (n) => {
      for (const c of n.children) {
        if (c.className.split(" ").indexOf(cls) >= 0) return c;
        const r = walk(c);
        if (r) return r;
      }
      return null;
    };
    return walk(this);
  }
  querySelectorAll(sel) {
    // 支持 ".line" 与 ".line:not(.pending-line)" 两种（本项目用到的全部形态）
    const exclude = /:not\(([^)]+)\)/.exec(sel);
    const base = sel.split(":")[0].replace(".", "");
    const out = [];
    const walk = (n) => {
      for (const c of n.children) {
        const cls = c.className.split(" ");
        const hit = cls.indexOf(base) >= 0;
        const excluded = exclude
          ? cls.indexOf(exclude[1].replace(".", "")) >= 0
          : false;
        if (hit && !excluded) out.push(c);
        walk(c);
      }
    };
    walk(this);
    return out;
  }
  addEventListener() {}
  scrollIntoView() {}
}

/* ---------- 从 app.js 里抽出要测的逻辑（花括号配平提取） ---------- */
const src = fs.readFileSync(path.join(__dirname, "static", "app.js"), "utf8");

function extractFunc(name) {
  const i = src.indexOf("function " + name + "(");
  if (i < 0) throw new Error("找不到函数 " + name);
  let depth = 0, started = false, j = src.indexOf("{", i);
  for (let k = j; k < src.length; k++) {
    if (src[k] === "{") { depth++; started = true; }
    else if (src[k] === "}") {
      depth--;
      if (started && depth === 0) return src.slice(i, k + 1);
    }
  }
  throw new Error("函数 " + name + " 提取失败");
}

const factory = new Function(
  "DOC", "BOX", "WS", "CURRENT_ID", "LINE_HTML", "BIND_LINE", "MARK_ACTIVE", "FMT_TIME", "ESC",
  "const document = DOC;\n" +
  "const $ = BOX.$;\n" +
  "const box = BOX.node;\n" +
  "const ws = WS;\n" +
  "const currentId = CURRENT_ID();\n" +
  "const fmtTime = FMT_TIME;\n" +
  "const esc = ESC;\n" +
  "lineHtml=" + extractFunc("lineHtml") + "\n" +
  "bindLine=" + extractFunc("bindLine") + "\n" +
  "ensureLines=" + extractFunc("ensureLines") + "\n" +
  "return function(t){ ensureLines(t); };"
);

const box = new El("div");
const ws = {
  linesTask: null, linesSig: null, lineEls: [], segs: [], active: -1,
  lineCount: 0, lineTail: "", renderedTask: null, scrollTop: 0,
};
const lineHtml = (s, i) =>
  '<div class="line" data-i="' + i + '"><time>' + s.start + "</time><span>" + (s.text || "") + "</span></div>";
const bound = [];
const bindLine = (el) => { bound.push(el); };
const markActive = () => {};
let currentId = "task1";
const BOX = { $: (sel) => { if (sel === "#lines") return box; throw new Error("未预期的选择器 " + sel); }, node: box };
const DOC = { createElement: (tag) => new El(tag) };
const render = factory(DOC, BOX, ws, () => currentId, lineHtml, bindLine, markActive,
  (x) => x, (x) => x);

function seg(n, prefix) {
  const out = [];
  for (let i = 0; i < n; i++) {
    out.push({ start: i * 10, text: (prefix || "第") + (i + 1) + "句" });
  }
  return out;
}
function liveTask(n) {
  return { status: "running", stage: "transcribing", segments: seg(n) };
}
function doneTask(n) {
  return { status: "transcribed", stage: "transcribed", segments: seg(n) };
}

/* ---------- 断言 ---------- */
let pass = 0, fail = 0;
function check(name, cond, detail) {
  if (cond) { pass++; emit("  PASS  " + name + (detail ? "  —— " + detail : "")); }
  else { fail++; emit("  FAIL  " + name + (detail ? "  —— " + detail : "")); }
}
function lineNodes() {
  return box.querySelectorAll(".line:not(.pending-line)");
}

/* ================= 场景 1：增量追加而非重建 ================= */
emit("=".repeat(66));
emit("场景 1：连续 partial 应只追加新句，不重建已有节点");
emit("-".repeat(66));
render(liveTask(3));
const after3 = lineNodes();
emit("  首批渲染 " + after3.length + " 句");
const firstNode = after3[0];

// 再来一次 partial，句数变多
render(liveTask(3));           // 同样的内容：签名不变，应整块跳过
check("内容未变时不重绘", lineNodes()[0] === firstNode, "节点复用");

// 模拟签名变化：换一批内容更长的 segments
currentId = "task1";
ws.linesSig = null;            // SSE partial 会强制清签名
ws.lineTail = "";              // 允许追加（尾句文本一致）
const before = lineNodes().length;
ws.segs = seg(3);
ws.lineCount = 3;
ws.lineTail = "第3句";
const t5 = { status: "running", stage: "transcribing", segments: seg(5) };
render(t5);
const after5 = lineNodes();
check("追加后句数正确", after5.length === 5, before + " -> " + after5.length);
check("已有节点被复用（未被重建）", after5[0] === firstNode,
  after5[0] === firstNode ? "首个 DOM 节点保持同一对象" : "DOM 被重建了");
check("第 4 句内容正确", after5[3] && after5[3].text().indexOf("第4句") >= 0,
  after5[3] ? after5[3].text() : "(无节点)");

/* ================= 场景 2：乱序插入要退回全量重建 ================= */
emit("");
emit("=".repeat(66));
emit("场景 2：并发返回导致乱序时，必须退回全量重建（否则句子错位）");
emit("-".repeat(66));
// 后到的分片时间更早 → 已渲染的第 3 句不再是末尾，内容不匹配
ws.linesSig = null;
const tShuffled = {
  status: "running", stage: "transcribing",
  segments: [{ start: 0, text: "补插的更早句子" },
             { start: 10, text: "第1句" },
             { start: 20, text: "第2句" },
             { start: 30, text: "第3句" }],
};
render(tShuffled);
const shuffled = lineNodes();
check("乱序后仍渲染 4 句", shuffled.length === 4, "" + shuffled.length);
check("新插入的句子出现在最前",
  shuffled[0] && shuffled[0].text().indexOf("补插的更早句子") >= 0,
  shuffled[0] ? shuffled[0].text() : "(无)");
check("顺序按时间排列（第1句在第 2 位）",
  shuffled[1] && shuffled[1].text().indexOf("第1句") >= 0,
  shuffled[1] ? shuffled[1].text() : "(无)");

/* ================= 场景 3：转写提示的增删 ================= */
emit("");
emit("=".repeat(66));
emit("场景 3：「正在识别后续内容…」的出现与移除");
emit("-".repeat(66));
// 重置
ws.lineCount = 0; ws.renderedTask = null; ws.lineTail = "";
ws.linesSig = null;
box.children = [];
render(liveTask(2));
check("转写中出现提示", !!box.querySelector(".pending-line"));
check("提示不在句子之前", box.children[box.children.length - 1] === box.querySelector(".pending-line"),
  "提示位于末尾");

// 再追加，提示应保持在末尾而不是夹在中间
ws.linesSig = null;
ws.lineTail = "第2句";
render({ status: "running", stage: "transcribing", segments: seg(4) });
check("追加后提示仍在末尾",
  box.children[box.children.length - 1] === box.querySelector(".pending-line"));

// 转写结束
ws.linesSig = null;
render(doneTask(4));
check("转写结束后提示消失", !box.querySelector(".pending-line"));
check("句子数不受影响", lineNodes().length === 4, "" + lineNodes().length);

/* ================= 场景 4：空态提示 ================= */
emit("");
emit("=".repeat(66));
emit("场景 4：还没有任何文字时的空态");
emit("-".repeat(66));
ws.lineCount = 0; ws.renderedTask = null; ws.lineTail = "";
ws.linesSig = null;
box.children = [];
box.innerHTML = "";
render({ status: "running", stage: "transcribing", segments: [] });
check("转写中显示引导文案",
  box.innerHTML.indexOf("正在识别") >= 0, box.innerHTML.slice(0, 40));

emit("");
emit("=".repeat(66));
emit("总计：" + pass + " 通过 / " + fail + " 失败");
emit("结论：" + (fail === 0 ? "全部通过" : "有失败项，需修复"));
fs.writeFileSync(path.join(__dirname, "lines_append.txt"), OUT.join("\n"), "utf8");
process.exit(fail === 0 ? 0 : 1);
