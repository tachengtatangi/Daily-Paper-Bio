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

patchright 抓取顺序：

1. 如果 `PAPER_READER_CDP_URL` / `CHROME_CDP_URL` 指向一个已开启 remote debugging 的 Chrome，优先连接它，cookie 来源记为 `existing_cdp_browser`。
2. 否则尝试用真实 Chrome `User Data` 目录启动 CDP Chrome；如果 profile 未被占用，cookie 来源记为 `real_chrome_profile`。
3. 如果默认 profile 正在运行，只能回退临时 profile，cookie 来源记为 `temp_profile_no_cookies`。这类结果不能假定带有机构登录 cookies。

如果必须复用正在打开的日常 Chrome cookies，需要先用 remote debugging 启动 Chrome，然后设置 `PAPER_READER_CDP_URL`。

## 输入处理

### PubMed URL

- 提取 PMID
- 调用 PubMed E-utilities 获取标题、作者、摘要、DOI、关键词等元数据
- 默认按 `基于摘要/元数据` 生成笔记

### DOI / DOI URL

- 先打开 DOI 页面
- 尝试提取标题、摘要、期刊、关键词、可能的 PDF 链接
- 如果找到 PDF 且能提取可读文本，则升级为 `基于全文/PDF文本提取`
- 否则退回 `基于网页内容/元数据`

### 本地 PDF

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
