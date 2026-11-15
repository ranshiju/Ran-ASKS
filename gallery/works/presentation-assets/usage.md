# PPT 素材库 · 使用说明

四项参数化素材：环形双箭头 v1、量子启发智能计算 v2、量子材料与器件 v2、太赫兹光电技术 v2。

每项包含透明 PNG、矢量 SVG、原生可编辑 PPTX 与 asset.json。PPTX 中图形作为一个组保存，可在 PowerPoint 取消组合后局部修改。PNG 适合直接插入，SVG 适合矢量缩放。外部留白透明；图标圆盘为可调整的实色背景。

## 交给 Agent 使用

自然语言示例：请使用 Gallery 的 PPT 素材库，为我的报告选择适合的图标或关系箭头。保留图形含义，按页面背景和主题调整配色与尺寸，优先插入原生可编辑对象；若使用双向箭头，请根据内容填写两种关系的标签。

安装 ASKS 的 visual 依赖组后，将本包解压到仓库 temp/gallery-assets/。以下命令在 ASKS 仓库根目录运行，输出目录必须尚不存在。直接传描述路径即可使用，不需要先收藏：

```bash
python3 .scripts/wg.py presentation-asset show --asset temp/gallery-assets/quantum-inspired-computing/asset.json
python3 .scripts/wg.py presentation-asset use --asset temp/gallery-assets/quantum-inspired-computing/asset.json --preset blue-silver --width 512 --output temp/my-quantum-icon
python3 .scripts/wg.py presentation-asset use --asset temp/gallery-assets/quantum-materials-devices/asset.json --color 'background=#4D235E' --color 'primary=#D5B1E8' --color 'secondary=#FFF8E9' --output temp/my-materials-icon
python3 .scripts/wg.py presentation-asset use --asset temp/gallery-assets/circular-dual-arrow/asset.json --preset blue-gray --width 160 --output temp/my-feedback-arrow
```

## 配色与几何

三枚图标支持 red-gold、blue-silver、forest、ink 预设；background 为圆盘背景，primary 为主线，secondary 为节点或光波，计算图标另有 symbol 控制 ψ。可重复传 --color '角色=#RRGGBB' 自由配色；当前接口支持不透明实色，圆盘外部仍透明。默认配色为已选定的红金风格。

环形双箭头支持 red、blue-gray、mono 预设，primary/secondary 控制两段颜色；还可通过 --values JSON 调整环宽、间隙和箭头角度，范围以 show 输出为准。关系须用上下文和标签说明，不能只靠颜色区分。

完整能力规范：https://github.com/ranshiju/Ran-ASKS/blob/main/operations/PRESENTATION_ASSET.md

## 来源与使用边界

由冉仕举与 ASKS 协作创作、筛选，2026-11-14 收入 Gallery。图形是概念标识；轨道环不是经典粒子轨迹模型，晶格不指认真实材料，光波不是实验谱线。包内不含风格参考原图、制作期来源记录或原始汇报。

公开展示和提供下载不自动授予第三方转载、改编或商业许可；使用前请阅读同包 RIGHTS.md。参数描述的 public 标记仅表示该公开副本允许随 Gallery 分发，不是版权许可证。
