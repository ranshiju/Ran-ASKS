"""dispatch.py — DSH 工作流选择器。

根据用户意图选择 visual/query/ingest/research loop：
- 显式图片/PDF 转可编辑 PPT → VisualReconstructionAgentLoop
- 显式视觉检查/质检，或依赖可见状态的视觉产物修改 → VisualAgentLoop
- ingest/摄入 → IngestAgentLoop
- query/查/问/找 → AgentLoop
- research/project/研究 → AgentLoop（研究记忆工具已在 query tool seam 中）
"""
from __future__ import annotations

import re

from dsh.agent_loop import AgentLoop, IngestAgentLoop
from dsh.visual_agent_loop import VisualAgentLoop
from dsh.visual_reconstruction_agent_loop import VisualReconstructionAgentLoop


VISUAL_RECONSTRUCTION_INTENTS = (
    "visual reconstruction", "editable ppt", "editable powerpoint",
    "转为可编辑 ppt", "转成可编辑 ppt", "转换为可编辑 ppt", "转换成可编辑 ppt",
    "转为可编辑ppt", "转成可编辑ppt", "转换为可编辑ppt", "转换成可编辑ppt",
    "图片转 ppt", "图片转ppt", "图像转 ppt", "图像转ppt",
    "pdf 转 ppt", "pdf转ppt", "复刻成 ppt", "复刻成ppt",
    "复刻为 ppt", "复刻为ppt", "对象化为 ppt", "对象化为ppt",
)


VISUAL_INTENTS = (
    "visual qa", "visual check", "视觉检查", "视觉质检", "视觉 qa",
    "视觉产物",
    "检查图片", "检查图像", "图片质检", "图像质检", "检查 pdf 页面",
    "pdf 页面质检", "检查 ppt", "检查 pptx", "ppt 质检", "pptx 质检",
    "幻灯片质检", "检查幻灯片", "作图 qa", "图件质检",
)


VISUAL_ARTIFACT_TERMS = (
    "图片", "图像", "图件", "这张图", "图例", "figure", "image",
    "pdf 页面", "pdf页", "页面", "页", "ppt", "pptx", "幻灯片", "slide",
)


VISUAL_EDIT_ACTIONS = (
    "修改", "调整", "优化", "修复", "改成",
    "modify", "adjust", "optimize", "fix", "change",
)


VISUAL_DIRECT_EDIT_ACTIONS = (
    "移动", "上移", "下移", "左移", "右移", "对齐", "放大", "缩小",
    "加大", "减小", "调大", "调小", "美化",
    "move", "align", "resize", "enlarge", "shrink", "beautify",
)


VISUAL_STATE_TERMS = (
    "布局", "排版", "位置", "颜色", "配色", "字号", "字体", "大小", "比例",
    "间距", "留白", "空白", "遮挡", "重叠", "裁切", "边界", "图例", "标注",
    "线条", "形状", "对象", "元素", "上方", "下方", "左边", "右边", "居中",
    "layout", "position", "color", "font", "size", "spacing", "whitespace",
    "overlap", "crop", "legend", "label", "line", "shape", "object", "center",
)


def _needs_visual_context(text: str) -> bool:
    """Return whether an edit depends on inspecting the artifact's visible state."""
    has_artifact = any(term in text for term in VISUAL_ARTIFACT_TERMS) or bool(
        re.search(r"(?:图|表|fig(?:ure)?\.?|table)\s*[0-9一二三四五六七八九十]+", text)
    )
    return (
        has_artifact
        and (
            any(term in text for term in VISUAL_DIRECT_EDIT_ACTIONS)
            or (
                any(term in text for term in VISUAL_EDIT_ACTIONS)
                and any(term in text for term in VISUAL_STATE_TERMS)
            )
        )
    )


def dispatch_loop(intent: str, mode: str = "agent"):
    """返回适合当前意图的 DSH loop 实例。"""
    text = (intent or "").strip().lower()
    if any(k in text for k in VISUAL_RECONSTRUCTION_INTENTS):
        return VisualReconstructionAgentLoop(mode=mode)
    if any(k in text for k in VISUAL_INTENTS) or _needs_visual_context(text):
        return VisualAgentLoop(mode=mode)
    explicit_ingest = any(k in text for k in ("ingest", "摄入", "inbox", "文档摄入"))
    if not explicit_ingest and any(k in text for k in ("query", "查询", "查", "问", "找", "research", "研究")):
        return AgentLoop(mode=mode)
    if any(k in text for k in ("ingest", "摄入", "inbox", "论文 pdf", "会议纪要", "文档摄入")):
        return IngestAgentLoop(mode=mode)
    if any(k in text for k in ("query", "查", "问", "找", "research", "研究")):
        return AgentLoop(mode=mode)
    return AgentLoop(mode=mode)
