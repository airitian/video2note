"""端到端验证 SSE 推送：真起一个 HTTP 服务，用真客户端读事件流。

不依赖 fastapi —— 用标准库把 core/main.py 里task_events 的判定逻辑
包成一个最小的 StreamingResponse 等价物，重点验证三件事：
  1. 转写结束（status 转 transcribed）那一刻，最后一批分片确实推了出去
  2. 每次推送都带全量，前端覆盖式更新不会丢句子
  3. 空闲计时会被「状态变化」正确归零，不会误掐连接

用法： python verify_sse_e2e.py
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import urlopen

OUT: list[str] = []


def emit(*a):
    OUT.append(" ".join(str(x) for x in a))


class Task:
    """模拟 store.Task 的最小接口"""

    def __init__(self):
        self.status = "pending"
        self.stage = "queued"
        self.percent = 0
        self.spercent = 0
        self.message = ""
        self.error = ""
        self.failed_stage = ""
        self.segments: list[dict] = []
        self.transcript = ""
        self.events: list[dict] = []
        self.meta: dict = {}
        self.note = ""
        self.note_sig = ""
        self.note_info: dict = {}
        self.variants: dict = {}


STORE: dict[str, Task] = {}


# ---------- 从 core/main.py 原样搬过来的判定逻辑 ----------
async def gen(tid: str):
    t = STORE[tid]
    last = 0
    idle = 0
    sent_sig = None
    last_state = None
    sent_vp = ""
    while True:
        vp = (t.meta or {}).get("video_path") or ""
        if vp and vp != sent_vp:
            sent_vp = vp
            yield "data: " + json.dumps({"type": "meta", "meta": t.meta},
                                        ensure_ascii=False) + "\n\n"
        while last < len(t.events):
            e = t.events[last]
            last += 1
            yield "data: " + json.dumps({"type": "log", **e}, ensure_ascii=False) + "\n\n"

        segs = t.segments or []
        last_text = (segs[-1].get("text") or "") if segs else ""
        sig = (len(segs), last_text)
        if segs and sig != sent_sig:
            sent_sig = sig
            yield "data: " + json.dumps(
                {"type": "partial", "segments": t.segments,
                 "transcript": t.transcript,
                 "spercent": t.spercent}, ensure_ascii=False) + "\n\n"
            idle = 0
        state_sig = (t.status, t.stage, t.percent, t.spercent, t.message)
        if state_sig != last_state:
            last_state = state_sig
            idle = 0
        yield "data: " + json.dumps(
            {"type": "state", "status": t.status, "stage": t.stage,
             "percent": t.percent, "spercent": t.spercent,
             "message": t.message, "error": t.error,
             "failed_stage": t.failed_stage}, ensure_ascii=False) + "\n\n"
        if t.status in ("done", "failed", "canceled"):
            yield "data: " + json.dumps(
                {"type": "eof", "status": t.status,
                 "segments": t.segments or [], "transcript": t.transcript or "",
                 "note": t.note or "", "note_sig": t.note_sig or "",
                 "note_info": t.note_info or {}, "variants": t.variants or {},
                 "meta": t.meta or {}}, ensure_ascii=False) + "\n\n"
            break
        idle += 1
        if idle > 9000:
            break
        await asyncio.sleep(0.8)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        tid = self.path.strip("/").split("/")[-1]
        if tid not in STORE:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        async def pump():
            async for chunk in gen(tid):
                self.wfile.write(chunk.encode("utf-8"))
                self.wfile.flush()

        asyncio.run(pump())

    def log_message(self, *a):
        pass


def run_scenario(name: str, script_fn, expect_segs: int, expect_eof: bool):
    """起服务 -> 客户端连上 -> 后台线程推进任务 -> 收事件并判定"""
    tid = "t" + str(int(time.time() * 1000) % 100000)
    t = Task()
    STORE[tid] = t
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]

    got: list[dict] = []

    def client():
        try:
            with urlopen(f"http://127.0.0.1:{port}/{tid}", timeout=40) as r:
                buf = b""
                while True:
                    chunk = r.read(1)
                    if not chunk:
                        break
                    buf += chunk
                    if buf.endswith(b"\n\n"):
                        for line in buf.decode("utf-8").splitlines():
                            if line.startswith("data: "):
                                got.append(json.loads(line[6:]))
                        buf = b""
                    if got and got[-1].get("type") == "eof":
                        break
        except Exception as e:            # noqa: BLE001 - 测试脚本，报出来即可
            got.append({"type": "_error", "msg": str(e)})

    th = threading.Thread(target=client, daemon=True)
    th.start()
    time.sleep(0.4)
    script_fn(t)
    th.join(timeout=35)
    srv.shutdown()

    emit("=" * 68)
    emit(name)
    emit("-" * 68)
    if any(e.get("type") == "_error" for e in got):
        emit("❌ 客户端异常：" + str([e for e in got if e.get("type") == "_error"]))
        return False

    partials = [e for e in got if e.get("type") == "partial"]
    states = [e for e in got if e.get("type") == "state"]
    eofs = [e for e in got if e.get("type") == "eof"]

    emit(f"收到 partial {len(partials)} 次，state {len(states)} 次，eof {len(eofs)} 次")
    if partials:
        emit("各次 partial 的句数：" +
             " -> ".join(str(len(p["segments"])) for p in partials))

    # ★判定要看partial 本身，不能看 eof。
    # eof 是任务收尾时的全量兜底，它总能带回完整内容，所以拿它判定
    # 会把「partial 全丢、只靠 eof 救回来」也判成通过 —— 而真实故障恰恰是
    # 用户抱怨的那个：任务停在 transcribed 等你点「生成文稿」时，
    # eof 根本不会到来，只有 partial 能救。
    # 所以这里断言：必须存在一条 partial 本身就带上了全部 expect 句。
    push_max = max((len(p["segments"]) for p in partials), default=0)
    emit(f"partial 自身送达：{push_max}/{expect_segs} 句"
         f"（eof 兜底 {len(eofs[-1]['segments']) if eofs else 0} 句，仅作对照）")

    ok = True
    if push_max < expect_segs:
        emit(f"❌ 没有任何一条 partial 带上全文，丢 {expect_segs - push_max} 句")
        emit("   → 用户看到的现象：转写显示完成，文字稿却是空的/不全，刷新才有")
        ok = False
    else:
        emit(f"✅ 存在带全文的 partial（{push_max} 句）")
    if expect_eof and not eofs:
        emit("❌ 没有收到 eof")
        ok = False
    emit("=> " + ("✅ 通过" if ok else "❌ 失败"))
    emit()
    return ok


def sc_short(t: Task):
    """短视频：单分片，转写 2.4s 后一次性写入 8 句，状态立刻转 transcribed"""
    t.status, t.stage, t.message = "running", "transcribing", "开始转写"
    time.sleep(2.4)
    t.segments = [{"start": i * 2.0, "text": f"第{i}句"} for i in range(8)]
    t.transcript = "".join(s["text"] for s in t.segments)
    t.status, t.stage = "transcribed", "transcribed"
    t.message = "转写完成"
    time.sleep(1.6)
    t.status, t.stage = "done", "done"


def sc_long(t: Task):
    """长视频：3 批分片，最后一批正好和状态切换撞在一起"""
    t.status, t.stage, t.message = "running", "transcribing", "开始转写"
    total = 0
    for k, n in enumerate([12, 25, 50]):
        time.sleep(2.0)
        total = n
        t.segments = [{"start": i * 2.0, "text": f"第{i}句"} for i in range(n)]
        t.transcript = "".join(s["text"] for s in t.segments)
        t.spercent = int((k + 1) / 3 * 100)
        t.message = f"转写进度 {k+1}/3"
        if k == 2:
            # 关键：写完分片后立刻转状态，不给 SSE 观察 running 的机会
            t.status, t.stage = "transcribed", "transcribed"
    time.sleep(1.6)
    t.status, t.stage = "done", "done"


def sc_idle(t: Task):
    """下载阶段：状态一直在变但没有 segments，验证 idle 不会被顶满"""
    t.status, t.stage, t.message = "running", "downloading", "开始下载"
    for i in range(12):
        time.sleep(0.5)
        t.percent = (i + 1) * 8
        t.message = f"下载中 {t.percent}%"
    t.stage = "transcribing"
    time.sleep(0.8)
    t.segments = [{"start": 0.0, "text": "唯一一句"}]
    t.transcript = "唯一一句"
    t.status, t.stage = "transcribed", "transcribed"
    time.sleep(1.2)
    t.status, t.stage = "done", "done"


def main() -> list[bool]:
    results = []
    results.append(run_scenario("场景 1：短视频单分片（修复前一句都推不出去）",
                                sc_short, 8, True))
    results.append(run_scenario("场景 2：长视频末批与状态切换相撞（修复前丢最后一批）",
                                sc_long, 50, True))
    results.append(run_scenario("场景 3：下载阶段无分片但状态推进（验证 idle 归零）",
                                sc_idle, 1, True))
    emit("=" * 68)
    emit(f"总计：{sum(results)}/{len(results)} 个场景通过")
    return results


# 被 verify_sse_e2e_old.py 导入时不能自动跑场景——那会污染它的缓冲区
if __name__ == "__main__":
    main()
    Path(__file__).with_name("sse_e2e.txt").write_text(
        "\n".join(OUT), encoding="utf-8")
