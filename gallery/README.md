# Gallery · 精选作品

精选成品拥有独立的 Gallery 网站，按功能和作品类型浏览，并可搜索主题。每件作品的详情页展示完整成品、所用功能、下载入口与一条制作类似作品的示例指令。

**网站入口：[Gallery · 精选作品](https://ranshiju.github.io/Ran-ASKS/)。** 以下链接也可直接在仓库内查看，网站发布由手动 Gallery Pages 工作流完成。

| 作品 | 形式 | 使用功能 |
| --- | --- | --- |
| [MPE 科普展板](works/mpe-science-poster/README.md) | A0 科普海报 | 科普展板 · 插图与图示 · 理解试读 · 版式与导出 |
| [MPE 学术报告](works/mpe-academic-talk/README.md) | 12 页学术报告 · PPTX / PDF | 演示文稿制作 · 版式与导出 |
| [中英文学术长简历](works/bilingual-academic-cv/README.md) | 中文 / English · 完整长简历 PDF | 简历制作与维护 |

## 本地预览

在仓库根目录运行：

```bash
python3 .scripts/gallery_site.py
python3 -m http.server 8765 --bind 127.0.0.1 --directory temp/gallery-site
```

打开 <http://localhost:8765/>。也可以直接打开生成的 `temp/gallery-site/index.html`，作品阅读和下载不依赖网络服务。

[新增作品与网站维护](../operations/GALLERY.md) · [作品使用说明](RIGHTS.md)

**English:** An independent static gallery for selected ASKS works, with capability/type filters, search, artifact pages and downloads. Website: <https://ranshiju.github.io/Ran-ASKS/>. Publication uses the manual Gallery Pages workflow; the repository links above also remain available for reading and downloads.
