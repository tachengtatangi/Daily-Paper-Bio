---
name: paper-reader
description: |
  用于读单篇论文或论文落地页。支持 PubMed URL、DOI、本地 PDF、普通 http/https 网页链接。支持三种模式：标准阅读、快速看一下、批判性分析。所有操作统一走内部单一入口脚本。
---

# Paper Reader

## Scope

这个 skill 用来读单篇论文或论文落地页，支持：

1. `PubMed URL`
2. `DOI` 或 `DOI URL`
3. `本地 PDF`
4. 普通 `http://` / `https://` 网页链接

统一由单一入口脚本完成，不要求用户再区分内部子步骤。

## 入口脚本

优先直接运行：

```powershell
python ..\paper-reader\run_reader.py "<source>" --mode standard
```

模式映射：

- `读一下这篇论文 ...` -> `--mode standard`
- `快速看一下这篇论文 ...` -> `--mode quick`
- `批判性分析这篇论文 ...` -> `--mode critical`

本地 PDF 默认行为与附加选项：

- **默认（推荐）**：直接用 `--mode standard` 或 `--mode standard --prefer-visible-browser`，**不要加 `--local-only`**。
  这样会用 OpenAlex + PubMed API（纯 HTTP，约 1-2 秒）补全 PMID、作者、期刊、摘要等元数据，
  全文内容仍来自本地 PDF，不会触发浏览器抓取。
  **PMID 能被正确填入，笔记质量更好。**

- `--local-only`：完全跳过所有网络请求（OpenAlex / PubMed / Elsevier API 都不调用）。
  PMID 将为空，元数据仅来自 PDF 本身。
  **仅在确实无网络连接时使用。**

例如：

```powershell
python ..\paper-reader\run_reader.py "https://pubmed.ncbi.nlm.nih.gov/41803465/" --mode standard
python ..\paper-reader\run_reader.py "10.1007/s10822-026-00780-y" --mode standard
python ..\paper-reader\run_reader.py "<LOCAL_PDF_PATH>" --mode standard
python ..\paper-reader\run_reader.py "<LOCAL_PDF_PATH>" --mode standard --prefer-visible-browser
python ..\paper-reader\run_reader.py "<LOCAL_PDF_PATH>" --mode standard --local-only
python ..\paper-reader\run_reader.py "https://example.com/article" --mode critical
```

## 浏览器 / Cookies

需要出版社全文或 PDF 时，优先使用：

```powershell
python ..\paper-reader\run_reader.py "<DOI_OR_URL>" --mode standard --prefer-visible-browser
```

patchright 抓取策略（优先级）：

1. **已有 CDP Chrome**（可选）：如果环境变量 `PAPER_READER_CDP_URL` / `CHROME_CDP_URL` 指向已开启 remote debugging 的 Chrome，优先连接并复用其 cookies。
2. **默认：`launch_persistent_context`**：patchright 自行启动 Chromium，使用按发行商分类的持久化 profile（存于系统临时目录 `pdf_fetcher_pw_<publisher>/`）。
   - 所有反自动化检测补丁生效（`navigator.webdriver` 隐藏，fingerprint 伪装）。
   - 持久化 profile 会复用已有 cookie 和被动验证状态，因此重复访问通常更稳定。
   - 显式 CAPTCHA、Turnstile 复选框或 “Are you a robot?” 不能保证自动通过；无人值守任务应快速报告并回退，不得自动点击或长时间等待。

如需机构登录 cookie（付费论文），设置 `PAPER_READER_CDP_URL` 指向已登录机构账户的 Chrome 实例。


## Figure 下载策略

- Figure 1 优先从出版社 HTML/DOM 中下载真实图片文件，不把截图当作成功结果。
- 出版社 PDF/Fig1 URL 规则和 F12 调试记录维护在 `references/publisher-assets.md`。
- 代码可消费的规则维护在 `publisher_rules.py`；当出版社 URL 改版时，先更新 reference 记录，再更新规则函数。
- HTML 图必须满足真实 `image/*` 响应且不是明显缩略图；否则继续尝试下一个候选或回退到 PDF 裁图。
- 如果网页图和 PDF 图都不可得，不阻塞正文笔记生成；Figures 部分降级为空或仅保留文字分析。

## 输入处理

### PubMed URL

三步流水线（Step A → Step C）：

- **Step A（PMC 优先）**：通过 NCBI eLink 查 PMC ID。若有，取 PMC XML 全文（JATS，结构最优）并用 PubMed 元数据补全。同时用 patchright `launch_persistent_context` 打开 PMC 文章页，滚动触发懒加载，下载 Fig1（CDN 直链，无 Cloudflare）并保存 PDF。
- **Step B（已移除）**：PubMed HTML 全文链接抓取已删除——PMC 有的走 A 覆盖，PMC 没有的需要 patchright 绕过，直接走 C。
- **Step C（发行商 DOI）**：Step A 无 PMC 记录时，取 PubMed 元数据获得 DOI，再用 patchright `launch_persistent_context` 打开 DOI 页面（自动绕过 Cloudflare），下载 PDF 并提取 Fig1。Elsevier DOI 优先调 API 全文。

### DOI / DOI URL

- 先打开 DOI 页面
- 尝试提取标题、摘要、期刊、关键词、可能的 PDF 链接
- 如果找到 PDF 且能提取可读文本，则升级为 `基于全文/PDF文本提取`
- 否则退回 `基于网页内容/元数据`

### 本地 PDF

- PDF 正文提取现在采用 MinerU-first：优先调用本机 `mineru` 将 PDF 转成 Markdown，再把 Markdown 正文作为全文材料。
- MinerU 只用于文本，不使用 MinerU 切出的图片作为 `figure_paths`，因为复合图容易被拆成多个局部图。
- 如果 MinerU 不存在、失败、超时或输出过短，自动回退到旧路径：`pdftotext` -> `pypdf` -> PDF raw stream。
- 可用 `PAPER_READER_PDF_TEXT_ENGINE=legacy` 临时关闭 MinerU；`PAPER_READER_MINERU_TIMEOUT` 可调超时时间，默认 300 秒。
- 每篇笔记成功保存后，`paper-reader` 会自动删除该篇 `mineru_output_dir` 下的非 `.md` 中间文件（PDF 副本、JSON、切图等），只保留 MinerU Markdown 文本证据。调试时可设置 `PAPER_READER_KEEP_MINERU_ARTIFACTS=1` 临时保留完整输出。

- 直接读取 PDF 并提取可见文本片段
- 如果文本提取成功，按 `基于全文/PDF文本提取`
- 如果提取质量有限，则按 `基于 PDF 可提取文本片段`

### 普通 http/https 网页链接

- 抓取网页标题、meta description、keywords、citation 元数据
- 如果网页里带 DOI，则继续尝试 DOI 页面补全
- 如果网页里暴露 PDF 链接，则继续尝试 PDF 文本提取
- 最终按 `基于网页内容/元数据` 或 `基于全文/PDF文本提取` 生成笔记

## 输出模式

### standard

输出标准结构化论文笔记，包括：

- 论文主题
- 一句话总结
- 研究问题
- 核心方法
- 主要发现
- 局限性

### quick

用于快速判断值不值得细读：

- 更短的主题判断
- 快速提炼研究问题和核心发现
- 明确告诉用户这是“快速浏览模式”

### critical

用于批判性阅读：

- 更强调证据边界
- 更强调方法假设
- 更强调外推风险、样本、设计和统计等后续核查点

## 保存

生成的笔记默认保存到：

- `PaperNotes/_inbox`

并自动刷新：

- `PaperNotes/PaperNotes.md`

## 注意

- 这是单一入口 skill，不需要用户手动再拆子流程
- 优先支持生物医学论文与论文落地页
- 不要求支持 arXiv URL 作为主工作流
- Elsevier support: if `sources.elsevier_api_key` is set in `../_shared/user-config.local.json` or `../_shared/user-config.json`, DOI inputs that resolve to Elsevier or ScienceDirect articles will try the Elsevier Article Retrieval API first with `view=FULL`, then fall back to browser/PDF extraction.

## HARD GUARDRAILS: publisher PDF assets

- For DOI/PubMed inputs from publishers with known downloadable PDFs, do not treat web-text-only extraction as a complete success. At minimum this includes DOI prefixes `10.1111/`, `10.1002/`, `10.1186/`, and `10.1007/`.
- PubMed inputs must follow the DOI to the publisher asset path when a DOI exists. If the PubMed-source note ends with empty `downloaded_pdf`, `pdf_path`, and `local_pdf`, retry once with the DOI input before reporting failure.
- A successful publisher-PDF note must write a local PDF path in frontmatter and the file must exist and start with `%PDF`. MinerU should then be used for PDF text extraction when available.
- Wiley PDFs should prefer `https://onlinelibrary.wiley.com/doi/pdfdirect/<DOI>?download=true`; Springer/BMC `10.1186/` PDFs should prefer `https://link.springer.com/content/pdf/<DOI>_reference.pdf`.
- Figure 1 should still prefer real publisher image files when available; if not available, PDF figure extraction is an acceptable fallback, but screenshot images are not.
