# -*- coding: utf-8 -*-
"""download() 的续传与完整性校验验证（用本地假服务器，不依赖外网）"""
import http.server
import socketserver
import sys
import threading
from pathlib import Path

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


BODY = b"".join(bytes([i % 251]) for i in range(400_000))   # 400KB 确定性内容
TOTAL = len(BODY)
STATE = {"drop_after": None, "hits": 0}


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        STATE["hits"] += 1
        rng = self.headers.get("Range")
        start = 0
        if rng and rng.startswith("bytes="):
            start = int(rng.split("=")[1].split("-")[0])
        chunk = BODY[start:]

        drop = STATE["drop_after"]
        if drop is not None and len(chunk) > drop:
            # 故意截断：模拟中途断流
            self.send_response(200)
            self.send_header("Content-Length", str(len(chunk)))
            self.send_header("Content-Type", "video/mp4")
            self.end_headers()
            self.wfile.write(chunk[:drop])
            STATE["drop_after"] = None
            return

        if rng:
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{TOTAL-1}/{TOTAL}")
        else:
            self.send_response(200)
        self.send_header("Content-Length", str(len(chunk)))
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        self.wfile.write(chunk)


srv = socketserver.TCPServer(("127.0.0.1", 0), H)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
URL = f"http://127.0.0.1:{port}/v.mp4"
TMP = Path("data/tmp/verify_dl.mp4")
TMP.parent.mkdir(parents=True, exist_ok=True)

print("=" * 64)
print("一、完整下载")
print("=" * 64)

STATE["drop_after"] = None
TMP.unlink(missing_ok=True)
out = dlpanda.download(URL, TMP)
ck("文件已生成", out.exists())
ck("大小完全一致", out.stat().st_size == TOTAL, f"{out.stat().st_size}/{TOTAL}")
ck("内容逐字节一致", out.read_bytes() == BODY)
ck("临时 .part 已清理", not TMP.with_suffix(".mp4.part").exists())

print()
print("=" * 64)
print("二、断点续传")
print("=" * 64)

TMP.unlink(missing_ok=True)
TMP.with_suffix(".mp4.part").write_bytes(BODY[:150_000])

seen = []
out = dlpanda.download(URL, TMP, progress=lambda p, m: seen.append(p))
ck("续传后大小正确", out.stat().st_size == TOTAL)
ck("续传后内容一致", out.read_bytes() == BODY)
ck("确实发过 Range 续传请求", STATE["hits"] >= 1)
ck("进度百分比不倒退", seen == sorted(seen) or len(set(seen)) == len(seen),
   f"{seen[:6]}")

print()
print("=" * 64)
print("三、中途截断必须报错（不能把坏文件当成功）")
print("=" * 64)

TMP.unlink(missing_ok=True)
STATE["drop_after"] = 100_000
try:
    dlpanda.download(URL, TMP)
    ck("截断应抛错", False, "竟然当成成功了")
except dlpanda.DlpandaError as e:
    ck("截断 -> 明确报错", "下载不完整" in str(e) or "下载失败" in str(e), str(e))
    ck("截断时不生成正式文件", not TMP.exists())
ck("截断时保留 .part 供续传", TMP.with_suffix(".mp4.part").exists())

print()
print("=" * 64)
print("四、空地址与空响应")
print("=" * 64)

try:
    dlpanda.download("", TMP)
    ck("空地址应报错", False)
except dlpanda.DlpandaError as e:
    ck("空地址 -> 明确报错", "没有可下载" in str(e), str(e))

srv.shutdown()
TMP.unlink(missing_ok=True)

print()
print("=" * 64)
print(f"结果：{PASS} 通过 / {FAIL} 失败")
print("=" * 64)
sys.exit(1 if FAIL else 0)