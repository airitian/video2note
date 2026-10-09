"""🖥️ 【本地版】启动入口：python run.py [--port 8765]

技术栈：FastAPI + static/ 下的自绘前端（原生 HTML/CSS/JS）
默认端口 8765，可用 --port 更改；界面在浏览器打开 http://127.0.0.1:8765

另一个版本☁️【Gradio 版】是 app.py（ModelScope 创空间用，固定 7860 端口）。
两者共用 core/ 业务内核与 data/ 配置，可同时运行。

在ModelScope 创空间里，Dockerfile 用 APP_ENTRY=run 也能拉起本版本，
此时监听地址/端口会被强制为 0.0.0.0:7860。
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def main() -> None:
    ap = argparse.ArgumentParser(description="视频转笔记 Web 服务（本地版）")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--reload", action="store_true")
    ap.add_argument(
        "--root-path",
        default="",
        help="反代路径前缀，留空则从环境变量 ROOT_PATH / FASTAPI_ROOT_PATH 读取",
    )
    args = ap.parse_args()

    # Docker / 创空间场景：容器在网关后面，静态资源需要同一个前缀
    root_path = (
        args.root_path
        or (os.environ.get("ROOT_PATH") or "")
        or (os.environ.get("FASTAPI_ROOT_PATH") or "")
    ).strip().rstrip("/")

    import uvicorn
    from core.config import ensure_dirs, load_settings
    from core.main import app  # noqa: F401

    ensure_dirs()
    s = load_settings()
    print("=" * 56)
    print("  视频转笔记 · 本地版(🖥️ FastAPI)  →  http://%s:%d" % (args.host, args.port))
    if root_path:
        print("  反代前缀 root_path = /%s" % root_path)
    print("  （Gradio 版入口为 app.py，默认端口 7860）")
    print("=" * 56)
    print(f"  ASR : {s['asr_base_url']}  model={s['asr_model']}  key={'已配置' if s['asr_api_key'] else '缺失'}")
    print(f"  LLM : {s['llm_base_url']}  model={s['llm_model']}  key={'已配置' if (s['llm_api_key'] or s['asr_api_key']) else '缺失'}")
    if not s["asr_api_key"]:
        print("  提示：请先在页面右上角「设置」中填写模力方舟 API Token")
    print("=" * 56)
    uvicorn.run(
        "core.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        root_path=root_path or "",
    )


if __name__ == "__main__":
    main()
