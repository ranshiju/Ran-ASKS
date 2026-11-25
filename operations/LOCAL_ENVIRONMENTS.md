# Python运行环境外置与换机重建

运行环境是本机依赖，Raw/Wiki/图和项目成果仍在完整工作区。此入口将已存在的`.venv`或`.venv-docling`外置到群晖目录之外，原调用路径保持；不改第三方包字节，不将环境上传公开仓库。源码、规范与测试均在仓库内，Agent/API调用相同确定性工具。

统一入口为`python3 .scripts/wg.py checkpoint externalize-env|prepare-env|restore-env <环境名>`，不要求配置restic。下文独立入口提供相同功能及显式`--root`支持。

## 当前电脑外置

```bash
python3 .scripts/local_environments.py externalize .venv
python3 .scripts/local_environments.py externalize .venv-docling
```

默认外部根`~/WikiGraphData/environments/`，按工作区绝对路径哈希分隔，避免不同恢复副本共用指向错误仓库的editable安装。可在子命令前指定`--root`和`--store`；存储必须在工作区外，使用Drive时也应在实际同步根外。只支持macOS同文件系统原子交换，其他系统/跨卷拒绝，不退化成copy-and-delete。

先核验源为真实虚拟环境、目标不存在、环境未被Git跟踪，并保存解释器/依赖清单；随后使用renamex_np的RENAME_SWAP一次交换原目录和外部自指链接，原入口连续指向同一套包。文件内容和原脚本shebang不变；Python的sys.prefix可以规范化为新的外部位置。实际解释器版本及prefix目标需通过核验，否则原子回退。不读取/改写Raw、Wiki、图或业务成果。

`.project/runtime-environments.json`仅保存Python版本及受限的`包名==版本`清单，不含本机绝对路径、凭据、第三方包本体。随私有工作区/检查点保存，默认不进公开发布；外部placement回执保存本机路径。不要手动把未知目录标为运行环境来绕过备份范围。

## 另一台电脑或新恢复副本

无需同步本机环境本体。准备Python及uv，取得最新工作区和依赖清单后执行：

```bash
python3 .scripts/local_environments.py prepare .venv
python3 .scripts/local_environments.py prepare .venv-docling
```

在全新的外部位置建立环境并按已保存版本安装，`ran-asks`从当前恢复工作区本地安装；成功后才创建原位置的本机链接。已有入口（含断链）或外部目标均拒绝覆盖，先核对并保留本机旧环境。不会执行同步过来的任意安装命令或原文指令，包标识只接受受限的固定版本形式。

默认使用清单的精确Python版本；可用`--python /本机/python3`选择已安装解释器，但主次版本必须相同。另一架构/平台可能没有相同版本的依赖包，应明确处理安装失败，不默默换版本。安装失败保留外部目录供诊断，不发布可用入口。此行为依赖联网包源，不保证离线重建；本机.env及认证仍需单独配置。创建成功只验证解释器和安装命令，Docling等实际功能还需导入/使用验收。

如果通过Drive直接同步工作区，环境链接仍属于本机接入信息，不能把另一台机器的绝对路径当作可用环境。检查点专门排除已登记根环境的链接，并使用仓库内清单记录环境信息，不访问外部链接目标；普通/Raw符号链接仍按原规则保留与提示。

## 原机回退

```bash
python3 .scripts/local_environments.py restore-in-tree .venv
python3 .scripts/local_environments.py restore-in-tree .venv-docling
```

仅适用于本工具原子外置且placement回执匹配的原环境：逆向交换后移除工具自己的链接，不删除环境内容。另一机prepare建立的环境有外部shebang，拒绝直接逆交换，应保持外部使用或重新创建所需环境。

## 同步与验收边界

此操作使环境文件物理移出工作区树，不等同于已确认群晖上传队列减少同样数量。Synology客户端对隐藏文件及部分链接类型有限制，应以当前客户端排除规则/实测为准；不因`.gitignore`推断Drive设置，也不宣称符号链接在所有客户端都按同样方式处理。彻底避免活跃工作区文件事件的首选仍是完整本机工作目录＋检查点封包，外置环境是可以单独落地的过渡措施。

`.scripts/test_local_environments.py`使用真实隔离虚拟环境验证原子交换、旧shebang、回退、换位失败/事后失败、覆盖/边界拒绝、跨工作区prepare、损坏清单及不跟随链接的检查点；结合`test_workspace_checkpoint.py`及工程图校验。环境包无需也不应随公开源码交付。
