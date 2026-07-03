# DailyPaper Skills（生物方向改造版）

面向生物医学方向的 Codex CLI skill 流水线。每天一句话「今日论文推荐」，自动从 PubMed + bioRxiv 抓取 → 打分 → 分级 → 生成评论 → 为"必读"生成结构化笔记，全部落到本地 Obsidian vault。

Fork 自 [dailypaper-skills](https://github.com/huangkiki/dailypaper-skills)（原版基于 arXiv + HuggingFace Daily），Apache-2.0。本版改为 PubMed + bioRxiv 数据源，增加中科院分区过滤、本地 PDF profile、patchright/Elsevier/bioRxiv PDF 抓取等模块。

---

## 一、首次安装

### 1. 拷贝 skill 目录到 Codex

把整个 `skills/` 目录放到：

```
Windows: %USERPROFILE%\.codex\skills\
Linux  : ~/.codex/skills/
```

### 2. 安装 Python 依赖

一条命令装全（paper-reader + daily-papers 依赖都在这里）：

```bash
pip install -r daily-papers/requirements.txt
```

**推荐**：同时安装 patchright 浏览器（用于抓闭源期刊全文 PDF，如 Elsevier/Springer/Wiley）：

```bash
patchright install chromium
```

> **不装 patchright/chromium 也能正常使用**：  
> - 本地 PDF (`/paper-reader` 传 PDF 路径) → 直接 pypdf/PyMuPDF 本地提取，**不需要浏览器**  
> - PubMed / bioRxiv 开放论文 → PMC API 获取全文，**不需要浏览器**  
> - 闭源期刊（Elsevier/Nature/Wiley 等付费文章）→ 无法抓全文，笔记退回摘要模式

### 3. 填写 `_shared/user-config.json`

用文本编辑器打开 `_shared/user-config.json`，修改 `obsidian_vault` 为你自己的 Obsidian 仓库路径（**必填**）：

```json
"paths": {
  "obsidian_vault": "C:\\Users\\YourName\\Documents\\Obsidian\\PaperVault"
}
```

`temp_folder` 留空即可，系统会自动使用 `~/tmp/`（Windows）或 `/tmp/`（Linux/Mac）。

可选：`preference_pdf_library_folder`（你已有的 PDF 库，用于自动提取 boost keywords）。

### 4. 创建 `_shared/user-config.local.json`（敏感信息，勿进 git）

模板：

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

- `ncbi_api_key`：可选。有 key：10 req/s；无 key：3 req/s。在 <https://www.ncbi.nlm.nih.gov/account/> 免费申请。
- `elsevier_api_key`：可选。用于 `10.1016/*` DOI 走官方全文 API。
- `llm`：可选回退。默认优先用 Codex CLI；Codex 不可用时才尝试 OpenAI 兼容 API；都不可用则会 `RuntimeError`。

---

## 二、日常使用

对 Codex 说：

- `今日论文推荐`
- `过去3天论文推荐`
- `过去一周论文推荐`
- `2025-3-21 到 2025-3-24 论文推荐`
- `今日论文推荐，关键词 convergent evolution taste receptor`（临时覆盖关键词）

明确日期区间按闭区间处理：先把日期规范化为 `YYYY-MM-DD`，再用结束日期作为 `--date`，天数作为 `--days`。例如 `2025-3-21 到 2025-3-24` 会转换为 `--date 2025-03-24 --days 4`；`--date` 永远表示窗口结束日期且含当天。

内部会自动串联：

1. **daily-papers-fetch** — 抓取 + 打分 + 富化
2. **daily-papers-review** — 生成分级骨架 + Agent 写评论 + 写入正式推荐文件
3. **daily-papers-notes** — 为「必读」论文调 `paper-reader` 生成结构化笔记 + 回填链接 + 刷新 MOC

### 读单篇论文

- `读一下这篇论文 https://pubmed.ncbi.nlm.nih.gov/41803465/` → standard 模式
- `快速看一下这篇论文 10.1101/2024.01.01.123456` → quick 模式
- `批判性分析这篇论文 https://example.com/article.pdf` → critical 模式

支持的输入：PubMed URL、DOI / DOI URL、本地 PDF、普通网页链接。

**本地 PDF 附加选项 `--local-only`**：完全跳过网络元数据补全（OpenAlex / PubMed / Elsevier API），只从 PDF 本身提取文本和图片。速度最快，适合已知论文直接快读的场景：

```powershell
python ..\paper-reader\run_reader.py "D:\papers\mypaper.pdf" --mode standard --local-only
```

---

## 三、配置项速查

所有配置在 `_shared/user-config.json`（主配置）+ `user-config.local.json`（敏感，不进 git）。两者深度 merge，local 优先。

### paths
| key | 说明 |
|---|---|
| `obsidian_vault` | Obsidian 仓库根路径（**必填**） |
| `paper_notes_folder` | 笔记根目录，默认 `PaperNotes` |
| `daily_papers_folder` | 每日推荐目录，默认 `DailyPapers` |
| `concepts_folder` | 概念图谱目录，默认 `_concepts` |
| `pdf_figure_folder` | PDF 首图保存目录，默认 `AllPdfFig` |
| `temp_folder` | 流水线中间 JSON 目录，留空自动 `~/tmp` |
| `preference_pdf_library_folder` | 本地 PDF 库路径，用于自动 profile |

### sources
| key | 说明 |
|---|---|
| `pubmed_enabled` | 是否抓 PubMed |
| `biorxiv_enabled` | 是否抓 bioRxiv |
| `biorxiv_retmax` | bioRxiv 单次最多抓条数 |
| `biorxiv_categories` | bioRxiv 分区白名单（类比原版 `arxiv_categories`） |
| `ncbi_api_key` / `elsevier_api_key` | API Key，放 local |

### daily_papers
| key | 说明 |
|---|---|
| `keywords` | 正向关键词（PubMed 检索 + 本地打分） |
| `domain_boost_keywords` | 领域加分词（只加分不准入） |
| `negative_keywords` | 负面词，命中立拒 |
| `rejected_journals` | 期刊名黑名单（个人口味） |
| `min_score` | 最低入选分数 |
| `min_quartile` | **CAS 分区阈值**。`1`=只 Q1；`2`=Q1+Q2；`3`=Q1~Q3；`4`=全接受。bioRxiv 不受此过滤影响 |
| `top_n` | 每天保留条数（多天模式自动 × days） |
| `build_review_must_score_min` | 「必读」分数门槛 |
| `notes_parallelism` | 必读笔记并发数 |
| `update_profile_from_pdf_library` | 是否用本地 PDF 库刷新 boost 词 |

### automation
| key | 说明 |
|---|---|
| `auto_refresh_indexes` | 每次跑完是否刷 MOC |
| `git_commit` / `git_push` | 是否自动 commit / push vault |

---

## 四、流水线全景

```
┌─ 用户说「今日论文推荐」
│
├─ daily-papers-fetch
│  ├── fetch_and_score.py
│  │   ├── 读配置 + apply_library_profile（可选扫本地 PDF 库）
│  │   ├── PubMed：esearch → efetch XML → CAS 分区过滤 → 词干打分
│  │   ├── bioRxiv：details API 并发游标 → 分区过滤 → 打分
│  │   ├── dedup（source/doi/pmid/title）+ history dedup（30 天滚动）
│  │   └── → {TEMP}/daily_papers_top30.json + filter_audit.json
│  └── enrich_papers.py
│      └── 异步 curl 并发 PMC/Europe PMC/bioRxiv HTML → → enriched.json
│
├─ daily-papers-review
│  ├── build_review.py：按分数分级（必读 30% / 值得看 40% / 可跳过 30%），
│  │   生成 draft（评论留空）
│  ├── Agent 亲笔写：顶部锐评 / 每篇推荐理由 / 每篇摘要短评
│  ├── 写入 {VAULT}/DailyPapers/YYYY-MM-DD-论文推荐.md
│  ├── 删 draft
│  ├── update_history.py：把 top30 id 追加到 .history.json
│  └── 可选 git commit/push
│
├─ daily-papers-notes
│  ├── 筛「必读」
│  ├── 对每篇调 paper-reader/run_reader.py（并发 notes_parallelism）
│  │   ├── PubMed URL → E-utilities 元数据
│  │   ├── 10.1101/* → _fetch_biorxiv_direct（直接 urllib 抓 .full.pdf）
│  │   ├── 10.1016/* + Elsevier API key → curl Elsevier API
│  │   └── 其他 → patchright (CDP Chrome) 绕 Cloudflare + PDF + Fig1
│  ├── backfill_links.py：回填笔记链接到推荐文件
│  ├── generate_paper_mocs.py / generate_concept_mocs.py
│  └── 可选 git commit/push
│
└─ 输出：
   ├── {VAULT}/DailyPapers/YYYY-MM-DD-论文推荐.md
   ├── {VAULT}/PaperNotes/_inbox/<PMID> - <title>.md
   └── {VAULT}/PaperNotes/PaperNotes.md（MOC）
```

---

## 五、打分规则

**PubMed 抓取流程**（类比原版 arXiv categories → 本地打分）：
1. esearch：`journal article[pt] AND hasabstract[text]` + 日期范围，拉取最近 `search_retmax` 条 PMID
2. efetch XML：100 篇一批解析
3. **CAS 分区过滤**：`quartile > min_quartile` → 丢弃（记入 `rejected_quartile`）
4. **keyword 预筛**：title + abstract 中无任何 keyword token 匹配 → 丢弃（记入 `rejected_no_keyword`），此步骤令后续打分只处理有意义的候选
5. **完整打分**：`score_paper()` → score < min_score → 丢弃

每篇论文最终 `score`：

- **标题**命中 `keywords` → +3 / 条
- **摘要 / paper keywords**命中 `keywords` → +1 / 条（标题已中的不再计）
- `domain_boost_keywords` 命中 ≥ 2 个 → +2；= 1 个 → +1
- `domain_boost_keywords` 在标题里命中 → 额外 +1
- **负向硬过滤**：命中 `negative_keywords` 或 `rejected_journals` → -999（直接丢）
- **CAS 硬过滤**（仅 PubMed）：`quartile > min_quartile` → 直接丢（计入 `rejected_quartile` 审计）

关键词匹配使用 **词根归一化 + 多词窗口匹配**，只做复数归一（`genes → gene`, `studies → study`），不再对 `-ing/-ed/-ly/-ation` 等做激进抽根。如果你要让关键词匹配 `enhancer` / `enhanced` / `enhancing`，用 `CANONICAL_VARIANTS` 显式配置。

---

## 六、常见问题


**Q：PubMed 抓回一堆 IDs 但最后只剩几条？**
A：大概率是 CAS 分区过滤。把 `min_quartile` 从 1 调到 2 或 3 放宽，再看 `{TEMP}/daily_papers_filter_audit.json` 里 `rejected_quartile` 详情。

**Q：bioRxiv 全部拿不到？**
A：检查 `biorxiv_categories` 是否匹配你的方向。category 名大小写敏感但脚本已做 normalize，不用太介意。

**Q：LLM 不可用报错？**
A：要么登录 Codex CLI（`codex --version` 可用），要么在 `user-config.local.json` 的 `llm.api_key` 填一个 OpenAI 兼容 key。review 阶段报错时推荐文件顶部会加横幅提示。

**Q：必读笔记只拿到摘要没拿到全文？**
A：
- bioRxiv：应该直接抓到 `.full.pdf`。拿不到先 curl 一下那个 URL 看是否 403/404。
- 闭源期刊：需要 `patchright install chromium`。
- Elsevier：填 `elsevier_api_key`。

**Q：我的 vault 改动会自动提交到 git 吗？**
A：默认 `git_commit=false` / `git_push=false`。打开后会在 `obsidian_vault` 目录里 `git add` 当日推荐文件和必读笔记并 commit。失败只警告，不中断流程。

---

## 七、安装说明（接收者）

你收到的这个 zip 已经是可用的分发包。按以下步骤配置：

### 1. 解压到 Codex skills 目录

```
Windows: %USERPROFILE%\.codex\skills\
```

把 `dailypaper-skills\` 里的内容（`_shared/`、`paper-reader/` 等）直接放进 `skills\` 目录。

### 2. 创建 `_shared/user-config.local.json`

把 `_shared/user-config.local.json.example` 复制一份，改名为 `user-config.local.json`，填入：

- `ncbi_api_key`：可选，在 <https://www.ncbi.nlm.nih.gov/account/> 免费申请
- `elsevier_api_key`：可选，读 Elsevier 全文时用

### 3. 修改路径配置

打开 `_shared/user-config.json`，修改 `paths` 节：

```json
"paths": {
  "obsidian_vault": "你的 Obsidian 仓库路径",
  "temp_folder": "临时文件目录（留空则用 ~/tmp）"
}
```

### 4. 安装依赖

```bash
pip install -r daily-papers/requirements.txt
patchright install chromium
```

`requirements.txt` 已包含全部依赖（pypdf、PyMuPDF、beautifulsoup4、trafilatura、readability-lxml、patchright、openpyxl、openai、nltk）。

### 5. 调整关键词（重要）

`user-config.json` 里的 `keywords` / `domain_boost_keywords` / `negative_keywords` / `rejected_journals` 默认是分发者的研究方向（比较基因组、蝙蝠免疫等），**必须换成你自己的领域关键词**，否则每日推荐大概率 0 条。

### 6. 首次试跑（排查 0 结果）

如果第一次跑「今日论文推荐 1 天」返回 0 条，按顺序排查：

1. 临时把 `daily_papers.min_quartile` 改成 `4`（所有分区都接收），`min_score` 改成 `1`
2. 看 `{temp_folder}/daily_papers_filter_audit.json`，里面记录了每条被丢弃的理由（分区不够/关键词不命中/命中负面词）
3. 如果 `rejected_no_keyword` 占大头，说明你的 `keywords` 还没匹配到今天的文献，再补几个主题词
4. 如果 `rejected_quartile` 占大头，维持 `min_quartile: 4` 或放宽到 `3` 即可
5. 确认有输出后再收紧阈值

---

## 八、授权

- 代码：Apache-2.0（继承自原版）
- CAS 分区数据：`_shared/data/cas_quartiles_2025.xlsx`，仅用于本人/朋友研究用途
- License token 机制仅用于限制分发层面的使用窗口，不影响代码 fork
