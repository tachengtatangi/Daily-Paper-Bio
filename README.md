English: [README.en.md](README.en.md)

# Daily Paper Bio

Daily Paper Bio 是一套面向生命科学论文筛选和 Obsidian 笔记的 Codex skills。它解决的事情很简单：你不想每天手动翻 PubMed、bioRxiv、出版社页面和一堆 PDF，但又希望知道今天有没有真正值得看的论文。

日常使用时，基本只需要对 Codex 说一句话：

```text
今日论文推荐
过去3天论文推荐
读一下这篇论文 https://pubmed.ncbi.nlm.nih.gov/41803465/
```

它会抓取新论文，按你的研究方向筛一轮，写入 Obsidian 推荐页，并只为「必读」论文生成完整结构化笔记。

本项目参考并改造自 [huangkiki/dailypaper-skills](https://github.com/huangkiki/dailypaper-skills)。原版主要面向 arXiv / HuggingFace Daily 论文流；这个版本围绕 PubMed、bioRxiv、出版社网页、CAS 期刊分区、PDF 提取和生物医学方向 Obsidian 笔记重新整理。

## 它会帮你做什么

- 从 PubMed 和 bioRxiv 抓近期论文。
- 用你的 `keywords`、`domain_boost_keywords`、`negative_keywords`、期刊黑名单和可选 CAS 分区进行筛选打分。
- 生成每日推荐页，分成「必读 / 值得看 / 可跳过」。
- review 阶段由 Codex Agent 写审稿人口吻的真实评论，不把模板草稿直接发布。
- 只为「必读」论文生成完整笔记，避免把所有候选都读一遍浪费时间。
- 支持 PubMed URL、DOI、出版社网页、本地 PDF 和 bioRxiv PDF。
- 自动维护 Obsidian 论文目录页和概念索引。

最后在 Obsidian 里大概会长这样：

```text
ObsidianVault/
├── DailyPapers/
│   └── YYYY-MM-DD-论文推荐.md
├── PaperNotes/
│   ├── _inbox/
│   │   └── PMID - Paper title.md
│   ├── _concepts/
│   └── PaperNotes.md
└── AllPdfFig/
```

## 怎么用

每日推荐：

```text
今日论文推荐
过去3天论文推荐
过去一周论文推荐
2026-06-13 到 2026-06-16 论文推荐
今日论文推荐，关键词 convergent evolution taste receptor
```

读单篇论文：

```text
读一下这篇论文 https://pubmed.ncbi.nlm.nih.gov/41803465/
快速看一下这篇论文 10.1101/2024.01.01.123456
批判性分析这篇论文 D:\papers\paper.pdf
```

手动刷新 Obsidian 索引：

```text
更新索引
```

## 需要准备什么

必需：

- 支持本地 skills 的 Codex。
- Python 3.10 或更新版本。
- 一个 Obsidian vault。

强烈推荐：

- **MinerU**。这个项目不把 MinerU 打包进仓库，但如果本机能找到 `mineru` 命令，`paper-reader` 会优先用它提取 PDF 文本。实际使用下来，MinerU 从论文 PDF 里得到的 Markdown 往往比普通 PDF parser 更干净，尤其是双栏论文、复杂版式和长正文；这会直接影响必读笔记质量。
  默认超时为 900 秒，并复用已存在的合格 MinerU Markdown。PyPDF/pypdf 只负责 PDF 结构与页数校验，以及 MinerU、pdftotext 都失败后的末级文本回退，不是默认正文提取器。

可选但有用：

- `patchright` Chromium，用于需要浏览器会话的出版社网页。
- NCBI API key，用于加速 PubMed 请求。
- Elsevier API key，用于可访问时读取 ScienceDirect / Cell Press 全文。

## 浏览器会话和人机验证

出版社 PDF 和 Figure 抓取会通过 `patchright` 使用按出版社分开的持久化浏览器 profile。这样可以复用 cookies 和 Cloudflare clearance；很多被动浏览器检测在第一次通过后，后续自动化会稳定很多。

如果出版社明确显示 CAPTCHA 或 `Are you a robot?` 复选框，skill 不会尝试自动破解或自动点击。你可以在打开的浏览器窗口里手动完成一次验证，之后同一 profile 通常能复用验证 cookie。cookie 过期、网络/IP 改变、临时 profile 被清理，或出版社调整风控规则时，验证仍可能再次出现。

如果要放到无人值守自动化里，建议先手动预热常用出版社页面，例如 Cell Press、ScienceDirect、Wiley、OUP、Nature、Science 和 PNAS。即使后续仍遇到访问限制，流程也应该明确报告访问受限，并回退到 API、摘要、HTML 正文或 HTML Figure 1 等可用证据，而不是一直空等。

## 安装

安装方式参考原版 skill 仓库：clone 以后，把 skill 目录复制到本机 Codex skills 目录。不需要额外安装脚本。

先 clone 仓库：

```bash
git clone https://github.com/tachengtatangi/Daily-Paper-Bio.git
cd Daily-Paper-Bio
```

安装 Python 依赖：

```bash
python -m pip install -r daily-papers/requirements.txt
```

安装 MinerU，并确保终端里能运行 `mineru`。请按 MinerU 官方文档选择适合你系统的安装方式。Daily Paper Bio 只调用本机已有的 MinerU CLI，不会在仓库里内置 MinerU。

内部实际调用大致是：

```bash
mineru -p "<pdf_path>" -o "<output_root>" --method auto --backend pipeline
```

可选安装浏览器运行时：

```bash
patchright install chromium
```

把 skill 目录复制到 Codex。

macOS / Linux / Git Bash：

```bash
mkdir -p ~/.codex/skills
cp -R _shared daily-papers daily-papers-fetch daily-papers-review daily-papers-notes paper-reader generate-mocs playwright ~/.codex/skills/
```

Windows PowerShell：

```powershell
New-Item -ItemType Directory -Force "$env:USERPROFILE\.codex\skills"
Copy-Item -Recurse -Force _shared,daily-papers,daily-papers-fetch,daily-papers-review,daily-papers-notes,paper-reader,generate-mocs,playwright "$env:USERPROFILE\.codex\skills\"
```

如果是在已有安装上更新，先备份或事后恢复你的本地配置：

```text
~/.codex/skills/_shared/user-config.json
~/.codex/skills/_shared/user-config.local.json
```

## 配置

安装后编辑运行目录里的配置：

```text
~/.codex/skills/_shared/user-config.json
```

至少要改 Obsidian vault 路径和研究关键词：

```json
{
  "paths": {
    "obsidian_vault": "C:\\Users\\YourName\\Documents\\Obsidian\\PaperVault"
  },
  "daily_papers": {
    "keywords": ["genome evolution", "comparative genomics"],
    "domain_boost_keywords": ["mammalian", "sensory receptor"],
    "negative_keywords": ["crop", "bacteria"]
  }
}
```

真实 API key 放在 local 覆盖文件里，不要写进公开配置：

```bash
cp ~/.codex/skills/_shared/user-config.local.json.example ~/.codex/skills/_shared/user-config.local.json
```

Windows PowerShell：

```powershell
Copy-Item "$env:USERPROFILE\.codex\skills\_shared\user-config.local.json.example" "$env:USERPROFILE\.codex\skills\_shared\user-config.local.json"
```

然后按需填写：

```json
{
  "sources": {
    "ncbi_api_key": "",
    "elsevier_api_key": ""
  },
  "llm": {
    "api_key": "",
    "base_url": "",
    "model": "gpt-4o-mini"
  }
}
```

`user-config.local.json` 已经被 `.gitignore` 排除。不要提交真实 API key、cookie 或你的本机私有路径。

## CAS 分区表

仓库包含可再分发的 CAS 期刊分区表：

```text
_shared/data/cas_quartiles_2025.xlsx
```

它用于 PubMed 论文的期刊分区过滤。bioRxiv 预印本不受 CAS 分区过滤。第一次调试时建议先放宽：

```json
{
  "daily_papers": {
    "min_quartile": 4,
    "min_score": 1
  }
}
```

确认有结果后，再把阈值收紧到你的阅读容量能承受的程度。

## 它内部大概怎么跑

`今日论文推荐` 实际拆成三步：

1. `daily-papers-fetch`：抓 PubMed + bioRxiv，打分，去重，富化元数据，写入临时 JSON。
2. `daily-papers-review`：生成结构化 draft，由 Codex Agent 写真实评论，然后发布正式 Obsidian Markdown 并更新 history。
3. `daily-papers-notes`：只为「必读」论文生成完整笔记，回填链接，刷新 MOC。

如果只是想测试抓取和 draft 生成，不发布正式推荐页：

```bash
python daily-papers/run_pipeline.py --date 2026-07-03 --days 3 --notes-limit 0
```

这个命令只会在配置的临时目录里生成 draft。draft 不是正式推荐页，里面任何 `TODO_AGENT` 都必须由 review 阶段替换后才能发布。

## 仓库里有什么

```text
Daily-Paper-Bio/
├── _shared/                 # 共享配置、CAS 查询、MOC 生成器
├── daily-papers/            # 抓取、打分、富化、draft、history
├── daily-papers-fetch/      # fetch skill 包装
├── daily-papers-review/     # review skill 包装
├── daily-papers-notes/      # notes skill 包装
├── paper-reader/            # 单篇论文阅读和笔记生成
├── generate-mocs/           # Obsidian 索引生成
├── playwright/              # 浏览器辅助 skill
├── README.md
└── README.en.md
```

日常真正会直接用到的主要是：

- `daily-papers`
- `paper-reader`
- `generate-mocs`

其他几个 skill 是为了把流水线拆清楚，方便调试和重跑。

## FAQ

**一定要用 Obsidian 吗？**

不一定。输出本质是 Markdown。但这个 workflow 会用到 wiki link、目录页和概念页，所以 Obsidian 最顺手。

**一定要装 MinerU 吗？**

不是硬依赖，但强烈建议装。没有 MinerU 时会回退到本地 PDF parser，很多论文也能读；但多栏 PDF 和复杂版式下，MinerU 的正文提取通常更稳定，笔记质量会明显更好。

**为什么推荐结果是 0？**

先把 `min_quartile` 改成 `4`、`min_score` 改成 `1`，然后看 `daily_papers_filter_audit.json`。大多数 0 结果来自关键词太窄、分区过滤太严，或者负面词误杀。

**会自动 commit 我的 Obsidian vault 吗？**

默认不会。`automation.git_commit` 和 `automation.git_push` 默认都是 false。

**生成的笔记能直接用于论文写作吗？**

建议把它当成 related work 整理、阅读记录和追问提纲。AI 生成内容可能误读或遗漏，正式写作前必须回到原文核验。

## 免责声明

这是个人研究工作流的开源整理，不是保证完全稳定的产品。AI 生成的推荐、点评和笔记可能有事实错误、遗漏或误读，更适合作为辅助工具，而不是替代自己的研究判断。

## License

Apache-2.0。见 [LICENSE](LICENSE)。

部分辅助资源保留自身声明，例如 `playwright/` 下的 license/notice 文件。
