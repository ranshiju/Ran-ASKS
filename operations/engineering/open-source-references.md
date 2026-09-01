# 开源项目参考记录

> 本文件记录 WikiGraph 项目参考、借鉴或直接使用的开源项目。每次新增参考时须追加条目，不得遗漏。
> 面向公开仓库的英文摘要与许可证边界见
> `operations/engineering/open-source-assets/THIRD_PARTY_NOTICES.md`。

## 记录规范

- **项目名**：官方名称
- **类型**：`architecture` / `code` / `tool` / `api`
- **参考方式**：`直接依赖` / `架构借鉴` / `API 服务` / `工具内化` / `理念参考`
- **使用位置**：代码路径或模块名
- **参考内容**：具体借鉴了什么
- **首次引用**：日期
- **来源 URL**：官方仓库、项目主页或技术报告；无公开来源时明确记录
- **备注**：许可证、替代方案、是否仍需保留等

## 参考项目

| 项目名 | 类型 | 参考方式 | 使用位置 | 参考内容 | 首次引用 | 来源 URL | 备注 |
|--------|------|----------|----------|----------|----------|----------|------|
| DeepSeek Harness (DSH) | `architecture` | 架构借鉴 | `dsh/` | ToolRegistry + Hook 瀑布 + Session log + Guard chain 的 plugin 化设计；`Everything is a Plugin` 理念 | 2026-08-20 | <https://github.com/deepseek-ai/deepseek-harness> | MIT；纯 Python 适配，不依赖 Cordis 运行时 |
| MinerU | `api` | API 服务 | `.scripts/extractor.py` | PDF 结构化提取（Markdown 输出），论文摄入首选引擎 | 2026-07 | <https://github.com/opendatalab/MinerU> | MinerU Open Source License；需 API token；失败不静默回落 |
| BLSC OCR (GLM-4.6V) | `api` | API 服务 | `.scripts/extractor.py` | 视觉模型逐页 OCR，扫描版 PDF 回退引擎 | 2026-08-22 | 无公开仓库；内部 API 服务 | 复用 `.env` LLM endpoint；需 base64 图片 |
| Docling | `tool` | 直接依赖 | `.scripts/extractor.py` | 本地 PDF 提取引擎（独立 venv `.venv-docling`） | 2026-07 | <https://github.com/docling-project/docling> | MIT；论文 PDF 不自动回落，须显式 `--engine docling` |
| PyMuPDF (fitz) | `tool` | 直接依赖 | `.scripts/extractor.py`, `ingest_inbox.py`, `ingest_document.py`, `.scripts/visual_qa.py`, `.scripts/visual_to_editable_ppt.py` | PDF 文字层读取、页面渲染、元数据提取和矢量对象读取 | 2026-06 | <https://github.com/pymupdf/PyMuPDF> | AGPL-3.0；商业许可可选 |
| CodexInspiration | `code` | 工具内化 | `.scripts/extractor.py` | PDF 提取管道整体架构（四级级联、parse-meta 管理），从原项目复制并适配 | 2026-07 | 无公开仓库；个人工具 | 保留本地来源说明 |
| SQLite | `tool` | 直接依赖 | `.scripts/graph_lib.py`, `graph.db` | 图数据库存储引擎 | 2026-06 | <https://www.sqlite.org/> | Python 内置 `sqlite3` 接口；单文件数据库 |
| PyYAML | `tool` | 直接依赖 | 多处配置文件 | YAML 配置解析（`yaml.safe_load`） | 2026-06 | <https://github.com/yaml/pyyaml> | MIT |
| Semantica | `architecture` | 架构借鉴 | `operations/config/graph-schema.yaml`, `.scripts/graph_validate.py`, provenance 与 `temporal_facts` 机制 | 借鉴声明式图约束、可追溯记录和时间有效性，保留 ASKS 的单一图源与 Raw 证据边界 | 2026-08-23 | <https://github.com/semantica-agi/semantica> | MIT；借鉴范围见 ADR-005，不引入 RDF/OWL 运行时 |
| python-pptx | `tool` | 直接依赖 | `.scripts/visual_to_editable_ppt.py`, `projects/ASKS/manu/v9/figures/update_fig3_pptx.py` 及后续稿件版本 | 写入原生文本框、线段、自选图形、自由曲线和 Open XML PowerPoint 对象 | 2026-08-29 | <https://github.com/scanny/python-pptx> | MIT；复杂区域仍按工具契约允许可追踪位图 fallback |

## 待定 / 远期参考

| 项目名 | 参考方式 | 说明 | 来源 URL | 状态 |
|--------|----------|------|----------|------|
| Leiden | 理念参考 | 图社区检测（community detection），用于知识结构涌现分析 | <https://github.com/vtraag/leidenalg> | 远期，`projects/知识结构涌现/notes.md` |
| Cordis | 理念参考 | DSH 原版 TypeScript 运行时 | <https://github.com/cordiverse/cordis> | 已决策不接入（ADR-004），Python 适配层覆盖核心概念 |

---

**维护纪律**：每次新增对开源项目的依赖、借鉴或参考时，必须在本文件追加条目。建设任务中 agent 自主判断是否需要更新；使用任务中发现遗漏时提示用户补充。
