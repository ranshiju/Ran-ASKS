# 平台兼容原语

本模块属于 shared 确定性内核；无模型、网络、Raw/Wiki/Graph 写入策略。调用者继续拥有路径范围、事务、提交和回滚。

- 文件锁：POSIX 使用原 fcntl；Windows 使用 LockFileEx/UnlockFileEx，锁定文件起始一字节，共享/独占/非阻塞语义保持。永久错误传播，不无限重试，不吞解锁失败。进程退出释放 OS 锁。
- 受保护文件打开：POSIX 使用 O_NOFOLLOW 拒绝最终符号链接，父级范围由调用者检查；Windows 拒绝路径各级重解析点（含 junction），并检查普通文件类型。Windows 使用 OPEN_REPARSE_POINT，并核验文件属性、句柄最终路径及父级路径；不以缺失 O_NOFOLLOW 为由取消防护。
- 刷盘：文件使用可写句柄 flush/fsync；POSIX 目录 fsync，Windows 明确返回目录同步不支持。原子替换不等于断电后目录项持久化。文件错误不被吞掉；现有事务恢复规则保持。
- 子进程：受管 Python 输出/输入为 UTF-8，覆盖继承的 PYTHONIOENCODING/PYTHONUTF8。先捕获字节，再在调用线程严格解码 stdout/stderr，避免 Windows 后台读取线程的解码异常未向调用者传播。输出统一换行；check=True 的非零退出保留已解码诊断。原始字节输入不转码。外部原生程序单独决定编码，不统一强加 Python 协议。
- 相对协议路径：统一反斜杠为斜杠，拒绝 POSIX 绝对路径、Windows 盘符/UNC/设备路径、父级跳转；文件系统绝对路径仍由原调用者解析和范围校验。

Windows 原生分支需在 Windows CI 运行；POSIX 上的模拟只能检查分派与错误处理。使用 `.scripts/test_windows_compat.py` 的相同测试集合和 GitHub Actions 矩阵，禁止用跳过安全/互斥断言换取通过。

平台 CI 安装 `analysis,visual,office,pdf` 可选依赖；知识单元的固定 token 预算回归使用 analysis 中的 tiktoken，不能把缺少分词器的字符估算当成相同预算。Core 无数值依赖的初始化和恢复另由发布回归验证。LibreOffice 显式路径、环境配置及系统安装路径的发现不要求用户主目录存在；主目录不可解析时只省略用户缓存候选。
