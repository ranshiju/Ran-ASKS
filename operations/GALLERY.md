# Gallery · 精选作品收录

Gallery 是成果展示与功能发现入口。首页展示精选成品；详情页重点说明实际使用的系统功能及其贡献。读者页面直接呈现选定的最佳展示版本，不展开修改轮次、任务记录和工程校验流水账。

## 归属与目录

本能力属于 **shared 静态内容与发布边界**，由当前宿主 Agent 选择作品、核对材料和撰写介绍，现有确定性发布器负责白名单与发布校验。没有新的 Agent/API 执行 adapter，不改变 Raw、Wiki 或 `graph.db`。

- `gallery/README.md`：仓库内阅读入口与本地预览说明。仓库主 README 仅在开头给出 Gallery 入口，不嵌入作品预览。
- `gallery/catalog.json`：展示顺序、首页推荐作品与功能分类；一件作品可以关联多个功能。
- `gallery/site/`：无第三方前端运行依赖的样式与渐进增强脚本。
- `gallery/site/analytics.js`：可选Cloudflare采集加载器，只在配置的生产地址运行，统计失败不影响阅读。
- `.scripts/gallery_metrics.py`：使用只读Cloudflare Analytics API生成公开汇总，不输出管理密钥、访客记录或非公开路径。
- `.scripts/gallery_site.py`：shared 确定性静态站点构建器，输出完整 HTML、CSS、JS 与必要成品文件。
- `gallery/works/<slug>/`：选定成品、预览、功能介绍和可公开的 `metadata.json`。
- `gallery/local/<slug>/`：仅本地保存的源文件快照、项目与 Raw locator、来源哈希、选择依据及授权记录。不得加入公开白名单。
- 项目工作区继续保存创作状态和历史。Gallery 收录不改写制作期的验收、交付或审阅状态。

## 收录步骤

1. 确定作品与最终展示版。优先检查实际文件；手改 PPTX 与旧 manifest 不一致时按实际文件重新导出 PDF/图片，并单独记录来源哈希，不复用旧预览或篡改旧 manifest。
2. 核对介绍中的科学事实及功能使用依据。事实回溯 Raw；运行记录只能支持功能使用与版本来源，不能替代科学证据。仅列实际使用的功能，链接到当前操作规范，不把系统可用能力全部罗列成作品经历。
3. 保存公开作品包。至少包括 `README.md`、`metadata.json`、缩略图、完整预览和最终成品；同源 PDF 与预览的文本、页面尺寸及实际布局应一致。元数据记录稳定 ID、作品版本、收录日期、功能 ID、公开引用、文件 SHA-256 与权利说明链接。
4. 本地 provenance 记录来源、变换和用户指令。入选与公开的依据分别明确；本地留存的历史草稿标记不应被改写成科学验收。选择已有最终展示稿无需重新启动创作或模拟读者审阅。
5. 每个公开文件在 `operations/engineering/open-source-manifest.yaml` 的 `include` 中**逐文件列出**，不使用 `gallery/**` 或整个项目目录。`gallery/local/**` 显式排除；新增未列文件默认不发布。
6. 检查文件哈希、图片与 PDF、全部相对链接、公开元数据及实际发布树。沿既有版本、文档同步、回归和受管发布流程进入 GitHub。后续新增作品遵循相同边界。

## 作品页模板

```markdown
# 作品名称

一句话介绍主题、受众与成品形式。

![完整预览](poster.png)

[下载成品](poster.pdf)

## 用到了哪些 ASKS 功能

| 功能 | 对这件作品的作用 | 使用入口 |
| --- | --- | --- |
| 实际使用的功能 | 具体贡献 | 对应操作规范链接 |

## 制作类似作品

准备材料与一条自然语言启动指令。

## 来源与使用

公开参考文献、署名、日期与作品许可链接。
```

## 独立网站与扩展

网站由三个层次组成：精选首页、可搜索筛选的作品目录、每件作品的独立详情页。首页以暖白底、青绿色和作品预览形成展厅；详情页聚焦成品和功能贡献，不展示内部工程流水账。桌面与手机共享页面，功能与作品类型可组合筛选，URL 保存筛选条件。JavaScript 只增强搜索、筛选和复制，核心阅读与下载是静态链接。

### 新增作品

1. 按上面的收录步骤准备真实成品，放入 `gallery/works/<slug>/`。`slug` 使用小写字母、数字和连字符；不添加占位作品。
2. 在 `metadata.json` 的 `presentation` 中填写 `name`、`summary`、`type_label`、`format_label`、`audience`、`cover`、`preview`、`downloads`、`capability_uses`、`prompt`、`source_note`、`credit`、`boundary`。`capability_uses` 的 ID 必须与作品 `capabilities` 一致；仅描述实际贡献。
3. 多语言或多文档作品可增加 `presentation.previews` 数组，每项包含 `file`、`label`、`document`：分别是预览图片、明确的语言/页码标签、已列入 `downloads` 的完整文档。页面逐项展示，关闭 JavaScript 也能阅读。长文档的首页预览必须标注“首页”，并链接完整 PDF。
4. `cover`、`preview`、额外预览和下载文件必须出现在已有 `files` 哈希表中。预览使用 PNG/JPEG/WebP；下载可以指向已核验成品，站点不复制整个作品文件夹。网站页脚链接当前系统说明和更新日志，历史作品内容沿用创作时的版本背景。
5. 可在作品元数据顶层设置 `related_works`（作品 ID 数组），生成关联作品入口。引用必须指向同一目录中的其他作品，禁止重复、自引用或不存在的 ID；需要互相链接时分别登记。
6. 把作品 ID 加入 `catalog.json` 的 `works`；顺序即目录顺序。`featured` 指定首页推荐作品。新功能可在 `capabilities` 添加稳定 ID、显示名称、用途和真实 `operations/` 文档路径；只展示已有作品使用的功能。作品类型选项从实际作品自动生成。
7. 逐文件更新发布白名单，再构建并检查页面。元数据哈希变化时同步本地 provenance，保留先前记录；成品不变时无需重新导出或增加作品版本。

### 构建与预览

```bash
python3 .scripts/gallery_site.py
python3 -m http.server 8765 --bind 127.0.0.1 --directory temp/gallery-site
python3 .scripts/test_gallery_site.py
```

浏览 <http://localhost:8765/>。默认构建目录为 `temp/gallery-site/`，也支持 `--output <canonical-path>`；路径需为实际路径，不经过符号链接。输出只能位于源库外或 `temp/` 内；重建只替换带本构建器标记的目录，校验失败保留旧站点。发布站点不需要 Python 服务器，直接托管生成目录即可。

构建器验证目录/功能 ID、功能贡献映射、公开文件白名单、来源路径与 SHA-256，拒绝路径逃逸、符号链接和递归 Gallery 白名单。HTML 对文本转义，外部文献只允许 HTTPS。输出只复制页面实际使用的封面、预览与下载文件，绝不复制本地 provenance、项目、Raw 或整个仓库。站内资源采用相对地址，兼容 GitHub 项目子路径和本地文件打开。

### GitHub Pages 发布准备

`.github/workflows/gallery-pages.yml` 支持手动`workflow_dispatch`及每日20:23 UTC（北京时间次日04:23）的统计刷新，在公开仓库构建并只上传生成站点；普通push不直接触发部署。每日任务使用当前公开main，同时刷新其公开汇总。站点位于 `https://ranshiju.github.io/Ran-ASKS/`，页脚及`site-info.json`显示实际构建的VERSION，发布后应核对它与预期版本一致。

发布时先完成现有受管公开发布的版本与读者文档/PDF同步，再将 GitHub Pages 来源设置为 GitHub Actions，手动运行 Gallery Pages 并确认返回的实际站点地址。预期地址为 https://ranshiju.github.io/Ran-ASKS/ ，实际上线状态以成功部署及页面核验为准。不得把本地预览完成描述为线上部署成功。

### 免费访问统计与公开汇总

`catalog.json`的可选`site_url`指定规范HTTPS地址及项目子路径；构建生成canonical、Open Graph、sitemap和robots文件。修改为自己的地址即可部署到另一站点。版本读取源根VERSION，不从页面文本推断。

使用Cloudflare免费Web Analytics，网页收集PV/Visits、来源、设备及页面性能；Visits遵循服务的入口定义，不称精确独立人数。当前不支持下载/复制等自定义事件，直接打开PDF也不经过网页统计。只对公开Gallery启用，不接入知识库或Agent/API运行记录。

GitHub仓库Actions配置如下：

| 类型 | 名称 | 内容 |
| --- | --- | --- |
| Variable | `GALLERY_CLOUDFLARE_TOKEN` | Web Analytics脚本中的公开32位站点token |
| Variable | `CLOUDFLARE_ACCOUNT_ID` | 所属账户ID |
| Variable | `CLOUDFLARE_SITE_TAG` | 统计详情页网址中的siteTag；与采集token不是同一值 |
| Secret | `CLOUDFLARE_ANALYTICS_API_TOKEN` | 限定对应账户的Account Analytics / Read凭据，仅供构建中的汇总步骤使用 |

无采集token或本地预览不加载远程统计脚本；浏览器只在规范地址的origin和项目路径匹配时加载Cloudflare固定地址。设置`spa:false`避免搜索/筛选的history变更成为额外页面浏览。统计说明页允许使用`?analytics=off`在当前浏览器持久停用；存储不可用时至少停用当前页。恢复用`?analytics=on`。采集代码无管理密钥；服务脚本拦截不影响页面功能。

统计采集与公开汇总是两个权限边界。定时任务从GraphQL读取近30个UTC自然日（含当日），只保留已发布首页/说明/作品路径，生成近7日/30日PV、Visits与前五件作品的浏览量。首页静态显示汇总和更新时间，`metrics.json`只包含经过筛选的汇总，不含账户凭据、访客IP、原始搜索或来源路径。Cloudflare采样及拦截可能使结果不完整，不能当审计计数或实际使用人数。

汇总读取失败或凭据缺失时，尽量保留此前已发布的有效快照并标为stale；无旧数据则标unavailable并显示横线，不伪造零。只有成功API响应确实无访问时才显示零。日常刷新不回写源仓库、不修改版本或创建统计提交；若每日workflow长期未运行，页面时间仍能反映快照年龄。定时运行时间以GitHub调度为准。

本地构建示例（凭据由环境提供，勿写进公开文件）：

```bash
python3 .scripts/gallery_metrics.py --output temp/gallery-metrics.json
python3 .scripts/gallery_site.py --metrics temp/gallery-metrics.json
python3 .scripts/test_gallery_metrics.py
```

构建器本身不联网；汇总读取是独立的确定性工具。网页可在JavaScript关闭时阅读汇总、作品与下载。启用统计后的真实请求/后台数值须分别检查，不把安装了脚本等同已观测到访问。

## 长简历公开展示版

中英文长简历作为同一作品收录，保留原有学术栏目及完整成果清单。Gallery 的公开衍生版以用户选定的历史成品为基础，仅作经用户同意的信息删减、元数据清理和导出兼容调整；不是新生成的正式 CV edition，不反写工作主记录、不改写历史版本或核验状态。正式履历更新仍使用 CV 受管入口。

个人背景字段、学生具名信息和未确认公开的项目细节不进入公开包。原始 Word、处理脚本、来源哈希、核对记录仅存 `gallery/local/`；对外只列入清理后的 PDF、首页预览、封面和作品介绍。文档正文应先删减再导出，PDF 也要检查元数据、附件与批注，不能将格式转换当成脱敏。

## 公众号科普文章阅读版

每篇完整图文稿作为独立作品收录，使用 `science-article` 类型。系列按原篇次排序，关联前后篇及总介绍；首页可按“科普文章”筛选。每篇提供全文 PDF、首页与另一页图文预览，功能说明区分文章所讨论的能力与实际制作文章使用的写作、漫画配图、原生图示、版式导出等功能。

以实际选定的含图稿为转换依据，保留全文、图片、水印、图注与参考文献，不用旧 Markdown 替代后来修订的 Word。转换只调整导出字体映射、段落分页、PDF 元数据和阅读书签；核对全部段落、图片数量、文字边界与来源哈希，并对页面布局进行视觉检查。导出副本、处理脚本和核验记录留在 `gallery/local/`，不反写原稿或项目审阅状态。

公众号稿存在不等于已确认公众号发表；未取得可核验的发布链接与日期时，作品页只说明图文稿来源，不编造发布日期。历史稿中的系统行为与研究结果保留原稿版本背景，Gallery 收录不表示重新验证或更新其中全部主张。

## 更新与撤下

同一作品更新时增加作品版本，保存新成品及哈希，并在本地保留旧版本来源；不得让项目源文件变化自动改变展出的作品。撤下时同时移除首页入口与白名单条目，按受管流程重新发布。Git 历史不是保密撤回手段，涉及已公开敏感内容时另行处理。

## 权利

作品级权利说明与代码许可证分开。公开展示不自动授予第三方转载、改编或商业许可；保留引用、作者与素材归属。仅导出用户授权范围内的公开成品和介绍。
