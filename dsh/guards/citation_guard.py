"""citation-guard — 回答引用核验 guard。

借鉴 DSH 的 session log 不变量和 WikiGraph Batch 4 的 citation contract：
- 跟踪所有成功的 read_raw 调用，记录 locator
- 在 answer 决策时验证引用是否全部存在于已读来源
- 不阻断回答，只标记核验状态（verified/unverified/no_citations）
"""
from __future__ import annotations

from dataclasses import dataclass, field

from dsh.harness import ToolExecution, ToolExecutionResult


@dataclass
class CitationCheck:
    ok: bool = False
    status: str = ""           # verified | unverified | no_citations
    count: int = 0
    unverified: list[str] = field(default_factory=list)
    message: str = ""


class CitationGuard:
    """回答引用核验 guard。

    跟踪 read_raw 成功调用的 locator，在最终回答时验证引用。
    """

    def __init__(self):
        self.read_sources: list[str] = []

    def on_post_execute(self, exec_ctx: ToolExecution,
                        result: ToolExecutionResult):
        """read_raw 成功时记录 locator。"""
        if exec_ctx.name == "read_raw" and not result.is_error:
            locator = exec_ctx.arguments.get("locator", "")
            if locator and locator not in self.read_sources:
                self.read_sources.append(locator)
        return None

    def check(self, citations: list[str]) -> CitationCheck:
        """验证回答引用是否全部存在于已读来源。"""
        if not citations:
            return CitationCheck(ok=False, status="no_citations",
                                 message="回答无引用")
        unverified = [c for c in citations if c not in self.read_sources]
        if unverified:
            return CitationCheck(ok=False, status="unverified",
                                 unverified=unverified, count=len(citations),
                                 message=f"以下引用未经 read_raw 核验: {unverified}")
        return CitationCheck(ok=True, status="verified", count=len(citations),
                             message=f"全部 {len(citations)} 条引用已核验")
