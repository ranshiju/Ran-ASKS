#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""lint_specs.py — 提示词规范文档健康度审计（WikiRan 适配版）

审计 AGENTS.md / operations/*.md / */SCHEMA.md / memory/playbooks.md 的提示词健康度。
校验闭环：改规范 → 跑本脚本 → 修全部 ERROR → 复验至 ERROR=0 → 完成
         WARN/INFO 供人复核（语义判断，不强制清零）。

检查项：
  C1 元说明残留              ERROR   "本文件定义…""是…的具体展开"
  C2 圆括号日期版本标记       ERROR   (2026-07-25 新增) 等过程性注解（含版本语义词才判）
  C3 原则引用注解            ERROR   "属原则7""符合单一事实源"
  C4 过程性/历史性叙述        WARN    "重构为""v3→v5改进"
  C5 section 标题日期后缀     ERROR   ## xxx (2026-07-25)
  C6 远期机制密度             WARN    section 内远期词≥3
  C7 route.py 映射命中        ERROR   所有 task/mode/stage 截取成功（含空 stdout 检测）
  C8 跨文件重复原则声明        INFO    原则词出现≥3处且跨≥2文件
  C9 路由契约与提示词预算      ERROR   profile/JSON/预算/旧动作检查

退出码：有 ERROR 返回 1；仅 WARN/INFO 返回 0；未找到文件返回 2。
代码块感知：``` 围栏内的命中不计。
"""
import argparse
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# ---- 默认配置 ----
DEFAULT_EXCLUDE_DIRS = [
    ".git", "node_modules", "__pycache__", "updates", "archive",
    "projects", "slide-library", "agents", "inbox",
]
DEFAULT_PRINCIPLES_FILE = "operations/shared-conventions.md"
DEFAULT_ROUTE = ".scripts/route.py"
DEFAULT_ROUTE_TIMEOUT = 30.0
MAX_DIAGNOSTIC_CHARS = 120
MAX_C8_LOCATIONS = 6
QUERY_PROFILES = {
    "fact": ("这个是什么", 18000),
    "enumeration": ("列出所有经历", 22000),
    "relation": ("关系如何", 24000),
    "traceability": ("为什么以及依据是什么", 26000),
}
FORBIDDEN_QUERY_MARKERS = ("read_keyword_index", "grep_section_names", "contains")

# ---- 检查模式 ----
RE_META = re.compile(r'^>\s*(本文件定义|本子项目.*定义|AGENTS\.md 是全局宪法|.*具体展开)')

# C2: 只判带版本语义的括号日期（新增/改名/v3等），避免误伤业务日期
RE_DATE_PAREN = re.compile(
    r'[（(]'
    r'(?=[^()（）\n]{0,48}(?:版本|修订|更新|改版|调整|迁移|废弃|发布|新增|改名|内化|'
    r'升格|v\d|主数据化|收口|修复|简化|补入))'
    r'20\d{2}-\d{2}-\d{2}[^()（）\n]*'
    r'[)）]',
    re.IGNORECASE,
)

RE_PRIN_NOTE = re.compile(
    r'(符合单一事实源|符合奥卡姆|属原则\s*\d|见[^，。）\n]*三分法|呼应\s*Pink|'
    r'呼应.*机制|对应\s*(Du|Wu|Pink))')

RE_PROCESS = re.compile(
    r'(原["\'].*?违反.*?重构为|重构为[:：]|v\d[^，。\n]*v\d[^，。\n]*改进|'
    r'最初方案.*?撤回|后来改为)')

RE_SEC_DATE = re.compile(r'^#{2,4}\s+.*[（(]20\d{2}-\d{2}-\d{2}')
RE_FUTURE = re.compile(r'(远期|未来|待触发|未实现|not\s*yet|远期第五层)')

# C8: 规范化名称 dict（固定名称→独立正则），避免贪婪正则误匹配
C8_PATTERNS = {
    "单一事实源": re.compile(r"单一事实源"),
    "规模不变性": re.compile(r"规模不变性"),
    "语义-确定性分离": re.compile(r"语义[^，。；;\n]{0,30}确定性[^，。；;\n]{0,20}分离"),
    "奥卡姆剃刀": re.compile(r"奥卡姆剃刀"),
    "最小机制": re.compile(r"最小机制"),
    "能派生则派生": re.compile(r"能派生则派生"),
}


def truncate(text, limit=MAX_DIAGNOSTIC_CHARS):
    """截断诊断信息，避免超长输出。"""
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[:limit - 1] + "…"


def discover_files(root, exclude_dirs):
    """自动探测规范文档。"""
    root = Path(root)
    files = []
    for p in sorted(root.rglob("*.md")):
        if any(part in set(exclude_dirs) for part in p.parts):
            continue
        # 只保留规范文件
        if p.name == "AGENTS.md" or "operations" in p.parts or p.name == "SCHEMA.md" or p.name == "playbooks.md":
            files.append(p)
    return files


def iter_lines(path):
    """代码块感知逐行：返回 (lineno, text, in_fence)。"""
    in_fence = False
    for i, line in enumerate(path.read_text(encoding="utf-8").split("\n"), 1):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        yield i, line, in_fence


def collect_c1_to_c6(path, findings, principles_file):
    """逐文件扫描 C1-C6。准则出处文件跳过 C1/C3/C4。"""
    rel = str(path.relative_to(REPO)) if path.is_relative_to(REPO) else str(path)
    skip_principle = rel == principles_file
    cur_section = ""
    cur_section_line = 0
    section_future = {}  # (section, line) -> count

    for i, line, in_fence in iter_lines(path):
        if in_fence:
            continue
        m_sec = re.match(r'^(#{2,4})\s+(.+)', line)
        if m_sec:
            cur_section = m_sec.group(2).strip()
            cur_section_line = i
        if RE_META.match(line):
            findings.append(("C1", "ERROR", rel, i, f"元说明残留: {truncate(line.strip())}"))
        for m in RE_DATE_PAREN.finditer(line):
            findings.append(("C2", "ERROR", rel, i, f"圆括号日期注: {m.group(0)}"))
        if not skip_principle:
            for m in RE_PRIN_NOTE.finditer(line):
                findings.append(("C3", "ERROR", rel, i, f"原则引用注解: {m.group(0)}"))
            for m in RE_PROCESS.finditer(line):
                findings.append(("C4", "WARN", rel, i, f"过程性叙述: {truncate(m.group(0))}"))
        if RE_SEC_DATE.match(line):
            findings.append(("C5", "ERROR", rel, i, f"section标题日期: {truncate(line.strip())}"))
        if RE_FUTURE.search(line):
            key = (cur_section, cur_section_line)
            section_future[key] = section_future.get(key, 0) + 1
    for (sec, sec_line), n in section_future.items():
        if n >= 3:
            findings.append(("C6", "WARN", rel, sec_line, f"section「{sec}」远期词频 {n}，考虑移按需"))


def collect_c7(findings, route_path, timeout):
    """route.py 映射命中校验。遍历所有 task/mode/stage 组合。
    检测：返回码非零(ERROR) + stdout 为空(ERROR，静默失败)。"""
    rp = REPO / route_path if not Path(route_path).is_absolute() else Path(route_path)
    if not rp.exists():
        findings.append(("C7", "INFO", route_path, 0, "无 route.py，跳过映射命中校验"))
        return
    spec = importlib.util.spec_from_file_location("route", str(rp))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if not hasattr(mod, "ROUTES"):
        findings.append(("C7", "INFO", route_path, 0, "route.py 无 ROUTES，跳过 C7"))
        return

    routes = mod.ROUTES
    checked = 0
    for task, cfg in routes.items():
        if task != "ingest":
            # 非 ingest：只需 --task
            args = [sys.executable, str(rp), "--task", task]
            try:
                r = subprocess.run(args, capture_output=True, text=True,
                                   cwd=str(REPO), timeout=timeout)
            except subprocess.TimeoutExpired:
                findings.append(("C7", "ERROR", route_path, 0,
                                 f"路由校验超时(>{timeout:g}s) task={task}"))
                continue
            checked += 1
            if r.returncode != 0:
                findings.append(("C7", "ERROR", route_path, 0,
                                 f"映射未命中 task={task}: {truncate(r.stderr or r.stdout)}"))
            elif not r.stdout.strip():
                findings.append(("C7", "ERROR", route_path, 0,
                                 f"映射返回成功但 stdout 为空 task={task}"))
            continue
        # ingest：遍历 mode + stage
        modes = cfg.get("modes", {})
        for mode, mode_cfg in modes.items():
            if "stages" in mode_cfg:
                total = len(mode_cfg["stages"])
                for si in range(1, total + 1):
                    args = [sys.executable, str(rp), "--task", task,
                            "--mode", mode, "--stage", str(si),
                            "--subproject", "academic"]
                    try:
                        r = subprocess.run(args, capture_output=True, text=True,
                                           cwd=str(REPO), timeout=timeout)
                    except subprocess.TimeoutExpired:
                        findings.append(("C7", "ERROR", route_path, 0,
                                         f"路由校验超时(>{timeout:g}s) task={task} mode={mode} stage={si}"))
                        continue
                    checked += 1
                    if r.returncode != 0:
                        findings.append(("C7", "ERROR", route_path, 0,
                                         f"映射未命中 task={task} mode={mode} stage={si}: {truncate(r.stderr or r.stdout)}"))
                    elif not r.stdout.strip():
                        findings.append(("C7", "ERROR", route_path, 0,
                                         f"映射返回成功但 stdout 为空 task={task} mode={mode} stage={si}"))
            elif "sections" in mode_cfg:
                args = [sys.executable, str(rp), "--task", task,
                        "--mode", mode, "--subproject", "academic"]
                try:
                    r = subprocess.run(args, capture_output=True, text=True,
                                       cwd=str(REPO), timeout=timeout)
                except subprocess.TimeoutExpired:
                    findings.append(("C7", "ERROR", route_path, 0,
                                     f"路由校验超时(>{timeout:g}s) task={task} mode={mode}"))
                    continue
                checked += 1
                if r.returncode != 0:
                    findings.append(("C7", "ERROR", route_path, 0,
                                     f"映射未命中 task={task} mode={mode}: {truncate(r.stderr or r.stdout)}"))
                elif not r.stdout.strip():
                    findings.append(("C7", "ERROR", route_path, 0,
                                     f"映射返回成功但 stdout 为空 task={task} mode={mode}"))
    findings.append(("C7", "INFO", route_path, 0, f"route.py 校验 {checked} 组合"))


def collect_c8(findings, files, principles_file):
    """跨文件重复原则声明统计（INFO）。出现≥3处且跨≥2文件才报告。"""
    counts = {name: [] for name in C8_PATTERNS}
    for path in files:
        rel = str(path.relative_to(REPO)) if path.is_relative_to(REPO) else str(path)
        if rel == principles_file:
            continue
        for i, line, in_fence in iter_lines(path):
            if in_fence:
                continue
            for name, pat in C8_PATTERNS.items():
                if pat.search(line):
                    counts[name].append((rel, i))
    for name, locs in counts.items():
        distinct_files = set(f for f, _ in locs)
        if len(locs) >= 3 and len(distinct_files) >= 2:
            shown = locs[:MAX_C8_LOCATIONS]
            loc_str = "; ".join(f"{f}:{l}" for f, l in shown)
            if len(locs) > len(shown):
                loc_str += f"; 另 {len(locs) - len(shown)} 处"
            findings.append(("C8", "INFO", "—", 0,
                             f"「{name}」在 {len(distinct_files)} 文件出现 {len(locs)} 处，考虑收敛: {loc_str}"))


def collect_c9(findings, route_path, timeout):
    """验证当前 profile 路由契约、JSON 输出和提示词字符预算。"""
    rp = REPO / route_path if not Path(route_path).is_absolute() else Path(route_path)
    if not rp.exists():
        return
    for expected, (query, limit) in QUERY_PROFILES.items():
        args = [sys.executable, str(rp), "--task", "query", "--query", query,
                "--profile", "auto", "--format", "json"]
        try:
            r = subprocess.run(args, capture_output=True, text=True,
                               cwd=str(REPO), timeout=timeout)
        except subprocess.TimeoutExpired:
            findings.append(("C9", "ERROR", route_path, 0, f"profile 路由超时 profile={expected}"))
            continue
        if r.returncode != 0:
            findings.append(("C9", "ERROR", route_path, 0, f"profile 路由失败 profile={expected}: {truncate(r.stderr or r.stdout)}"))
            continue
        try:
            payload = json.loads(r.stdout)
        except json.JSONDecodeError as exc:
            findings.append(("C9", "ERROR", route_path, 0, f"profile 非法 JSON profile={expected}: {exc}"))
            continue
        actual = payload.get("profile")
        if actual != expected:
            findings.append(("C9", "ERROR", route_path, 0, f"profile 分类漂移 expected={expected} actual={actual}"))
        prompt = payload.get("prompt", "")
        if not prompt or payload.get("estimated_chars") != len(prompt):
            findings.append(("C9", "ERROR", route_path, 0, f"prompt 计量不一致 profile={expected}"))
        if len(prompt) > limit:
            findings.append(("C9", "ERROR", route_path, 0, f"prompt 超预算 profile={expected} chars={len(prompt)} limit={limit}"))
        for marker in FORBIDDEN_QUERY_MARKERS:
            if marker in prompt:
                findings.append(("C9", "ERROR", route_path, 0, f"profile 含废弃/禁止标记 {marker} profile={expected}"))
    findings.append(("C9", "INFO", route_path, 0, "query profile 契约、JSON 和字符预算已校验"))


def main():
    ap = argparse.ArgumentParser(description="提示词规范文档健康度审计")
    ap.add_argument("files", nargs="*", help="审计指定文件（默认自动探测）")
    ap.add_argument("--paths", nargs="+", help="审计多个指定文件")
    ap.add_argument("--exclude", action="append", default=None,
                    help="排除目录（可重复指定）")
    ap.add_argument("--principles-file", default=DEFAULT_PRINCIPLES_FILE,
                    help=f"准则出处文件（跳过其 C1/C3/C4）")
    ap.add_argument("--route", default=DEFAULT_ROUTE,
                    help=f"route.py 路径（不存在则跳过 C7）")
    ap.add_argument("--route-timeout", type=float, default=DEFAULT_ROUTE_TIMEOUT,
                    help=f"单条路由校验超时秒数（默认 {DEFAULT_ROUTE_TIMEOUT:g}）")
    ap.add_argument("--no-c7", action="store_true", help="跳过 C7 route 校验（省时）")
    args = ap.parse_args()

    if args.paths and args.files:
        print("错误: 位置参数与 --paths 不能同时使用", file=sys.stderr)
        sys.exit(2)

    if args.paths:
        targets = [Path(p) for p in args.paths]
    elif args.files:
        targets = [Path(f) for f in args.files]
    else:
        excludes = args.exclude if args.exclude is not None else DEFAULT_EXCLUDE_DIRS
        targets = discover_files(REPO, excludes)

    targets = [t for t in targets if t.exists()]
    if not targets:
        print("未找到任何规范文件", file=sys.stderr)
        sys.exit(2)

    findings = []
    for path in targets:
        try:
            collect_c1_to_c6(path, findings, args.principles_file)
        except (OSError, UnicodeDecodeError) as e:
            print(f"文件读取失败 {path}: {e}", file=sys.stderr)
            sys.exit(2)
    if not args.no_c7:
        collect_c7(findings, args.route, args.route_timeout)
        collect_c9(findings, args.route, args.route_timeout)
    collect_c8(findings, targets, args.principles_file)

    findings.sort(key=lambda x: (x[1] != "ERROR", int(x[0][1:]) if x[0][1:].isdigit() else 99, x[2], x[3]))
    err = sum(1 for f in findings if f[1] == "ERROR")
    warn = sum(1 for f in findings if f[1] == "WARN")
    info = sum(1 for f in findings if f[1] == "INFO")

    print(f"# 提示词规范文档健康度审计\n")
    print(f"扫描 {len(targets)} 文件 | ERROR={err} WARN={warn} INFO={info}\n")
    if not findings:
        print("✓ 全部通过")
        sys.exit(0)
    cur = None
    for cid, sev, f, line, msg in findings:
        if cur != cid:
            cur = cid
            print(f"\n## {cid}")
        print(f"  [{sev}] {f}:{line}  {msg}")
    print(f"\n{'─' * 40}")
    print(f"汇总: ERROR={err} WARN={warn} INFO={info}")
    sys.exit(1 if err else 0)


if __name__ == "__main__":
    main()
