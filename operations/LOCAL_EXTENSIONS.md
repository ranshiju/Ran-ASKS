# 本地扩展

本地扩展为获授权的私人能力提供仓库通用入口。公开发行版只含加载器及其契约，没有任何预装私人能力，也不依赖私人目录存在。

## 发现与调用

- `python3 .scripts/wg.py extension list`：读取已安装清单，不导入或执行代码。
- `python3 .scripts/wg.py extension show <id>`：取得规范位置和入口。
- `python3 .scripts/wg.py extension run <id> -- <args...>`：按用户任务明确选择后执行。
- `python3 .scripts/local_extension.py` 提供相同命令，直接调用保留程序退出码；wg 以标准 envelope 的 ok/error 表示结果。

受信任安装位置为 `private/extensions/<id>/extension.json`。ID 使用小写字母起始、后接字母/数字/连字符，最多64字符。清单 schema=`local-extension-v1`，必须含同名 id、title、summary、version、entry、guide、backend=`agent`、visibility=`private`。entry 为包内 Python 文件，guide 为包内文档；相对路径禁止上跳、绝对路径和符号链接。功能的持续工作类型、命令及效果在自身规范说明。

只按确定路径发现已安装代码，不自动安装，不从来源文档、附件、工作区状态或网络读取执行入口。清单和代码属于用户受信任的安装面；这不是不可信程序沙箱。run 使用当前解释器及 argv、继承宿主权限；调用者先阅读所选规范并遵循用户授权。无 shell 展开、后台控制循环或 API 回退。新增宿主 adapter 不能改变该边界。

## 私人交付

专用代码、规范、模板、测试、注册和依赖说明一起保存在私有包内；实例放独立私人目录。不得在公开注册表、帮助文字、说明、测试样本或公共节点契约中写入专用身份和引用。私有工程节点可在内部登记，公开工程图只保留交付路径闭包；公共能力不得依赖私有节点。

private 强制禁止公开发布；这是路径边界，复制出去的内容不自动解密为可公开材料。发布前验证独立公开树及内容引用，确认不安装私人包时仍可发现通用入口。私人包需独立的本地版本管理/备份与恢复说明，不自动配置或上传远程仓库。

## 验证

`python3 .scripts/test_local_extension.py` 验证空安装、静态发现、路径和显式执行；各包独立验证初始化、保存、跨进程恢复及业务边界。`function_registry.py validate --runtime` 验证公共统一入口，`open_source_release.py build/verify` 验证发行树。
