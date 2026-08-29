# 物理论文写作讨论注意力清单

> 适用范围：研究论文（尤其物理 / PRX 风格）的写作、修改与讨论。
>
> 使用方式：这些要点在讨论和改写中**内化使用**，不按固定流程输出。除非用户明确要求列出清单，否则只针对当前文本或当前问题提出相关的点，不逐条念检查表。
>
> 能力组合：进入实际起草、改写或润色时，先调用 `python3 .scripts/route.py --capability write --capability-profile academic`，加载 `WRITE.md` 的共享落笔约定；研究讨论阶段直接使用本清单。

## 三条最高原则

1. **先找论文的物理因果主线，再改句子。**
   - 不按“贡献一、贡献二、贡献三”的行政列表组织论文。
   - 优先用因果推进：定义问题 → 分离单一资源 → 推导后果 → 指出未解释部分 → 引入下一资源。

2. **章节层级必须反映创新层级。**
   - 核心定理 / 核心有限族分类 / 数值结果 / 校准或 attainability 例子 / 标准背景，不能拥有同等结构权重。
   - 校准或应用已有结果的部分，通常应作为核心结果的 subsection，而不是独立大节。

3. **claim strength ≤ evidence strength。**
   - 任何结论的动词和范围，都不能超过它实际拥有的证据强度。
   - 有限族结果必须显式写出有限范围；数值结果不能写成解析定理或全局最优；定向诊断不能推广为全族分类器。

## 证据等级

在讨论一个结论时，先在心里把它归入以下四类之一：

- **A：严格解析结果**
  - 可用的措辞：prove、derive、exact、rigorous、speed limit、minimum interaction time。
  - 只有确有数学证明时才可使用。

- **B：有限族穷举结果**
  - 可用的措辞：exhaustive、exact classification within this finite family、complete finite-family result。
  - 必须写出族范围；不得说成任意 N 或普适定律。

- **C：数值优化 / envelope 结果**
  - 可用的措辞：numerically resolved optimum、numerically saturates the resolved envelope、within the tested resolutions。
  - 不得写成 analytic speed limit、rigorous global optimum、exact M→∞ optimum。

- **D：定向数值诊断**
  - 可用的措辞：targeted test、resolves the tested cases、provides a dynamical route to inference。
  - 不得写成 full-family recovery、universal classifier、complete topology reconstruction，除非相关 case 全部真的测过。

## 讨论中要主动注意的点

### 物理主线与结构

- 每个主要段落是否推进同一条物理因果链。
- 结果之间是否被写成平级贡献列表，而不是层级化叙述。
- 校准、数值方法、标准背景是否被错误地抬升为核心创新。
- 支持性小节是否从属于它服务的核心结果。

### 证据与语言

- 一句话的动词是否匹配证据等级。
- 有限族结论是否写清了范围。
- 数值收敛是否被写成解析证明。
- 定向测试是否被推广成全局分类。
- 是否使用“most difficult”“retain a clear signature”“may potentially indicate”等模糊或过度表达。

### 定义与计算透明度

- 核心量是否在首次实质使用前定义。
- 单位、时间尺度、控制类、目标量、接口集、拓扑描述、分类器是否明确。
- 若正文报告穷举或精确分类，主文是否说明了搜索空间和优化问题，而不是只把定义藏进附录。

### 新旧结果边界

- 已有结果是否被标为标准背景并正确引用。
- 复用已知结果得出新结论时，是否清楚说明 novelty 边界。
- 是否把标准对象或已知方法包装成新定理。

### 摘要、引言与结论

- 摘要是否沿正文同一物理逻辑推进，而不是罗列数值和阈值。
- 引言是否用因果过渡介绍核心结果，而不是 mini 版目录。
- 结论是否在更高概念层面收束，而不是重复开头。
- 摘要中是否出现不必要的 bookkeeping：orbit 标签、9/9、阈值小数、运行参数等。

### 图与正文一致性

- 每张主图是否都在正文被引用。
- 图注动词是否匹配图的实际内容：schematic、exhaustive、representative、quantitative 要分清楚。
- 不要用 “summarized in Fig. X(a)” 描述一个只画了示意图的 panel。

### 文献与优先级

- novelty、首次使用、标准术语、原定义、prior art 等主张必须回到一次文献验证。
- 不确定是否首次提出时，使用安全表述，不写 first defined by。

### 风格

- 优先直接、肯定的物理名词和动词。
- 避免 semicolon；长句优先拆成两句。
- 固定容量与“replenishment”类表述要准确：被补充的是新鲜自由度，不是 capacity 本身。

## 讨论时的默认行为

- 发现问题时，先解释科学上为什么重要，再给推荐写法。
- 对 claim 边界，可同时给一个更强和一个更安全的措辞，让用户选择。
- 不机械输出“证据不足”，要指出具体证据等级和范围缺口。
- 不逐条念清单；只在相关时提出最相关的 1–3 个点。
- 用户给出论文写作、修改或讨论指令时，同样进入本注意力模式。

## 可用能力调用（写作中随时可调）

讨论或修改时需要核验事实、定位文献、回溯 raw 原文，调用 `.scripts/wg.py`（统一 JSON 输出，跨功能、回合中可调）：

- `wg.py lookup <term>` — 关键词查节点（导航层，默认省 token）
- `wg.py neighbors <page>` — 关联召回（图边是定位器非答案）
- `wg.py relations <page> [--predicate P]` — 节点关系边，返回带 `sources` locator
- `wg.py read-raw <locator>` — 按 locator 读 raw 片段核验事实（locator 形如 `path#section`，来自 `relations` 的 `sources`）
- `wg.py hub-of <page>` — 页所属 Hub
- `wg.py recall <project>` — 研究记忆恢复（研究项目内）

溯源纪律不变：图边和 Navigation 只用于定位，任何事实主张（尤其 novelty、首次使用、prior art）必须 `read-raw` 回到 raw 原文确认（见「证据等级」与「文献与优先级」）。

## 实际修改 LaTeX 时

- 新建版本，不覆盖上一版 canonical 文稿。
- 修改后生成 diff。
- 至少编译两遍，并检查：未定义引用、未定义 citation、重复 label、overfull box、图和公式引用。

## 自检速览

- 物理主线是否清楚？
- 章节层级是否匹配创新层级？
- 校准结果是否从属于核心结果？
- 标准结果是否引用并标为标准？
- 核心量是否先定义后使用？
- 强结论的证据等级是否正确？
- 有限族结果是否显式限域？
- 数值最优是否写为数值结果？
- 定向诊断是否只限已测 case？
- 图注是否准确描述 panel？
- 摘要是否去掉不必要 bookkeeping？
- 引言是否保持单一因果链？
- novelty/priority 是否经一次文献验证？
- 若改了 LaTeX，是否新建版本、生成 diff、编译两遍？
