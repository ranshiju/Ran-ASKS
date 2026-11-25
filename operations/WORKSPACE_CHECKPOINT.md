# 本机工作副本与 NAS 检查点

## 按工作区位置选择

| 工作区和传输方式 | 首次登记 | 以后如何更新 |
|---|---|---|
| 普通本地目录，含已安装云盘但工作区在同步范围外的电脑 | `setup --mode local`，在当前 clone 内工作 | 在该 clone 执行 `git pull --ff-only` |
| 云盘内的主目录，文件已完整下载，可控制同步排除项 | `setup --mode synology-direct --drive-root <同步根> --work-dir <主目录>` | 由关联上游的一台电脑执行下文的直同步升级，其他电脑接收同步结果 |
| 在云盘外工作，只通过云盘传递完整检查点 | `setup --mode synology --drive-root <同步根> --work-dir <本地工作区>` | 在关联上游的本地 clone 更新，关闭写入后重新封包交接 |

命令中的 `setup` 指 `python3 -m ran_asks setup`。首次交互回车选择单目录，已登记的设置优先。直同步要求各机保留完整本地文件，能排除每机自己的 Git 指针、凭据和环境，并在换机前确认同步完成。其他云盘可按这些条件选择布局；使用占位文件、无法排除本机入口或无法确认文件已到齐时，可选云盘外单目录或封包交接。云盘品牌本身不决定布局。单个项目允许只在原电脑保存过程文件，其当前可编辑成果仍可随主目录传递。

## 首次选择本机目录

从当前工作区执行 `python3 -m ran_asks setup`（安装后也可用 `ran-asks setup`）。首次在终端选择本机单目录、群晖内直同步或群晖外交接包。选择单目录使用当前工作区。选择交接包时提供本机 Drive 同步根；工作目录尚未指定时，若当前工作区在同步范围外可按回车选用，否则指定群晖外工作目录。同步根下的 `WikiGraph-Checkpoints` 用于交接。设置只登记位置，已有文件的迁移或恢复按下文办理。

Agent 在首次部署和每次交接时先运行 `python3 -m ran_asks setup --status`。已确认的单目录选择用 `setup --mode local` 登记；已知群晖路径时用 `setup --mode synology --drive-root <本机同步根> --work-dir <群晖外工作区>` 登记。无交互调用返回的 `missing` 指出仍需补充的字段，已有明确答案直接传参。状态为 `configured` 时复用返回的工作区和交接目录；`open_configured_workspace` 指示先打开已指定的工作区，如需迁移则按恢复流程准备。接收机恢复到新路径后，沿用该机已知同步根为新工作区登记。历史增量导出和单完整包轮换分别遵循各自发布规则。

设置按本机和工作区保存在用户配置目录（默认 `~/.config/ran-asks/storage.json`，支持 `XDG_CONFIG_HOME`）。重新指定模式或路径时再次运行 setup。普通查问、摄入和构建沿用既有工作区。

## 群晖内主工作区与本机 Git（2026-11-25）

直同步选择 `setup --mode synology-direct --drive-root <本机同步根> --work-dir <群晖内主目录>`。在已核对的主目录执行 `python3 -m ran_asks setup --externalize-git`，原有独立 Git 对象、分支与暂存索引留在本机。接收机在无 Git 的新目录用相同入口建立本机仓库。两机分别排除 .git 指针、凭据和环境链接。旧的 synology 模式继续解释为封包交接，单机用户仍选 local。主机的凭据与依赖只在本机配置。

首次迁移使用静止工作区的已核验副本，执行 `python3 -m ran_asks.drive_layout --migrate` 将受管的系统目录放到普通目录名下；保留原工作区用于回退。接收机等 Drive 完整下载后，从根目录执行 `python3 -m ran_asks.drive_layout` 重建本机兼容入口，然后登记 setup。已有旧目录含内容时先在本机保全、对账，避免覆盖未交接工作。system-tools、project-config 与 agent-resources 保存实现、配置和技能，项目的 workspace-state 与 research-memory 保存接续状态。工具约定的隐藏入口由各机本地提供。Raw 保持原样。在途依赖和不可再生输入随工作区，临时渲染和环境探测放在 setup 返回的本机 scratch_dir。关闭写入、等源端上传和接收端下载完成后再切换机器，验证 Raw 定位和项目接续。直同步按文件到达；独立备份仍按需要保管。

### 从 GitHub 升级

普通 Git 工作区采用单目录。即使电脑安装了云盘，只要工作区在同步范围外，也可选 `python3 -m ran_asks setup --mode local`，把 Git、项目和制作中间文件放在原处。升级时在该目录执行 `git pull --ff-only`。

采用群晖直同步且已迁移为可见目录时，在关联公开上游的那台电脑上暂停 Drive 同步、结束正在写入工作区的任务，然后在工作区根目录运行：

```bash
python3 -m ran_asks.drive_layout --upgrade
```

命令从当前分支的上游获取更新，将受这次更新影响的目录临时还原为 Git 跟踪的名称，快进后恢复可见名称和兼容入口。Git 遇到有覆盖风险的本机修改时会停止。完成后恢复 Drive 同步，其他电脑收到文件后运行 `python3 -m ran_asks.drive_layout` 重建本机入口。接收机只需要 Drive 即可取得同一版系统。

旧版若已经迁移目录且缺少 `--upgrade`，在更新机运行 `git fetch`，从已核实的公开上游提交取出 `ran_asks/drive_layout.py` 到本机临时目录，再从工作区根目录执行 `python3 /该临时目录/drive_layout.py --upgrade`。意外中断后保持同步暂停，先运行 `python3 -m ran_asks.drive_layout --migrate` 恢复入口，再重试升级。曾经直接 pull 后出现两份脚本目录时，先保全两份内容，按上游提交核对并合并本地编辑，再恢复入口。

## 封包交接时只保留最新完整包

两台电脑轮流使用时，可采用每机云盘外完整工作区、云盘内一份最新完整包的部署方式。下文另列增量交接命令。

每次正式换机由宿主Agent执行以下流程，复用已有shared工具，不添加后台自动任务：

1. 等待Drive当前交接目录下载完成，读取其verification.json与唯一ready.json核对repository_id、包哈希及快照。先确认本机工作副本是从这个检查点恢复或上次发布时登记的源；在对应配置执行status/list并比较state.json中的源起点与最新快照。另一机已发布新包但本机还在旧起点时，保留本机未交接改动并停止发布；不能通过新建库绕过分叉检查。无新改动不重封。
2. 暂停写入，完成受管检查。每次用群晖外**全新不存在的**配置/仓库/封包目录，仅保存当前工作区一个快照。沿用安全口令，workspace-id不变。示例（<本轮>由执行者换成唯一批次名；路径不可复用）：

```text
python3 .scripts/workspace_checkpoint.py --config ~/WikiGraphData/<本轮>/writer/config.json configure --repository ~/WikiGraphData/<本轮>/repository --password-file ~/WikiGraphData/keys/checkpoint-password --workspace-id wikigraph --init
python3 .scripts/workspace_checkpoint.py --config ~/WikiGraphData/<本轮>/writer/config.json save --source <当前群晖外工作区> --quiescent
python3 .scripts/workspace_checkpoint.py --config ~/WikiGraphData/<本轮>/writer/config.json export-pack --directory ~/WikiGraphData/<本轮>/packs
python3 .scripts/workspace_checkpoint.py --config ~/WikiGraphData/<本轮>/reader/config.json configure --repository ~/WikiGraphData/<本轮>/verify-repository --password-file ~/WikiGraphData/keys/checkpoint-password --workspace-id wikigraph --defer-connect
python3 .scripts/workspace_checkpoint.py --config ~/WikiGraphData/<本轮>/reader/config.json import-pack --directory ~/WikiGraphData/<本轮>/packs
python3 .scripts/workspace_checkpoint.py --config ~/WikiGraphData/<本轮>/reader/config.json restore --snapshot latest --target <新的群晖外验证副本>
```

3. 确认只有一个快照、一个sequence=1且parent=null的包，独立恢复全部哈希/SQLite验证通过；保留匹配的最新工具ZIP与校验文件，写入本轮verification.json和换机README。失败保留原同步目录与原配置，不发布半成品。
4. 确认源自捕获后未继续写入，核对Drive目录仍是步骤1的包，才以已验证的新目录整体替换群晖WikiGraph-Checkpoints；旧目录移到群晖外回收站。本机当前配置/仓库切到新链，保留其源起点登记。实际发布是本地目录替换，Drive远端可能短暂不完整，必须等客户端同步完成才换机。
5. 另一机每轮使用新的空仓库/新配置import，恢复到不存在的群晖外目录；不能将新repository_id导入旧库。依据工作区环境清单prepare-env，按需配置凭据并核验受管绝对路径绑定、知识查询和项目接续。确认无未交接改动后再把宿主切到新目录。旧机原工作区不能直接当最新副本继续写入。
6. 新链已验证且无未交接改动的旧仓库/旧配置可移出活跃目录进回收站。只保留当前活跃链；不重写Git历史，不将密钥放入群晖或工具ZIP。

这个方案以每次重新捕获、压缩和全量传输换取单包独立恢复及固定同步文件数；不是只上传变化内容。程序仍支持增量，但此部署默认不使用。可再生temp按已批准留存边界清理；在途任务、无法确认可再生的唯一输入及接续状态不能仅因目录名temp而排除。工作目录整体迁出后，这些小文件也在群晖外，仅有效保留项进入完整包。


本功能适用于多台电脑轮流工作、同一时间只有一台写入的情形。完整 WikiGraph 工作区放在 Drive 同步范围外（例如 `~/WikiGraph`）；restic 库保留本机或直连 NAS；使用现有 Drive 通道时，只同步封存增量包及选定成品。`operations/SYNC.md` 仍是知识一致性对账，和本功能不同。

## 依赖与入口

Python 3、项目基础 Python 依赖以及 restic（本轮 macOS 验证版本 0.19.1）。macOS 用 `brew install restic`；其他宿主按 restic 官方说明安装。工具、测试和本规范随仓库交付；环境、密钥、本机配置不随公开源码分发。当前平台支持以项目整体声明为准。

统一入口 `python3 .scripts/wg.py checkpoint <参数>`；独立入口 `python3 .scripts/workspace_checkpoint.py <参数>`。`--config` 必须放在子命令之前；首次建立库示例：

```bash
python3 .scripts/workspace_checkpoint.py \
  --config ~/WikiGraphData/checkpoints/config.json configure \
  --repository 'sftp:my-nas:/volume1/private-backups/wikigraph' \
  --password-file ~/WikiGraphData/keys/checkpoint-password \
  --workspace-id wikigraph --init
```

地址是示例，必须替换成实际 NAS 的 SSH 配置别名/路径。SSH 端口和认证由本机 SSH 配置负责，不在聊天或命令行写密码。也支持 NAS 已挂载目录或本地演练目录。目标库不可再被 Drive 双向同步。`--init` 在口令文件不存在时生成随机口令，权限600；已有仓库不加 `--init`，使用原口令文件。**解密口令需要独立、安全的恢复副本**，只留在当前电脑会使电脑损坏后的备份不可恢复。

配置、缓存、暂存、操作锁、接续起点和恢复回执位于配置所在目录。每个后端使用独立目录；配置文件存在时不静默替换。配置失败会保留配置/口令便于诊断，不自动初始化第二个仓库。

## 保存和接续

停止当前工作区的摄入、生成、数据库和 Git 写入，在静止窗口执行：

```bash
python3 .scripts/wg.py checkpoint save --quiescent
python3 .scripts/wg.py checkpoint list
python3 .scripts/wg.py checkpoint status
python3 .scripts/wg.py checkpoint restore --snapshot latest --target ~/WikiGraph-next
```

`--quiescent` 是调用者确认写入已停的执行前提，工具不能自动暂停所有宿主或其他电脑。保存先做独立暂存和源前后元数据比对，再向 restic 备份；成功才推进本机起点。复制及哈希校验需要扫描和本机 I/O，当前版本每次重新暂存，并非零成本增量捕获；网络传输由 restic 去重。

保存要求本机起点等于该工作区的最新远端检查点。未登记副本或旧副本必须先恢复到新目录；不能用时间戳覆盖。每次恢复都选择指定工作区标签下的明确快照ID，核验清单、全部文件SHA-256、权限与活跃数据库（含 WAL）的 quick_check，成功才激活目标并记录起点。现有目标（即使空目录）一律拒绝。旧副本始终保留，需要找回未保存改动时显式比较/合并；不做自动合并。

restic 存储锁不是应用写锁。本机配置目录的操作锁只阻止同一配置下重入；远端 head 前后检查能发现部分冲突，**不构成跨电脑的原子锁或并发保证**。两机同时保存不受支持，发生冲突保留双方快照，先人工确定接续分支。备份上传后检查失败时不推进本机起点；可能留下可列出的快照，需检查或恢复，不能把失败输出当交接完成。

`save` 成功表示 restic 上传完成、仓库元数据检查通过；不声称每次保存都重下载所有数据。`restore --verify` 加独立 SHA-256 校验才是完整数据读取验证。首次切换及更换后端必须做完整恢复演练。没有自动删除、forget/prune、远端解锁或后台自动上传；保留策略另行决定。

## 内容和一致性

历史临时产物先按 [临时产物留存协议](TEMP_RETENTION.md) 用 `archive-temp` 整组归档；`restore-temp` 在另一台电脑恢复完整历史。归档包与保留的引用文件继续进入检查点，外部待删除原件不再占用跨机共享范围；未知和在途temp仍保留。

- 原样保留 Raw/Wiki、private、项目全部修订、可接续 temp、普通未跟踪文件、完整独立 `.git`。gitignore 不作为备份规则。
- 排除已由 `pyvenv.cfg` 确认的 `.venv*`/`venv*` 环境、非 Raw 的 `__pycache__`、有 package.json 的 node_modules，以及根目录 `.env`/`.env.local`/`.env.production`、`.DS_Store`。完整排除路径写入清单。其他私人配置可能仍在加密检查点中，检查点从不属于公开发布物。
- 经[运行环境外置入口](LOCAL_ENVIRONMENTS.md)登记的根`.venv`/`.venv-docling`链接也排除；环境元数据只读工作区内`.project/runtime-environments.json`，不跟随外链，即使原机环境暂不可达也保留重建清单。其他链接按既有规则原样保存。
- 环境在目标机重建，本机 `.env` 和 SSH/外部解析服务认证单独配置。清单 `environments` 记录被排除虚拟环境的 Python 与已安装包版本（仅读取元数据，不执行环境）；恢复回执保留该清单。`ran-asks` 等本仓库本地安装包应从恢复工作区安装，不把旧包版本盲目从公网安装。相对入口保持不变；已存绝对路径及外部工具能力必须另行核验。
- 活跃公共/私人图、索引和项目 `.workspace/index.sqlite` 先通过 `graph_lib.connect(read_only=True)` 与 `backup_graph` 验证已提交 WAL；检查点内原样保留数据库和 WAL 字节，仅排除瞬时 SHM，避免被Git跟踪的数据库因备份重排产生伪修改。历史/Raw数据库文件原样保存。源文件不重写；读 SQLite 可能产生临时 SHM，这是 SQLite 读取副作用，不是事实内容修改。
- 调用者静止约定、Git锁检查、前后元数据比对共同约束一致性。它不防御恶意写入者伪造时间戳，不代替摄入事务；检测漂移后拒绝检查点并保留失败暂存供诊断。
- 符号链接原样保存、从不跟随复制。绝对/外部/断链在结果与清单报告为便携性警告；Raw 链接不由备份入口修复。Git linked worktree 与外置 alternate objects 拒绝捕获，先准备独立库。

## 演练、切换和回退

无需后端即可运行 `capture --source <工作区> --destination <不存在的外部包目录> --quiescent`，用 `verify <包目录>` 重验；包包含 `workspace/` 和 `checkpoint.json`，它不是异机备份。

正式迁移顺序：停止写入 → 保存 → 在群晖外新目录完整恢复 → 重建环境/本机凭据 → 验证查问、Raw可达、项目/PPT接续及Git状态 → 将应用项目路径切到新工作区 → 原 Drive 工作目录退出同步并仅保留回退副本。删除或移动原目录单独明确执行，工具不自动做。NAS直连或Drive封包至少一种路径应完成本机独立导入/恢复验收；真实异机和同步完成状态另行确认。切换后不要继续写旧副本。

失败暂存包含私有数据，位于群晖外的受限目录，错误返回其位置。完成诊断后再处理；不要让它进入公开交付树。恢复校验只读取数据，不执行恢复出的脚本/文档指令，业务验收在确认来源后另行进行。

## 验证

`python3 .scripts/test_workspace_checkpoint.py` 使用临时独立 Git 库、SQLite WAL 和真实本地 restic 仓库验证往返、私有边界、暂存/未暂存/未跟踪数据、源漂移拒绝、旧起点拒绝、目标不覆盖和内容篡改拒绝。未安装 restic 会跳过真实后端测试，不能将这种结果当作完整验收。

## 使用现有群晖同步目录（无需 SSH）

restic 活库和口令留在 `~/WikiGraphData/`，群晖根下另设 `WikiGraph-Checkpoints/`。首次封包包含完整加密库，以后只封新增不可变对象；每次通常新增一个 `.tar` 和一个 `.ready.json`。旧包不重写，最新包依赖全部祖先包，不能只复制最后一个包。导入前会校验父链、整包与逐对象SHA-256；拒绝缺包、截断、路径逃逸、分叉、对象覆盖以及本机尚未交付的独立修改。

```bash
python3 .scripts/wg.py checkpoint --config ~/WikiGraphData/checkpoints-local/config.json save --quiescent
python3 .scripts/wg.py checkpoint --config ~/WikiGraphData/checkpoints-local/config.json export-pack --directory ~/SynologyDrive/WikiGraph-Checkpoints
```

封包先在群晖外生成，再发布到同步目录，完成标记最后发布。同卷用原子硬链接发布；跨卷先写未完成文件，完成标记仍是导入门槛。返回成功只表示本地发布，不等于Drive已上传；离开电脑前确认客户端同步完成，到另一机也等下载完成。包已加密，但口令仍须另行安全保管。

另一机准备 restic、Python 和仓库中的这两个共享脚本及 graph_lib/platform_compat 依赖（可使用公开发布的项目副本），保管好同一口令文件，配置**空的本地库**：

```bash
python3 .scripts/workspace_checkpoint.py --config ~/WikiGraphData/checkpoints-local/config.json configure --repository ~/WikiGraphData/checkpoint-repository --password-file ~/WikiGraphData/keys/checkpoint-password --workspace-id wikigraph --defer-connect
python3 .scripts/workspace_checkpoint.py --config ~/WikiGraphData/checkpoints-local/config.json import-pack --directory ~/SynologyDrive/WikiGraph-Checkpoints
python3 .scripts/workspace_checkpoint.py --config ~/WikiGraphData/checkpoints-local/config.json restore --target ~/WikiGraph-next
```

接续用相同 `workspace-id`、同一条封包链和原仓库口令；不要 `--init` 一个新的库来接续。双方完成修改后均保存、封包；切回旧电脑先 `import-pack` 再恢复到新目录。已有副本和未交接修改保留，不覆盖。此功能不做分叉合并。

维护边界：当前封包链只追加，不做 restic forget/prune/key rotation；检测到既有对象变更或移除时要求建立新的完整传输链，不悄悄发送不完整增量。没有自动清理旧包。中断导入可重试，已存在同哈希对象复用；上传中断等Drive续传即可。若导出已发布完成标记但本机回执写入失败，先import-pack补齐本机链状态。

验收增加 `python3 .scripts/test_checkpoint_transfer.py`：真实双本地库模拟A→B→A交接、增量包小于首包、旧包不重写、截断/路径逃逸/缺祖先拒绝，以及两机独立修改冲突保护。
