# Windows 兼容性实施计划

> 归档范围说明（2026-11-06）：用户明确 GitHub 主仓库只负责 macOS。以下保留此前调查和实现记录，不再作为主仓库的活动建设计划或发布门槛；CI 已收敛到 macOS。已观察到的 Windows PPTX 换行哈希、来源路径分隔符问题仍未验收，不宣称 Windows 全流程兼容，后续由对应版本维护者处理。

日期：2026-11-06。基线为当前 0.24.1 工作树，保留已有未提交内容。附件是问题线索，不执行其代码、克隆命令或外部补丁。

## 因果与责任边界

- Windows 缺少 fcntl/O_NOFOLLOW，顶层导入或打开失败；中文区域默认编码进入机器 JSON 通道；目录/只读文件刷盘使用 POSIX 假设。责任组件是 shared 平台原语与调用入口。
- ZIP 内部路径使用宿主 Path；视觉 endpoint 硬拼 /v1；失败结果优先取 JSON 首行。责任组件分别为 slide_reuse、visual_qa 和 ingest_inbox；已做最小复现。
- 模型 pass 可遗漏页面内容。shared 检查只能增加可解释的复核线索，不认证语义、不静默降低已有问题或增加模型调用。

## 实施顺序与验收

1. 先注册 shared platform_compat 内核、使用文档、回归和 CI 边界。语义 Agent/API adapter 不合并，平台模块不持有模型控制循环。
2. 修复 ZIP 路径、结构化错误优先、视觉 API_PATH/版本路径及 Windows LibreOffice 发现。
3. 引入平台锁、受保护普通文件打开、刷盘、UTF-8 子进程契约和跨平台相对路径校验。Windows 锁使用原生 LockFileEx/UnlockFileEx，保留共享/独占/非阻塞，永久错误立即传播；不使用吞异常的无限重试。
4. Windows 安全打开保留重解析点防护、父级检查与句柄最终路径核验，不把 O_NOFOLLOW 缺失解释成关闭检查。目录刷盘在 Windows 明确为不支持，文件刷盘/原子替换及事务恢复仍验证；不声称相同断电持久性。
5. 迁移现有锁调用与刷盘责任组件，包括新 citing-papers/paper-impact、图写入、Source/KU；WG 和 Agent 受管子进程统一 UTF-8 严格解码，stdout/stderr 边界明确，二进制输入保留。
6. 添加 PPT 内容稀少/符号字体复核提示，作为非阻断、非事实的提示，保留原视觉回执。
7. 本机针对性回归、隔离进程并发/崩溃恢复测试和 Win32 API 分支模拟；新增 Windows 3.12/3.14 + POSIX CI，中文路径、CP936 调用环境、真实 LibreOffice 渲染与图片替换、junction/symlink 边界单列。

验收不以 unittest discover 的不同用例数作对比；明确脚本清单与跳过原因。当前宿主不是 Windows，Windows 原生测试通过与否必须以实际 CI/实机结果为准；不推送或触发远端工作流，不冒称完成 Windows 认证。

## 实施结果（2026-11-06）

| 反馈 | 已落地处理 |
| --- | --- |
| W01 / W02 | 8 个顶层 POSIX 导入、Graph 写锁及 Source/KU 锁统一进入平台模块；Windows 原生共享/独占锁，非阻塞争用和永久错误分开处理。 |
| W03 | PPTX relationships 使用 PurePosixPath；真实替换图片、ZIP 内容与 LibreOffice PDF 渲染均已验证。 |
| W04 | VISUAL_QA_API_PATH / --api-path、自定义版本前缀、最终 endpoint 缓存绑定；PPT 视觉与展板读者检查传递该设置。 |
| W05 | 失败优先保留结构化 errors/error 与恢复字段，不再显示 JSON 首行。 |
| W06 | WG / Agent task / 安装 CLI 明确受管 Python UTF-8 输入输出；CP936 环境和合法 U+FFFD 保真，非法 UTF-8 严格报错。原生 LibreOffice 输出单独接收。 |
| W07 | 目录与文件刷盘进入公共原语；Windows 目录能力缺失显式返回，普通文件错误仍上抛。 |
| W08 | 受保护打开保留 POSIX no-follow；Windows 用原生句柄检查重解析点、普通文件及最终路径，不以常量置零降级。 |
| W09 | LibreOffice 增加 ProgramW6432 / ProgramFiles / ProgramFiles(x86) 查找。 |
| W10 | Agent 相对协议路径接受反斜杠，拒绝盘符、UNC、设备、ADS 和父级跳转；原文件系统绝对路径保留范围验证。 |
| 内容覆盖 | v2/v3 PPT 摄入增加文本稀少/较大图像、符号字体的非阻断提示；原始模型回执与判定保持，无新增模型调用。 |

### 已执行的本机验证

- macOS，系统 Python 3.12；真实 Office 测试使用项目 `.venv/bin/python`（系统 Python 未安装 python-pptx）。
- 新兼容性测试：14 项，13 通过、1 项仅 Windows junction/device 原生测试在本机跳过。包括真实多进程互斥、共享锁、阻塞恢复、进程退出释放、错误传播、中文路径/CP936、Agent/WG 边界及真实 LibreOffice 渲染。
- 回归通过：WG、Agent task、visual_qa、DSH visual tools、slide_reuse、PPT document/academic/intake、Source、KU、review、presentation_state、science_exhibit/reader/workflow、oral_exam、citing_papers、paper_impact、relocate_own_ip、relativize_internal_symlinks、安装 CLI、功能注册表及开源导出。science_exhibit_workflow 的 3 项原有可选测试未执行，不能解读为这些项通过。
- 新增 PPT 摄入集成用例验证低覆盖提示不会修改已封存回执或重复调用 API；该套件 11 项通过。
- 工程图校验、功能注册表 runtime 校验通过；开源白名单包含平台模块、测试、文档及 CI 工作流。

### 尚待原生平台结果

Windows 3.12/3.14 与 Linux/macOS 的 GitHub Actions 矩阵已编写，但本次没有推送或触发远端任务。Win32 句柄、文件锁与 junction 的原生行为仍须以 Windows 实际运行结果验收；本机模拟和 POSIX 回归不替代它。当前工作树已落地，未发布版本。
