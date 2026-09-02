# 共享惯例

> 多个子项目共用的命名规范和约定。

---

## 会议纪要文件命名规范

会议纪要存放在 `raw/conferences/YYYY/` 中（按年份分目录），文件名为 `MMDD.ext`（月日各两位数字，保留原始扩展名）。

- 示例：`raw/conferences/2026/0608.txt` → 2026年6月8日
- 示例：`raw/conferences/2026/0624.docx` → 2026年6月24日
- 摄入时自动从路径和文件名提取 `YYYY-MM-DD`，写入 Frontmatter `date` 字段

**低置信度提醒**：会议纪要多为口头讨论，存在不准确可能。仅限以下情况参考：
(a) 用户明确要求，(b) 需要追踪过程文件或获取灵感。
Query 回答中不应将会议纪要作为唯一权威来源，引用需注明"据会议讨论，可能存在不准确"。


---

## 网页资料 raw 格式

网页抓取内容存为 `*/raw/web-references/YYYY/YYYYMMDD-<标题简写>.md`（纯文本 md，保留原文关键内容不加工）。头部用固定字段块，便于程序提取与可回溯：

```
<!-- extracted_by: <工具名/手动> -->
# <页面标题>

> **来源**：<站点/作者名>
> **原文链接**：<URL>
> **发布时间**：YYYY-MM-DD（如页面标注）
> **抓取时间**：YYYY-MM-DD
> **机构/出处**：<发布机构>（如有）
```

随后为正文。wiki 页 frontmatter 的 `url`/`source_name`/`confidence` 与此头部对齐；`source_type: web`，`confidence` 按**来源权威性**分层（见 `academic/SCHEMA.md`）：官方/权威站点→high，论坛/自媒体→low，默认 medium。

---

## 命名与链接规范

- 文件名 kebab-case
- 内部链接 `[[wikilinks]]`
- 锚点 `[[page#slug]]`
- 来源引用相对 `raw/` 路径
- 中文文件名允许,wikilink 需一致

## 子项目与操作规范清单

- **子项目**:academic(学术)/ admin(行政)/ teaching(教学)/ business(转化)/ cross-domain(跨域)/ **private(私人,物理隔离)**。各 `*/SCHEMA.md` 定义页面类型 + Frontmatter。
  - **private 隔离**:独立 `private/graph.db`,不进 `cross-domain` 聚合;主库 ingest/query 不扫描 private,反之亦然(见 INGEST.md「private 领域」)。
- **操作规范**(均在 `operations/` 下,各文件开头自述触发条件):Ingest / Query / Write / Lint / Scan / Sync / Inbox / Discussion

## 下游同步清单(建设行为必检)

修改任一 `SCHEMA.md`/`INGEST.md`/`QUERY.md`/`LINT.md`/`SYNC.md` 时,检查其余规范是否需同步(尤其 INGEST 作主入口最易脱节)。典型场景:

- 新增/变更 Frontmatter 字段 → 所有 SCHEMA + INGEST + ingest_check.py
- 调整索引/图结构 → INGEST + QUERY + SYNC + graph 脚本
- 新增关系类型/谓词 → INGEST + LINT + graph_lib.py(INVERSE_PAIRS 等)
- 新增页面类型 → INGEST + QUERY + 对应 SCHEMA + ingest_check.py 枚举
- 新增/变更确定性规则(枚举/上限/字段) → 同步 `ingest_check.py`/`graph_*.py` 硬编码(壳统一到 spec.yaml 后此处改为改一处)

建设前先用 `.scripts/engineering_graph.py impact <节点> --verify` 查询工程元图影响面及最小回归集；对高风险脚本先用 `.scripts/engineering_graph.py contract <节点>` 读取前置、可写、禁止和成功后验证。该结果是最小充分起点，不替代对实际命中规范/实现的阅读。元图未覆盖节点或输出与实现不符时，必须显式报告并补充检查，不得把未覆盖误作无影响。

## 建设交付的工程文档维护

使用任务不自动更新工程文档。建设任务落地后，必须检查 `operations/engineering/engineering-handbook.md`、`operations/engineering/code-guidance.md`、`operations/engineering/graph.yaml` 及受影响的专项规范；实现与回归通过后，由 Agent 自主同步实际受影响的文档，不等待另一次提醒，也不扩写无关章节。用户明确要求“同步工程文档”时，按当前实现做定向对账并立即修正漂移。工程手册与代码指南分别承担工程全景与脚本调用指导，使下一位 LLM 无需依赖对话历史，仍能掌握当前工程的真理源、流程、脚本职责、数据边界和操作禁令。

`projects/知识结构涌现/idea-deltas.md` 只记录核心设计理念与方法论，**仅在用户明确要求更新时修改**；建设变更本身不自动改写它。若实现变化与其中理念冲突，应向用户指出，不得静默重释或重写理念。

工程级不可逆分工（工程元图、数据真理源、金样边界）记录在 `operations/engineering/adr/`，与 `graph.yaml` 同域管理；不要写入研究课题目录。


---

## section retrieval 读法

建设任务读取工程规范、配置、代码和测试时，不因首次接触而整读：

1. `engineering_graph.py impact <target> --verify` 取得影响面；先直接 `read` 它给出的 graph.yaml 与 code-guidance 推荐精确 locator。
2. 推荐不足时才运行 impact 给出的 `engineering_locator.py list <path> --prefix <prefix>`；不得先枚举大型 YAML 全表。
3. 选择返回的 Markdown/YAML/Python/行段 locator 执行 `read`；失败时根据 candidates 修正，不静默全文回退。

`rg` 只用于定位候选文件或符号。Wiki/Raw 内容继续使用各自专用 section/raw locator；功能性任务不调用工程 locator。


---

## 系统设计原则(建设行为的设计判断依据)

> 软性指引(非硬合规),按基础性排序,前为后之前提。详述见 `projects/kr-wiki-paper/thesis.md` 3.7。

1. 效果导向(六维联合效果,非技术偏好)
2. 去繁就简与效率优先(项目优先减少机制、状态、往返和重复扫描；新增复杂度必须证明其必要性与净收益)
3. 导航中心的图建设(graph.db 优先服务发现、到达和关键关联，不追求事实穷举；所有入图关系仍须符合事实并可回溯)
4. 单一事实源(主知识一份,视图派生;v4:节点属性在 md、边在 graph.db 分离式)
5. 证据保持与可回溯(每层带 sources 链回溯 raw)
6. 语义-确定性分离(原子任务级:语义归 LLM,机械归程序)
7. Schema 契约(语义开放、接口封闭)
8. 闭环提交(LLM 提议→程序校验→定向修复→复验→派生提交)
9. 预防优先的问题治理(先消除错误产生的条件与不合理流程，防止再次出现；阻断、拦截和事后修复仅作不得已的兜底)
10. 能验证则验证(程序可机械判断的不依赖 LLM 自检)
11. 能派生则派生(可稳定重建的不让 LLM 重复生成)
12. 失败显式化(不静默降级,错误可定位)
13. 最小充分读取(读最小成本信息,停止条件是完成度非固定层数)
14. 规模不变性(单次操作消耗不随库体量增长)
15. 局部即时 vs 全局周期(摄入查局部,全库健康周期扫)
16. 最小机制(不重复造轮子 + 奥卡姆)

## 开源发布治理

公开发布采用**白名单构建**，不是从个人知识库删减复制。权威清单是 `operations/engineering/open-source-manifest.yaml`：未显式声明的文件默认私有；`raw/`、`wiki/`、数据库、缓存、收件箱、个人记忆、真实项目资料和输出不进入公开树。

- 构建与审计入口：`.scripts/open_source_release.py build <目标目录> --clean --force`，随后执行 `verify <目标目录>`；目标目录会写入 `.wikigraph-public-release` 标记。
- 公开发布版本号以根目录 `VERSION` 为唯一来源（`MAJOR.MINOR.PATCH`）；构建器把它写入发布树并同步到公开 `README.md` 的 `> Current release: v<version>` 行，`verify` 会拒绝来源、发布树、README 三者不一致。该版本与摄入管线 `CURRENT_PIPELINE_VERSION` 分离。
- 工程规则与个人信息混在同一文档时，必须先拆分：稳定、通用的机制放公开工程文件；真实状态、实验、身份信息或业务上下文放私有 `*.private.md` 或私有项目状态文件。
- 新增可公开工程文件时，同步更新 manifest、发布资产（如有）和 `.scripts/test_open_source_release.py`；不得手工向生成的公开树补入业务文件。
