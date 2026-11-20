# -*- coding: utf-8 -*-
"""对照实验：python 直推 raw 流，测服务端接收速度（区分浏览器/服务端谁慢）"""
import http.client
import json
import os
import time
import urllib.request

PATH = "data/media/0e39255d8a85/video.mp4"
total = os.path.getsize(PATH)

conn = http.client.HTTPConnection("127.0.0.1", 8765, timeout=120)
conn.putrequest("POST", "/api/tasks/upload-raw?style=general&name=speedtest.mp4")
conn.putheader("Content-Type", "application/octet-stream")
conn.putheader("Content-Length", str(total))
conn.endheaders()

t0 = time.time()
last = t0
sent = 0
marks = []
with open(PATH, "rb") as f:
    while True:
        chunk = f.read(4 * 1024 * 1024)
        if not chunk:
            break
        conn.send(chunk)
        sent += len(chunk)
        now = time.time()
        if now - last >= 2.0:
            marks.append("%d%%@%.1fs" % (sent * 100 // total, now - t0))
            last = now

resp = conn.getresponse()
d = json.loads(resp.read())
el = time.time() - t0
print("接收 %dMB 用时 %.1fs -> %.1f MB/s" % (total / 1e6, el, total / 1e6 / el))
print("进度点:", marks)
tid = d.get("id")
print("任务:", tid)
# 立刻取消并删除，别让它跑转写
try:
    urllib.request.urlopen(urllib.request.Request(
        "http://127.0.0.1:8765/api/tasks/%s/cancel" % tid, data=b"{}",
        headers={"content-type": "application/json"}, method="POST"), timeout=15).read()
except Exception as e:
    print("取消:", e)
time.sleep(2)
urllib.request.urlopen(urllib.request.Request(
    "http://127.0.0.1:8765/api/tasks/" + tid, method="DELETE"), timeout=15).read()
print("已清理测试任务")
