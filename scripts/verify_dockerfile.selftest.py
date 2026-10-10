"""校验器自测：故意破坏 Dockerfile，确认 verify_dockerfile.py 真的能拦住。

这些用例同时充当 Dockerfile 的回归测试：以后改 Dockerfile 改坏了，跑一遍就知道。
用法：python scripts/verify_dockerfile.selftest.py
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "Dockerfile"
VERIFIER = ROOT / "scripts" / "verify_dockerfile.py"

# (用例名, 对原文的替换, 是否应被拦截)
CASES: list[tuple[str, list[tuple[str, str]], bool]] = [
    ("EXPOSE 端口与默认不一致", [("EXPOSE 8765", "EXPOSE 9000")], True),
    (
        "SERVER_HOST 绑成 127.0.0.1",
        [("    SERVER_HOST=0.0.0.0 \\", "    SERVER_HOST=127.0.0.1 \\")],
        True,
    ),
    (
        "SERVER_PORT 改成 8000",
        # 注意：SERVER_PORT 是 ENV 续行块的最后一行，行尾没有反斜杠
        [("    SERVER_PORT=8765", "    SERVER_PORT=8000")],
        True,
    ),
    (
        "删掉 SERVER_HOST",
        [("    SERVER_HOST=0.0.0.0 \\", "")],
        True,
    ),
    ("COPY 源文件不存在", [("COPY requirements.txt .", "COPY nope.txt .")], True),
    ("COPY 引用不存在的阶段", [("--from=builder", "--from=nope")], True),
    ("硬编码 API Key", [("EXPOSE 8765", "EXPOSE 8765\nENV V2N_LLM_API_KEY=sk-abcdef0123456789abcdef")], True),
    ("首条指令不是 FROM", [("FROM python:3.11-slim-bookworm AS builder", "RUN echo hi\nFROM python:3.11-slim-bookworm AS builder")], True),
    ("拼错指令名", [("EXPOSE 8765", "EXPSOE 8765")], True),
    ("CMD 指向不存在的文件", [("exec python -u run.py", "exec python -u nope.py")], True),
    # 真实线上事故：COPY --from=builder /opt/venv 写在装 Chromium 的 RUN 之后，
    # 构建报 "/opt/venv/bin/python: not found"。校验器必须靠「阶段隔离 + 先收集后检查」抓出来。
    (
        "venv 复制晚于 Chromium 安装",
        [
            ("# venv 必须先落地：下面装 Chromium 的 RUN 要用 /opt/venv/bin/python\nCOPY --from=builder /opt/venv /opt/venv\n\n", ""),
            ("WORKDIR /app\nCOPY . /app", "WORKDIR /app\nCOPY . /app\nCOPY --from=builder /opt/venv /opt/venv"),
        ],
        True,
    ),
]


def run_verifier() -> tuple[int, str]:
    r = subprocess.run(
        [sys.executable, str(VERIFIER)], capture_output=True, text=True, cwd=str(ROOT)
    )
    return r.returncode, r.stdout


def main() -> int:
    original = DOCKERFILE.read_text(encoding="utf-8")
    failures: list[str] = []
    try:
        for name, repls, should_fail in CASES:
            content = original
            for old, new in repls:
                if old not in content:
                    failures.append(f"用例[{name}]自身失效：Dockerfile 里找不到 {old!r}")
                    content = None
                    break
                content = content.replace(old, new)
            if content is None:
                continue
            DOCKERFILE.write_text(content, encoding="utf-8", newline="\n")
            code, out = run_verifier()
            blocked = code != 0
            first_err = next((l for l in out.splitlines() if l.startswith("ERROR")), "")
            if blocked == should_fail:
                if should_fail:
                    print(f"✅ 拦截 {name} -> {first_err[:88]}")
                else:
                    print(f"✅ 放行 {name}（符合预期）")
            else:
                if should_fail:
                    print(f"❌ 漏报 {name} -> 校验器返回 {code}，未产生 ERROR")
                else:
                    print(f"❌ 误报 {name} -> {first_err[:88]}")
                failures.append(f"{'漏报' if should_fail else '误报'} {name}")
    finally:
        DOCKERFILE.write_text(original, encoding="utf-8", newline="\n")

    code, out = run_verifier()
    print("\n还原后:", out.strip().splitlines()[-1] if out.strip() else f"exit={code}")

    if failures:
        print(f"\n❌ {len(failures)} 个用例未通过")
        return 1
    print(f"\n✅ 全部 {len(CASES)} 个用例通过，校验器有效")
    return 0


if __name__ == "__main__":
    sys.exit(main())
