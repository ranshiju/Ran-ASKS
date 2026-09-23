# CV（简历制作与维护）操作规范

> `artifact.cv` 是 `workspace` 状态上的用户功能。它读取双语履历工作主记录，执行状态检查、正式版校验、DOCX 渲染和版本比较；不创建新的持续状态或事实源。

## 功能边界

- `projects/<workspace>/cv-records.yaml` 保存选材、双语表述、核验状态和 edition 归属，是工作状态而非事实权威。
- `projects/<workspace>/templates/<id>.template.yaml` 是模块顺序、双语标签、类别映射、必备类别和组内排序的唯一配置；同名 `.docx` 保存版式、占位符与模块书签。edition 只引用模板 ID，不重复声明栏目。
- 学历、任职、论文、项目、奖励和学术服务等事实仍以 Raw 为准；Wiki 与 Graph 只负责发现和到达。
- 新事实或事实状态变化先经过受管 ingest。简历功能只消费已经落入 Raw 的证据，不直接修改 Raw、Wiki 或 `graph.db`。
- DOCX、manifest、PDF 和预览均为可重建派生产物，不自动进入摄入流程。
- Agent backend 负责理解用户意图、组织措辞和发起确定性命令；共享程序不调用模型或 DSH。

## 工作主记录

主记录采用 `schema_version: 1`，每条记录至少包含稳定 `id`、`category`、`date`、双语 `title`、`evidence`、`verification` 和 `include_in`。

完整版模板应在各模块内声明 `required_categories`；校验器不仅检查已入选记录，也阻止名为“完整版”的 edition 因漏选而静默缺少核心栏目。任何入选记录的 `category` 都必须且只能映射到一个模板模块，否则正式渲染会被阻断。

正式 edition 只允许选择：

1. `verification: verified` 的记录；
2. 至少有一个存在的 Raw locator；文件路径表示全篇，带 fragment 时还必须能定位到对应段落、锚点或页面；
3. 在目标语言中存在可用文本的记录。

中文学术简历中的英文论文、英文会议文章和英文专著可以保留英文原文；优先读取 `source_language: en`，早期导入的书目类记录也按类别执行兼容回退，校验报告必须记录该回退。其他缺译不得静默跳过。`draft`、`needs_verification` 和 `retired` 记录可留在工作主记录，但不得进入正式版。

## 用户入口

```bash
python3 .scripts/wg.py cv status --workspace '简历维护'
python3 .scripts/wg.py cv validate academic-full-zh --workspace '简历维护'
python3 .scripts/wg.py cv render academic-full-zh --workspace '简历维护'
python3 .scripts/wg.py cv history --workspace '简历维护'
python3 .scripts/wg.py cv diff <version-a> <version-b> --workspace '简历维护'
python3 .scripts/wg.py cv doctor --workspace '简历维护'
```

`wg.py` 只提供统一 JSON envelope；schema、路径约束、证据门禁、渲染和版本语义由 `.scripts/cv.py` 持有。

## 更新流程

用户报告新增或变化的履历事实时：

1. 判断它是事实变化、措辞变化还是 edition 选材变化；
2. 事实变化先走受管 ingest，取得 Raw locator；
3. Agent 基于已提交事实更新同一条稳定 ID 记录，或新增候选记录；
4. 执行 `cv validate <edition>`，修复阻断项；
5. 执行 `cv render <edition>` 生成不可覆盖的新版本；
6. 用 `cv diff` 向用户说明相对前一版本的记录变化。

纯措辞或翻译更新不需要把生成文本摄入 Raw，但不得改变事实强度。知识库扫描只能提出候选变化，不能静默修改工作主记录。

## DOCX 与版本

- 首期渲染器为本地 `python-docx==1.2.0`；优先使用当前 Python，缺失时可从可信的 Codex bundled runtime 加载。不得自动安装依赖或回落到远程服务。
- DOCX 使用 `native-docx-v1` 模板契约，从工作主记录重新生成，不覆盖原始附件或历史版本。
- 模板 DOCX 使用 `{{profile.<field>}}`、`{{module.<id>.label}}`、`{{module.<id>.records}}` 占位符；每个 YAML 模块必须有同序的不可见 `cv_module_<id>` 书签。书签、占位符或映射不一致均阻断渲染。
- 当前 `academic-full` 模板从既有中文版简历复用页面设置、样式和段落格式，但正文、页眉页脚内容、媒体、自定义 XML/属性和身份元数据均已移除。模板本身不得保存姓名、邮箱、出生日期、机构、项目、论文、合作者、学生或专利号。
- 默认文件名为 `姓名-个人简历-YYYYMMDD.docx` 或 `Name-CV-YYYYMMDD.docx`；同日冲突依次使用 `-r2`、`-r3`。
- 每个 DOCX 同时生成不可变的 `*.manifest.json`，记录 edition、语言、主记录哈希、模板 ID 及模板包哈希、渲染器版本、入选记录 ID、文本哈希、告警和产物 SHA-256。
- `versions/index.jsonl` 是由 manifest 重建的索引，不是版本事实源。

## 校验与交付

正式交付前至少检查：

- 记录 ID 唯一、edition 存在、入选记录状态合格；
- Raw 证据路径和 locator 可用；
- 目标语言文本完整，回退均有明确报告；
- YAML 模块、DOCX 书签及占位符一致，栏目顺序和组内日期排序由模板稳定控制；
- DOCX 可重新打开，段落非空，manifest 中的哈希与产物一致；
- 新版本没有覆盖同名历史文件。

机械检查不等于用户完成视觉验收。涉及字体替换、分页、跨平台 Word 显示或特定申请模板时，仍需实际打开成品检查。
