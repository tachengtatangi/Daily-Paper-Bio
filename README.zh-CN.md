English: [README.md](README.md)

# Daily Paper Bio

Daily Paper Bio 是一组面向生命科学论文筛选和 Obsidian 笔记的 Codex skills。它可以从 PubMed 和 bioRxiv 抓取近期论文，按照你的研究关键词打分和分桶，由 Codex Agent 写出审稿人口吻的每日推荐，并只为「必读」论文生成结构化笔记。

这个仓库不是普通 Python 包，而是一组 Codex skill 目录。安装方式是把这些目录复制到 Codex 的 skills 目录。

## 功能概览

- 从 PubMed 和 bioRxiv 抓取论文。
- 按 `keywords`、`domain_boost_keywords`、`negative_keywords`、期刊黑名单和可选 CAS 分区进行筛选打分。
- 生成 `必读`、`值得看`、`可跳过` 三类每日推荐草稿。
- review 阶段必须由 Agent 亲自改写顶部锐评、推荐理由和摘要短评，避免把模板草稿当成正式推荐。
- notes 阶段只为 `必读` 论文生成完整笔记，并回填推荐页链接。
- `paper-reader` 支持 PubMed URL、DOI、出版社网页、本地 PDF 和 bioRxiv PDF。
- 如果本机安装了 MinerU，会优先用 MinerU 提取 PDF 文本；失败时自动回退到本地 PDF 解析器。
- API key、本机路径和其他私有配置放在 `.gitignore` 排除的 local 配置文件里。

## 目录结构

| 路径 | 作用 |
|---|---|
| `_shared/` | 共享配置、Obsidian 路径、MOC 生成器、CAS 分区表。 |
| `daily-papers/` | 抓取、打分、富化、draft 生成、history、运行缓存清理。 |
| `daily-papers-fetch/` | fetch 阶段的 Codex skill 包装。 |
| `daily-papers-review/` | review/finalize 阶段的 Codex skill 包装。 |
| `daily-papers-notes/` | 必读笔记生成阶段的 Codex skill 包装。 |
| `paper-reader/` | 单篇论文阅读和笔记生成。 |
| `generate-mocs/` | Obsidian 目录页/MOC 刷新。 |
| `playwright/` | 浏览器控制辅助 skill 和参考资料。 |

## 环境要求

- Python 3.10 或更新版本。
- 支持本地 skills 的 Codex。
- 一个 Obsidian vault。
- 可选：`patchright` Chromium，用于需要浏览器会话的出版社页面。
- 可选：MinerU，用于更好的 PDF 文本提取。
- 可选：NCBI API key、Elsevier API key。

安装 Python 依赖：

```powershell
python -m pip install -r daily-papers/requirements.txt
```

安装可选浏览器运行时：

```powershell
patchright install chromium
```

MinerU 是可选项。只要 `mineru.exe` 或 `mineru` 在 `PATH` 中，`paper-reader` 会优先尝试它。实际命令大致是：

```powershell
mineru -p "<pdf_path>" -o "<output_root>" --method auto --backend pipeline
```

笔记成功写入后，非 Markdown 的 MinerU 中间文件会被自动清理；Markdown 结果可以保留用于检查。

## 安装

先把仓库 clone 到任意项目目录：

```powershell
git clone https://github.com/tachengtatangi/Daily-Paper-Bio.git
cd Daily-Paper-Bio
```

然后把 skill 目录同步到 Codex：

```powershell
.\sync_to_codex.bat
```

同步脚本会保留 Codex 运行目录里已经存在的 `_shared/user-config.json` 和 `_shared/user-config.local.json`，所以更新 skill 时不会覆盖你的本机路径或 API key。

Windows 手动目标目录：

```text
%USERPROFILE%\.codex\skills\
```

Linux/macOS 手动目标目录：

```text
~/.codex/skills/
```

需要复制到 `skills/` 下的目录包括：`_shared`、`daily-papers`、`daily-papers-fetch`、`daily-papers-review`、`daily-papers-notes`、`paper-reader`、`generate-mocs`、`playwright`。

## 配置

安装后编辑 `_shared/user-config.json`。至少要设置你的 Obsidian vault 路径和研究关键词：

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

真实密钥放在 `_shared/user-config.local.json`，不要放进 `user-config.json`：

```powershell
copy _shared\user-config.local.json.example _shared\user-config.local.json
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

`user-config.local.json` 已被 `.gitignore` 排除。不要提交真实 API key、浏览器 cookie 或本机私有路径。

## CAS 分区表

仓库包含可再分发的 CAS 期刊分区表：

```text
_shared/data/cas_quartiles_2025.xlsx
```

PubMed 论文可以根据期刊分区做硬过滤。bioRxiv 预印本不受 CAS 分区过滤影响。如果你删除或替换该表，代码仍可运行；表不存在时 CAS 查询返回空。首次调试建议把 `daily_papers.min_quartile` 设为 `4`，确认有结果后再收紧到 `1` 或 `2`。

## 在 Codex 中使用

直接对 Codex 说：

```text
今日论文推荐
过去3天论文推荐
过去一周论文推荐
2026-06-13 到 2026-06-16 论文推荐
今日论文推荐，关键词 convergent evolution taste receptor
```

完整流程分三步：

1. `daily-papers-fetch`：抓取、打分、去重、富化，写入临时 JSON。
2. `daily-papers-review`：生成结构化 draft，由 Agent 写评论，发布正式 Markdown，并更新 history。
3. `daily-papers-notes`：只为必读论文生成笔记，回填链接，刷新 MOC。

如果只想做安全 smoke test，不写正式 Obsidian 推荐页：

```powershell
python daily-papers\run_pipeline.py --date 2026-07-03 --days 3 --notes-limit 0
```

这个命令只会在配置的临时目录下生成 draft。正式发布必须走 review skill，因为所有 `TODO_AGENT` 都需要 Agent 基于论文信息亲自改写。

## 读单篇论文

示例：

```text
读一下这篇论文 https://pubmed.ncbi.nlm.nih.gov/41803465/
快速看一下这篇论文 10.1101/2024.01.01.123456
批判性分析这篇论文 https://example.com/article.pdf
```

本地 PDF 且不需要联网补全元数据时：

```powershell
python paper-reader\run_reader.py "D:\papers\paper.pdf" --mode standard --local-only
```

## 常见问题

| 问题 | 检查项 |
|---|---|
| 没有推荐结果 | 先把 `min_score` 调低、`min_quartile` 设为 `4`，检查 `daily_papers_filter_audit.json`。 |
| PubMed 慢或限流 | 在 `user-config.local.json` 填 `sources.ncbi_api_key`。 |
| 必读笔记只有摘要 | 安装浏览器支持；必要时配置 Elsevier API；或者提供本地 PDF。 |
| PDF 文本质量差 | 安装 MinerU，并确认 `mineru` 在 `PATH` 中。 |
| 正式推荐页还有 `TODO_AGENT` | 停止交付，重新走 review 阶段；draft 不是正式推荐页。 |

## GitHub 创建仓库时怎么选

如果你已经有本地 git 仓库，GitHub 创建新仓库时建议关闭 `Add README`、`.gitignore` 和 `License`。原因不是不需要这些文件，而是本地仓库已经有了；让 GitHub 生成会多出一个远端初始提交，push 时需要处理 unrelated histories。

创建空仓库后，从本地推送：

```powershell
cd /d path\to\Daily-Paper-Bio
git remote rename origin local-origin
git remote add origin https://github.com/tachengtatangi/Daily-Paper-Bio.git
git branch -M main
git push -u origin main
```

## License

本项目使用 Apache-2.0。见 [LICENSE](LICENSE)。

部分浏览器辅助资源保留其自身声明，例如 `playwright/` 下的 license/notice 文件。