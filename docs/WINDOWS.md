# 在 Windows 上运行 Ran-ASKS

Ran-ASKS 原本只在 macOS 上开发和验证。本文档说明 Windows 上的安装步骤、
与 POSIX 的行为差异，以及两项需要额外配置的可选能力。

[English summary](#english-summary) is at the bottom.

## 1. 前置条件

| 组件 | 要求 | 说明 |
| --- | --- | --- |
| Python | 3.11 或更高 | 从 [python.org](https://www.python.org/downloads/windows/) 安装，勾选 “Add python.exe to PATH” |
| Git | 任意近期版本 | 克隆仓库、发布校验（`open_source_release.py`）需要 |

Windows 的 Python 安装器只提供 `python` 命令，不提供 `python3`。
项目文档里的命令一律写作 `python3 ...`，在 Windows 上把它换成 `python` 即可：

```powershell
# 文档写：python3 .scripts/engineering_graph.py validate
# Windows 上执行：
python .scripts/engineering_graph.py validate
```

若希望直接照抄文档命令，可在 PowerShell 配置文件里加一个别名：

```powershell
# 打开配置文件：notepad $PROFILE
Set-Alias python3 python
```

## 2. 安装

```powershell
git clone https://github.com/ranshiju/Ran-ASKS.git
cd Ran-ASKS
python -m pip install -r requirements.txt
Copy-Item .env.example .env
python .scripts/engineering_graph.py validate
```

最后一条命令应输出 `工程元图有效: ... 节点, ... 边, ... 能力包, ... 契约`。

### 强烈建议：把控制台设为 UTF-8

知识库内容以中文为主，而简体中文 Windows 的默认 locale 编码是 `cp936`。
程序内部所有读写都已显式指定 UTF-8，但控制台输出仍受代码页影响：

```powershell
chcp 65001
$env:PYTHONUTF8 = "1"
```

把 `PYTHONUTF8=1` 设为用户级环境变量可以一劳永逸：

```powershell
[Environment]::SetEnvironmentVariable("PYTHONUTF8", "1", "User")
```

## 3. 需要额外配置的两项能力

### 3.1 符号链接（可选）

项目在若干边界校验中会创建符号链接。Windows 默认不允许普通用户创建符号链接，
需要开启**开发者模式**：

> 设置 → 系统 → 开发者选项 → 开发人员模式

未开启时，依赖符号链接的测试会自动跳过（而不是失败），主流程不受影响。

### 3.2 LibreOffice（可选，PPT/PPTX 渲染）

视觉 QA 与图像转可编辑 PPT 需要 LibreOffice 做无头渲染。
安装后脚本会自动在下列位置查找，无需额外配置：

- `C:\Program Files\LibreOffice\program\soffice.exe`
- `C:\Program Files (x86)\LibreOffice\program\soffice.exe`
- `PATH` 中的 `soffice.exe` / `soffice` / `libreoffice`

也可以用 `SOFFICE_BIN` 环境变量或 `--soffice-bin` 参数显式指定。

## 4. 与 macOS 的行为差异

| 项目 | macOS | Windows | 说明 |
| --- | --- | --- | --- |
| `.doc` / `.docx` 取文本 | `textutil` | 内置 OOXML 解析（`.docx`）；`.doc` 走 LibreOffice 或 pandoc | 无需安装 `textutil` 的等价物 |
| 删除到废纸篓 | `trash` 命令 | 移入项目内 `temp/trash/` | 两个平台都可恢复，不会永久删除 |
| 打开生成的图 | `open` | `os.startfile` | `graph_visualize.py --open` |
| 文件锁 | `fcntl.flock` | `msvcrt.locking` | 图写入串行化语义一致 |
| section 读取 | `.scripts/read_section.sh` | `.scripts/read_section.py` | 两者输出与退出码一致 |
| 引用发现 | `ripgrep`（若已安装） | 未装 `rg` 时用纯 Python 遍历 | 排除规则相同 |

### 换行符

所有文本产物一律以 **LF** 写出，与 macOS 生成的字节完全一致。
这是必需的：项目大量使用内容哈希（源指纹、`require_hash`、
`CHECKSUMS.sha256`），若在 Windows 上写成 CRLF，同一份内容的哈希会与
macOS 不一致，知识库将无法跨平台共享。

建议同时配置 Git 不要改写换行：

```powershell
git config core.autocrlf false
```

## 5. 运行测试

```powershell
$env:PYTHONUTF8 = "1"
$env:INGEST_BACKEND = "api"     # 部分回归测试假定 API 编排
python .scripts/test_prompt_audit.py
python .scripts/engineering_graph.py validate
```

跑全部测试：

```powershell
Get-ChildItem -Recurse -Filter "test_*.py" | ForEach-Object { python $_.FullName }
```

### 已知失败（与平台无关）

以下用例依赖未随公开仓库发布的私有内容（`.gitignore` 排除了
`projects/**` 与 `memory/**`），在 macOS 上全新克隆同样会失败：

| 用例 | 缺失内容 |
| --- | --- |
| `.scripts/test_playbook_dispatch.py` | `memory/playbooks/index.md` |
| `.scripts/test_workspace_state.py` | `projects/_templates/research/` |
| `paper-artifacts/v0.2.1/physh/frozen-source/test_run.py` | `projects/` 目录本身 |

## English summary

Ran-ASKS was developed on macOS only. This port makes it run on Windows:
POSIX-only file locking, `textutil`, `trash`, `open`, `find`, `curl` and
`SIGALRM` now have Windows equivalents; repository-relative paths are always
emitted as POSIX; every text write is pinned to LF so content hashes match
across platforms; SQLite handles are closed so Windows can replace and delete
the files.

Install with `python -m pip install -r requirements.txt`, set `PYTHONUTF8=1`,
and replace `python3` with `python` in documented commands. Symbolic links need
Developer Mode; PPT rendering needs LibreOffice. Both are optional.
