"""build-locator-guard — 建设任务工程读取顺序守卫。

借鉴 DSH guard chain：guard 不执行读取，只改变工具调用管道状态。
- 建设任务必须先用 engineering_graph impact 建立影响面。
- locator read/list 在 impact 完成前不可调用。
- read 要求 canonical locator；list 要求 prefix，禁止无前缀枚举。
- raw/wiki/graph.db 即使误传也拒绝，防止工具面绕开文件边界。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from dsh.harness import PostToolDecision, PreToolDecision, ToolExecution, ToolExecutionResult

REPO = Path(__file__).resolve().parent.parent.parent
_ENGINEERING_SCRIPTS = (REPO / ".scripts").resolve()

_LOCATOR_RE = re.compile(
    r"^(?:[^\s#]+)#(?:(?:md|yaml|py):|L\d+(?:-L?\d+)?$)",
    re.IGNORECASE,
)
_FORBIDDEN_PATH_PREFIXES = (
    "raw/",
    "wiki/",
    "academic/raw/",
    "admin/raw/",
    "teaching/raw/",
    "business/raw/",
)

TOOL_BUILD_IMPACT = "build_engineering_impact"
TOOL_BUILD_READ = "build_locator_read"
TOOL_BUILD_LIST = "build_locator_list"


def _clean(value) -> str:
    return str(value or "").strip()


def _is_engineering_path(path: str) -> bool:
    path = _clean(path)
    if not path:
        return False
    if path.startswith("/") or ".." in Path(path).parts:
        return False
    lowered = path.lstrip("./").casefold()
    if lowered.endswith((".db", ".sqlite", ".pdf", ".png", ".jpg", ".jpeg")):
        return False
    if any(lowered.startswith(prefix.casefold()) for prefix in _FORBIDDEN_PATH_PREFIXES):
        return False
    if any(part.casefold() in {"raw", "wiki"} for part in Path(path).parts):
        return False
    return True


@dataclass
class BuildLocatorAudit:
    impact_seen: bool = False
    impact_target: str = ""
    read_locators: list[str] = field(default_factory=list)
    listed_paths: list[str] = field(default_factory=list)
    denials: list[dict] = field(default_factory=list)
    compliant: bool = False

    def as_dict(self) -> dict:
        return {
            "impact_seen": self.impact_seen,
            "impact_target": self.impact_target,
            "read_locators": list(self.read_locators),
            "listed_paths": list(self.listed_paths),
            "denials": list(self.denials),
            "compliant": self.impact_seen and bool(self.read_locators),
        }


class BuildLocatorGuard:
    """建设读取顺序守卫。"""

    def __init__(self):
        self.audit = BuildLocatorAudit()

    def on_pre_execute(self, exec_ctx: ToolExecution) -> PreToolDecision | None:
        name = exec_ctx.name
        args = exec_ctx.arguments or {}

        if name == TOOL_BUILD_IMPACT:
            target = _clean(args.get("target"))
            if not target:
                return PreToolDecision(kind="deny", reason="target 不能为空")
            return None

        if name == TOOL_BUILD_LIST:
            if not self.audit.impact_seen:
                return PreToolDecision(kind="deny", reason="必须先执行 build_engineering_impact 建立影响面")
            path = _clean(args.get("path"))
            prefix = _clean(args.get("prefix"))
            if not path:
                return PreToolDecision(kind="deny", reason="path 不能为空")
            if not prefix:
                return PreToolDecision(kind="deny", reason="list 必须提供过滤 prefix，禁止无前缀枚举")
            if not _is_engineering_path(path):
                return PreToolDecision(kind="deny", reason=f"list 不允许读取该路径: {path}")
            self.audit.listed_paths.append(path)
            return None

        if name == TOOL_BUILD_READ:
            if not self.audit.impact_seen:
                return PreToolDecision(kind="deny", reason="必须先执行 build_engineering_impact 再精确读取")
            locator = _clean(args.get("locator"))
            if not locator or not _LOCATOR_RE.match(locator):
                return PreToolDecision(kind="deny", reason="read 需要 canonical engineering locator")
            path = locator.split("#", 1)[0]
            if not _is_engineering_path(path):
                return PreToolDecision(kind="deny", reason=f"read 不允许读取该路径: {path}")
            self.audit.read_locators.append(locator)
            return None

        return None

    def on_post_execute(self, exec_ctx: ToolExecution,
                        result: ToolExecutionResult) -> PostToolDecision | None:
        if exec_ctx.name != TOOL_BUILD_IMPACT or result.is_error:
            return None
        try:
            payload = json.loads(result.content)
        except json.JSONDecodeError:
            return None
        if not payload.get("ok", False):
            return None
        self.audit.impact_seen = True
        self.audit.impact_target = _clean(exec_ctx.arguments.get("target"))
        return None

    def snapshot(self) -> dict:
        return self.audit.as_dict()
