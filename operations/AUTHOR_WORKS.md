# 作者成果检索与统计

## 入口

```bash
python3 .scripts/wg.py author-works '作者姓名或注册别名' --compact
python3 .scripts/author_works.py 'academic/wiki/authors/person' > projects/author-inventory.json
```

Python：`author_works.inventory(author, repo=Path(...), max_pages=20000)`。Agent、API共享确定性实现，不启动模型、DSH或网络。`--repo`用于独立检出或隔离测试，不混用公共/私人数据库。

## 检索与计数契约

作者须通过图中的人物路径、完整姓名或已注册别名唯一解析；同名歧义拒绝静默选择。去除姓名中的格式标点不是模糊姓氏检索。作者边双向读取；边稀疏时联合Wiki作者字段及来源前置信息，不把某一次top-k或邻居边数当全集。

先用rg在公共academic/wiki文本中定位姓名和注册别名，再解析命中文件的authors字段；正文提及或参考文献命中不会直接进入作者集合。作者边作为补充入口。只读取作者候选所指Raw的前置区域；Raw名字命中仍需核对作者身份。已定位的具体缺口可用重复的`--source academic/raw/...`补查，不默认全量扫描Raw或Source回执。Wiki标明的废弃页不作现行默认来源。库外未登记文件及作者尚未入库成果不在覆盖承诺中。

`counts` 是候选源记录、身份分组和分类提示计数。`coverage.prefilter`标明实际rg或Python兼容后端。`publication_count=null`明确避免自动输出无依据的发表总数。`coverage`保留读取失败、无文本PDF、预算截断和世界全集未知；有效零结果与不可查询区分。

相同读取目标或原文独立DOI标头形成去重组；同题名只返回复核候选，不自动合并不同DOI、会议与期刊版本。分类提示来自标题、来源名和Wiki元数据，不能代替原文发表记录。补充材料、专著、知识产权、会议论文集等分列。原始来源和`header_locator`/逐行署名定位随每条记录返回；元数据缺失或缺少受管解析时保留未决，不从PDF正文临时提取冒充已摄入论文文本。

## 宿主交付

1. 调用一次完整枚举，检查覆盖状态，按重复组阅读必要Raw。
2. 逐项确认作者、文献类型、发表状态；将独立研究论文、综述、评论/社论、专业通讯文章、会议材料、预印本分别统计。预印本与正式发表版本关系须有证据，不凭相近标题合并。
3. 汇总表必须能回到逐项清单与Raw。可存projects报告；报告不是新的事实源，不自动摄入。
4. 明确“库内已确认”与作者现实全部发表数量的区别。图边缺失、元数据错误按既有受管维护处理，统计入口不写Raw/Wiki/图。

## 验证

`python3 .scripts/test_author_works.py`；WG/功能注册及工程图回归见实施计划。实现、规范与测试随仓库交付，个人实际统计报告保持在用户项目中。

## 可选的已审阅分类统计

宿主需要重复计算已核查清单时，可传`--review <json>`；不是默认必经阶段，也不追加模型审核。输入`author-works-review-v1`包含本次`inventory_digest`及`decisions`列表，每项用候选`id`、`category`、`evidence:[{locator,quote}]`表达人工/宿主判断，重复版本另给`duplicate_of`。`journal_research/journal_review/journal_commentary/journal_editorial`计入期刊文章；`magazine_article`另列；会议、预印本、补充材料、专著、知识产权、论文集、非作品、未知各自分组。程序只检查当前集合、原文引用和加总，不认证分类语义或外部发表全集。

这避免将本次手工结论硬编码进检索器，也允许普通查询直接读检索结果回答。
