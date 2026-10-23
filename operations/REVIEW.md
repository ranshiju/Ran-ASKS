# 通用建议型独立审阅

## 功能边界

Canonical ID：`artifact.review`，preview，当前只实现 Agent backend。它是现有工作上下文内可组合的功能，不新建持续工作类型。用户说“请让非专题专家试读这一章，只给建议不改稿”或“从论证结构角度审阅方案”时加载 `python3 .scripts/route.py --capability review`。

宿主持有控制循环，程序只管理快照、协议、机械校验、接收及报告。不调用模型、不启动子 Agent、不隐式回退 API。现有 PNG 展板 API reader 保持独立入口和原行为。

当前接收仓库内 `projects/`、`temp/`、`examples/` 下显式指定的 UTF-8 `.md/.txt` 或单张静态 `.png`；拒绝 private、符号链接及越界路径。选择 `public/internal` 不会改变原来源保密约束；不明确的敏感性或处理权限先确认，不靠更名或复制解除限制。Agent 不等于离线，授权当前宿主不等于授权其他 API。

报告是可反驳的建议，不作作品 pass/fail、自动评分、质量阈值、科学批准或真实人群认证。空意见、只有优点、整体取舍或未采纳均正常。不自动改稿、写 Raw/Wiki/Graph、创建事项、保存长期偏好、再次委派或触发提醒。图片报告按实际附件标识范围，不虚构文本行号；角色复述、直觉图景和理解断点分项呈现，报告可从冻结结果重建而不重调模型。

## 三项独立与实例权限

分别记录新上下文、提供材料范围及工具/文件强制权限，不能合并为 `isolated=true`。宿主能力文件由主 Agent 根据实际配置和观测准备，不由稿件或审阅模型填写；它仍是宿主声明，不是认证凭证。

能力 JSON 使用 `review-host-capabilities-v1`：`host_id`、可选 `host_version`、`fresh_context`（observed/enforced/unknown/unavailable）、`tools`（none/declared-output/broad）、`tools_enforced`（true/false/null）、`processing_location`（local/remote/unknown）、`evidence`、`limitations`。

新上下文 unknown/unavailable 拒绝执行。没有最小工具范围强制时，普通非 private 材料也必须由主 Agent 明确评估并传 `--accept-unenforced-tools`；`--require-enforced-tools` 要求强制最小权限，不能被该风险标记覆盖。该参数只表示本次授权，不使提示词成为沙箱。敏感材料不适用这种弱隔离运行。

实际授权是用户授权、reviewer 上限、全局功能策略及执行器能力的交集。审阅任务只允许返回候选；全局允许 `sub_agent` 使用写作能力不等于本实例获准使用。具备工具过滤的宿主应禁用非必要工具；宿主不能过滤时如实记录，不宣称操作系统隔离。运行时与 OS 账户属于信任基础。

上下文探针只用合成 canary；未出现标记只是有限观测，不证明没有所有隐式上下文。不能创建所需独立会话时报告 unsupported，不在父会话扮演读者充数。

## 准备和执行

公开入口为 `python3 .scripts/wg.py review ...`，底层确定性入口为 `.scripts/review.py`。

```bash
python3 .scripts/wg.py review reviewers
python3 .scripts/wg.py review reviewers reader-comprehension
python3 .scripts/wg.py review prepare \
  --target examples/demo/advisory-review/chapter.md \
  --brief examples/demo/advisory-review/brief.json \
  --reviewer reader-comprehension \
  --host-capabilities temp/host-observations.json \
  --sensitivity public --allow-host-processing
```

若宿主未强制最小工具范围，只在本次材料适用且已真实授权时追加风险标记。示例不是对任何宿主能力的预先证明。可用 `--lines START:END` 精确缩小范围；`--workspace projects/<project>` 将本次记录直接保存在该现有授权项目的 `outputs/reviews/<run-id>/`。不指定则是明确的临时记录，不自动创建工作区。

材料最大 120000 字符，JSON/输入有 512 KiB 字节预算；反馈规范化后最多 256 KiB。超限要求明确缩小范围，不静默截断或用摘要替代原文。每 24 行确定性分块，块 ID 只稳定于本次快照；行号记录原稿选区范围，不承诺跨版本定位。图片、附录、版式和文外事实未检查要在限制中说明。

prepare 返回宿主控制视图：run 路径、来源范围、能力及 `agent-task-v1`。**不要将完整控制视图转发给审阅者。** 主 Agent 只提取 packet 命令的 `result` 或声明的 `packet.json`，以新上下文启动子 Agent，不复制父会话。blind 不提供作者解释、参考答案、其他审阅结果、项目状态、私密路径或工程规范；informed 也只使用有效简报和明确提供的背景。

```bash
python3 .scripts/wg.py review inspect --run <run-path>
python3 .scripts/wg.py review packet --run <run-path>
python3 .scripts/wg.py review start --run <run-path> --call-id <actual-host-call-id>
```

子 Agent 仅返回符合包中 output_schema 的 JSON；主 Agent 原样保存到 task.outputs 声明的唯一 candidate 路径。父 Agent 不改写反馈来迎合结论。`start` 记录实际调用 ID；身份、模型和用量未知时保留 null，不从候选或文字长度推算。该 ID 是宿主记录，不是经认证的模型身份。

```bash
python3 .scripts/wg.py review check --run <run-path>
python3 .scripts/wg.py review collect --run <run-path>
python3 .scripts/wg.py review report --run <run-path>
```

task 的 `read/check/commit` 只映射 review 的 packet/check/collect；commit 是提交审阅记录，不批准作品。不得使用 `wg.py task advance` 或摄入事务状态推进审阅。prepare 不表示已启动，start 不表示已完成，只有接收实际候选才产生结果。

## 审阅者与有效简报

PNG 分支保留冻结原始整图，不静默缩放或裁切；最多16 MiB/2400万像素。packet 只包含匿名 attachment_id、格式、尺寸与字节哈希，不包含本机图片路径或作者答案；宿主从 agent-task inputs 取得冻结图片，以原生附件交给新上下文。单图实际交接后执行 start --image-attached；该标记是宿主声明，不认证传输或模型视觉能力。不支持附件时明确 unsupported，不把 OCR 文本冒充看图。

需要看细节时，prepare 可显式加 `--image-regions <image-view-spec-v1.json>`，复用 visual_qa 的共享本地材料接口；格式和预算见 `VISUAL_QA.md#共享本地材料准备`。不自动分区或调用模型。整图仍为 document-1，1–6 张局部依次为 detail-1 等；`material.views` 使用 `image-view-set-v1` 记录来源哈希、实际整图归一化区域、原整图像素边界和输出字节哈希。PNG 局部不增加源细节；可加 `--detail-source <单页PDF>`，但必须有同目录、同一成品清单分别绑定 PNG 与 PDF。PDF 字节一同冻结，仅供重建和溯源，不交给盲读者；比例一致不代替来源绑定。PDF 改变会使适用性 stale，冻结材料损坏则拒绝。

多图任务只把 packet 和 task 声明的所有 PNG 附件转交新上下文，不转交区域配置文件、成品清单、PDF、父会话或作者解释。实际附图后逐项声明：

```bash
python3 .scripts/wg.py review start --run <run> --call-id <actual-call-id> --attached-image document-1 --attached-image detail-1 --attached-image detail-2
```

ID 必须与当前声明完全一致，不接受单一 `--image-attached` 代替多图交接。候选在 `material_observation.views` 对每张图返回 `{attachment_id,image_received,readability}`，缺图或不可读不计为技术完成；可选 `display_size=[width,height]` 与 `limitations` 仅为自报，未知可省略，不推断为原生分辨率。scope 可用 attachment_id 指明依据哪个附件，但 `region` 一律相对整图，不能把局部坐标直接回填。逐图观察及映射进入可重建 receipt/report；不是实际传输认证、视觉质量通过或理解效果证明。旧单图/文本 packet、schema 与已保存结果不改写。

图片候选额外返回 `material_observation={image_received,readability}`；readability 为 readable/partly-readable/unreadable/unavailable。未收到或不可读不计作审阅完成。图片反馈可用 `scope.type=image`、location 和可选归一化 region=[x,y,width,height]，不支持声称已机械验证的图中引文或文本行号。自报看图与位置描述均不是意见正确性的证明。文本候选保持原公共字段，专属反馈继续按冻结版本校验。

prepare 的可选 --artifact-manifest 只接收与该实际 PNG 同目录且哈希匹配的展板 manifest；快照仅供宿主绑定，不转给盲读者。绑定范围随源图、简报或 manifest 变化而过时。展板 prepare-review/record-review/compare 组合原 review 内核，详见 SCIENCE_EXHIBIT，不改变本功能“不改稿、不批准”的边界。

受信任定义目录为 `agents/reviewers/<id>/`，版本化定义、指令、专属反馈结构各自维护。新增角色不改 Route profile 或通用调度器。默认提供 reader-comprehension 和 argument-structure，不把科普复述强加给公共协议。

brief 是严格 JSON，只覆盖定义声明的 configurable 槽位。公共语义槽位为 goal、audience、use_context、context_mode、focus、constraints、assumptions、parameters；专用参数在 parameters 的本地 schema 中定义。defaults 是默认，不冒充用户已确认；有效值与来源冻结。必要输入不足或方法与授权冲突时明确报错，不静默削弱方法。

定义、指令和本地 schema 在 prepare 时复制并哈希绑定；定义升级不影响在途旧任务，接收用冻结 schema。首版采用 README 描述的有界本地 schema 子集，不执行远程引用或任意扩展代码。评估答案、私人样稿和个人策略不进入公开定义目录。

公共反馈含 schema、overview、feedback、limitations 和角色 extensions。反馈项含 id、kind、scope、observation、basis；options/uncertainty 可选。全文意见无需引文；有引文才机械验证存在性。位置失配单列诊断，不能标成已核实；程序不从引文匹配推导意见正确。

## 运行、完整性与保存

执行进度、协议/快照完整性、当前适用性和可选采纳记录分别解释。原稿/brief 改变只使旧结果 stale；实际审阅快照或冻结资产损坏则拒绝绑定。恢复和 inspect 按已保存 result 推导终态，不以可变进度覆盖已接收结果。

同一候选 hash 重复接收幂等，冲突不得覆盖。坏候选保留在 attempts 并记录技术失败；纠正格式应保留原始候选，需新的模型调用时使用新 run。不因批评数量、意见严厉或未采纳而重试。cancel 只取消本地接收，真实宿主调用取消仍由主 Agent 负责。

`result.json` 是本次接收记录；receipt.json、report.md 可重建。报告生成失败后直接 report，不重新委派；删除 temp candidate 后持久记录仍应能解释。hash 是字节绑定和误改诊断，不是对恶意宿主的真实性证明，不承诺远程 exactly-once。

技术失败不能隐瞒，也不代表稿件质量差。用户明确要求完成审阅而执行失败时，该审阅子任务未完成；不自动改写写作或研究原有完成状态。

## 消费与发布

主 Agent 核对版本和范围，区分正文观察、假设、偏好及待核实事实，可以采纳、部分采纳、反对或暂缓。重要分歧说明理由；不投票、不平均分、不要求用户逐条签字。用户只授权审阅不等于授权改稿。需要改稿时沿原能力入口；采纳不等于已实施。

所有运行文件继承来源敏感性，公开发布只包含受信任代码、匿名定义和合成示例。首次试点使用 examples/demo/advisory-review；离线验证运行 `python3 .scripts/test_review.py`。生产目录不放原始宿主日志、真人反馈或真实审稿材料。preview 不自动升 stable；真实调用、语义效果和真人适用性分别验证。
