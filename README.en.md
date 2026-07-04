Chinese: [README.md](README.md) | English

# Daily Paper Bio

Daily Paper Bio is a Codex skill workflow for people who want to keep up with life-science papers without manually checking PubMed, bioRxiv, publisher pages, and local PDFs every day.

In day-to-day use, you mostly say one sentence to Codex:

```text
今日论文推荐
过去3天论文推荐
读一下这篇论文 https://pubmed.ncbi.nlm.nih.gov/41803465/
```

It will fetch new papers, score them against your research interests, write an Obsidian recommendation page, and generate full notes for the papers marked as must-read.

This project is adapted from [huangkiki/dailypaper-skills](https://github.com/huangkiki/dailypaper-skills). The original workflow focuses on arXiv/HuggingFace-style paper streams; this version is rebuilt around PubMed, bioRxiv, publisher pages, CAS journal quartiles, PDF extraction, and Obsidian notes for biology and biomedical research.

## What It Does

- Fetches recent papers from PubMed and bioRxiv.
- Scores papers with your `keywords`, `domain_boost_keywords`, `negative_keywords`, journal filters, and optional CAS quartile filtering.
- Generates a daily recommendation page with `must-read`, `worth reading`, and `skip` buckets.
- Uses the Codex agent to write reviewer-style comments instead of publishing raw templates.
- Generates full structured notes only for must-read papers.
- Reads PubMed URLs, DOI links, publisher article pages, local PDFs, and bioRxiv PDFs.
- Maintains Obsidian paper-note indexes and concept MOCs.

The output in Obsidian is roughly:

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

## Everyday Usage

Daily recommendations:

```text
今日论文推荐
过去3天论文推荐
过去一周论文推荐
2026-06-13 到 2026-06-16 论文推荐
今日论文推荐，关键词 convergent evolution taste receptor
```

Single-paper reading:

```text
读一下这篇论文 https://pubmed.ncbi.nlm.nih.gov/41803465/
快速看一下这篇论文 10.1101/2024.01.01.123456
批判性分析这篇论文 D:\papers\paper.pdf
```

Refresh Obsidian indexes manually:

```text
更新索引
```

## Requirements

Required:

- Codex with local skill support.
- Python 3.10 or newer.
- An Obsidian vault.

Strongly recommended:

- **MinerU** for PDF text extraction. The workflow still works without it, but MinerU usually produces cleaner full-text Markdown from PDFs than generic PDF parsers. That directly improves paper notes and summaries, especially for multi-column biology papers.

Optional but useful:

- `patchright` Chromium for publisher pages that need a browser session.
- NCBI API key for faster PubMed requests.
- Elsevier API key for ScienceDirect / Cell Press full-text retrieval when available.

## Browser Sessions and Human Verification

Publisher PDF and figure extraction uses `patchright` with per-publisher persistent browser profiles. This lets the workflow reuse cookies and Cloudflare clearance between runs. Passive browser checks often pass after the first successful visit.

If a publisher shows an explicit CAPTCHA or `Are you a robot?` checkbox, the skill does not try to auto-solve or auto-click it. Complete the check manually once in the opened browser, then later automation runs can usually reuse the stored clearance cookie. The check can reappear if cookies expire, the network/IP changes, the temporary profile is cleared, or the publisher changes its risk rules.

For unattended automation, it is worth running one manual warm-up on common publisher sites such as Cell Press, ScienceDirect, Wiley, OUP, Nature, Science, and PNAS. When access still fails, the workflow should report the access limitation and fall back to available API, abstract, HTML text, or HTML Figure 1 evidence rather than waiting indefinitely.

## Install

The install method follows the original skill repository style: clone the repo, then copy the skill directories into your local Codex skills directory. No installer script is required.

Clone the repository:

```bash
git clone https://github.com/tachengtatangi/Daily-Paper-Bio.git
cd Daily-Paper-Bio
```

Install Python dependencies:

```bash
python -m pip install -r daily-papers/requirements.txt
```

Install MinerU and make sure the `mineru` command works in your terminal. Follow the official MinerU installation instructions for your platform. This project does not vendor MinerU; it only calls the local CLI when available.

The command used internally is essentially:

```bash
mineru -p "<pdf_path>" -o "<output_root>" --method auto --backend pipeline
```

Install the optional browser runtime:

```bash
patchright install chromium
```

Copy the skill directories into Codex.

macOS / Linux / Git Bash:

```bash
mkdir -p ~/.codex/skills
cp -R _shared daily-papers daily-papers-fetch daily-papers-review daily-papers-notes paper-reader generate-mocs playwright ~/.codex/skills/
```

Windows PowerShell:

```powershell
New-Item -ItemType Directory -Force "$env:USERPROFILE\.codex\skills"
Copy-Item -Recurse -Force _shared,daily-papers,daily-papers-fetch,daily-papers-review,daily-papers-notes,paper-reader,generate-mocs,playwright "$env:USERPROFILE\.codex\skills\"
```

If you are updating an existing installation, back up your runtime config first or re-apply your local settings afterwards:

```text
~/.codex/skills/_shared/user-config.json
~/.codex/skills/_shared/user-config.local.json
```

## Configure

Edit the runtime config after installing the skills:

```text
~/.codex/skills/_shared/user-config.json
```

Minimum configuration:

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

Put private keys in the local override file, not in the public config:

```bash
cp ~/.codex/skills/_shared/user-config.local.json.example ~/.codex/skills/_shared/user-config.local.json
```

On Windows PowerShell:

```powershell
Copy-Item "$env:USERPROFILE\.codex\skills\_shared\user-config.local.json.example" "$env:USERPROFILE\.codex\skills\_shared\user-config.local.json"
```

Then edit:

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

`user-config.local.json` is ignored by git. Do not commit API keys, cookies, or private paths.

## CAS Quartile Data

This repository includes a redistributable CAS journal quartile workbook:

```text
_shared/data/cas_quartiles_2025.xlsx
```

It is used by the optional PubMed journal filter. bioRxiv papers are not filtered by CAS quartile. For first-time debugging, set:

```json
{
  "daily_papers": {
    "min_quartile": 4,
    "min_score": 1
  }
}
```

After you confirm the workflow returns papers, tighten the thresholds to match your reading capacity.

## How It Works

`今日论文推荐` is split into three skills:

1. `daily-papers-fetch`: fetch PubMed + bioRxiv, score papers, deduplicate, enrich metadata, and write temporary JSON.
2. `daily-papers-review`: build a structured draft, then let the Codex agent write real comments before publishing the final Obsidian Markdown.
3. `daily-papers-notes`: generate full notes only for must-read papers, backfill note links, and refresh MOCs.

A safe smoke test that does not publish a final recommendation page:

```bash
python daily-papers/run_pipeline.py --date 2026-07-03 --days 3 --notes-limit 0
```

This writes a draft under the configured temp folder. A draft is not a final recommendation page; any `TODO_AGENT` field must be replaced by the review stage before publishing.

## Repository Layout

```text
Daily-Paper-Bio/
├── _shared/                 # shared config, CAS lookup, MOC builders
├── daily-papers/            # fetch, score, enrich, draft, history
├── daily-papers-fetch/      # fetch skill wrapper
├── daily-papers-review/     # review skill wrapper
├── daily-papers-notes/      # notes skill wrapper
├── paper-reader/            # single-paper reader and note generator
├── generate-mocs/           # Obsidian index generation
├── playwright/              # browser helper skill
├── README.md
└── README.en.md
```

## FAQ

**Do I have to use Obsidian?**

The output is Markdown, so you can read it anywhere. Obsidian is recommended because the workflow uses wiki links, indexes, and concept pages.

**Do I have to use MinerU?**

No, but it is strongly recommended. Without MinerU, the reader falls back to local PDF parsers. That is enough for many PDFs, but the text is usually less structured, especially for multi-column papers.

**Why did I get zero recommendations?**

First set `min_quartile` to `4` and `min_score` to `1`, then inspect `daily_papers_filter_audit.json`. Most zero-result cases are caused by overly narrow keywords, strict quartile filtering, or negative keywords catching too much.

**Does it automatically commit my Obsidian vault?**

No. `automation.git_commit` and `automation.git_push` are false by default.

**Can this replace reading the paper?**

No. Treat it as triage, reading notes, and a starting point for related-work organization. AI-generated comments and notes can be wrong; verify important claims against the original paper.

## Disclaimer

This is a personal research workflow, not a fully managed product. AI-generated recommendations, comments, and notes may contain factual errors, omissions, or misreadings. Use it as an assistant, not as a replacement for your own research judgment.

## License

Apache-2.0. See [LICENSE](LICENSE).

Some bundled helper assets keep their own notices, including files under `playwright/`.
