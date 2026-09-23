# Presentation 验收与回归计划

本文件区分可执行测试与尚待实现用例；上位协议为 `operations/PRESENTATION.md`。不使用真实未发表稿件、私有材料或网络 API 作为自动化 fixture。

## 阶段0：当前可执行

```bash
python3 .scripts/test_presentation_runtime.py
python3 .scripts/test_presentation_runtime.py --render
python3 .scripts/test_prompt_audit.py
python3 .scripts/test_visual_qa.py
python3 .scripts/engineering_graph.py validate
```

| 编号 | 验收 | 实施方式 |
|---|---|---|
| P0-01 | presentation/create 按需路由、不创建新 task；拒绝未知 profile 和混合 task/capability | 真实 CLI 子进程 |
| P0-02 | create 路由明确说明阶段1B工程实现与待完成的真实用户验收，只加载当前动作与边界，不注入全套协议 | 路由内容断言 |
| P0-03 | doctor 只检查本地依赖；缺项返回 unavailable 和非零，不安装、不远程回退 | 依赖缺失注入与 CLI 退出码检查 |
| P0-04 | smoke 缓存根符号链接被拒绝，不能经链接写入 Raw 或其他区域 | 临时隔离目录的符号链接用例 |
| P0-05 | 生成或渲染失败记录实际阶段及 failed receipt；已有运行不被覆写 | stub 机械生成/转换依赖，连续运行 |
| P0-06 | 真实单页包含可编辑文字、两个矩形和绑定端点的连接线；PDF 回读包含中英文 | --render 强制真实测试；依赖不满足则失败，不记 SKIP/pass |
| P0-07 | 实际 PDF/PNG、输入/输出哈希、依赖版本及请求/实际字体有记录；视觉审阅和用户批准仍是未执行 | 回执及文件交叉检查 |
| P0-08 | 旧 write/general 与 academic 路由、视觉 QA 原契约不退化 | 现有 prompt 与 QA 回归 |

普通回归不强制启动 LibreOffice，但会清晰报告“未执行真实渲染”；`--render` 才覆盖真实依赖链。代码中的失败注入只用于机械边界，不模拟/评分 Agent 推理。

## 阶段1A：状态与单页回归已实现

```bash
python3 .scripts/test_presentation_state.py
python3 .scripts/test_presentation_state.py --render
python3 .scripts/test_agent_task.py
python3 .scripts/test_prompt_audit.py
python3 .scripts/test_visual_qa.py
python3 .scripts/test_open_source_release.py
python3 .scripts/engineering_graph.py validate
```

普通测试使用隔离临时仓库和显式 TEST-ONLY renderer/确认记录，只验证机械状态边界；--render 才实际生成三个版式并核对 PPTX/PDF/PNG，中英文回读、图片和连接线。真实渲染测试停在 preview_review，没有替用户接受预览或锁定。保存的合成页仅供宿主检查，不是生产模板。

下表 P1-01 至 P1-13 为当前受限单页协议的已实现回归，P1-14 由下述阶段1B导出回归覆盖；P1-15 真实人机验收**尚未完成**，不将整个阶段1标为完成。

| 编号 | 场景 | 必须观察到的结果 |
|---|---|---|
| P1-01 | 新 session、非法 schema、重复页/对象 ID、越界/悬空连接 | 合法输入可提交；非法输入失败且当前指针不变 |
| P1-02 | 未确认内容直接批准设计/生成；缺确认来源 | 拒绝，不允许越级签批 |
| P1-03 | 内容更改/来源 hash 更改 | 内容及下游批准全部失效 |
| P1-04 | 仅改设计 | 内容批准保留，设计及下游失效 |
| P1-05 | 修改第三页 | 第一页锁定依赖及批准快照完全不变 |
| P1-06 | 锁定页普通修改、主题更新、排序及页码变化 | 拒绝隐式改动并报告影响页；显式解锁/重批后才能继续 |
| P1-07 | 旧 expected revision、双进程并发提交 | 一个成功，另一个明确冲突，不丢更新 |
| P1-08 | 在 session/plan/content/design/IR/build/receipt 文件写入后、revision rename及current切换前后共11处终止子进程 | 恢复为完整旧/新 revision；孤立目录只报告；进程锁释放后可继续提交 |
| P1-09 | 删除 temp 缓存后接续 | 持久批准及批准预览依据仍在，可安全重建缓存 |
| P1-10 | 原件改写、哈希素材/单页PPTX快照改写或receipt损坏 | 明确 blocked/完整性错误；源变更须解锁重提，单页成品不自动反写 IR；整套PPT外部改写由 delivery 哈希验证拒绝，不反向覆盖 IR |
| P1-11 | 私有来源在普通项目派生、声明 public 或假冒 raw_verified | private不允许降级，始终local_only，无远程adapter；引用脱敏另按用户明确指令验收，不作为默认交付门禁 |
| P1-12 | 仅机械pass、未记录视觉审阅、review=failed或未接受警告 | 不产生用户批准，拒绝锁定；无手写build-pass导入接口 |
| P1-13 | Agent 语义产物准备与提交 | agent-task-v1 → 同一 validator/commit；不导入 DSH/llm_structured |
| P1-14（工程已覆盖） | 最终组装与默认交付 | 保留结构、文件完整性与页数/页序机械校验；不自动执行三项按需检查，标记未执行，不冒充最终视觉复核通过 |
| P1-15（待真实用户验收） | 文字配图、流程图和标题三页真实人机制作 | 真实用户确认、实际PowerPoint编辑与整套PPTX+receipt；三版式机械渲染不能替代 |

实现映射：`StateTests` 覆盖 schema/IR、批准顺序与快照、内容/设计失效、第三页独立修改、锁页/主题/页序、来源变更、并发与11点进程中断、缓存删除与持久预览、损坏回执/单页PPT、隐私继承、视觉失败、renderer/font过期、Agent task与真实CLI及符号链接隔离；`RealRenderTests` 不 mock renderer，覆盖三个真实版式。普通测试不会因缺渲染依赖伪装跳过真实验收，--render缺依赖直接失败。

当前限制需持续保留：只支持单设备本机POSIX事务，不以本地并发测试证明Synology多机同步一致；只支持现有页集合，不假装已实现插页/删页和页码；PDF文字回读不证明无溢出，字体文件指纹不证明跨PowerPoint字体等价。后续必须补齐P1-15，不能伪造用户确认。三项辅助检查按下列独立扩展验收，不要求用户必须使用它们，也不把未执行作为默认本地交付门禁。

## 阶段1B：整套导出与独立按需入口回归

| 编号 | 场景 | 必须观察到的结果 |
|---|---|---|
| O-01 | 用户仅要求制作/导出 PPT | 不执行形式检查、证据核查或引用脱敏；可简短提醒用户检查，不产生虚假的 passed 记录 |
| O-02 | 用户只要求“PPT形式检查”，指定第3–5页 | 仅检查指定页最终画面与批准预览的差异，报告问题，不改页、不脱敏、不扩展证据核查 |
| O-03 | 用户质疑某页数字/主张/图片 | 仅对疑问范围做“PPT证据核查”，定位来源版本与具体证据，区分有来源和得到支持；没有疑问时不默认全量映射 |
| O-04 | 用户只要求“PPT引用脱敏” | 只处理指定范围的引用信息，保留原件，不连带执行另外两项，不将脱敏等同于公开授权 |
| O-05 | 未请求三项检查或拒绝提醒 | 默认本地交付不受阻；结构错误、来源敏感性、远程授权与锁页边界仍保持 |

上述边界由 `.scripts/test_presentation_delivery.py` 的隔离机械测试覆盖；Agent 对用户意图的理解、实际看图和证据支持判断不由脚本模拟或评分。

```bash
python3 .scripts/test_presentation_delivery.py
python3 .scripts/test_presentation_delivery.py --render
```

- 普通回归覆盖：未锁定/旧 revision 拒绝导出、默认不触发三项动作、temp 删除后成品保留、显式指令/问题/范围、实际预览哈希绑定、非 pass 形式结果、原文精确行定位/引文与缺证据、源漂移、脱敏独立副本/原件与批准不变、check 不渲染、外部改写与符号链接拒绝、依赖变更/生成失败、重复提交/双进程只成功一次、三个实际子进程中断点、独立路由和真实 CLI。
- --render 不 mock renderer，实际生成三个版式单页后组装整套，核对三页 PDF 与原生可编辑文字，再按明确 TEST-ONLY 请求替换备注引用，生成独立 PPTX 并检查旧测试内部引用标识不再存在于 XML。保存合成 PPTX/PDF/PNG 和 TEST-ONLY 说明，生产知识区与活跃项目不受写入。
- 导出测试为了覆盖“必须已有用户锁页”的机械边界使用显式 TEST-ONLY 批准记录，**不是**实际用户接受或 Agent 视觉通过。python-pptx 打开/编辑也不是 PowerPoint GUI 验收。
- 当前引用脱敏只支持指定原生文本字段；图片像素/外部任意 PPTX/全面隐私扫描不在实现承诺内。PDF扫描图/栅格证据不能逐字校验时必须报告不足，不认证科学支持或 raw_verified。

## 阶段2及以后：案例驱动

- 用一份明确授权材料完成 8–12 页学术汇报，核对事实、引用、原始图与数据表；正文可编辑，图片 fallback 如实标记。
- 实际 PowerPoint 打开且无修复提示，检查中英文字体、连接线移动、原生文本/表格/图表编辑。LibreOffice/PDF 回读不能替代这一人工验收。
- 选取 2–3 个现有素材模板测试受支持槽位，不支持的对象要报告，不静默删除或强转为“完全可编辑”。
- 不依赖付费 API 的可用性作为 shared 内核验收条件；新增 API adapter 时，另加同契约/独立控制循环及显式授权测试。
