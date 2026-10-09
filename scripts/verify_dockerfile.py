"""Dockerfile 静态校验：不依赖 Docker daemon，模拟构建期常见失败点。

检查项：
  1. 指令拼写与顺序（FROM 必须在最前，CMD 收尾）
  2. 平台硬性要求：EXPOSE 7860、不出现 8080
  3. 关键环境变量：监听 0.0.0.0:7860、数据落 /mnt/workspace
  4. COPY 的源文件是否真实存在（不存在会在构建时直接失败）
  5. .dockerignore 是否覆盖了 data/ 等运行期产物

用法：
    python scripts/verify_dockerfile.py
退出码 0 表示通过，1 表示有 ERROR。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "Dockerfile"
DOCKERIGNORE = ROOT / ".dockerignore"

# Docker 内置指令白名单（常见子命令也放行）
KNOWN = {
    "FROM", "RUN", "CMD", "LABEL", "EXPOSE", "ENV", "ADD", "COPY", "ENTRYPOINT",
    "VOLUME", "USER", "WORKDIR", "ARG", "ONBUILD", "STOPSIGNAL", "HEALTHCHECK",
    "SHELL", "ARG", "STOPSIGNAL",
}
# ModelScope 平台约束
FORBIDDEN_PORTS = {"8080"}
REQUIRED_PORT = 7860
VALID_INSTRUCTIONS = sorted(KNOWN)


def parse(text: str) -> list[tuple[int, str, str]]:
    """返回 [(行号, 指令, 参数)]，支持反斜杠续行；续行块内的 # 注释行剔除"""
    items: list[tuple[int, str, str]] = []
    buf = ""
    start = 0
    in_block = False
    for i, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not buf:
            if not line or line.startswith("#"):
                continue
            start = i
        # 续行块内的独立注释行：Docker 实际会忽略，直接丢弃
        if line.startswith("#"):
            if buf:
                continue
            continue
        # 反斜杠续行
        if line.endswith("\\"):
            buf += line[:-1].strip() + " "
            in_block = True
            continue
        buf += line
        in_block = False
        parts = buf.split(None, 1)
        instr = parts[0].upper()
        if not re.match(r"^[A-Z]+$", instr):
            buf = ""
            continue
        items.append((start, instr, parts[1].strip() if len(parts) > 1 else ""))
        buf = ""
    return items


def main() -> int:
    errors: list[str] = []
    warns: list[str] = []

    if not DOCKERFILE.exists():
        print("ERROR 找不到 Dockerfile")
        return 1
    text = DOCKERFILE.read_text(encoding="utf-8")
    items = parse(text)

    # ---- 1. 指令合法性 ----
    for lineno, instr, args in items:
        if instr in ("MAINTAINER", "VOLUME"):  # 允许但会有提示
            warns.append(f"L{ineno}: {instr} 已废弃，建议移除")
        if not re.match(r"^[A-Z]+$", instr):
            errors.append(f"L{lineno}: 指令格式非法 -> {instr!r}")
        elif instr not in KNOWN:
            errors.append(f"L{lineno}: 未知指令 {instr}（合法: {', '.join(VALID_INSTRUCTIONS)}）")

    if not items:
        print("ERROR Dockerfile 为空")
        return 1

    # ---- 2. 首个必须是 FROM，最后一个必须是 CMD 或 ENTRYPOINT ----
    if items[0][1] != "FROM":
        errors.append(f"L{items[0][0]}: 首条指令必须是 FROM，实际是 {items[0][1]}")
    last = items[-1][1]
    if last not in ("CMD", "ENTRYPOINT"):
        errors.append(f"L{items[-1][0]}: 最后一条指令应是 CMD/ENTRYPOINT，实际是 {last}")

    # 多阶段：FROM 数量与别名引用
    froms = [(l, a) for l, i, a in items if i == "FROM"]
    stage_aliases: set[str] = set()
    for _, args in froms:
        m = re.match(r"^\S+(?:\s+AS\s+(\S+))?", args, re.IGNORECASE)
        if m and m.group(1):
            stage_aliases.add(m.group(1).lower())

    # ---- 3. 端口约束 ----
    exposes: list[int] = []
    for lineno, instr, args in items:
        if instr == "EXPOSE":
            for tok in re.split(r"[\s/]+", args):
                if tok.isdigit():
                    exposes.append(int(tok))
    if REQUIRED_PORT not in exposes:
        errors.append(f"必须 EXPOSE {REQUIRED_PORT}（平台硬性要求），当前: {exposes or '无'}")
    bad = sorted(set(exposes) & FORBIDDEN_PORTS)
    if bad:
        errors.append(f"EXPOSE 了平台占用端口 {bad}，会导致启动失败")
    for lineno, instr, args in items:
        if instr in ("ENV", "CMD", "ENTRYPOINT") and "8080" in args:
            warns.append(f"L{lineno}: 出现 8080，确认不是要把服务端口设成它")

    # ---- 4. 关键环境变量 ----
    # ENV 可以写成 "A=1 B=2" 一行，也可以多行反斜杠续行，这里统一抽成键值对
    env_pairs: dict[str, str] = {}
    for _, instr, args in items:
        if instr != "ENV":
            continue
        for m in re.finditer(r"([A-Za-z_][A-Za-z0-9_]*)=(\"[^\"]*\"|'[^']*'|\S*)", args):
            env_pairs[m.group(1)] = m.group(2).strip("\"'")

    def _val(key: str) -> str | None:
        # Dockerfile ENV 覆盖 CMD 内联环境变量，两处都要认
        if key in env_pairs:
            return env_pairs[key]
        for _, instr, args in items:
            if instr in ("CMD", "ENTRYPOINT") and f"{key}=" in args:
                m = re.search(rf"{key}=(\S+)", args)
                if m:
                    return m.group(1).strip("\"'")
        return None

    if _val("SERVER_HOST") != "0.0.0.0":
        errors.append(
            f"SERVER_HOST 必须是 0.0.0.0（平台反代后无法访问 127.0.0.1），当前: {_val('SERVER_HOST')!r}"
        )
    if _val("SERVER_PORT") != "7860":
        errors.append(f"SERVER_PORT 必须是 7860，当前: {_val('SERVER_PORT')!r}")
    if "/mnt/workspace" not in text:
        warns.append("未出现 /mnt/workspace，数据将无法跨重启保留（core/config.py 依赖该路径）")

    # ---- 5. COPY 源存在性 + 阶段别名 ----
    for lineno, instr, args in items:
        if instr != "COPY":
            continue
        parts = [p for p in re.split(r"\s+", args) if p]
        if not parts:
            errors.append(f"L{lineno}: COPY 缺少参数")
            continue
        # `--from=builder a b`：跳过 --from 本身，剩下全是构建阶段内的路径，不查本地
        if parts[0].startswith("--from="):
            alias = parts[0].split("=", 1)[1].lower()
            if alias not in stage_aliases:
                errors.append(f"L{lineno}: COPY --from={alias} 但没有同名构建阶段")
            for tok in parts[1:-1]:
                if not tok.startswith("/") and tok not in (".", "./"):
                    warns.append(f"L{lineno}: --from 阶段的相对路径 {tok} 建议用绝对路径")
            continue
        for src in parts[:-1]:
            if src.startswith("--from="):
                errors.append(f"L{lineno}: --from 必须写在 COPY 首位")
                continue
            if src in (".", "./", "/"):
                continue
            # 支持通配，只做粗略存在性判断
            if any(c in src for c in "*?["):
                if not list(ROOT.glob(src)):
                    warns.append(f"L{lineno}: COPY 通配 {src} 在仓库里没匹配到文件")
                continue
            if src.startswith("/"):
                continue  # 容器内绝对路径，跳过
            if not (ROOT / src).exists():
                errors.append(f"L{lineno}: COPY 源不存在 -> {src}")

    # ---- 6. .dockerignore 覆盖度 ----
    if not DOCKERIGNORE.exists():
        warns.append("没有 .dockerignore，构建上下文会带上 data/ 与本地媒体")
    else:
        di = DOCKERIGNORE.read_text(encoding="utf-8")
        for pattern in ("data/", "__pycache__/", ".git"):
            if pattern not in di:
                warns.append(f".dockerignore 未排除 {pattern}")

    # ---- 7. 密钥硬编码扫描 ----
    for lineno, raw in enumerate(text.splitlines(), 1):
        if re.search(r"(api[_-]?key|token|secret)\s*=\s*[\"']?[A-Za-z0-9_\-]{16,}", raw, re.I):
            errors.append(f"L{lineno}: 疑似硬编码密钥，必须走环境变量/secret")

    # ---- 8. CMD/ENTRYPOINT 里启动的 .py 必须真实存在 ----
    for lineno, instr, args in items:
        if instr not in ("CMD", "ENTRYPOINT"):
            continue
        for m in re.finditer(r"\b([A-Za-z0-9_\-]+\.py)\b", args):
            target = m.group(1)
            if target.startswith("-") or not (ROOT / target).exists():
                errors.append(f"L{lineno}: {instr} 启动的 {target} 在仓库里不存在")

    # ---- 9. 指令顺序：路径必须先被创建/复制，后面才能使用 ----
    # 真实踩过的坑：COPY --from=builder /opt/venv 写在装 Chromium 的 RUN 之后，
    # 构建报 "/opt/venv/bin/python: not found"。这里按出现顺序做数据流检查。
    #
    # 关键：**按构建阶段隔离**。builder 阶段的 python -m venv /opt/venv 对运行阶段
    # 不可见，若不隔离会漏判（L24 提供 /opt/venv，L59 在运行阶段用它）。
    # 同一条指令内部「先创建后使用」是合法的，所以自身 provides 先登记再判uses。
    order_problems: list[tuple[int, int, str, str]] = []  # (使用行, 提供行, 路径, 指令)

    def _collect_provides(instr: str, args: str) -> list[str]:
        out: list[str] = []
        if instr == "COPY":
            out += [p for p in re.split(r"\s+", args) if p and not p.startswith("--")]
        elif instr == "RUN":
            # python -m venv X / mkdir -p X / install -d X 都算创建 X
            out += re.findall(r"-m\s+venv\s+([\w/.-]+)", args)
            out += re.findall(r"mkdir\s+(?:-p\s+)?([\w/.-]+)", args)
            out += re.findall(r"install\s+-d\s+([\w/.-]+)", args)
        return out

    def _collect_uses(instr: str, args: str) -> list[str]:
        out: list[str] = []
        if instr in ("RUN", "CMD", "ENTRYPOINT"):
            out += re.findall(r"(?<![\w/])(/opt/[\w/.-]+)", args)
        elif instr == "ENV":
            out += re.findall(r"PATH=[\"']?([^\"'\s]+)", args)
        return out

    # 每个 FROM 开一个新阶段。阶段之间文件系统不共享，provided 不能跨阶段累积。
    # 每个阶段内部要「先收集全阶段 provides，再检查 uses」：单遍遍历时，
    # 后面的 COPY 还没登记，前面的使用就查不到来源，会漏判
    # （真实事故：COPY --from=builder /opt/venv 在装 Chromium 的 RUN 之后，单遍查不出来）。
    stages: list[list[tuple[int, str, str]]] = [[]]
    for item in items:
        if item[1] == "FROM":
            stages.append([])
        stages[-1].append(item)

    for stage in stages:
        provided: dict[str, int] = {}
        for lineno, instr, args in stage:
            for p in _collect_provides(instr, args):
                provided.setdefault(p, lineno)
        for lineno, instr, args in stage:
            for u in _collect_uses(instr, args):
                if "$PATH" in u:
                    continue
                src = None
                for p, ln in provided.items():
                    if u == p or u.startswith(p.rstrip("/") + "/"):
                        if src is None or ln < src:
                            src = ln
                # src > lineno 表示「路径在用到它之后才被创建/复制」——构建会报 not found
                if src is not None and src > lineno:
                    order_problems.append((lineno, src, u, instr))
    for use_ln, src_ln, path, instr in order_problems:
        errors.append(
            f"L{use_ln}: {instr} 使用了 {path}，但它在 L{src_ln} 才被复制/创建 —— 构建会报 not found"
        )

    # ---- 输出 ----
    for w in warns:
        print(f"WARN  {w}")
    for e in errors:
        print(f"ERROR {e}")
    print(f"\n阶段数={len(froms)} 指令数={len(items)} EXPOSE={exposes} 警告={len(warns)} 错误={len(errors)}")
    if errors:
        print("\n❌ 校验未通过")
        return 1
    print("✅ Dockerfile 静态校验通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
